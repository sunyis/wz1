import warnings
warnings.filterwarnings("ignore")
import os
import sys
import json
import stat
import logging
import asyncio
import subprocess
import time
import urllib.parse
import zipfile
import io
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

# 【关键修复】判断是否为 PyInstaller 打包环境，动态获取资源绝对路径
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    RUN_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RUN_DIR = BASE_DIR

sys.path.insert(0, BASE_DIR)

from core.config import ConfigManager
from core.auth import AuthManager
from core.ip_detector import IPDetector
from core.port_detector import PortDetector
from core.ssh_manager import SSHManager
from core.file_ops import FileManager

# 确保所有必需目录在启动前自动创建
log_dir = os.path.join(RUN_DIR, "logs")
Path(log_dir).mkdir(parents=True, exist_ok=True)
Path(os.path.join(BASE_DIR, "web/static/css")).mkdir(parents=True, exist_ok=True)
Path(os.path.join(BASE_DIR, "web/static/js")).mkdir(parents=True, exist_ok=True)
Path(os.path.join(BASE_DIR, "web/templates")).mkdir(parents=True, exist_ok=True)

# 【关键修复】初始化配置管理器 (必须在日志配置之前，以便读取日志开关)
config_path = os.path.join(RUN_DIR, "config.json")
config = ConfigManager(config_path)

# 【日志优化】控制台输出纯文本，文件保留详细日志，支持配置开关
log_enable = config.get("logging", "enable", default=True)
log_level_str = config.get("logging", "level", default="INFO")
log_level = logging.INFO if log_level_str.upper() == "INFO" else logging.DEBUG

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter('%(message)s'))

root_logger = logging.getLogger()
root_logger.setLevel(log_level)
root_logger.addHandler(stream_handler)

