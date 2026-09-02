"""
SettingsManager 单元测试（适配精准信号 setting_changed）
运行方式: pytest tests/test_settings_manager.py -v
"""

import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QStandardPaths

from phonemic.utils import paths
from phonemic.utils.settings_manager import SettingsManager


@pytest.fixture(scope="session")
def qapp():
    """提供 QCoreApplication 实例（信号机制需要）"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


@pytest.fixture
def mock_config_path(tmp_path):
    """临时替换 QStandardPaths 返回的配置目录"""
    mock_app_config = tmp_path / "PhoneMic/config"
    mock_app_config.mkdir(parents=True, exist_ok=True)

    def fake_writable_location():
        return Path(tmp_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paths, "_get_local_app_data", fake_writable_location)
        yield mock_app_config


@pytest.fixture
def reset_singleton():
    """重置 SettingsManager 单例状态"""
    SettingsManager._instance = None
    yield
    SettingsManager._instance = None


def test_singleton(reset_singleton):
    """测试单例模式"""
    sm1 = SettingsManager.instance()
    sm2 = SettingsManager.instance()
    assert sm1 is sm2


def test_load_default_config_when_file_missing(mock_config_path, reset_singleton):
    """配置文件不存在时，自动创建默认配置"""
    sm = SettingsManager.instance()
    config_file = mock_config_path / "settings.json"
    assert config_file.exists()

    assert sm.get("hud_timeout_sec") == 5
    assert sm.get("hud_font_size") == 14
    assert sm.get("hud_escape_enabled") is True
    assert sm.get("mobile_max_records") == 10
    assert sm.get("language") == "zh_CN"

    with open(config_file, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["hud_timeout_sec"] == 5


def test_set_and_save(mock_config_path, reset_singleton):
    """修改配置后自动保存并发射精准信号"""
    sm = SettingsManager.instance()
    received_key = None
    received_value = None

    def on_changed(key, value):
        nonlocal received_key, received_value
        received_key = key
        received_value = value

    sm.setting_changed.connect(on_changed)

    sm.set("hud_timeout_sec", 10)
    sm.set("hud_font_size", "system")

    assert sm.get("hud_timeout_sec") == 10
    assert sm.get("hud_font_size") == "system"

    config_file = mock_config_path / "settings.json"
    with open(config_file, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["hud_timeout_sec"] == 10
    assert saved["hud_font_size"] == "system"

    assert received_key == "hud_font_size"
    assert received_value == "system"


def test_set_same_value_no_save(mock_config_path, reset_singleton):
    """设置相同值时不应保存文件也不发射信号"""
    sm = SettingsManager.instance()
    config_file = mock_config_path / "settings.json"
    original_mtime = config_file.stat().st_mtime

    call_count = 0

    def on_changed(key, value):
        nonlocal call_count
        call_count += 1

    sm.setting_changed.connect(on_changed)

    sm.set("hud_timeout_sec", 5)
    time.sleep(0.01)
    new_mtime = config_file.stat().st_mtime
    assert new_mtime == original_mtime
    assert call_count == 0


def test_load_merges_new_default_keys(mock_config_path, reset_singleton):
    """旧配置文件缺少新增字段时，加载时自动合并默认值"""
    old_config = {
        "hud_timeout_sec": 3,
        "hud_font_size": 18,
        "mobile_max_records": 20,
        "language": "en-US"
    }
    config_file = mock_config_path / "settings.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(old_config, f)

    sm = SettingsManager.instance()
    assert sm.get("hud_escape_enabled") is True
    assert sm.get("network_selection_mode") == "ask"
    assert sm.get("last_network_mac") is None
    assert sm.get("close_action") is None
    assert sm.get("hud_timeout_sec") == 3


def test_load_corrupted_json_resets_to_default(mock_config_path, reset_singleton):
    """配置文件损坏（非法 JSON）时，重置为默认配置并覆盖原文件"""
    config_file = mock_config_path / "settings.json"
    with open(config_file, "w", encoding="utf-8") as f:
        f.write("this is not json{")

    sm = SettingsManager.instance()
    assert sm.get("hud_timeout_sec") == 5

    with open(config_file, "r", encoding="utf-8") as f:
        content = json.load(f)
    assert content["hud_timeout_sec"] == 5


def test_hud_font_size_type_validation(mock_config_path, reset_singleton):
    """类型校验：hud_font_size 只接受 int 或 "system" """
    invalid_config = {
        "hud_timeout_sec": 5,
        "hud_font_size": "14px",
        "hud_escape_enabled": True,
        "mobile_max_records": 10,
        "language": "zh-CN"
    }
    config_file = mock_config_path / "settings.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(invalid_config, f)

    sm = SettingsManager.instance()
    assert sm.get("hud_font_size") == 14

    sm.set("hud_font_size", "system")
    assert sm.get("hud_font_size") == "system"


def test_get_all_returns_copy(mock_config_path, reset_singleton):
    """get_all() 返回字典副本，修改返回的字典不影响内部状态"""
    sm = SettingsManager.instance()
    all_config = sm.get_all()
    all_config["hud_timeout_sec"] = 999
    assert sm.get("hud_timeout_sec") == 5


def test_connect_changed_convenience_method(mock_config_path, reset_singleton):
    """connect_changed 便捷方法：为特定 key 绑定回调，自动过滤"""
    sm = SettingsManager.instance()
    timeout_received = None
    font_received = None

    def on_timeout(value):
        nonlocal timeout_received
        timeout_received = value

    def on_font(value):
        nonlocal font_received
        font_received = value

    sm.connect_changed("hud_timeout_sec", on_timeout)
    sm.connect_changed("hud_font_size", on_font)

    sm.set("hud_timeout_sec", 8)
    assert timeout_received == 8
    assert font_received is None

    sm.set("hud_font_size", 16)
    assert font_received == 16


def test_setting_changed_signal_emits_key_and_value(mock_config_path, reset_singleton, qapp):
    """setting_changed 信号携带 (key, new_value)"""
    sm = SettingsManager.instance()
    received = []

    def handler(key, value):
        received.append((key, value))

    sm.setting_changed.connect(handler)
    sm.set("mobile_max_records", 15)
    sm.set("language", "en-US")

    assert len(received) == 2
    assert received[0] == ("mobile_max_records", 15)
    assert received[1] == ("language", "en-US")


def test_network_selection_mode_validation(mock_config_path, reset_singleton):
    """校验 network_selection_mode 只接受有效值"""
    invalid_config = {
        "hud_timeout_sec": 5,
        "hud_font_size": 14,
        "mobile_max_records": 10,
        "language": "zh_CN",
        "network_selection_mode": "invalid_mode"
    }
    config_file = mock_config_path / "settings.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(invalid_config, f)

    sm = SettingsManager.instance()
    assert sm.get("network_selection_mode") == "ask"


def test_e2ee_algorithm_legacy_values_migrated(mock_config_path, reset_singleton):
    """历史算法值（xsalsa20/xchacha20）加载时迁移为 auto（加密，算法协商）"""
    config_file = mock_config_path / "settings.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"e2ee_algorithm": "xchacha20"}, f)

    sm = SettingsManager.instance()
    assert sm.get("e2ee_algorithm") == "auto"

    # xsalsa20 同样迁移
    SettingsManager._instance = None
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"e2ee_algorithm": "xsalsa20"}, f)
    sm = SettingsManager.instance()
    assert sm.get("e2ee_algorithm") == "auto"


def test_e2ee_algorithm_valid_values_kept(mock_config_path, reset_singleton):
    """合法值 none/auto 原样保留"""
    config_file = mock_config_path / "settings.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"e2ee_algorithm": "auto"}, f)

    sm = SettingsManager.instance()
    assert sm.get("e2ee_algorithm") == "auto"


def test_e2ee_algorithm_invalid_value_reset_to_default(mock_config_path, reset_singleton):
    """非法值重置为默认 none"""
    config_file = mock_config_path / "settings.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"e2ee_algorithm": "bogus_algo"}, f)

    sm = SettingsManager.instance()
    assert sm.get("e2ee_algorithm") == "none"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])