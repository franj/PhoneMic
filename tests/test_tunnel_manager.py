"""
TunnelManager 单元测试。
"""

from unittest.mock import patch, MagicMock, call

import pytest

from phonemic.tunnel.manager import TunnelManager
from phonemic.tunnel.mode import TunnelMode
from phonemic.tunnel.cloudflare import CloudflareTunnel


@pytest.fixture
def manager():
    return TunnelManager(port=12000, bridge=MagicMock())


@pytest.fixture
def sync_switch():
    """让 switch_mode 中的后台线程同步执行，便于测试。"""
    class SyncThread:
        def __init__(self, target=None, daemon=False, **kwargs):
            self._target = target
        def start(self):
            if self._target:
                self._target()
    with patch("phonemic.tunnel.manager.threading.Thread", SyncThread):
        yield


class TestSwitchMode:
    @patch("phonemic.tunnel.manager.restart_server")
    @patch.object(CloudflareTunnel, "_find_binary", return_value="/fake/cloudflared")
    @patch("phonemic.tunnel.manager.CloudflareTunnel")
    def test_switch_to_cloudflare_starts_tunnel(self, mock_cf_cls, mock_binary, mock_restart, manager, sync_switch):
        mock_tunnel = MagicMock()
        mock_cf_cls.return_value = mock_tunnel
        manager._tunnel = mock_tunnel
        mock_tunnel.is_available.return_value = True

        result = manager.switch_mode(TunnelMode.CLOUDFLARE)

        assert result is True
        assert manager.mode == TunnelMode.CLOUDFLARE
        mock_restart.assert_called_once_with("127.0.0.1", 12000, manager._bridge)
        mock_tunnel.start.assert_called_once_with(12000)

    @patch("phonemic.tunnel.manager.restart_server")
    @patch.object(CloudflareTunnel, "is_available", return_value=False)
    def test_switch_to_cloudflare_no_binary_falls_back(self, mock_avail, mock_restart, manager, sync_switch):
        on_error = MagicMock()
        manager.set_callbacks(on_error=on_error)

        result = manager.switch_mode(TunnelMode.CLOUDFLARE)

        assert result is True
        assert manager.mode == TunnelMode.LAN
        mock_restart.assert_any_call("0.0.0.0", 12000, manager._bridge)
        on_error.assert_called()

    @patch("phonemic.tunnel.manager.restart_server")
    def test_switch_to_lan_stops_tunnel(self, mock_restart, manager, sync_switch):
        manager._mode = TunnelMode.CLOUDFLARE
        manager._tunnel = MagicMock()

        manager.switch_mode(TunnelMode.LAN)

        assert manager.mode == TunnelMode.LAN
        manager._tunnel.stop.assert_called_once()
        mock_restart.assert_called_once_with("0.0.0.0", 12000, manager._bridge)

    @patch("phonemic.tunnel.manager.restart_server")
    def test_switch_same_mode_noop(self, mock_restart, manager, sync_switch):
        result = manager.switch_mode(TunnelMode.LAN)
        assert result is True
        mock_restart.assert_not_called()


class TestCallbacks:
    @patch("phonemic.tunnel.manager.restart_server")
    def test_on_url_callback(self, mock_restart, manager):
        on_url = MagicMock()
        manager.set_callbacks(on_url=on_url)
        manager._mode = TunnelMode.CLOUDFLARE

        manager._on_tunnel_url("https://test.trycloudflare.com")

        on_url.assert_called_once_with("https://test.trycloudflare.com")
        assert manager._url_obtained is True

    @patch("phonemic.tunnel.manager.restart_server")
    def test_on_error_callback(self, mock_restart, manager):
        on_error = MagicMock()
        manager.set_callbacks(on_error=on_error)

        manager._on_tunnel_error("some error")

        on_error.assert_called_once_with("some error")

    @patch("phonemic.tunnel.manager.restart_server")
    @patch.object(CloudflareTunnel, "is_available", return_value=True)
    def test_on_mode_changed_callback(self, mock_avail, mock_restart, manager, sync_switch):
        on_mode = MagicMock()
        manager.set_callbacks(on_mode_changed=on_mode)
        manager._tunnel = MagicMock()
        manager._tunnel.is_available.return_value = True

        manager.switch_mode(TunnelMode.CLOUDFLARE)

        on_mode.assert_called_once_with(TunnelMode.CLOUDFLARE)


class TestAutoFallback:
    @patch("phonemic.tunnel.manager.restart_server")
    def test_stopped_before_url_triggers_fallback(self, mock_restart, manager):
        on_error = MagicMock()
        manager.set_callbacks(on_error=on_error)
        manager._mode = TunnelMode.CLOUDFLARE
        manager._url_obtained = False
        manager._tunnel = MagicMock()

        manager._on_tunnel_stopped()

        assert manager.mode == TunnelMode.LAN
        mock_restart.assert_called_with("0.0.0.0", 12000, manager._bridge)
        on_error.assert_called_once()

    @patch("phonemic.tunnel.manager.restart_server")
    def test_stopped_after_url_no_fallback(self, mock_restart, manager):
        on_error = MagicMock()
        manager.set_callbacks(on_error=on_error)
        manager._mode = TunnelMode.CLOUDFLARE
        manager._url_obtained = True
        manager._tunnel = MagicMock()

        manager._on_tunnel_stopped()

        assert manager.mode == TunnelMode.CLOUDFLARE
        on_error.assert_called_once()