# 如果开启日志，则添加文件记录器
if log_enable:
    file_handler = logging.FileHandler(os.path.join(log_dir, "app.log"), encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    root_logger.addHandler(file_handler)

# 全局标志：记录是否因为端口被占用而关闭
is_port_in_use = False

# Uvicorn 日志中文翻译过滤器
class ChineseLogFilter(logging.Filter):
    def filter(self, record):
        global is_port_in_use
        msg = str(record.msg)
        if "Started server process" in msg:
            record.msg = msg.replace("Started server process", "已启动服务进程")
        elif "Waiting for application startup." in msg:
            record.msg = msg.replace("Waiting for application startup.", "等待应用程序启动...")
        elif "Application startup complete." in msg:
            record.msg = msg.replace("Application startup complete.", "应用程序启动完成。 (启动下 CTRL+C 退出)")
        elif "Uvicorn running on" in msg:
            return False # 直接丢弃，不显示
        elif "Shutting down" in msg:
            if is_port_in_use: return False # 端口占用时隐藏
            record.msg = msg.replace("Shutting down", "正在关闭")
        elif "Waiting for application shutdown." in msg:
            if is_port_in_use: return False # 端口占用时隐藏
            record.msg = msg.replace("Waiting for application shutdown.", "等待应用程序关闭...")
        elif "Application shutdown complete." in msg:
            if is_port_in_use: return False # 端口占用时隐藏
            record.msg = msg.replace("Application shutdown complete.", "应用程序关闭完成。")
        elif "Finished server process" in msg:
            record.msg = msg.replace("Finished server process", "已停止服务进程")
        elif "error while attempting to bind on address" in msg and "address already in use" in msg:
            record.msg = "端口被占用！请使用 pkill -f wzfilemanager 停止旧进程，或修改 config.json 中的端口后重试。"
            is_port_in_use = True # 标记为端口占用
        # 拦截 asyncio 强制取消导致的报错
        elif "CancelledError" in msg or "asyncio.exceptions.CancelledError" in msg:
            return False
        return True

# 清除 uvicorn 默认处理器，传播到 root_logger 统一处理
uvicorn_logger = logging.getLogger("uvicorn")
uvicorn_logger.handlers = []
uvicorn_logger.propagate = True
logging.getLogger("uvicorn.error").addFilter(ChineseLogFilter())
logging.getLogger("uvicorn.access").addFilter(ChineseLogFilter())

# 屏蔽 Paramiko 底层打印的 sftp session closed 等冗余日志
logging.getLogger("paramiko").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

config_path = os.path.join(RUN_DIR, "config.json")
config = ConfigManager(config_path)
auth = AuthManager(config)
ssh = SSHManager(config)

real_config_dir = os.path.dirname(os.path.realpath(config_path))
file_manager = FileManager(ssh, real_config_dir)

app = FastAPI(title="WzFileManager", version="1.0.0")

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "web/static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "web/templates"))

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    
    # 【严格修复】仅允许 /s/ 免登录，其它全部要验证登录
    # 白名单仅保留：登录页、登录接口、静态资源、公网信息接口、分享页/直链、以及分享前端调用的列表接口
    public_paths = ["/login", "/api/login", "/static", "/api/public-info", "/s", "/api/share/content"]
    
    if any(path.startswith(p) for p in public_paths):
        return await call_next(request)

    allowed_ips = config.get("security", "allowed_ips", default=[])
    if allowed_ips:
        client_ip = request.client.host if request.client else ""
        if client_ip not in allowed_ips:
            return JSONResponse({"success": False, "msg": "IP 不在白名单中"}, status_code=403)

    token = request.cookies.get("session_token") or request.headers.get("X-Session-Token")
    if not token or not auth.verify_session(token, request):
        if path.startswith("/api/"):
            return JSONResponse({"success": False, "msg": "会话已过期, 请刷新网页"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    return await call_next(request)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("file_manager.html", {"request": request})

@app.get("/api/public-info")
async def public_info():
    return {
        "success": True,
        "public_ip": IPDetector.get_public_ip(),
        "local_ip": IPDetector.get_local_ip(),
        "port": config.get("server", "port")
    }

@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    password = data.get("password", "")
    success, msg, token = auth.login(request, password)
    response = JSONResponse({"success": success, "msg": msg})
    if success and token:
        response.set_cookie(
            key="session_token", value=token, httponly=True,
            max_age=config.get("auth", "session_timeout", default=2592000), samesite="lax"
        )
    return response

@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get("session_token")
    if token: auth.logout(token)
    response = JSONResponse({"success": True, "msg": "已退出"})
    response.delete_cookie("session_token")
    return response

@app.get("/api/ssh/status")
async def ssh_status():
    is_configured = bool(config.get("ssh", "password") or config.get("ssh", "key_password"))
    ok = False
    msg = "未配置 SSH 密码或密钥"
    if is_configured:
        try:
            ok, msg = ssh.ensure_connected()
        except Exception:
            ok = False
            msg = "SSH 连接异常，请检查配置"
    return {
        "success": True, "connected": ok, "message": msg, "configured": is_configured,
        "host": config.get("ssh", "host"), "port": config.get("ssh", "port"), "username": config.get("ssh", "username")
    }

@app.post("/api/ssh/connect")
async def ssh_connect():
    ok, msg = ssh.connect()
    return {"success": ok, "msg": msg}

@app.get("/api/files/list")
async def list_files(path: str = "/"):
    return file_manager.list_dir(path)

@app.get("/api/files/info")
async def file_info(path: str):
    return file_manager.get_file_info(path)

@app.get("/api/files/read")
async def read_file(path: str):
    return file_manager.read_file(path)

@app.post("/api/files/save")
async def save_file(request: Request):
    data = await request.json()
    return file_manager.save_file(data.get("path"), data.get("content", ""))

@app.post("/api/files/create")
async def create_item(request: Request):
    data = await request.json()
    path = data.get("path")
    if data.get("type") == "dir":
        return file_manager.create_dir(path)
    return file_manager.create_file(path)

@app.post("/api/files/delete")
async def delete_item(request: Request):
    data = await request.json()
    permanent = data.get("permanent", False)
    return file_manager.delete(data.get("path"), permanent=permanent)

@app.post("/api/files/rename")
async def rename_item(request: Request):
    data = await request.json()
    return file_manager.rename(data.get("old_path"), data.get("new_path"))

@app.post("/api/files/copy")
async def copy_item(request: Request):
    data = await request.json()
    src = data.get("src"); dst = data.get("dst"); overwrite = data.get("overwrite", False)
    sftp = file_manager._ensure_sftp()
    if not sftp: return JSONResponse({"success": False, "msg": "SFTP 未连接"})
    try:
        sftp.stat(dst); exists = True
    except IOError:
        exists = False
    if exists and not overwrite:
        return JSONResponse({"success": False, "code": "exists", "msg": "目标文件已存在"})
    if exists and overwrite:
        file_manager.delete(dst)
    return file_manager.copy(src, dst)

@app.post("/api/files/move")
async def move_item(request: Request):
    data = await request.json()
    src = data.get("src"); dst = data.get("dst"); overwrite = data.get("overwrite", False)
    sftp = file_manager._ensure_sftp()
    if not sftp: return JSONResponse({"success": False, "msg": "SFTP 未连接"})
    try:
        sftp.stat(dst); exists = True
    except IOError:
        exists = False
    if exists and not overwrite:
        return JSONResponse({"success": False, "code": "exists", "msg": "目标文件已存在"})
    if exists and overwrite:
        file_manager.delete(dst)
    return file_manager.move(src, dst)

@app.post("/api/files/chmod")
async def chmod_item(request: Request):
    data = await request.json()
    mode = int(data.get("mode"), 8)
    return file_manager.chmod(data.get("path"), mode)

@app.post("/api/files/perm")
async def set_file_permission(request: Request):
    data = await request.json()
    path = data.get("path"); perm = data.get("perm"); group = data.get("group"); recursive = data.get("recursive", False)
    return file_manager.set_permission(path, perm, group, recursive)

@app.post("/api/files/upload")
async def upload_file(path: str = Form(...), file: UploadFile = File(...), overwrite: bool = Form(False)):
    file_data = await file.read()
    sftp = file_manager._ensure_sftp()
    if not sftp: return JSONResponse({"success": False, "msg": "SFTP 未连接"})
    remote_file = file_manager._normalize_path(f"{path}/{file.filename}")
    try:
        sftp.stat(remote_file); exists = True
    except IOError:
        exists = False
    if exists and not overwrite:
        return JSONResponse({"success": False, "code": "exists", "msg": f"文件 {file.filename} 已存在"})
    return file_manager.upload_file(path, file_data, file.filename)

@app.get("/api/files/download")
async def download_file(path: str):
    sftp = file_manager._ensure_sftp()
    if not sftp:
        return JSONResponse({"success": False, "msg": "SFTP 未连接"})
    
    # 1. 预检：防止下载目录或无权限文件导致流式响应中途崩溃
    try:
        stat_info = sftp.stat(path)
        if stat.S_ISDIR(stat_info.st_mode):
            return JSONResponse({"success": False, "msg": "不能下载文件夹，请压缩后再下载"})
    except Exception as e:
        err_str = str(e)
        if "Failure" in err_str or "No such file" in err_str or "Permission denied" in err_str:
            return JSONResponse({"success": False, "msg": "无法下载该文件，可能不存在或无读取权限"})
        return JSONResponse({"success": False, "msg": str(e)})

    filename = os.path.basename(path)
    ascii_filename = filename.encode('ascii', 'ignore').decode('ascii') or 'download'
    quoted_filename = urllib.parse.quote(filename)
    
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quoted_filename}",
        "X-Content-Type-Options": "nosniff"
    }

    # 【关键修复】使用流式传输，每次只读取 64KB 到内存，防止大文件导致内存溢出
    def file_stream():
        try:
            with sftp.open(path, 'rb') as f:
                while True:
                    chunk = f.read(65536)  # 每次读取 64KB
                    if not chunk:
                        break
                    yield chunk
        except Exception:
            pass  # 流式传输中途如果遇到网络断开等异常，直接忽略结束

    return StreamingResponse(file_stream(), media_type="application/octet-stream", headers=headers)

@app.post("/api/files/compress")
async def compress_files(request: Request):
    data = await request.json()
    return file_manager.compress(data.get("paths", []), data.get("output"), data.get("format", "tar.gz"))

@app.post("/api/files/extract")
async def extract_file(request: Request):
    data = await request.json()
    file_path = data.get("path")
    target_dir = data.get("target_dir")
    if not target_dir:
        base_name = os.path.basename(file_path).split('.')[0]
        target_dir = file_manager._normalize_path(os.path.join(os.path.dirname(file_path), base_name))
    return file_manager.extract(file_path, target_dir)

@app.get("/api/files/search")
async def search_files(path: str, keyword: str, recursive: bool = False):
    return file_manager.search(path, keyword, recursive)

@app.get("/api/system/disk")
async def disk_usage(path: str = "/"):
    return file_manager.get_disk_usage(path)

@app.get("/api/system/dir-size")
async def dir_size(path: str):
    return file_manager.get_dir_size(path)

@app.get("/api/files/analyze")
async def analyze_disk(path: str):
    return file_manager.analyze_disk(path)

@app.get("/api/trash/list")
async def list_trash():
    return file_manager.list_trash()

@app.post("/api/trash/restore")
async def restore_trash(request: Request):
    data = await request.json()
    trash_id = data.get("trash_id")
    if not trash_id: return JSONResponse({"success": False, "msg": "缺少 trash_id"})
    return file_manager.restore(trash_id)

@app.post("/api/trash/delete")
async def delete_trash_item_api(request: Request):
    data = await request.json()
    trash_id = data.get("trash_id")
    if not trash_id: return JSONResponse({"success": False, "msg": "缺少 trash_id"})
    trash_base = file_manager._get_trash_base()
    if trash_id.startswith("root_"):
        item_path = f"{trash_base}/{trash_id[5:]}"
    else:
        item_path = f"{trash_base}/{trash_id}"
    ok, out, err = ssh.execute(f'rm -rf "{item_path}"')
    if ok: return {"success": True, "msg": "已彻底删除"}
    return {"success": False, "msg": err or "删除失败"}

@app.post("/api/trash/restore-batch")
async def restore_trash_batch_api(request: Request):
    data = await request.json()
    trash_ids = data.get("trash_ids", [])
    if not trash_ids: return JSONResponse({"success": False, "msg": "无选中项"})
    for tid in trash_ids:
        file_manager.restore(tid)
    return {"success": True, "msg": f"已尝试还原 {len(trash_ids)} 项"}

@app.get("/api/trash/download")
async def download_trash_file(trash_id: str):
    result = file_manager.get_trash_file_info(trash_id)
    
    if not result.get("success"):
        return JSONResponse(result)
        
    real_path = result["real_path"]
    original_name = result["original_name"]
    
    # 2. 检查 SFTP 连接
    sftp = file_manager._ensure_sftp()
    if not sftp:
        return JSONResponse({"success": False, "msg": "SFTP 未连接"})

    # 3. 处理文件名编码，确保中文文件名能正常下载
    ascii_filename = original_name.encode('ascii', 'ignore').decode('ascii') or 'download'
    quoted_filename = urllib.parse.quote(original_name)
    
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quoted_filename}",
        "X-Content-Type-Options": "nosniff"
    }

    # 4. 使用流式传输，每次只读取 64KB 到内存，防止大文件导致内存溢出
    def file_stream():
        try:
            with sftp.open(real_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)  # 每次读取 64KB
                    if not chunk:
                        break
                    yield chunk
        except Exception:
            pass  # 流式传输中途如果遇到网络断开等异常，直接忽略结束

    return StreamingResponse(file_stream(), media_type="application/octet-stream", headers=headers)

