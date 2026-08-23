"""
隧道模式管理单元测试。
"""

from unittest.mock import patch, MagicMock

import pytest

from phonemic.tunnel.mode import TunnelMode, get_mode, set_mode, get_bind_address


class TestTunnelMode:
    def test_default_is_lan(self):
        with patch("phonemic.tunnel.mode.SettingsManager") as mock_sm_cls:
            mock_sm = MagicMock()
            mock_sm.get.return_value = "lan"
            mock_sm_cls.instance.return_value = mock_sm
            assert get_mode() == TunnelMode.LAN

    def test_cloudflare_mode(self):
        with patch("phonemic.tunnel.mode.SettingsManager") as mock_sm_cls:
            mock_sm = MagicMock()
            mock_sm.get.return_value = "cloudflare"
            mock_sm_cls.instance.return_value = mock_sm
            assert get_mode() == TunnelMode.CLOUDFLARE

    def test_invalid_mode_falls_back_to_lan(self):
        with patch("phonemic.tunnel.mode.SettingsManager") as mock_sm_cls:
            mock_sm = MagicMock()
            mock_sm.get.return_value = "invalid"
            mock_sm_cls.instance.return_value = mock_sm
            assert get_mode() == TunnelMode.LAN

    def test_no_mode_in_config_defaults_to_lan(self):
        with patch("phonemic.tunnel.mode.SettingsManager") as mock_sm_cls:
            mock_sm = MagicMock()
            mock_sm.get.return_value = None
            mock_sm_cls.instance.return_value = mock_sm
            assert get_mode() == TunnelMode.LAN


class TestSetMode:
    def test_set_lan(self):
        with patch("phonemic.tunnel.mode.SettingsManager") as mock_sm_cls:
            mock_sm = MagicMock()
            mock_sm_cls.instance.return_value = mock_sm
            set_mode(TunnelMode.LAN)
            mock_sm.set.assert_called_once_with("tunnel_mode", "lan")

    def test_set_cloudflare(self):
        with patch("phonemic.tunnel.mode.SettingsManager") as mock_sm_cls:
            mock_sm = MagicMock()
            mock_sm_cls.instance.return_value = mock_sm
            set_mode(TunnelMode.CLOUDFLARE)
            mock_sm.set.assert_called_once_with("tunnel_mode", "cloudflare")


class TestBindAddress:
    def test_lan_binds_all_interfaces(self):
        assert get_bind_address(TunnelMode.LAN) == "0.0.0.0"

    def test_cloudflare_binds_localhost(self):
        assert get_bind_address(TunnelMode.CLOUDFLARE) == "127.0.0.1"
