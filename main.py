import os
import sys
import json
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import ConfigManager
from core.auth import AuthManager
from core.ip_detector import IPDetector
from core.port_detector import PortDetector
from core.ssh_manager import SSHManager
from core.file_ops import FileManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/app.log", encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 【修复 Errno 2】确保所有必需目录在启动前自动创建
Path("logs").mkdir(parents=True, exist_ok=True)
Path("web/static/css").mkdir(parents=True, exist_ok=True)
Path("web/static/js").mkdir(parents=True, exist_ok=True)
Path("web/templates").mkdir(parents=True, exist_ok=True)

config = ConfigManager("config.json")
auth = AuthManager(config)
ssh = SSHManager(config)
file_manager = FileManager(ssh)

app = FastAPI(title="WzFileManager", version="1.0.0")

app.mount("/static", StaticFiles(directory="web/static"), name="static")
templates = Jinja2Templates(directory="web/templates")


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
    return {
        "success": True, "connected": ok, "message": msg,
        "host": config.get("ssh", "host"), "port": config.get("ssh", "port"),
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
    public_ip = IPDetector.get_public_ip()
    
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
    asyncio.create_task(cleanup_loop())

@app.on_event("shutdown")
async def shutdown_event():
    ssh.disconnect()
    logger.info("WzFileManager 已关闭")

if __name__ == "__main__":
    port = config.get("server", "port")
    host = config.get("server", "host", default="0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")