@app.post("/api/trash/delete-batch")
async def delete_trash_batch_api(request: Request):
    data = await request.json()
    trash_ids = data.get("trash_ids", [])
    if not trash_ids: return JSONResponse({"success": False, "msg": "无选中项"})
    trash_base = file_manager._get_trash_base()
    for tid in trash_ids:
        if tid.startswith("root_"):
            item_path = f"{trash_base}/{tid[5:]}"
        else:
            item_path = f"{trash_base}/{tid}"
        ssh.execute(f'rm -rf "{item_path}"')
    return {"success": True, "msg": f"已彻底删除 {len(trash_ids)} 项"}

@app.post("/api/trash/clear")
async def clear_trash():
    trash_base = file_manager._get_trash_base()
    ok, out, err = ssh.execute(f'rm -rf "{trash_base}"/*')
    if ok: return {"success": True, "msg": "回收站已清空"}
    return {"success": False, "msg": err}

@app.get("/api/favorites")
async def get_favorites():
    favs = config.get("favorites", default=[])
    return {"success": True, "favorites": favs if isinstance(favs, list) else []}

@app.post("/api/favorites")
async def save_favorites(request: Request):
    data = await request.json()
    favs = data.get("favorites", [])
    if not isinstance(favs, list):
        return JSONResponse({"success": False, "msg": "数据格式错误"})
    config.set("favorites", favs)
    return {"success": True, "msg": "收藏夹已同步"}

@app.get("/api/config")
async def get_config():
    return {
        "success": True,
        "config": {
            "server": config.get("server"),
            "auth": {
                "password": config.get("auth", "password"),
                "session_timeout": config.get("auth", "session_timeout"),
                "max_attempts": config.get("auth", "max_attempts"),
                "lock_minutes": config.get("auth", "lock_minutes")
            },
            "ssh": {
                "host": config.get("ssh", "host"),
                "port": config.get("ssh", "port"),
                "username": config.get("ssh", "username"),
                "auth_type": config.get("ssh", "auth_type"),
                "key_path": config.get("ssh", "key_path")
            },
            "security": config.get("security")
        }
    }

def _translate_ssh_error(err_msg):
    err_msg = str(err_msg)
    if "Unable to connect to port" in err_msg or "Errno None" in err_msg or "Connection refused" in err_msg or "timed out" in err_msg:
        return "请在顶部设置中配置"
    if "Authentication failed" in err_msg or "All configured authentication methods failed" in err_msg:
        return "认证失败，密码或密钥不正确"
    if "not a valid RSA private key" in err_msg or "Invalid key" in err_msg:
        return "密钥文件无效或格式错误"
    return err_msg

