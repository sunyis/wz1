import os
import stat
import time
import uuid
from pathlib import PurePosixPath
from typing import Dict, Any, Tuple
import paramiko

class FileManager:
    def __init__(self, ssh_manager, base_dir="."):
        self.ssh = ssh_manager
        # 获取传入目录的绝对路径，如果是软链接会解析到真实路径
        self.base_dir = os.path.abspath(base_dir)
        # 回收站直接放在 config.json 所在的目录下
        self.trash_dir = f"{self.base_dir}/.wzfilemanager_trash"

    def _translate_error(self, e):
        """将常见的系统底层错误翻译为中文"""
        err_msg = str(e)
        if "No such file" in err_msg or "Errno 2" in err_msg:
            return "文件或目录不存在,可以刷新试试"
        if "Permission denied" in err_msg or "Errno 13" in err_msg:
            return "权限不足，拒绝访问"
        if "File exists" in err_msg or "Errno 17" in err_msg:
            return "文件已存在"
        # 命令不存在的错误翻译
        if "7z: command not found" in err_msg:
            return "服务器未安装 7z，无法操作 7z 格式"
        if "rar: command not found" in err_msg:
            return "服务器未安装 rar，无法操作 rar 格式"
        if "unrar: command not found" in err_msg:
            return "服务器未安装 unrar，无法解压 rar 格式"
        if "zip: command not found" in err_msg:
            return "服务器未安装 zip，无法压缩 zip 格式"
        if "unzip: command not found" in err_msg:
            return "服务器未安装 unzip，无法解压 zip 格式"
        return err_msg

    def _ensure_sftp(self):
        ok, msg = self.ssh.ensure_connected()
        if not ok: return None
        return self.ssh.sftp

    def _normalize_path(self, path: str) -> str:
        if not path: return "/"
        return str(PurePosixPath(path))

    def _get_trash_base(self) -> str:
        # 直接返回 config.json 所在目录下的回收站路径
        return self.trash_dir

    def list_dir(self, path: str) -> Dict[str, Any]:
        if path.startswith('~'):
            ok, out, err = self.ssh.execute(f'echo {path}')
            if ok: path = out.strip()
                
        path = self._normalize_path(path)
        sftp = self._ensure_sftp()
        if not sftp:
            return {"success": False, "msg": "SFTP 未连接"}
            
        files, dirs = [], []
        try:
            # 直接使用 SFTP 原生接口，完美兼容 OpenWrt 数据访问
            for attr in sftp.listdir_attr(path):
                filename = attr.filename
                if filename == '.' or filename == '': continue
                
                mode = attr.st_mode
                size = attr.st_size
                mtime = int(attr.st_mtime)
                is_dir = stat.S_ISDIR(mode)
                is_link = stat.S_ISLNK(mode)
                
                # 【新增】获取软链接的目标路径
                link_target = ""
                if is_link:
                    try:
                        link_target = sftp.readlink(f"{path}/{filename}")
                    except: pass

                item = {
                    "filename": filename,
                    "size": size,
                    "mtime": mtime,
                    "mode": mode,
                    "is_dir": is_dir,
                    "is_link": is_link,
                    "link_target": link_target,
                    "permissions": self._format_permissions(mode),
                    "octal_permissions": oct(mode & 0o777)[2:],  # 新增：获取数字权限，如 '755'
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    "size_str": self._format_size(size) if not is_dir else "-",
                    "extension": self._get_extension(filename),
                }
                if is_dir: dirs.append(item)
                else: files.append(item)
        except Exception as e:
            return {"success": False, "msg": self._translate_error(e)}
                
        dirs.sort(key=lambda x: x["filename"].lower())
        files.sort(key=lambda x: x["filename"].lower())
        return {"success": True, "path": path, "files": dirs + files, "dir_count": len(dirs), "file_count": len(files)}

    def delete(self, path: str, permanent: bool = False) -> Dict[str, Any]:
        if path.startswith('~'):
            ok, out, err = self.ssh.execute(f'echo {path}')
            if ok: path = out.strip()
                
        trash_base = self._get_trash_base()
        
        if permanent or trash_base in path:
            ok, out, err = self.ssh.execute(f'rm -rf "{path}"')
            if ok: return {"success": True, "msg": "已彻底删除"}
            return {"success": False, "msg": err}
            
        self.ssh.execute(f'mkdir -p "{trash_base}"')
        
        trash_id = f"trash_{uuid.uuid4().hex[:8]}"
        trash_dir = f"{trash_base}/{trash_id}"
        self.ssh.execute(f'mkdir -p "{trash_dir}"')
        
        ok, out, err = self.ssh.execute(f'mv "{path}" "{trash_dir}/"')
        if not ok:
            self.ssh.execute(f'rm -rf "{trash_dir}"')
            return {"success": False, "msg": err}
            
        info_file = f"{trash_dir}/.trash_info"
        escaped_path = path.replace("'", "'\\''")
        self.ssh.execute(f"echo '{escaped_path}' > '{info_file}'")
        
        return {"success": True, "msg": "已移至回收站"}

    def list_trash(self) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": True, "files": []}
            
        trash_base = self._get_trash_base()
        cmd = f'ls -A "{trash_base}" 2>/dev/null'
        ok, out, err = self.ssh.execute(cmd)
        if not ok or not out.strip():
            return {"success": True, "files": []}
            
        files = []
        for item_name in out.strip().split('\n'):
            item_name = item_name.strip()
            if not item_name: continue
            
            item_path = f"{trash_base}/{item_name}"
            ok_test, _, _ = self.ssh.execute(f'test -d "{item_path}"')
            is_trash_dir = ok_test  # 这是回收站的包裹目录
            
            if item_name.startswith("trash_") and is_trash_dir:
                trash_id = item_name
                ok_info, out_info, _ = self.ssh.execute(f'cat "{item_path}/.trash_info"')
                original_path = out_info.strip() if ok_info and out_info.strip() else "/"
                filename = os.path.basename(original_path)
                
                full_path = ""
                try:
                    for f_name in sftp.listdir(item_path):
                        if f_name != ".trash_info":
                            full_path = f"{item_path}/{f_name}"
                            break
                except:
                    pass
                
                size, mtime = 0, 0
                is_real_dir = False
                if full_path:
                    # 【关键修复】使用 test -d 判断真实文件是否为目录，兼容性最好
                    ok_real_test, _, _ = self.ssh.execute(f'test -d "{full_path}"')
                    is_real_dir = ok_real_test
                    
                    try:
                        attr = sftp.stat(full_path)
                        size = attr.st_size
                        mtime = int(attr.st_mtime)
                    except: pass
            else:
                trash_id = f"root_{item_name}"
                original_path = f"/{item_name}"
                filename = item_name
                full_path = item_path
                
                size, mtime = 0, 0
                is_real_dir = False
                # 判断 root_ 文件是否为目录
                ok_real_test, _, _ = self.ssh.execute(f'test -d "{full_path}"')
                is_real_dir = ok_real_test
                
                try:
                    attr = sftp.stat(full_path)
                    size = attr.st_size
                    mtime = int(attr.st_mtime)
                except: pass
                            
            files.append({
                "trash_id": trash_id,
                "filename": filename,
                "original_path": original_path,
                "size": size,
                "mtime": mtime,
                "size_str": self._format_size(size) if size > 0 else "-",
                "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                "is_dir": is_real_dir  # 返回真实的文件/文件夹类型
            })
            
        return {"success": True, "files": files}

    def restore(self, trash_id: str) -> Dict[str, Any]:
        trash_base = self._get_trash_base()
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        
        if trash_id.startswith("root_"):
            filename = trash_id[5:]
            src_path = f"{trash_base}/{filename}"
            target_path = f"/{filename}"
            
            ok_check, _, _ = self.ssh.execute(f'test -e "{target_path}"')
            if ok_check:
                dot = target_path.rfind('.')
                if dot == -1: target_path += "_(副本)"
                else: target_path = target_path[:dot] + "_(副本)" + target_path[dot:]
                
            ok_mv, _, err_mv = self.ssh.execute(f'mv "{src_path}" "{target_path}"')
            if not ok_mv: return {"success": False, "msg": err_mv}
            return {"success": True, "msg": f"已还原到: {target_path}", "target_path": target_path}
            
        else:
            trash_dir = f"{trash_base}/{trash_id}"
            ok, _, _ = self.ssh.execute(f'test -d "{trash_dir}"')
            if not ok: return {"success": False, "msg": "回收站项目不存在"}
            
            ok_info, out_info, _ = self.ssh.execute(f'cat "{trash_dir}/.trash_info"')
            if not ok_info: return {"success": False, "msg": "无法读取原路径信息"}
            
            original_path = out_info.strip()
            ok_check, _, _ = self.ssh.execute(f'test -e "{original_path}"')
            if ok_check:
                dot = original_path.rfind('.')
                if dot == -1: target_path = f"{original_path}_(副本)"
                else: target_path = f"{original_path[:dot]}_(副本){original_path[dot:]}"
            else:
                target_path = original_path
                
            target_dir = os.path.dirname(target_path)
            self.ssh.execute(f'mkdir -p "{target_dir}"')
            
            try:
                files_in_trash = sftp.listdir(trash_dir)
            except Exception as e:
                return {"success": False, "msg": f"读取回收站目录失败: {str(e)}"}
                
            real_filename = None
            for f in files_in_trash:
                if f != ".trash_info":
                    real_filename = f
                    break
                
            if not real_filename: 
                return {"success": False, "msg": "回收站项目为空"}
            
            full_path_in_trash = f"{trash_dir}/{real_filename}"
            
            ok_mv, _, err_mv = self.ssh.execute(f'mv "{full_path_in_trash}" "{target_path}"')
            if not ok_mv: return {"success": False, "msg": err_mv}
            
            self.ssh.execute(f'rm -rf "{trash_dir}"')
            return {"success": True, "msg": f"已还原到: {target_path}", "target_path": target_path}

    def get_trash_file_info(self, trash_id: str) -> Dict[str, Any]:
        """获取回收站文件的真实路径和原始文件名，供下载使用"""
        trash_base = self._get_trash_base()
        sftp = self._ensure_sftp()
        if not sftp:
            return {"success": False, "msg": "SFTP 未连接"}

        if trash_id.startswith("root_"):
            filename = trash_id[5:]
            full_path = f"{trash_base}/{filename}"
            original_name = filename
        else:
            trash_dir = f"{trash_base}/{trash_id}"
            ok, _, _ = self.ssh.execute(f'test -d "{trash_dir}"')
            if not ok:
                return {"success": False, "msg": "回收站项目不存在"}
            
            ok_info, out_info, _ = self.ssh.execute(f'cat "{trash_dir}/.trash_info"')
            if not ok_info:
                return {"success": False, "msg": "无法读取原路径信息"}
            
            original_path = out_info.strip()
            original_name = os.path.basename(original_path)
            
            try:
                files_in_trash = sftp.listdir(trash_dir)
            except Exception as e:
                return {"success": False, "msg": f"读取回收站目录失败: {str(e)}"}
                
            real_filename = None
            for f in files_in_trash:
                if f != ".trash_info":
                    real_filename = f
                    break
                    
            if not real_filename:
                return {"success": False, "msg": "回收站项目为空"}
                
            full_path = f"{trash_dir}/{real_filename}"

        # 预检：防止下载目录或无权限文件
        try:
            stat_info = sftp.stat(full_path)
            if stat.S_ISDIR(stat_info.st_mode):
                return {"success": False, "msg": "不能下载文件夹，请先还原"}
        except Exception as e:
            return {"success": False, "msg": self._translate_error(e)}

        return {
            "success": True,
            "real_path": full_path,
            "original_name": original_name
        }

    def get_file_info(self, path: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try:
            path = self._normalize_path(path)
            stat_info = sftp.stat(path)
            
            is_dir = stat.S_ISDIR(stat_info.st_mode)
            if not is_dir:
                try:
                    sftp.listdir(path)
                    is_dir = True
                except IOError:
                    is_dir = False

            if is_dir:
                # 【兼容优化】使用 du -sk (KB) 然后乘以 1024 转为字节，兼容所有精简系统
                ok_du, out_du, _ = self.ssh.execute(f'du -sk "{path}" 2>/dev/null')
                size = int(out_du.split()[0]) * 1024 if ok_du and out_du else 0
            else:
                size = stat_info.st_size

            group_name = ""
            ok_g, out_g, _ = self.ssh.execute(f'stat -c "%G" "{path}" 2>/dev/null')
            if ok_g:
                group_name = out_g.strip()

            return {
                "success": True,
                "info": {
                    "path": path,
                    "name": os.path.basename(path),
                    "size": size,
                    "size_str": self._format_size(size),
                    "permissions": self._format_permissions(stat_info.st_mode),
                    "octal_permissions": oct(stat_info.st_mode & 0o777),
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat_info.st_mtime)),
                    "is_dir": is_dir,
                    "group": group_name
                }
            }
        except Exception as e: 
            print(f"[ERROR] get_file_info: {str(e)}")
            return {"success": False, "msg": self._translate_error(e)}

    def read_file(self, path: str, encoding: str = "utf-8") -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try:
            with sftp.open(path, 'rb') as f: content = f.read()
            
            if b'\x00' in content[:1024]:
                return {"success": False, "msg": "该文件是二进制文件，不支持在线预览"}
                
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    text = content.decode('gbk')
                except UnicodeDecodeError:
                    text = content.decode('latin-1')
            return {"success": True, "content": text}
        except Exception as e:
            err_str = str(e)
            if "Failure" in err_str or "Permission denied" in err_str:
                return {"success": False, "msg": "该文件不支持在线预览"}
            return {"success": False, "msg": self._translate_error(e)}

    def save_file(self, path: str, content: str, encoding: str = "utf-8") -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try:
            with sftp.open(path, 'w') as f: f.write(content.encode(encoding))
            return {"success": True, "msg": "保存成功"}
        except Exception as e: return {"success": False, "msg": str(e)}

    def create_file(self, path: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try:
            with sftp.open(path, 'w') as f: f.write("")
            return {"success": True, "msg": "文件创建成功"}
        except Exception as e: return {"success": False, "msg": str(e)}

    def create_dir(self, path: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try: sftp.mkdir(path); return {"success": True, "msg": "目录创建成功"}
        except Exception as e: return {"success": False, "msg": str(e)}

    def rename(self, old_path: str, new_path: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try: sftp.rename(old_path, new_path); return {"success": True, "msg": "重命名成功"}
        except Exception as e: return {"success": False, "msg": str(e)}

    def copy(self, src: str, dst: str) -> Dict[str, Any]:
        ok, out, err = self.ssh.execute(f'cp -r "{src}" "{dst}"')
        if ok: return {"success": True, "msg": "复制成功"}
        return {"success": False, "msg": err}

    def move(self, src: str, dst: str) -> Dict[str, Any]:
        return self.rename(src, dst)

    def chmod(self, path: str, mode: int) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try: sftp.chmod(path, mode); return {"success": True, "msg": "权限修改成功"}
        except Exception as e: return {"success": False, "msg": str(e)}

    def set_permission(self, path: str, perm: str, group: str, recursive: bool) -> Dict[str, Any]:
        import re
        if not path:
            return {"success": False, "msg": "路径不能为空"}
            
        path = self._normalize_path(path)
        r_flag = "-R" if recursive else ""
        cmds = []
        
        if perm:
            if not re.match(r'^[0-7]{3,4}$', perm):
                return {"success": False, "msg": "权限格式错误，应为 3-4 位数字"}
            cmds.append(f'chmod {r_flag} {perm} "{path}"')
            
        if group:
            if not re.match(r'^[a-zA-Z0-9_\.\-]+$', group):
                return {"success": False, "msg": "用户组名称包含非法字符"}
            cmds.append(f'chown {r_flag} :{group} "{path}"')
            
        if not cmds:
            return {"success": False, "msg": "没有需要修改的项"}
            
        for cmd in cmds:
            ok, out, err = self.ssh.execute(cmd)
            if not ok:
                err_msg = err or f"执行失败: {cmd}"
                if 'invalid group' in err_msg:
                    return {"success": False, "msg": f"修改失败：系统中不存在名为 '{group}' 的用户组"}
                if 'Operation not permitted' in err_msg:
                    return {"success": False, "msg": "修改失败：操作被拒绝，当前 SSH 账号权限不足"}
                return {"success": False, "msg": err_msg}
                
        return {"success": True, "msg": "权限和用户组修改成功"}

    def upload_file(self, remote_path: str, file_data: bytes, filename: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try:
            remote_file = self._normalize_path(f"{remote_path}/{filename}")
            with sftp.open(remote_file, 'wb') as f: f.write(file_data)
            return {"success": True, "msg": "上传成功"}
        except Exception as e: return {"success": False, "msg": str(e)}

    def download_file(self, path: str) -> Tuple[bytes, str, str]:
        sftp = self._ensure_sftp()
        if not sftp: return None, "", "SFTP 未连接"
        try:
            with sftp.open(path, 'rb') as f: data = f.read()
            return data, os.path.basename(path), ""
        except Exception as e:
            err_str = str(e)
            if "Failure" in err_str:
                return None, "", "无法下载该文件，可能是无读取权限"
            return None, "", err_str

    def search(self, path: str, keyword: str, recursive: bool = False) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        
        # 使用原生 SFTP 递归遍历 (兼容 OpenWrt/BusyBox)
        items = []
        max_depth = 30 if recursive else 1
        max_results = 300  # 限制最大搜索结果数量，防止遍历过多导致卡顿
        
        def _search_dir(current_path, current_depth):
            if len(items) >= max_results or current_depth > max_depth:
                return
            try:
                for attr in sftp.listdir_attr(current_path):
                    if len(items) >= max_results:
                        break
                    
                    full_path = f"{current_path.rstrip('/')}/{attr.filename}"
                    is_dir = stat.S_ISDIR(attr.st_mode)
                    
                    # 检查文件名是否包含关键词
                    if keyword.lower() in attr.filename.lower():
                        mode = attr.st_mode
                        mtime = int(attr.st_mtime)
                        size_int = attr.st_size
                        
                        items.append({
                            "name": attr.filename,
                            "path": full_path,
                            "is_dir": is_dir,
                            "size": size_int,
                            "mtime": mtime,
                            "permissions": self._format_permissions(mode),
                            "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                            "size_str": "-" if is_dir else self._format_size(size_int),
                            "extension": "" if is_dir else self._get_extension(attr.filename)
                        })
                    
                    # 如果是目录且需要递归，则继续向下搜索
                    if is_dir and recursive:
                        _search_dir(full_path, current_depth + 1)
            except Exception:
                pass  # 忽略无权限访问的目录

        _search_dir(path, 1)
        return {"success": True, "items": items}

    def get_disk_usage(self, path: str = "/") -> Dict[str, Any]:
        ok, out, err = self.ssh.execute(f'df -k "{path}" | tail -n 1')
        if ok and out:
            parts = out.strip().split()
            if len(parts) >= 6:
                total = int(parts[1]) * 1024
                used = int(parts[2]) * 1024
                available = int(parts[3]) * 1024
                percent = parts[4]
                mount = parts[5]
                
                return {
                    "success": True,
                    "info": {
                        "filesystem": parts[0],
                        "total": total,
                        "used": used,
                        "available": available,
                        "percent": percent,
                        "mount": mount
                    }
                }
        return {"success": False, "msg": err or "无法获取磁盘信息"}

    def get_dir_size(self, path: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: 
            return {"success": False, "msg": "SFTP 未连接"}
        try:
            path = self._normalize_path(path)
            # 【兼容优化】使用 du -sk (KB)
            ok, out, _ = self.ssh.execute(f'du -sk "{path}" 2>/dev/null')
            size = 0
            if ok and out:
                try:
                    size = int(out.split()[0]) * 1024
                except:
                    pass
            return {"success": True, "size": self._format_size(size)}
        except Exception as e:
            return {"success": False, "msg": str(e)}

    def analyze_disk(self, path: str) -> Dict[str, Any]:
        if path.startswith('~'):
            ok, out, err = self.ssh.execute(f'echo {path}')
            if ok: path = out.strip()

        path = self._normalize_path(path)
        sftp = self._ensure_sftp()
        
        if not sftp:
            return {"success": False, "msg": "SFTP 未连接"}
            
        try:
            # 1. 顶部总大小：根目录用 df 读取，子目录用 du 读取当前目录真实占用
            total_size = 0
            excludes = ""
            if path == "/":
                excludes = "--exclude=/proc --exclude=/sys --exclude=/dev --exclude=/run --exclude=/snap"
                # 【兼容优化】根目录使用 df -k 读取
                ok_df, out_df, _ = self.ssh.execute(f'df -k "{path}" | tail -n 1')
                if ok_df and out_df:
                    parts = out_df.strip().split()
                    if len(parts) >= 6:
                        try:
                            total_size = int(parts[2]) * 1024 # 已用空间 KB 转 Byte
                        except ValueError:
                            pass
            else:
                # 【兼容优化】子目录使用 du -sk 计算总占用
                ok_du_total, out_du_total, _ = self.ssh.execute(f'du -sk "{path}" 2>/dev/null')
                if ok_du_total and out_du_total:
                    try:
                        total_size = int(out_du_total.split()[0]) * 1024
                    except ValueError:
                        pass
            
            # 2. 子项大小：使用 du -sk 一次性获取第一层目录大小，速度快
            cmd = f'du -sk {excludes} "{path}"/* 2>/dev/null'
            ok_du, out_du, _ = self.ssh.execute(cmd)
            
            size_map = {}
            if ok_du and out_du:
                for line in out_du.strip().split('\n'):
                    # 兼容 BusyBox 的输出格式 (用空白字符分割)
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        try:
                            size_val = int(parts[0]) * 1024 # KB 转 Byte
                            name = os.path.basename(parts[1].strip())
                            size_map[name] = size_val
                        except ValueError:
                            pass
            
            items = []
            
            for entry in sftp.listdir_attr(path):
                item_path = f"{path}/{entry.filename}"
                
                # 【关键保留】过滤掉 kcore 等虚拟文件，防止误删和计算出错
                if entry.filename == "kcore" or "proc/kcore" in item_path:
                    continue
                    
                is_dir = stat.S_ISDIR(entry.st_mode)
                
                if is_dir:
                    # 从 du 命令的结果中直接获取大小
                    size = size_map.get(entry.filename, 0)
                else:
                    size = entry.st_size
                    
                items.append({
                    "name": entry.filename,
                    "path": item_path,
                    "is_dir": is_dir,
                    "size": size,
                    "size_str": self._format_size(size)
                })
                
            return {
                "success": True, 
                "current_path": path, 
                "total_size": total_size, 
                "total_size_str": self._format_size(total_size), 
                "items": items
            }
        except Exception as e:
            return {"success": False, "msg": str(e)}

    def compress(self, paths: list, output: str, fmt: str = "tar.gz") -> Dict[str, Any]:
        if not paths:
            return {"success": False, "msg": "没有选择要压缩的文件"}
        
        output = self._normalize_path(output)
        paths_str = " ".join([f'"{self._normalize_path(p)}"' for p in paths])
        
        if fmt == "tar.gz":
            tar_parts = []
            for p in paths:
                p_norm = self._normalize_path(p)
                d = os.path.dirname(p_norm)
                b = os.path.basename(p_norm)
                if d and d != '/':
                    tar_parts.append(f'-C "{d}" "{b}"')
                else:
                    tar_parts.append(f'"{b}"')
            cmd = f'tar -czf "{output}" {" ".join(tar_parts)}'
        elif fmt == "tar.bz2":
            cmd = f'tar -cjf "{output}" {paths_str}'
        elif fmt == "zip":
            cmd = f'zip -r "{output}" {paths_str}'
        elif fmt == "rar":
            ok_check, _, _ = self.ssh.execute("which rar")
            if not ok_check:
                return {"success": False, "msg": "服务器未安装unrar或7z，无法压缩"}
            cmd = f'rar a "{output}" {paths_str}'
        elif fmt == "7z":
            cmd = f'7z a "{output}" {paths_str}'
        else:
            return {"success": False, "msg": "不支持的压缩格式"}

        ok, out, err = self.ssh.execute(cmd)
        if ok:
            return {"success": True, "msg": "压缩成功", "path": output}
        return {"success": False, "msg": self._translate_error(err or "压缩失败")}

    def extract(self, file_path: str, target_dir: str = None) -> Dict[str, Any]:
        file_path = self._normalize_path(file_path)
        if not target_dir:
            base_name = os.path.basename(file_path).split('.')[0]
            target_dir = self._normalize_path(os.path.join(os.path.dirname(file_path), base_name))
        
        self.ssh.execute(f'mkdir -p "{target_dir}"')
        
        if file_path.endswith(".tar.gz") or file_path.endswith(".tgz"):
            cmd = f'tar -xzf "{file_path}" -C "{target_dir}"'
        elif file_path.endswith(".tar.bz2"):
            cmd = f'tar -xjf "{file_path}" -C "{target_dir}"'
        elif file_path.endswith(".tar"):
            cmd = f'tar -xf "{file_path}" -C "{target_dir}"'
        elif file_path.endswith(".zip"):
            cmd = f'unzip -o "{file_path}" -d "{target_dir}"'
        elif file_path.endswith(".rar"):
            ok_check, _, _ = self.ssh.execute("which unrar")
            if ok_check:
                cmd = f'unrar x "{file_path}" "{target_dir}"'
            else:
                ok_check_7z, _, _ = self.ssh.execute("which 7z")
                if ok_check_7z:
                    cmd = f'7z x "{file_path}" -o"{target_dir}"'
                else:
                    return {"success": False, "msg": "服务器未安装unrar或7z，无法解压"}
        elif file_path.endswith(".7z"):
            cmd = f'7z x "{file_path}" -o"{target_dir}"'
        elif file_path.endswith(".gz") and not file_path.endswith(".tar.gz"):
            filename = os.path.basename(file_path)[:-3]
            cmd = f'gunzip -c "{file_path}" > "{target_dir}/{filename}"'
        else:
            return {"success": False, "msg": "不支持的压缩格式"}

        ok, out, err = self.ssh.execute(cmd)
        if ok:
            return {"success": True, "msg": "解压成功"}
        return {"success": False, "msg": self._translate_error(err or "解压失败")}

    def _format_permissions(self, mode: int) -> str:
        perms = ""
        for who in ("USR", "GRP", "OTH"):
            for what in ("R", "W", "X"):
                bit = getattr(stat, f"S_I{what}{who}")
                perms += what.lower() if mode & bit else "-"
        if stat.S_ISDIR(mode): perms = "d" + perms
        elif stat.S_ISLNK(mode): perms = "l" + perms
        else: perms = "-" + perms
        return perms

    def _format_size(self, size: int) -> str:
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024: return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    def _get_extension(self, filename: str) -> str:
        if "." in filename: return filename.rsplit(".", 1)[-1].lower()
        return ""