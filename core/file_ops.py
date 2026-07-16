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
        cmd = f'find "{path}" -mindepth 1 -maxdepth 1 -printf "%y|%m|%s|%T@|%f\\n" 2>/dev/null'
        ok, out, err = self.ssh.execute(cmd)
        
        if not ok:
            return {"success": False, "msg": err or "无法读取目录"}
            
        files, dirs = [], []
        lines = out.strip().split('\n')
        for line in lines:
            if not line: continue
            parts = line.split('|', 4)
            if len(parts) < 5: continue
            
            f_type, mode_str, size_str, mtime_str, filename = parts
            if filename == '.' or filename == '': continue
            
            try:
                mode = int(mode_str, 8)
                size = int(size_str)
                mtime = int(float(mtime_str))
            except ValueError:
                continue
            
            item = {
                "filename": filename,
                "size": size,
                "mtime": mtime,
                "mode": mode,
                "is_dir": f_type == 'd',
                "is_link": f_type == 'l',
                "permissions": self._format_permissions(mode),
                "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                "size_str": self._format_size(size) if f_type != 'd' else "-",
                "extension": self._get_extension(filename),
            }
            if item["is_dir"]: dirs.append(item)
            else: files.append(item)
                
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
            is_dir = ok_test
            
            if item_name.startswith("trash_") and is_dir:
                trash_id = item_name
                ok_info, out_info, _ = self.ssh.execute(f'cat "{item_path}/.trash_info"')
                original_path = out_info.strip() if ok_info and out_info.strip() else "/"
                filename = os.path.basename(original_path)
                
                ok_ls, out_ls, _ = self.ssh.execute(f'ls -A "{item_path}" | grep -v ".trash_info"')
                real_filename = out_ls.strip().split('\n')[0].strip() if ok_ls and out_ls.strip() else ""
                full_path = f"{item_path}/{real_filename}" if real_filename else ""
                
                size, mtime = 0, 0
                if full_path:
                    ok_stat, out_stat, _ = self.ssh.execute(f'stat -c "%s %Y" "{full_path}"')
                    if ok_stat and out_stat.strip():
                        p = out_stat.strip().split()
                        if len(p) == 2:
                            try:
                                size = int(p[0])
                                mtime = int(p[1])
                            except: pass
            else:
                trash_id = f"root_{item_name}"
                original_path = f"/{item_name}"
                filename = item_name
                full_path = item_path
                
                size, mtime = 0, 0
                ok_stat, out_stat, _ = self.ssh.execute(f'stat -c "%s %Y" "{full_path}"')
                if ok_stat and out_stat.strip():
                    p = out_stat.strip().split()
                    if len(p) == 2:
                        try:
                            size = int(p[0])
                            mtime = int(p[1])
                        except: pass
                            
            files.append({
                "trash_id": trash_id,
                "filename": filename,
                "original_path": original_path,
                "size": size,
                "mtime": mtime,
                "size_str": self._format_size(size) if size > 0 else "-",
                "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                "is_dir": is_dir
            })
            
        return {"success": True, "files": files}

    def restore(self, trash_id: str) -> Dict[str, Any]:
        trash_base = self._get_trash_base()
        
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
            
            ok_ls, out_ls, _ = self.ssh.execute(f'ls -A "{trash_dir}" | grep -v ".trash_info"')
            if not ok_ls or not out_ls.strip(): return {"success": False, "msg": "回收站项目为空"}
            
            real_filename = out_ls.strip().split('\n')[0].strip()
            full_path_in_trash = f"{trash_dir}/{real_filename}"
            
            ok_mv, _, err_mv = self.ssh.execute(f'mv "{full_path_in_trash}" "{target_path}"')
            if not ok_mv: return {"success": False, "msg": err_mv}
            
            self.ssh.execute(f'rm -rf "{trash_dir}"')
            return {"success": True, "msg": f"已还原到: {target_path}", "target_path": target_path}

    def _get_dir_size(self, sftp, path: str) -> int:
        """递归计算目录及其子目录的总大小"""
        size = 0
        try:
            # 获取目录下所有文件和文件夹的属性
            for attr in sftp.listdir_attr(path):
                full_path = path.rstrip('/') + '/' + attr.filename
                if stat.S_ISDIR(attr.st_mode):
                    # 如果是子目录，递归计算
                    size += self._get_dir_size(sftp, full_path)
                else:
                    # 如果是文件，累加大小
                    size += attr.st_size
        except Exception:
            # 遇到无权限访问等异常时，忽略并返回当前已计算的大小
            pass
        return size

    def get_file_info(self, path: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try:
            path = self._normalize_path(path)
            stat_info = sftp.stat(path)
            
            # 1. 判断是否为目录（双重校验，防止 st_mode 解析失败）
            is_dir = stat.S_ISDIR(stat_info.st_mode)
            if not is_dir:
                try:
                    sftp.listdir(path)
                    is_dir = True
                except IOError:
                    is_dir = False

            # 2. 计算大小
            if is_dir:
                size = self._get_dir_size(sftp, path)
            else:
                size = stat_info.st_size

            # 获取用户组名称 (通过执行 stat 命令获取)
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
                    "group": group_name  # 新增：返回当前用户组
                }
            }
        except Exception as e: 
            print(f"[ERROR] get_file_info: {str(e)}")
            return {"success": False, "msg": self._translate_error(e)}

    def read_file(self, path: str, encoding: str = "utf-8") -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        try:
            # 以二进制模式读取，防止不同编码导致乱码
            with sftp.open(path, 'rb') as f: content = f.read()
            # 自动尝试常见编码，确保不乱码
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    text = content.decode('gbk')
                except UnicodeDecodeError:
                    text = content.decode('latin-1') # 最终兜底
            return {"success": True, "content": text}
        except Exception as e: return {"success": False, "msg": self._translate_error(e)}

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
        """同时修改权限和用户组，支持递归"""
        import re
        if not path:
            return {"success": False, "msg": "路径不能为空"}
            
        path = self._normalize_path(path)
        r_flag = "-R" if recursive else ""
        cmds = []
        
        # 处理权限修改
        if perm:
            if not re.match(r'^[0-7]{3,4}$', perm):
                return {"success": False, "msg": "权限格式错误，应为 3-4 位数字"}
            cmds.append(f'chmod {r_flag} {perm} "{path}"')
            
        # 处理用户组修改
        if group:
            if not re.match(r'^[a-zA-Z0-9_\.\-]+$', group):
                return {"success": False, "msg": "用户组名称包含非法字符"}
            cmds.append(f'chown {r_flag} :{group} "{path}"')
            
        if not cmds:
            return {"success": False, "msg": "没有需要修改的项"}
            
        # 依次执行命令
        for cmd in cmds:
            ok, out, err = self.ssh.execute(cmd)
            if not ok:
                err_msg = err or f"执行失败: {cmd}"
                # 将常见的 Linux 错误翻译为中文
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
        except Exception as e: return None, "", str(e)

    def search(self, path: str, keyword: str, recursive: bool = False) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: return {"success": False, "msg": "SFTP 未连接"}
        
        # 使用 GNU find 命令，-maxdepth 1 表示不递归，去掉则递归
        depth = "-maxdepth 1" if not recursive else "-maxdepth 10"
        cmd = f'find "{path}" {depth} -name "*{keyword}*" -printf "%y|%m|%s|%T@|%p\\n" 2>/dev/null | head -100'
        
        ok, out, err = self.ssh.execute(cmd)
        items = []
        if ok and out.strip():
            for line in out.strip().split('\n'):
                parts = line.split('|', 4)
                if len(parts) < 5: continue
                f_type, mode_str, size_str, mtime_str, full_path = parts
                if full_path == path: continue
                
                mode = int(mode_str, 8)
                mtime = int(float(mtime_str))
                is_dir = f_type == 'd'
                size_int = int(size_str)
                name = os.path.basename(full_path)
                
                items.append({
                    "name": name,
                    "path": full_path,
                    "is_dir": is_dir,
                    "size": size_int,
                    "mtime": mtime,
                    "permissions": self._format_permissions(mode),
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    "size_str": "-" if is_dir else self._format_size(size_int),
                    "extension": "" if is_dir else self._get_extension(name)
                })
                
        return {"success": True, "items": items}

    def get_disk_usage(self, path: str = "/") -> Dict[str, Any]:
        ok, out, err = self.ssh.execute('df -B1 / | tail -n 1')
        if ok:
            parts = out.strip().split()
            if len(parts) >= 6:
                return {
                    "success": True,
                    "info": {
                        "filesystem": parts[0],
                        "total": int(parts[1]),
                        "used": int(parts[2]),
                        "available": int(parts[3]),
                        "percent": parts[4],
                        "mount": parts[5]
                    }
                }
        return {"success": False, "msg": err or "无法获取磁盘信息"}

    def get_dir_size(self, path: str) -> Dict[str, Any]:
        sftp = self._ensure_sftp()
        if not sftp: 
            return {"success": False, "msg": "SFTP 未连接"}
        try:
            path = self._normalize_path(path)
            # 调用递归方法计算真实大小，空文件夹将返回 0
            size = self._get_dir_size(sftp, path)
            # 为了和原接口返回格式保持一致，这里格式化为字符串返回
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
            # 获取当前目录总大小（使用递归方法，排除目录本身 4kb 占用）
            total_size = self._get_dir_size(sftp, path)
            items = []
            
            # 遍历当前目录下的所有文件和文件夹
            for entry in sftp.listdir_attr(path):
                item_path = f"{path}/{entry.filename}"
                is_dir = stat.S_ISDIR(entry.st_mode)
                
                if is_dir:
                    # 获取子目录真实大小（空文件夹返回 0）
                    size = self._get_dir_size(sftp, item_path)
                else:
                    # 文件直接获取大小
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
            cmd = f'rar a "{output}" {paths_str}'
        elif fmt == "7z":
            cmd = f'7z a "{output}" {paths_str}'
        else:
            return {"success": False, "msg": "不支持的压缩格式"}

        ok, out, err = self.ssh.execute(cmd)
        if ok:
            return {"success": True, "msg": "压缩成功", "path": output}
        # 【关键修改】调用 _translate_error 翻译错误信息
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
            cmd = f'unrar x "{file_path}" "{target_dir}"'
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
        # 【关键修改】调用 _translate_error 翻译错误信息
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