"""
Cloudflare Tunnel 隧道管理器。
通过 subprocess 运行 cloudflared 二进制文件，解析输出获取公网 URL。
"""

import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Optional, Callable

from phonemic.tunnel.base import TunnelProvider
from phonemic.utils.paths import get_bin_dir, get_app_root

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r'https://[\w-]+\.trycloudflare\.com')


class CloudflareTunnel(TunnelProvider):
    """
    Cloudflare 快速隧道管理器。

    通过 'cloudflared tunnel --url' 命令创建临时隧道，
    解析输出获取 *.trycloudflare.com 公网 URL。
    """

    def __init__(self, binary_path: Optional[str] = None):
        """
        Args:
            binary_path: cloudflared 可执行文件路径。
                         若为 None，则自动检测（PATH 或 bin 目录）。
        """
        self._binary_path = binary_path
        self._process: Optional[subprocess.Popen] = None
        self._url: Optional[str] = None
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._on_url: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_stopped: Optional[Callable[[], None]] = None
        self._stopped = False

    @property
    def name(self) -> str:
        return "Cloudflare"

    def set_callbacks(
        self,
        on_url: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_stopped: Optional[Callable[[], None]] = None,
    ) -> None:
        """设置事件回调。"""
        self._on_url = on_url
        self._on_error = on_error
        self._on_stopped = on_stopped

    def _find_binary(self) -> Optional[str]:
        """查找 cloudflared 二进制文件路径。"""
        if self._binary_path:
            return self._binary_path if os.path.isfile(self._binary_path) else None

        exe_name = "cloudflared.exe" if os.name == "nt" else "cloudflared"

        # 1. 应用目录下的 bin（打包内置）
        app_bin = get_app_root() / "bin" / exe_name
        if app_bin.is_file():
            return str(app_bin)

        # 2. bin 目录（用户手动安装到 LOCALAPPDATA）
        local_bin = get_bin_dir() / exe_name
        if local_bin.is_file():
            return str(local_bin)

        # 3. PATH
        found = shutil.which("cloudflared")
        if found:
            return found

        return None

    def is_available(self) -> bool:
        """cloudflared 二进制是否已安装。"""
        return self._find_binary() is not None

    def is_running(self) -> bool:
        """隧道进程是否正在运行。"""
        with self._lock:
            if self._process is None:
                return False
            return self._process.poll() is None

    def get_url(self) -> Optional[str]:
        """获取当前隧道公网 URL。"""
        with self._lock:
            return self._url

    def start(self, port: int) -> None:
        """
        启动 cloudflared 隧道，转发到本地指定端口。

        Args:
            port: 本地服务端口
        """
        binary = self._find_binary()
        if binary is None:
            msg = "cloudflared not found. Install it or place in bin directory."
            logger.error(msg)
            if self._on_error:
                self._on_error(msg)
            return

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                logger.warning("Tunnel already running.")
                return
            self._stopped = False
            self._url = None

        cmd = [
            binary,
            "tunnel",
            "--url", f"http://127.0.0.1:{port}",
            "--no-autoupdate",
        ]
        logger.info(f"Starting cloudflared: {' '.join(cmd)}")

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        self._monitor_thread = threading.Thread(
            target=self._monitor, daemon=True
        )
        self._monitor_thread.start()

    def _monitor(self) -> None:
        """监控 cloudflared 进程输出，解析 URL。"""
        url_found = False
        try:
            assert self._process is not None
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue

                if not url_found:
                    match = _URL_PATTERN.search(line)
                    if match:
                        url = match.group()
                        with self._lock:
                            self._url = url
                        url_found = True
                        logger.info(f"Tunnel URL: {url}")
                        if self._on_url:
                            self._on_url(url)

                # 检测错误日志
                if " ERR " in line and "Unable to reach the origin" in line:
                    logger.warning(f"cloudflared origin error: {line}")
                    if self._on_error:
                        self._on_error(line)

        except Exception as e:
            logger.exception(f"Monitor thread error: {e}")
        finally:
            # 进程结束
            ret = self._process.poll() if self._process else None
            with self._lock:
                self._url = None
            logger.info(f"cloudflared exited (code={ret})")
            if not self._stopped and self._on_stopped:
                self._on_stopped()

    def stop(self) -> None:
        """停止 cloudflared 进程。"""
        with self._lock:
            self._stopped = True

        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2.0)
            except Exception as e:
                logger.warning(f"Error stopping cloudflared: {e}")
            finally:
                self._process = None

        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3.0)

        with self._lock:
            self._url = None