@app.post("/api/config/ssh")
async def update_ssh_config(request: Request):
    data = await request.json()
    config.update("ssh", {
        "host": data.get("host", ""),
        "port": data.get("port", 22),
        "username": data.get("username", "root"),
        "auth_type": data.get("auth_type", "password"),
        "password": data.get("password", ""),
        "key_path": data.get("key_path", ""),
        "key_password": data.get("key_password", "")
    })
    ssh.disconnect()
    ok, msg = ssh.connect()
    if not ok:
        msg = _translate_ssh_error(msg)
    return {"success": ok, "msg": msg}

@app.post("/api/config/password")
async def change_password(request: Request):
    data = await request.json()
    success, msg = auth.change_password(data.get("old_password", ""), data.get("new_password", ""))
    return {"success": success, "msg": msg}

@app.post("/api/config/home-path")
async def set_home_path(request: Request):
    data = await request.json()
    req_path = data.get("path", "/").strip()
    if not req_path:
        req_path = "/"
    final_path = "/"
    sftp = file_manager._ensure_sftp()
    if sftp and req_path != "/":
        try:
            stat_info = sftp.stat(req_path)
            if stat.S_ISDIR(stat_info.st_mode):
                final_path = req_path
            else:
                final_path = os.path.dirname(req_path)
        except Exception:
            final_path = "/"
    server_config = config.get("server", default={})
    server_config["home_path"] = final_path
    config.update("server", server_config)
    return {"success": True, "path": final_path}

@app.post("/api/config/server")
async def update_server_config(request: Request):
    data = await request.json()
    new_port = int(data.get("port", 0))
    if new_port > 0:
        try:
            if subprocess.run(["which", "firewall-cmd"], capture_output=True).stdout:
                subprocess.run(["firewall-cmd", "--zone=public", "--add-port", f"{new_port}/tcp", "--permanent"])
                subprocess.run(["firewall-cmd", "--reload"])
            elif subprocess.run(["which", "ufw"], capture_output=True).stdout:
                subprocess.run(["ufw", "allow", f"{new_port}/tcp"])
        except: pass
    config.update("server", {
        "host": data.get("host", "0.0.0.0"),
        "auto_port": data.get("auto_port", True),
        "port": new_port
    })
    return {"success": True, "msg": "配置已更新，重启后生效"}

