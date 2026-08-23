"""
隧道管理器。
编排 CloudflareTunnel、服务端重启、认证模式切换，处理自动回退。
"""

import logging
import threading
from typing import Callable, Optional

from phonemic.server.api import restart_server, stop_server, set_tunnel_auth
from phonemic.tunnel.auth import PairingCodeManager, TokenManager
from phonemic.tunnel.cloudflare import CloudflareTunnel
from phonemic.tunnel.mode import TunnelMode, get_bind_address

logger = logging.getLogger(__name__)


class TunnelManager:
    """
    隧道模式编排器。
    管理模式切换、隧道启停、服务端重启和自动回退。
    """

    def __init__(self, port: int, bridge):
        self._port = port
        self._bridge = bridge
        self._tunnel = CloudflareTunnel()
        self._pairing = PairingCodeManager()
        self._tokens = TokenManager()
        self._mode = TunnelMode.LAN
        self._on_url: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_mode_changed: Optional[Callable[[TunnelMode], None]] = None
        self._url_obtained = False

    @property
    def mode(self) -> TunnelMode:
        return self._mode

    @property
    def pairing(self) -> PairingCodeManager:
        return self._pairing

    @property
    def tokens(self) -> TokenManager:
        return self._tokens

    def set_callbacks(
        self,
        on_url: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_mode_changed: Optional[Callable[[TunnelMode], None]] = None,
    ) -> None:
        self._on_url = on_url
        self._on_error = on_error
        self._on_mode_changed = on_mode_changed

    def switch_mode(self, mode: TunnelMode) -> bool:
        """
        切换到指定模式。
        在后台线程执行，避免阻塞 UI 线程。
        返回 True 表示切换已启动，False 表示模式相同无需切换。
        """
        if mode == self._mode:
            return True

        def _run():
            try:
                if mode == TunnelMode.CLOUDFLARE:
                    self._switch_to_cloudflare()
                else:
                    self._switch_to_lan()
            except Exception as e:
                logger.exception(f"Mode switch error: {e}")
                if self._on_error:
                    self._on_error(str(e))

        threading.Thread(target=_run, daemon=True).start()
        return True

    def _switch_to_cloudflare(self) -> bool:
        """切换到 Cloudflare 模式。"""
        if not self._tunnel.is_available():
            from phonemic.utils.i18n import I18n
            msg = I18n.instance().tr("tunnel.binary_not_found")
            logger.error(msg)
            if self._on_error:
                self._on_error(msg)
            self._fallback_to_lan()
            return False

        host = get_bind_address(TunnelMode.CLOUDFLARE)
        restart_server(host, self._port, self._bridge)
        set_tunnel_auth(True, self._pairing, self._tokens)
        self._url_obtained = False

        self._tunnel.set_callbacks(
            on_url=self._on_tunnel_url,
            on_error=self._on_tunnel_error,
            on_stopped=self._on_tunnel_stopped,
        )
        self._tunnel.start(self._port)
        self._mode = TunnelMode.CLOUDFLARE
        if self._on_mode_changed:
            self._on_mode_changed(TunnelMode.CLOUDFLARE)
        return True

    def _switch_to_lan(self) -> None:
        """切换到局域网模式。"""
        self._tunnel.stop()
        host = get_bind_address(TunnelMode.LAN)
        restart_server(host, self._port, self._bridge)
        set_tunnel_auth(False)
        self._mode = TunnelMode.LAN
        if self._on_mode_changed:
            self._on_mode_changed(TunnelMode.LAN)

    def _fallback_to_lan(self) -> None:
        """自动回退到局域网模式。"""
        logger.warning("Falling back to LAN mode")
        self._switch_to_lan()

    def _on_tunnel_url(self, url: str) -> None:
        self._url_obtained = True
        if self._on_url:
            self._on_url(url)

    def _on_tunnel_error(self, error: str) -> None:
        logger.error(f"Tunnel error: {error}")
        if self._on_error:
            self._on_error(error)

    def _on_tunnel_stopped(self) -> None:
        from phonemic.utils.i18n import I18n
        if not self._url_obtained:
            logger.warning("cloudflared stopped before URL was obtained, falling back")
            self._fallback_to_lan()
            if self._on_error:
                self._on_error(I18n.instance().tr("tunnel.start_failed"))
        else:
            logger.warning("cloudflared stopped unexpectedly")
            if self._on_error:
                self._on_error(I18n.instance().tr("tunnel.crashed"))

    def stop(self) -> None:
        """停止隧道和清理。"""
        self._tunnel.stop()
