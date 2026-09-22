"""
隧道模式管理单元测试。
"""

from unittest.mock import patch, MagicMock

import pytest

from phonemic.tunnel.mode import TunnelMode, get_mode, set_mode, effective_auth_method


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


class TestEffectiveAuthMethod:
    """effective_auth_method 返回 "tofu"/"url_fragment"，CF 模式强制 url_fragment。"""

    def test_lan_tofu_stays_tofu(self):
        assert effective_auth_method("tofu", TunnelMode.LAN) == "tofu"

    def test_lan_url_fragment_stays_url_fragment(self):
        assert effective_auth_method("url_fragment", TunnelMode.LAN) == "url_fragment"

    def test_cf_tofu_forced_to_url_fragment(self):
        """CF 模式下 tofu 被强制为 url_fragment。"""
        assert effective_auth_method("tofu", TunnelMode.CLOUDFLARE) == "url_fragment"

    def test_cf_url_fragment_stays_url_fragment(self):
        assert effective_auth_method("url_fragment", TunnelMode.CLOUDFLARE) == "url_fragment"