def open_firewall_port(port):
    try:
        if subprocess.run(["which", "firewall-cmd"], capture_output=True).stdout:
            subprocess.run(["firewall-cmd", "--zone=public", "--add-port", f"{port}/tcp", "--permanent"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["firewall-cmd", "--reload"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif subprocess.run(["which", "ufw"], capture_output=True).stdout:
            subprocess.run(["ufw", "allow", f"{port}/tcp"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass

# --- 分享功能接口 ---
def get_shares_file_path():
    return os.path.join(real_config_dir, "shares.json")

def load_shares():
    try:
        with open(get_shares_file_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_shares(data):
    with open(get_shares_file_path(), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

@app.post("/api/share/create")
async def create_share(request: Request):
    data = await request.json()
    paths = data.get("paths", [])
    expire_days = int(data.get("expire_days", 0)) # 0=永久, 1=1天, 7=7天
    
    if not paths:
        return JSONResponse({"success": False, "msg": "请选择要分享的文件"})
        
    share_id = uuid.uuid4().hex[:8]
    expire_time = 0
    if expire_days > 0:
        expire_time = int(time.time()) + expire_days * 86400
        
    shares = load_shares()
    shares[share_id] = {
        "paths": paths,
        "create_time": int(time.time()),
        "expire_time": expire_time
    }
    save_shares(shares)
    
    sftp = file_manager._ensure_sftp()
    # 判断分享类型，生成对应的链接
    is_single_file = False
    if len(paths) == 1 and sftp:
        try:
            if not stat.S_ISDIR(sftp.stat(paths[0]).st_mode):
                is_single_file = True
        except: pass
        
    name = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)}个文件"
    if is_single_file:
        share_url = f"/s/{share_id}/{name}"  # 单文件直链格式
    else:
        share_url = f"/s/{share_id}"         # 多文件网页浏览格式
    
    return {"success": True, "share_id": share_id, "url": share_url, "name": name}

@app.get("/api/share/list")
async def list_shares():
    shares = load_shares()
    now = int(time.time())
    active_shares = []
    to_remove = []
    
    sftp = file_manager._ensure_sftp()
    
    for sid, sdata in shares.items():
        if sdata["expire_time"] != 0 and sdata["expire_time"] < now:
            to_remove.append(sid)
            continue
        paths = sdata["paths"]
        
        # 判断是否是单文件直链
        is_single_file = False
        if len(paths) == 1 and sftp:
            try:
                if not stat.S_ISDIR(sftp.stat(paths[0]).st_mode):
                    is_single_file = True
            except: pass
            
        name = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)}个文件"
        share_url = f"/s/{sid}/{name}" if is_single_file else f"/s/{sid}"

        # 计算分享大小用于列表展示
        size_val = 0
        size_str = "-"
        if len(paths) == 1 and sftp:
            try:
                st = sftp.stat(paths[0])
                if not stat.S_ISDIR(st.st_mode):
                    size_val = st.st_size
                    size_str = file_manager._format_size(st.st_size)
            except Exception:
                pass

        active_shares.append({
            "id": sid,
            "name": name,
            "paths": paths,
            "expire_time": sdata["expire_time"],
            "create_time": sdata["create_time"],
            "url": share_url,
            "size": size_val,
            "size_str": size_str,
            "is_single_file": is_single_file
        })
        
    if to_remove:
        for sid in to_remove:
            del shares[sid]
        save_shares(shares)
        
    return {"success": True, "shares": active_shares}

@app.post("/api/share/update")
async def update_share_expire(request: Request):
    """更新分享的有效期，原链接不变"""
    data = await request.json()
    sid = data.get("id")
    expire_days = int(data.get("expire_days", 0))
    
    shares = load_shares()
    if sid not in shares:
        return {"success": False, "msg": "分享不存在"}
        
    if expire_days > 0:
        shares[sid]["expire_time"] = int(time.time()) + expire_days * 86400
    else:
        shares[sid]["expire_time"] = 0
        
    save_shares(shares)
    return {"success": True, "msg": "有效期更新成功"}

@app.post("/api/share/cancel")
async def cancel_share(request: Request):
    data = await request.json()
    sid = data.get("id")
    shares = load_shares()
    if sid in shares:
        del shares[sid]
        save_shares(shares)
    return {"success": True}

@app.get("/api/share/content")
async def share_content(share_id: str, path: str = ""):
    """获取分享目录内的文件列表，供网页浏览"""
    shares = load_shares()
    if share_id not in shares:
        return JSONResponse({"success": False, "msg": "分享不存在或已取消"}, status_code=404)
        
    sdata = shares[share_id]
    now = int(time.time())
    if sdata["expire_time"] != 0 and sdata["expire_time"] < now:
        del shares[share_id]
        save_shares(shares)
        return JSONResponse({"success": False, "msg": "分享已过期"}, status_code=404)
        
    paths = sdata["paths"]
    sftp = file_manager._ensure_sftp()
    if not sftp: return JSONResponse({"success": False, "msg": "服务器 SFTP 未连接"}, status_code=500)
    
    items = []
    
    if not path:
        # 根目录，展示 paths 里包含的文件和目录
        for p in paths:
            try:
                attr = sftp.stat(p)
                is_dir = stat.S_ISDIR(attr.st_mode)
                items.append({
                    "name": os.path.basename(p),
                    "path": os.path.basename(p), # 虚拟路径，以文件名开头
                    "is_dir": is_dir,
                    "size": "-" if is_dir else file_manager._format_size(attr.st_size)
                })
            except: pass
    else:
        # 子目录，去真实的 SFTP 路径下列出内容
        base_name = path.split('/')[0]
        real_base = None
        for p in paths:
            if os.path.basename(p) == base_name:
                real_base = p
                break
                
        if not real_base:
            return JSONResponse({"success": False, "msg": "路径不在分享范围内"})
            
        relative_path = '/'.join(path.split('/')[1:])
        real_path = os.path.join(real_base, relative_path)
        real_path = os.path.normpath(real_path)
        
        # 安全检查：防止目录穿越
        if not real_path.startswith(real_base.rstrip('/')):
            return JSONResponse({"success": False, "msg": "无权限访问该路径"})
            
        try:
            for attr in sftp.listdir_attr(real_path):
                items.append({
                    "name": attr.filename,
                    "path": path + "/" + attr.filename, # 拼接虚拟路径
                    "is_dir": stat.S_ISDIR(attr.st_mode),
                    "size": "-" if stat.S_ISDIR(attr.st_mode) else file_manager._format_size(attr.st_size)
                })
        except Exception as e:
            return JSONResponse({"success": False, "msg": str(e)})
            
    return {"success": True, "items": items}

@app.get("/s/{share_id}")
async def view_share(share_id: str):
    """多文件/文件夹分享的网页浏览界面"""
    shares = load_shares()
    if share_id not in shares:
        return HTMLResponse("<h1>分享不存在或已取消</h1>", status_code=404)
        
    sdata = shares[share_id]
    now = int(time.time())
    if sdata["expire_time"] != 0 and sdata["expire_time"] < now:
        del shares[share_id]
        save_shares(shares)
        return HTMLResponse("<h1>分享已过期</h1>", status_code=404)
        
    # 返回一个简单的 HTML 网页，修复手机端文件名显示不全及复制提示问题
    html_content = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=0.6, maximum-scale=1.0, user-scalable=yes">
        <title>文件分享</title>
        <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'><path d='M511.453372 511.500905m-499.665227 0a499.665228 499.665228 0 1 0 999.330455 0 499.665228 499.665228 0 1 0-999.330455 0Z' fill='%2356C3FD'/><path d='M701.63227 212.186604a106.093302 106.093302 0 0 0-64.597131 190.131365l-97.299726 170.072506a122.634731 122.634731 0 0 0-123.157592 13.071531l-60.366708-64.169336a74.341364 74.341364 0 1 0-22.24537 17.967414l61.792694 65.690387a122.634731 122.634731 0 1 0 168.408856-17.872348l97.584923-170.547835a106.140835 106.140835 0 1 0 39.880054-204.391217z' fill='%23FFFFFF'/></svg>">
        <style>
            body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; margin: 0; padding: 20px; }}
            .container {{ max-width: 800px; min-height: 80vh; margin: auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); box-sizing: border-box; }}
            .header {{ text-align: center; margin-bottom: 20px; border-bottom: 2px solid #f0f0f0; padding-bottom: 15px; }}
            .file-list {{ list-style: none; padding: 0; margin: 0; }}
            .file-item {{ display: flex; align-items: center; padding: 12px 10px; border-bottom: 1px solid #f5f5f5; cursor: pointer; transition: background 0.2s; flex-wrap: wrap; -webkit-tap-highlight-color: transparent; }}
            .file-item:hover {{ background: transparent; }}
            .icon {{ margin-right: 10px; font-size: 20px; width: 24px; text-align: center; flex-shrink: 0; }}
            .name {{ flex: 1; color: #333; font-size: 14px; word-break: break-all; white-space: normal; min-width: 150px; }}
            .size {{ color: #999; font-size: 12px; margin-left: 10px; margin-right: 15px; flex-shrink: 0; }}
            .actions {{ display: flex; gap: 8px; margin-left: auto; flex-shrink: 0; align-items: center; }}
            .btn {{ border: none; padding: 5px 10px; border-radius: 4px; font-size: 12px; cursor: pointer; color: #fff; }}
            .btn-download {{ background: #1890ff; }}
            .btn-copy {{ background: #52c41a; }}
            /* 面包屑路径样式 */
            .breadcrumb {{ margin-bottom: 15px; font-size: 14px; color: #999; word-break: break-all; }}
            .breadcrumb a {{ color: #1890ff; cursor: pointer; text-decoration: none; }}
            .breadcrumb a:hover {{ text-decoration: underline; }}
            
            /* 局部的复制提示样式，绝对定位在按钮下方 */
            .action-group {{ position: relative; display: flex; align-items: center; }}
            .copy-toast {{ 
                position: absolute; 
                top: 100%; 
                left: 50%; 
                transform: translateX(-50%); 
                margin-top: 5px;
                background: rgba(0,0,0,0.7); 
                color: #fff; 
                padding: 4px 8px; 
                border-radius: 4px; 
                font-size: 12px; 
                white-space: nowrap;
                display: none; 
                z-index: 10; 
                pointer-events: none; 
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2 style="margin:0; color:#333;">📂 文件分享中心</h2>
                <p style="color:#999; font-size:13px; margin:5px 0 0;">Share ID: {share_id}</p>
            </div>
            <div class="breadcrumb" id="breadcrumb">位置: /<a onclick="loadContent('')">根目录</a></div>
            <ul class="file-list" id="fileList"></ul>
        </div>
        <script>
            const shareId = '{share_id}';
            
            async function loadContent(path) {{
                // 更新面包屑路径
                const breadcrumb = document.getElementById('breadcrumb');
                let pathHtml = `<a onclick="loadContent('')">根目录</a>`;
                if (path) {{
                    const parts = path.split('/');
                    let currentPath = '';
                    parts.forEach(part => {{
                        currentPath = currentPath ? currentPath + '/' + part : part;
                        pathHtml += `/<a onclick="loadContent('${{currentPath}}')">${{part}}</a>`;
                    }});
                }}
                breadcrumb.innerHTML = '位置: /' + pathHtml;
                
                const res = await fetch(`/api/share/content?share_id=${{shareId}}&path=${{encodeURIComponent(path)}}`);
                const data = await res.json();
                const list = document.getElementById('fileList');
                list.innerHTML = '';
                
                if (data.success) {{
                    if (data.items.length === 0) {{
                        list.innerHTML = '<li style="text-align:center; color:#999; padding:30px;">此文件夹为空</li>';
                        return;
                    }}
                    data.items.forEach(item => {{
                        const li = document.createElement('li');
                        li.className = 'file-item';
                        
                        // 处理大小显示，如果是目录或者无大小则隐藏
                        const sizeHtml = (item.size && item.size !== '-') ? `<span class="size">大小: ${{item.size}}</span>` : '';
                        
                        if (item.is_dir) {{
                            const newPath = path ? path + '/' + item.name : item.name;
                            // 支持点击文件夹名称进入
                            li.innerHTML = `<span class="icon">📁</span><span class="name" onclick="event.stopPropagation(); loadContent('${{newPath}}')">${{item.name}}</span>${{sizeHtml}}`;
                            li.onclick = () => {{ loadContent(newPath); }};
                        }} else {{
                            // 支持点击文件名称下载
                            li.innerHTML = `
                                <span class="icon">📄</span>
                                <span class="name" onclick="event.stopPropagation(); downloadFile('${{item.path}}')">${{item.name}}</span>
                                ${{sizeHtml}}
                                <div class="actions">
                                    <div class="action-group">
                                        <button class="btn btn-copy" onclick="event.stopPropagation(); copyLink(this, '${{item.path}}')">复制链接</button>
                                        <div class="copy-toast">已复制到剪切板</div>
                                    </div>
                                    <button class="btn btn-download" onclick="event.stopPropagation(); downloadFile('${{item.path}}')">下载</button>
                                </div>
                            `;
                        }}
                        list.appendChild(li);
                    }});
                }} else {{
                    list.innerHTML = `<li style="color:red; padding: 20px; text-align: center;">${{data.msg || '加载失败'}}</li>`;
                }}
            }}
            
            function downloadFile(filePath) {{
                window.location.href = `/s/${{shareId}}/download?path=${{encodeURIComponent(filePath)}}`;
            }}

            function copyLink(btn, filePath) {{
                const link = `${{window.location.origin}}/s/${{shareId}}/download?path=${{encodeURIComponent(filePath)}}`;
                const textarea = document.createElement('textarea');
                textarea.value = link;
                document.body.appendChild(textarea);
                textarea.select();
                try {{
                    document.execCommand('copy');
                    const toast = btn.nextElementSibling;
                    if (toast) {{
                        toast.style.display = 'block';
                        setTimeout(() => {{ toast.style.display = 'none'; }}, 2000);
                    }}
                }} catch (err) {{
                    alert('复制失败，请手动复制: ' + link);
                }}
                document.body.removeChild(textarea);
            }}
            
            // 初始化：加载根目录
            loadContent('');
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html_content)

@app.get("/s/{share_id}/download")
async def download_share(share_id: str, path: str):
    """提供分享内单文件的流式下载 (网页浏览模式使用)"""
    shares = load_shares()
    if share_id not in shares:
        return HTMLResponse("分享不存在或已取消", status_code=404)
        
    sdata = shares[share_id]
    now = int(time.time())
    if sdata["expire_time"] != 0 and sdata["expire_time"] < now:
        del shares[share_id]
        save_shares(shares)
        return HTMLResponse("分享已过期", status_code=404)
        
    paths = sdata["paths"]
    sftp = file_manager._ensure_sftp()
    if not sftp: return HTMLResponse("服务器 SFTP 未连接", status_code=500)
    
    # 根据虚拟路径解析出真实路径
    base_name = path.split('/')[0]
    real_base = None
    for p in paths:
        if os.path.basename(p) == base_name:
            real_base = p
            break
            
    if not real_base:
        return HTMLResponse("文件不在分享范围内", status_code=404)
        
    relative_path = '/'.join(path.split('/')[1:])
    if relative_path:
        real_path = os.path.join(real_base, relative_path)
    else:
        real_path = real_base
        
    real_path = os.path.normpath(real_path)
    
    # 安全检查：防止目录穿越攻击
    if not real_path.startswith(real_base.rstrip('/')):
        return HTMLResponse("无权限访问该文件", status_code=403)
        
    try:
        stat_info = sftp.stat(real_path)
        if stat.S_ISDIR(stat_info.st_mode):
            return HTMLResponse("不能下载文件夹，请选择具体的文件", status_code=400)
    except:
        return HTMLResponse("文件不存在", status_code=404)
        
    # 流式下载单文件
    def file_stream():
        try:
            with sftp.open(real_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    yield chunk
        except: pass
        
    filename = os.path.basename(real_path)
    ascii_filename = filename.encode('ascii', 'ignore').decode('ascii') or 'download'
    quoted_filename = urllib.parse.quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quoted_filename}",
        "X-Content-Type-Options": "nosniff"
    }
    return StreamingResponse(file_stream(), media_type="application/octet-stream", headers=headers)

@app.get("/s/{share_id}/{filename:path}")
async def direct_download_share(share_id: str, filename: str):
    """单文件直链下载：直接访问 /s/{id}/{filename} 触发下载"""
    shares = load_shares()
    if share_id not in shares:
        return HTMLResponse("分享不存在或已取消", status_code=404)
        
    sdata = shares[share_id]
    now = int(time.time())
    if sdata["expire_time"] != 0 and sdata["expire_time"] < now:
        del shares[share_id]
        save_shares(shares)
        return HTMLResponse("分享已过期", status_code=404)
        
    paths = sdata["paths"]
    sftp = file_manager._ensure_sftp()
    if not sftp: return HTMLResponse("服务器 SFTP 未连接", status_code=500)
    
    # 只有当分享的是单个文件时，才允许直接通过文件名下载
    if len(paths) == 1:
        real_path = paths[0]
        try:
            stat_info = sftp.stat(real_path)
            if not stat.S_ISDIR(stat_info.st_mode):
                # 流式下载
                def file_stream():
                    try:
                        with sftp.open(real_path, 'rb') as f:
                            while True:
                                chunk = f.read(65536)
                                if not chunk: break
                                yield chunk
                    except: pass
                    
                fname = os.path.basename(real_path)
                ascii_fname = fname.encode('ascii', 'ignore').decode('ascii') or 'download'
                quoted_fname = urllib.parse.quote(fname)
                headers = {
                    "Content-Disposition": f"attachment; filename=\"{ascii_fname}\"; filename*=UTF-8''{quoted_fname}",
                    "X-Content-Type-Options": "nosniff"
                }
                return StreamingResponse(file_stream(), media_type="application/octet-stream", headers=headers)
        except:
            pass
            
    # 如果不是单文件，或者文件不存在，则跳转到网页浏览界面
    return RedirectResponse(f"/s/{share_id}", status_code=302)

@app.on_event("startup")
async def startup_event():
    port = config.get("server", "port")
    if not port:
        logger.error("未配置端口，请重新运行 install.sh")
        sys.exit(1)
        
    open_firewall_port(port)
    
    # 【关键修复】始终获取本机公网 IP 用于显示访问地址
    print("正在获取本机公网 IP...")
    public_ip = IPDetector.get_public_ip()
    if not public_ip:
        public_ip = "127.0.0.1" # 如果获取失败，回退到本地地址
        
    # 单独处理 SSH Host 的配置逻辑
    current_ssh_host = config.get("ssh", "host", default="")
    if not current_ssh_host or current_ssh_host == "0.0.0.0":
        print("检测到 SSH Host 未配置，自动将本机 IP 设为 SSH 主机...")
        ssh_config = config.get("ssh", default={})
        ssh_config["host"] = public_ip
        config.update("ssh", ssh_config)
        print(f"✅ 已自动获取公网 IP 并更新至 SSH 配置文件: {public_ip}")
    else:
        print(f"配置文件中已有 SSH Host ({current_ssh_host})，跳过自动获取。")
    
    print("=" * 60)
    print("WzFileManager 启动成功!")
    print(f"访问地址: http://{public_ip}:{port}")
    print(f"默认登录密码: admin123")
    
    binary_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
    binary_dir = os.path.dirname(binary_path)
    
    # 自动判断系统并生成对应的开机启动配置
    is_openwrt = os.path.exists("/etc/openwrt_release")
    
    if is_openwrt:
        # OpenWrt (init.d) 系统
        try:
            initd_path = "/etc/init.d/wzfilemanager"
            initd_content = f"""#!/bin/sh /etc/rc.common

START=99
STOP=01
PIDFILE="/var/run/wzfilemanager.pid"
LOGFILE="/var/log/wzfilemanager.log"

start() {{
    echo "Starting WzFileManager process"
    nohup {binary_path} >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
}}

stop() {{
    echo "Stopping WzFileManager process"
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        if ps | grep -v grep | grep "$pid" > /dev/null; then
            kill "$pid"
            sleep 2
            if ps | grep -v grep | grep "$pid" > /dev/null; then
                kill -9 "$pid"
            fi
            rm -f "$PIDFILE"
        fi
    fi
}}
"""
            with open(initd_path, 'w') as f:
                f.write(initd_content)
            os.chmod(initd_path, 0o755)
            os.system("/etc/init.d/wzfilemanager enable >/dev/null 2>&1")
            print("✅ 已自动创建并启用开机自启服务 (/etc/init.d/wzfilemanager)")
        except Exception as e:
            print(f"⚠️ 创建 OpenWrt 开机自启服务失败: {str(e)}")
            
        print("=" * 60)
        print("【管理命令】")
        print("-" * 20)
        print("1. OpenWrt 服务管理 (已支持开机自启):")
        print("启动: /etc/init.d/wzfilemanager start")
        print("停止: /etc/init.d/wzfilemanager stop")
        print("重启: /etc/init.d/wzfilemanager restart")
        print("状态: /etc/init.d/wzfilemanager status")
        print("-" * 20)
        print("2. 手动命令管理:")
        print(f"启动: {binary_path}")
        print(f"后台启动: cd {binary_dir} && nohup {binary_path} > /dev/null 2>&1 &")
        print(f"停止: pkill -f '{binary_path}'")
        print("=" * 60)
    else:
        # Systemd 系统 (Ubuntu/CentOS/Debian 等)
        try:
            service_path = "/etc/systemd/system/wzfilemanager.service"
            service_content = f"""[Unit]
Description=WzFileManager Service
After=network.target

[Service]
Type=simple
WorkingDirectory={binary_dir}
ExecStart={binary_path}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
"""
            need_update = True
            if os.path.exists(service_path):
                with open(service_path, 'r') as f:
                    old_content = f.read()
                if old_content == service_content:
                    need_update = False
            
            if need_update:
                with open(service_path, 'w') as f:
                    f.write(service_content)
                os.system("systemctl daemon-reload >/dev/null 2>&1")
                os.system("systemctl enable wzfilemanager >/dev/null 2>&1")
                print("✅ 已自动更新并启用开机自启服务 (wzfilemanager.service)")
        except Exception as e:
            print(f"⚠️ 创建或更新开机自启服务失败: {str(e)}")

        print("=" * 60)
        print("【管理命令】")
        print("-" * 20)
        print("1. Systemd 服务管理 (推荐，已支持开机自启):")
        print("启动: systemctl start wzfilemanager")
        print("停止: systemctl stop wzfilemanager")
        print("重启: systemctl restart wzfilemanager")
        print("状态: systemctl status wzfilemanager --no-pager")
        print("-" * 20)
        print("2. 手动命令管理:")
        print(f"启动: {binary_path}")
        print(f"后台启动: cd {binary_dir} && nohup {binary_path} > /dev/null 2>&1 &")
        print(f"停止: pkill -f '{binary_path}'")
        print("=" * 60)

    print("提示: 如果链接不能访问，请自行开放链接中端口 (软路由配置需端口映射)")
    print("配置信息已保存在 config.json 中, 可手动修改后重启生效")

    # 自动生成说明.txt (智能匹配系统命令)
    help_file_path = os.path.join(binary_dir, "说明.txt")
    if not os.path.exists(help_file_path):
        if is_openwrt:
            help_content = f"""============================================================
WzFileManager 启动成功!
访问地址: http://{public_ip}:{port}
默认登录密码: admin123
============================================================
【管理命令】
--------------------
1. OpenWrt 服务管理 (已支持开机自启):
启动: /etc/init.d/wzfilemanager start
停止: /etc/init.d/wzfilemanager stop
重启: /etc/init.d/wzfilemanager restart
状态: /etc/init.d/wzfilemanager status
--------------------
2. 手动命令管理:
启动: {binary_path}
后台启动: cd {binary_dir} && nohup {binary_path} > /dev/null 2>&1 &
停止: pkill -f '{binary_path}'
============================================================
提示: 如果链接不能访问，请自行开放端口 (链接中的端口)
配置信息已保存在 config.json 中, 可手动修改后重启生效
"""
        else:
            help_content = f"""============================================================
WzFileManager 启动成功!
访问地址: http://{public_ip}:{port}
默认登录密码: admin123
============================================================
【管理命令】
--------------------
1. Systemd 服务管理 (推荐):
启动: systemctl start wzfilemanager
停止: systemctl stop wzfilemanager
重启: systemctl restart wzfilemanager
状态: systemctl status wzfilemanager --no-pager
--------------------
2. 手动命令管理:
启动: {binary_path}
后台启动: cd {binary_dir} && nohup {binary_path} > /dev/null 2>&1 &
停止: pkill -f '{binary_path}'
重启: pkill -f '{binary_path}'; cd {binary_dir} && nohup {binary_path} > /dev/null 2>&1 &
============================================================
提示: 如果链接不能访问，请自行开放端口 (链接中的端口)
配置信息已保存在 config.json 中, 可手动修改后重启生效
"""
        try:
            with open(help_file_path, 'w', encoding='utf-8') as f:
                f.write(help_content)
            print("✅ 已自动生成说明文档: 说明.txt")
        except Exception as e:
            print(f"⚠️ 生成说明文档失败: {str(e)}")

    ssh_password = config.get("ssh", "password", default="")
    ssh_key_password = config.get("ssh", "key_password", default="")
    if config.get("ssh", "host") and (ssh_password or ssh_key_password):
        try:
            ok, msg = ssh.connect()
            print(f"SSH 自动连接: {msg}")
        except Exception as e:
            print(f"SSH 自动连接失败，请检查配置: {str(e)}")
    else:
        print("未配置 SSH 密码或密钥，跳过自动连接。")

    async def cleanup_loop():
        while True:
            await asyncio.sleep(300)
            auth.cleanup_expired()
            log_file_path = os.path.join(log_dir, "app.log")
            try:
                if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > 1 * 1024 * 1024:
                    with open(log_file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    last_50_lines = lines[-50:]
                    with open(log_file_path, 'w', encoding='utf-8') as f:
                        f.writelines(last_50_lines)
                    logger.info("日志文件超过 1MB，已自动清理并保留最新 50 行")
            except Exception:
                pass

    asyncio.create_task(cleanup_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global is_port_in_use
    ssh.disconnect()
    if not is_port_in_use:
        print("WzFileManager 已停止")

if __name__ == "__main__":
    port = config.get("server", "port")
    host = config.get("server", "host", default="0.0.0.0")
    
    if not port:
        import socket, random
        port_range = config.get("server", "port_range", default=[30000, 55000])
        used_ports = set()
        try:
            import subprocess
            result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 4 and ":" in parts[3]: used_ports.add(int(parts[3].rsplit(":", 1)[1]))
        except: pass
        
        for _ in range(200):
            random_port = random.randint(port_range[0], port_range[1])
            if random_port not in used_ports:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    try:
                        s.bind(("0.0.0.0", random_port))
                        port = random_port
                        break
                    except OSError:
                        continue
        
        if port:
            config.update("server", {"port": port})
            print(f"✅ 未配置端口，已自动分配随机端口: {port}")
        else:
            print("❌ 未配置端口，且自动分配失败，请手动在 config.json 中指定端口")
            sys.exit(1)
            
    uvicorn.run(app, host=host, port=port, log_level="info")