"""
隧道模式管理单元测试。
"""

from unittest.mock import patch, MagicMock

import pytest

from phonemic.tunnel.mode import TunnelMode, get_mode, set_mode, effective_algorithm


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


class TestEffectiveAlgorithm:
    """effective_algorithm 返回 "none"/"auto"，CF 模式强制加密。"""

    def test_lan_none_stays_none(self):
        assert effective_algorithm("none", TunnelMode.LAN) == "none"

    def test_lan_auto_stays_auto(self):
        assert effective_algorithm("auto", TunnelMode.LAN) == "auto"

    def test_cf_none_forced_to_auto(self):
        """CF 模式下配置 none 强制加密。"""
        assert effective_algorithm("none", TunnelMode.CLOUDFLARE) == "auto"

    def test_cf_auto_stays_auto(self):
        assert effective_algorithm("auto", TunnelMode.CLOUDFLARE) == "auto"

    def test_legacy_values_normalized_to_auto(self):
        """历史配置值 xsalsa20/xchacha20 统一归一化为 auto。"""
        assert effective_algorithm("xsalsa20", TunnelMode.LAN) == "auto"
        assert effective_algorithm("xchacha20", TunnelMode.LAN) == "auto"
        assert effective_algorithm("xsalsa20", TunnelMode.CLOUDFLARE) == "auto"
