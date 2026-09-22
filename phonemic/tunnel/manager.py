"""
隧道管理器。
编排 CloudflareTunnel、服务端重启，处理自动回退。
"""

import logging
import threading
from typing import Callable, Optional

from phonemic.server.api import get_secret_path, restart_server
from phonemic.tunnel.cloudflare import CloudflareTunnel
from phonemic.tunnel.keepalive import TunnelKeepalive
from phonemic.tunnel.mode import TunnelMode

logger = logging.getLogger(__name__)


class TunnelManager:
    """
    隧道模式编排器。
    管理模式切换、隧道启停、服务端重启和自动回退。
    """

    def __init__(self, port: int, bridge, lan_ip: str = "127.0.0.1"):
        self._port = port
        self._bridge = bridge
        self._lan_ip = lan_ip
        self._tunnel = CloudflareTunnel()
        self._keepalive = TunnelKeepalive(on_state_change=self._on_keepalive_state)
        self._mode = TunnelMode.LAN
        self._on_url: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_mode_changed: Optional[Callable[[TunnelMode], None]] = None
        self._on_reachability: Optional[Callable[[bool], None]] = None
        self._url_obtained = False

    @property
    def mode(self) -> TunnelMode:
        return self._mode

    def set_callbacks(
        self,
        on_url: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_mode_changed: Optional[Callable[[TunnelMode], None]] = None,
        on_reachability: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self._on_url = on_url
        self._on_error = on_error
        self._on_mode_changed = on_mode_changed
        self._on_reachability = on_reachability

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

    def restart_service(self) -> None:
        """重启当前模式的服务（网络菜单「重启服务」）。

        与 switch_mode 的区别：模式不变也能执行。CF 模式下这替用户省掉了
        「先切到局域网、再切回 Cloudflare」那套操作——`switch_mode` 对「已是当前
        模式」直接返回，所以在 CF 状态下反复点 Cloudflare 是空操作，换不来新域名。

        **两种模式都作废旧身份**：重启会换新的密钥对与 secret 路径，手机端需要重新
        配对——扫码认证重新扫二维码，手动审批则在电脑上再点一次「允许」（手机端
        检测到旧身份失效后会自动发起）。LAN 模式同样如此——「重启服务」是用户明确
        发起的操作，语义就是「一切重来」，与 CF 保持一致。
        """

        def _run():
            try:
                self._restart_current()
            except Exception as e:
                logger.exception(f"Service restart error: {e}")
                if self._on_error:
                    self._on_error(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _restart_current(self) -> None:
        """按当前模式重启一次服务（在后台线程中执行）。"""
        # 先把旧隧道和保活收干净：CloudflareTunnel.start() 见到进程还活着会直接
        # 返回（"Tunnel already running"），不先 stop 就拿不到新域名。
        self._keepalive.stop()
        self._tunnel.stop()
        if self._mode == TunnelMode.CLOUDFLARE:
            self._switch_to_cloudflare()
        else:
            restart_server(self._lan_ip, self._port, self._bridge)
            # 借 mode_changed 通道发布「身份已更新」：上层据此重建 SecureChannel
            # （新 secret 路径 + 新密钥对 → 新二维码）并恢复菜单可用状态。
            # CF 分支不重复发——_switch_to_cloudflare 内部已经发过一次，
            # 两条路走同一个出口，避免各写一份重建逻辑。
            if self._on_mode_changed:
                self._on_mode_changed(self._mode)

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

        restart_server("127.0.0.1", self._port, self._bridge)
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
        self._keepalive.stop()
        self._tunnel.stop()
        restart_server(self._lan_ip, self._port, self._bridge)
        self._mode = TunnelMode.LAN
        if self._on_mode_changed:
            self._on_mode_changed(TunnelMode.LAN)

    def _fallback_to_lan(self) -> None:
        """自动回退到局域网模式。"""
        logger.warning("Falling back to LAN mode")
        self._switch_to_lan()

    def _on_tunnel_url(self, url: str) -> None:
        self._url_obtained = True
        # 公网地址一到手就启动保活：快隧道在无人访问时 20 分钟以上会被
        # Cloudflare 回收（域名随之失效），必须有真实 HTTP 流量持续打进来
        self._keepalive.start(url, get_secret_path())
        if self._on_url:
            self._on_url(url)

    def _on_tunnel_error(self, error: str) -> None:
        logger.error(f"Tunnel error: {error}")
        if self._on_error:
            self._on_error(error)

    def _on_keepalive_state(self, reachable: bool) -> None:
        """保活探测到公网入口可达性翻转（从保活线程回调，仅翻转时触发一次）。"""
        logger.info(f"Tunnel reachability changed: reachable={reachable}")
        if self._on_reachability:
            self._on_reachability(reachable)

    def _on_tunnel_stopped(self) -> None:
        # 隧道进程已结束，保活失去意义（继续探测只会不断失败并刷无效告警）
        self._keepalive.stop()
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
        self._keepalive.stop()
        self._tunnel.stop()
