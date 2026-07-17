import os
import sys
import json
import stat
import logging
import asyncio
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

# 【关键修复】判断是否为 PyInstaller 打包环境，动态获取资源绝对路径
if getattr(sys, 'frozen', False):
    # 如果是 PyInstaller 打包的二进制运行，资源会被解压到 sys._MEIPASS
    BASE_DIR = sys._MEIPASS
    # 运行目录（用于存放 config.json 和 logs）
    RUN_DIR = os.path.dirname(sys.executable)
else:
    # 如果是源码运行，使用当前目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RUN_DIR = BASE_DIR

sys.path.insert(0, BASE_DIR)

from core.config import ConfigManager
from core.auth import AuthManager
from core.ip_detector import IPDetector
from core.port_detector import PortDetector
from core.ssh_manager import SSHManager
from core.file_ops import FileManager

# 【修复 Errno 2】确保所有必需目录在启动前自动创建
log_dir = os.path.join(RUN_DIR, "logs")
Path(log_dir).mkdir(parents=True, exist_ok=True)
Path(os.path.join(BASE_DIR, "web/static/css")).mkdir(parents=True, exist_ok=True)
Path(os.path.join(BASE_DIR, "web/static/js")).mkdir(parents=True, exist_ok=True)
Path(os.path.join(BASE_DIR, "web/templates")).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(log_dir, "app.log"), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

config_path = os.path.join(RUN_DIR, "config.json")
config = ConfigManager(config_path)
auth = AuthManager(config)
ssh = SSHManager(config)

# 获取 config.json 所在的真实目录（如果是软链接会解析到源文件目录，如 Docker 中的 data 目录）
real_config_dir = os.path.dirname(os.path.realpath(config_path))
file_manager = FileManager(ssh, real_config_dir)

app = FastAPI(title="WzFileManager", version="1.0.0")

# 使用前面计算好的绝对路径挂载静态文件和模板
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "web/static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "web/templates"))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = ["/login", "/api/login", "/static", "/api/public-info"]
    
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
    ok, msg = ssh.ensure_connected()
    # 【新增】检查是否已配置密码或密钥密码
    is_configured = bool(config.get("ssh", "password") or config.get("ssh", "key_password"))
    return {
        "success": True, 
        "connected": ok, 
        "message": msg,
        "configured": is_configured,  # 新增字段：是否已配置密码/密钥
        "host": config.get("ssh", "host"), 
        "port": config.get("ssh", "port"),
        "username": config.get("ssh", "username")
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
    src = data.get("src")
    dst = data.get("dst")
    overwrite = data.get("overwrite", False)
    
    sftp = file_manager._ensure_sftp()
    if not sftp: return JSONResponse({"success": False, "msg": "SFTP 未连接"})
    try:
        sftp.stat(dst)
        exists = True
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
    src = data.get("src")
    dst = data.get("dst")
    overwrite = data.get("overwrite", False)
    
    sftp = file_manager._ensure_sftp()
    if not sftp: return JSONResponse({"success": False, "msg": "SFTP 未连接"})
    try:
        sftp.stat(dst)
        exists = True
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

# 新增：处理权限和用户组同时修改的接口
@app.post("/api/files/perm")
async def set_file_permission(request: Request):
    data = await request.json()
    path = data.get("path")
    perm = data.get("perm")
    group = data.get("group")
    recursive = data.get("recursive", False)
    return file_manager.set_permission(path, perm, group, recursive)

@app.post("/api/files/upload")
async def upload_file(path: str = Form(...), file: UploadFile = File(...), overwrite: bool = Form(False)):
    file_data = await file.read()
    sftp = file_manager._ensure_sftp()
    if not sftp: return JSONResponse({"success": False, "msg": "SFTP 未连接"})
    
    remote_file = file_manager._normalize_path(f"{path}/{file.filename}")
    try:
        sftp.stat(remote_file)
        exists = True
    except IOError:
        exists = False
    
    if exists and not overwrite:
        return JSONResponse({"success": False, "code": "exists", "msg": f"文件 {file.filename} 已存在"})
    
    return file_manager.upload_file(path, file_data, file.filename)

@app.get("/api/files/download")
async def download_file(path: str):
    data, filename, error = file_manager.download_file(path)
    if error:
        return JSONResponse({"success": False, "msg": error})
    
    # 对文件名进行 RFC 5987 编码，支持中文和表情符号
    ascii_filename = filename.encode('ascii', 'ignore').decode('ascii') or 'download'
    quoted_filename = urllib.parse.quote(filename)
    
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quoted_filename}"
    }
    return Response(content=data, media_type="application/octet-stream", headers=headers)

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
    
