import warnings
warnings.filterwarnings("ignore")
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
    # 运行目录改为二进制文件所在目录，防止无写权限导致卡死
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

# 【新增】Uvicorn 日志中文翻译过滤器
class ChineseLogFilter(logging.Filter):
    def filter(self, record):
        if "Started server process" in record.msg:
            record.msg = record.msg.replace("Started server process", "已启动服务进程")
        elif "Waiting for application startup." in record.msg:
            record.msg = record.msg.replace("Waiting for application startup.", "等待应用程序启动...")
        elif "Application startup complete." in record.msg:
            record.msg = record.msg.replace("Application startup complete.", "应用程序启动完成。")
        elif "Uvicorn running on" in record.msg:
            record.msg = record.msg.replace("Uvicorn running on", "Uvicorn 运行在")
        elif "Shutting down" in record.msg:
            record.msg = record.msg.replace("Shutting down", "正在关闭")
        elif "Waiting for application shutdown." in record.msg:
            record.msg = record.msg.replace("Waiting for application shutdown.", "等待应用程序关闭...")
        elif "Application shutdown complete." in record.msg:
            record.msg = record.msg.replace("Application shutdown complete.", "应用程序关闭完成。")
        # 【新增】翻译端口被占用错误
        elif "error while attempting to bind on address" in record.msg and "address already in use" in record.msg:
            record.msg = "端口被占用！请使用 'pkill -f wzfilemanager' 停止旧进程，或修改 config.json 中的端口后重试。"
        return True

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(log_dir, "app.log"), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 给 Uvicorn 的日志加上翻译过滤器
logging.getLogger("uvicorn.error").addFilter(ChineseLogFilter())
logging.getLogger("uvicorn.access").addFilter(ChineseLogFilter())

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
    data, filename, error = file_manager.download_file(path)
    if error:
        return JSONResponse({"success": False, "msg": error})
    ascii_filename = filename.encode('ascii', 'ignore').decode('ascii') or 'download'
    quoted_filename = urllib.parse.quote(filename)
    headers = {"Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quoted_filename}"}
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
    
    current_ssh_host = config.get("ssh", "host", default="")
    public_ip = current_ssh_host
    if not current_ssh_host or current_ssh_host == "0.0.0.0":
        logger.info("检测到SSH未配置，正在自动获取公网 IP...")
        detected_ip = IPDetector.get_public_ip()
        if detected_ip:
            public_ip = detected_ip
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
    logger.info(f"默认登录密码: admin123")
    
    # 自动识别路径并输出管理命令
    binary_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
    binary_dir = os.path.dirname(binary_path)
    
    logger.info("-" * 60)
    logger.info("【管理命令】")
    logger.info(f"启动: {binary_path}")
    logger.info(f"后台启动: cd {binary_dir} && nohup {binary_path} > /dev/null 2>&1 &")
    logger.info(f"停止: pkill -f '{binary_path}'")
    logger.info(f"重启: pkill -f '{binary_path}'; cd {binary_dir} && nohup {binary_path} > /dev/null 2>&1 &")
    logger.info("-" * 60)
    logger.info("提示: 如果链接不能访问，请自行开放端口 (链接中的端口)")
    logger.info("=" * 60)

    # 自动创建开机启动配置
    if getattr(sys, 'frozen', False):
        try:
            service_path = "/etc/systemd/system/wzfilemanager.service"
            if not os.path.exists(service_path):
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
                with open(service_path, 'w') as f:
                    f.write(service_content)
                os.system("systemctl daemon-reload")
                os.system("systemctl enable wzfilemanager")
                logger.info("✅ 已自动创建并启用开机自启服务 (wzfilemanager.service)")
        except Exception as e:
            logger.warning(f"⚠️ 创建开机自启服务失败: {str(e)}")

    ssh_password = config.get("ssh", "password", default="")
    ssh_key_password = config.get("ssh", "key_password", default="")
    if config.get("ssh", "host") and (ssh_password or ssh_key_password):
        try:
            ok, msg = ssh.connect()
            logger.info(f"SSH 自动连接: {msg}")
        except Exception as e:
            logger.warning(f"SSH 自动连接失败，请检查配置: {str(e)}")
    else:
        logger.info("未配置 SSH 密码或密钥，跳过自动连接。")

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
    ssh.disconnect()
    logger.info("WzFileManager 已停止")

if __name__ == "__main__":
    port = config.get("server", "port")
    host = config.get("server", "host", default="0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")