# ----- 回收站接口 -----
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
        
    # 直接执行 rm -rf 彻底删除
    ok, out, err = ssh.execute(f'rm -rf "{item_path}"')
    if ok:
        return {"success": True, "msg": "已彻底删除"}
    return {"success": False, "msg": err or "删除失败"}

@app.post("/api/trash/restore-batch")
async def restore_trash_batch_api(request: Request):
    data = await request.json()
    trash_ids = data.get("trash_ids", [])
    if not trash_ids: return JSONResponse({"success": False, "msg": "无选中项"})
    for tid in trash_ids:
        file_manager.restore(tid)
    return {"success": True, "msg": f"已尝试还原 {len(trash_ids)} 项"}

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
        # 直接执行 rm -rf 彻底删除
        ssh.execute(f'rm -rf "{item_path}"')
        
    return {"success": True, "msg": f"已彻底删除 {len(trash_ids)} 项"}

@app.post("/api/trash/clear")
async def clear_trash():
    trash_base = file_manager._get_trash_base()
    ok, out, err = ssh.execute(f'rm -rf "{trash_base}"/*')
    if ok:
        return {"success": True, "msg": "回收站已清空"}
    return {"success": False, "msg": err}

# ----- 收藏夹接口 (云端同步) -----
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
                # 如果是文件，自动识别为其所在的当前目录
                final_path = os.path.dirname(req_path)
        except Exception:
            # 路径无效或不存在，自动恢复为 /
            final_path = "/"
    
    # 保存到 config.json
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
            subprocess.run(["firewall-cmd", "--zone=public", "--add-port", f"{port}/tcp", "--permanent"], check=False)
            subprocess.run(["firewall-cmd", "--reload"], check=False)
        elif subprocess.run(["which", "ufw"], capture_output=True).stdout:
            subprocess.run(["ufw", "allow", f"{port}/tcp"], check=False)
    except: pass

@app.on_event("startup")
async def startup_event():
    port = config.get("server", "port")
    if not port:
        logger.error("未配置端口，请重新运行 install.sh")
        sys.exit(1)
        
    open_firewall_port(port)
    
    # 【新增】自动获取公网 IP 并更新 config.json
    current_ssh_host = config.get("ssh", "host", default="")
    public_ip = current_ssh_host
    
    if not current_ssh_host or current_ssh_host == "0.0.0.0":
        logger.info("检测到 SSH Host 未配置 (0.0.0.0)，正在自动获取公网 IP...")
        detected_ip = IPDetector.get_public_ip()
        if detected_ip:
            public_ip = detected_ip
            # 读取现有配置，更新 host，写回文件，防止覆盖其他 SSH 字段
            ssh_config = config.get("ssh", default={})
            ssh_config["host"] = detected_ip
            config.update("ssh", ssh_config)
            logger.info(f"✅ 已自动获取公网 IP 并更新至配置文件: {detected_ip}")
        else:
            logger.warning("⚠️ 无法获取公网 IP，请在配置文件中手动设置 ssh.host")
    else:
        logger.info(f"配置文件中已有 SSH Host ({current_ssh_host})，跳过自动获取。")
    
    logger.info("=" * 60)
    logger.info("WzFileManager 启动成功!")
    logger.info(f"访问地址: http://{public_ip}:{port}")
    logger.info("=" * 60)

    if config.get("ssh", "host"):
        ok, msg = ssh.connect()
        logger.info(f"SSH 自动连接: {msg}")

    async def cleanup_loop():
        while True:
            await asyncio.sleep(300)
            auth.cleanup_expired()
            
            # 【新增】检查日志文件大小，超过 1MB 则只保留最新 50 行
            log_file_path = os.path.join(log_dir, "app.log")
            try:
                if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > 1 * 1024 * 1024:
                    with open(log_file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    # 保留最后 50 行
                    last_50_lines = lines[-50:]
                    with open(log_file_path, 'w', encoding='utf-8') as f:
                        f.writelines(last_50_lines)
                    logger.info("日志文件超过 1MB，已自动清理并保留最新 50 行")
            except Exception as e:
                pass # 防止日志清理失败导致主循环崩溃

    asyncio.create_task(cleanup_loop())

@app.on_event("shutdown")
async def shutdown_event():
    ssh.disconnect()
    logger.info("WzFileManager 已停止")

if __name__ == "__main__":
    port = config.get("server", "port")
    host = config.get("server", "host", default="0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")