# tests/test_commands_manager.py
"""
CommandsManager 单元测试
使用 pytest + monkeypatch + tmp_path 实现完全隔离的临时配置目录
"""

import json
import pytest
from pathlib import Path
from PySide6.QtCore import QStandardPaths, QObject, Signal
from PySide6.QtWidgets import QApplication

from phonemic.utils import paths
from phonemic.utils.commands_manager import CommandsManager, VoiceCommand

# ----------------------------------------------------------------------
# 辅助函数：直接写入 commands.json 到临时目录（用于准备初始数据）
# ----------------------------------------------------------------------
def _write_commands_file(tmp_path: Path, commands_list: list) -> Path:
    """将命令列表写入临时目录下的 PhoneMic/config/commands.json"""
    config_dir = tmp_path / "PhoneMic/config"
    config_dir.mkdir(parents=True, exist_ok=True)
    file_path = config_dir / "commands.json"
    file_path.write_text(json.dumps(commands_list, indent=2, ensure_ascii=False), encoding="utf-8")
    return file_path

def _write_empty(tmp_path: Path) -> Path:
    """写入空列表"""
    return _write_commands_file(tmp_path, [])

def _write_two_commands(tmp_path: Path) -> Path:
    """写入两条有效的命令"""
    data = [
        {
            "id": "abc123",
            "name": "打开记事本",
            "matchType": "exact",
            "matchPattern": "记事本",
            "actionType": "exec",
            "actionParams": "notepad.exe",
            "enabled": True,
        },
        {
            "id": "def456",
            "name": "粘贴命令",
            "matchType": "prefix",
            "matchPattern": "粘贴 ",
            "actionType": "key",
            "actionParams": "ctrl+v",
            "enabled": False,
        },
    ]
    return _write_commands_file(tmp_path, data)

def _write_malformed_json(tmp_path: Path) -> Path:
    """写入损坏的 JSON（用于测试异常恢复）"""
    config_dir = tmp_path / "PhoneMic/config"
    config_dir.mkdir(parents=True, exist_ok=True)
    file_path = config_dir / "commands.json"
    file_path.write_text("这不是合法的json", encoding="utf-8")
    return file_path

def _write_with_unknown_fields(tmp_path: Path) -> Path:
    """写入包含额外字段的命令（测试兼容性）"""
    data = [
        {
            "id": "extra1",
            "name": "额外字段命令",
            "matchType": "exact",
            "matchPattern": "测试",
            "actionType": "key",
            "actionParams": "enter",
            "enabled": True,
            "unknown_field": "应该被忽略",
        }
    ]
    return _write_commands_file(tmp_path, data)

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def ensure_qapp():
    """确保整个测试会话中有一个 QApplication 实例（用于信号）"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app

@pytest.fixture
def isolated_mgr(tmp_path, monkeypatch):
    """
    返回一个全新的 CommandsManager 实例，配置目录指向临时目录 tmp_path。
    不预置任何数据（空配置）。
    返回 (mgr, tmp_path) 以便测试中访问临时路径。
    """
    # 篡改 QStandardPaths.writableLocation，让 AppConfigLocation 返回临时目录
    def fake_writable_location():
        return Path(tmp_path)
    monkeypatch.setattr(paths, "_get_local_app_data", fake_writable_location)

    _write_empty(tmp_path)
    # 重置单例，确保每次 fixture 返回全新实例
    CommandsManager._instance = None
    mgr = CommandsManager()
    return mgr, tmp_path

@pytest.fixture
def mgr_with_initial_data(isolated_mgr):
    """
    返回一个已经包含两条预置命令的 CommandsManager。
    利用 isolated_mgr 框架，先写文件再重新加载。
    """
    mgr, tmp_path = isolated_mgr
    # 写入两条命令
    _write_two_commands(tmp_path)
    # 重新创建 mgr 以加载文件
    CommandsManager._instance = None
    mgr2 = CommandsManager()
    return mgr2, tmp_path

# ----------------------------------------------------------------------
# 测试用例
# ----------------------------------------------------------------------
class TestCommandsManagerCRUD:
    """基础增删改查测试"""

    def test_add_command(self, isolated_mgr):
        mgr, tmp_path = isolated_mgr
        cmd = VoiceCommand.new("测试命令", "exact", "hello", "key", "enter")
        mgr.add_command(cmd)

        commands = mgr.get_commands()
        assert len(commands) == 1
        assert commands[0].name == "测试命令"
        assert commands[0].matchPattern == "hello"

        # 验证文件确实写入
        file_path = tmp_path / "PhoneMic/config/commands.json"
        assert file_path.exists()
        data = json.loads(file_path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["name"] == "测试命令"

    def test_get_commands_returns_copy(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd = VoiceCommand.new("copy", "exact", "test", "key", "ctrl+c")
        mgr.add_command(cmd)
        commands = mgr.get_commands()
        # 修改副本不应影响原列表
        commands.pop()
        assert len(mgr.get_commands()) == 1

    def test_update_command(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd = VoiceCommand.new("old", "exact", "old", "key", "old")
        mgr.add_command(cmd)
        mgr.update_command(cmd.id, name="new", matchPattern="new_pattern")
        updated = mgr.get_commands()[0]
        assert updated.name == "new"
        assert updated.matchPattern == "new_pattern"
        # 未更新的字段保持不变
        assert updated.actionParams == "old"

        # 更新不存在的命令应抛出异常
        with pytest.raises(ValueError, match="not found"):
            mgr.update_command("nonexistent", name="x")

    def test_delete_command(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd1 = VoiceCommand.new("first", "exact", "a", "key", "")
        cmd2 = VoiceCommand.new("second", "exact", "b", "key", "")
        mgr.add_command(cmd1)
        mgr.add_command(cmd2)
        mgr.delete_command(cmd1.id)
        assert len(mgr.get_commands()) == 1
        assert mgr.get_commands()[0].id == cmd2.id

        # 删除不存在的命令应静默成功（设计为无副作用）
        mgr.delete_command("nonexistent")  # 不应报错
        assert len(mgr.get_commands()) == 1

    def test_set_enabled(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd = VoiceCommand.new("toggle", "exact", "test", "key", "")
        mgr.add_command(cmd)
        assert mgr.get_commands()[0].enabled is True
        mgr.set_enabled(cmd.id, False)
        assert mgr.get_commands()[0].enabled is False
        mgr.set_enabled(cmd.id, True)
        assert mgr.get_commands()[0].enabled is True

        with pytest.raises(ValueError, match="not found"):
            mgr.set_enabled("bad_id", True)


class TestCommandsManagerOrder:
    """顺序调整（移动）测试"""

    def test_move_up(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd_a = VoiceCommand.new("A", "exact", "a", "key", "")
        cmd_b = VoiceCommand.new("B", "exact", "b", "key", "")
        cmd_c = VoiceCommand.new("C", "exact", "c", "key", "")
        mgr.add_command(cmd_a)
        mgr.add_command(cmd_b)
        mgr.add_command(cmd_c)

        # 移动 B 向上 -> 顺序变为 B, A, C
        mgr.move_up(cmd_b.id)
        ids = [c.id for c in mgr.get_commands()]
        assert ids == [cmd_b.id, cmd_a.id, cmd_c.id]

        # 移动第一个元素向上（边界）应无变化
        mgr.move_up(cmd_b.id)
        assert [c.id for c in mgr.get_commands()] == [cmd_b.id, cmd_a.id, cmd_c.id]

    def test_move_down(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd_a = VoiceCommand.new("A", "exact", "a", "key", "")
        cmd_b = VoiceCommand.new("B", "exact", "b", "key", "")
        cmd_c = VoiceCommand.new("C", "exact", "c", "key", "")
        mgr.add_command(cmd_a)
        mgr.add_command(cmd_b)
        mgr.add_command(cmd_c)

        mgr.move_down(cmd_b.id)  # B 向下 -> A, C, B
        ids = [c.id for c in mgr.get_commands()]
        assert ids == [cmd_a.id, cmd_c.id, cmd_b.id]

        # 移动最后一个元素向下（边界）应无变化
        mgr.move_down(cmd_b.id)
        assert [c.id for c in mgr.get_commands()] == [cmd_a.id, cmd_c.id, cmd_b.id]

    def test_move_invalid_id(self, isolated_mgr):
        mgr, _ = isolated_mgr
        with pytest.raises(ValueError, match="not found"):
            mgr.move_up("invalid")
        with pytest.raises(ValueError, match="not found"):
            mgr.move_down("invalid")


class TestCommandsManagerPersistence:
    """持久化与加载测试"""

    def test_load_empty_file(self, isolated_mgr):
        mgr, tmp_path = isolated_mgr
        _write_empty(tmp_path)
        # 重新加载
        CommandsManager._instance = None
        mgr2 = CommandsManager()
        assert mgr2.get_commands() == []

    def test_load_two_commands(self, mgr_with_initial_data):
        mgr, _ = mgr_with_initial_data
        commands = mgr.get_commands()
        assert len(commands) == 2
        assert commands[0].name == "打开记事本"
        assert commands[0].matchPattern == "记事本"
        assert commands[1].enabled is False
        assert commands[1].actionParams == "ctrl+v"

    def test_load_malformed_json(self, isolated_mgr):
        mgr, tmp_path = isolated_mgr
        _write_malformed_json(tmp_path)
        # 重新加载，应该创建备份并返回空列表
        CommandsManager._instance = None
        mgr2 = CommandsManager()
        assert mgr2.get_commands() == []
        # 备份文件应该存在
        backup_path = tmp_path / "PhoneMic/config/commands.json.bak"
        assert backup_path.exists()
        assert backup_path.read_text(encoding="utf-8") == "这不是合法的json"

    def test_load_malformed_json_twice(self, isolated_mgr):
        """回归：第二次损坏时备份目标已存在，不能把构造带崩。

        旧实现用 file_path.rename(backup)，Windows 上目标已存在会抛
        FileExistsError —— 而这个调用就在 except 块里，异常直接冒出去，
        CommandsManager() 构造失败（表现为启动即崩 / 命令列表空）。
        """
        mgr, tmp_path = isolated_mgr
        backup_path = tmp_path / "PhoneMic/config/commands.json.bak"

        # 第一次损坏：正常生成备份
        _write_malformed_json(tmp_path)
        CommandsManager._instance = None
        assert CommandsManager().get_commands() == []
        assert backup_path.exists()

        # 第二次损坏：备份路径已被上一轮占用
        _write_malformed_json(tmp_path)
        (tmp_path / "PhoneMic/config/commands.json").write_text(
            "第二次损坏", encoding="utf-8")
        CommandsManager._instance = None
        mgr2 = CommandsManager()          # 修复前这里抛 FileExistsError

        assert mgr2.get_commands() == []
        assert backup_path.read_text(encoding="utf-8") == "第二次损坏"

    def test_load_extra_fields_ignored(self, isolated_mgr):
        mgr, tmp_path = isolated_mgr
        _write_with_unknown_fields(tmp_path)
        CommandsManager._instance = None
        mgr2 = CommandsManager()
        commands = mgr2.get_commands()
        assert len(commands) == 1
        assert commands[0].name == "额外字段命令"
        assert commands[0].matchPattern == "测试"

    def test_save_after_modification(self, isolated_mgr):
        mgr, tmp_path = isolated_mgr
        cmd = VoiceCommand.new("persist", "exact", "save_test", "key", "tab")
        mgr.add_command(cmd)
        mgr.update_command(cmd.id, name="updated")
        mgr.delete_command(cmd.id)
        # 最终文件应为空列表
        file_path = tmp_path / "PhoneMic/config/commands.json"
        data = json.loads(file_path.read_text(encoding="utf-8"))
        assert data == []


class TestCommandsManagerSignal:
    """信号发射测试"""

    def test_commands_changed_emitted_on_add(self, isolated_mgr):
        mgr, _ = isolated_mgr
        received = []

        def slot():
            received.append(True)

        mgr.commands_changed.connect(slot)
        cmd = VoiceCommand.new("signal", "exact", "test", "key", "")
        mgr.add_command(cmd)
        assert len(received) == 1

    def test_commands_changed_emitted_on_update(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd = VoiceCommand.new("test", "exact", "old", "key", "")
        mgr.add_command(cmd)
        received = []

        def slot():
            received.append(True)

        mgr.commands_changed.connect(slot)
        mgr.update_command(cmd.id, name="new")
        assert len(received) == 1

    def test_commands_changed_emitted_on_delete(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd = VoiceCommand.new("test", "exact", "del", "key", "")
        mgr.add_command(cmd)
        received = []

        def slot():
            received.append(True)

        mgr.commands_changed.connect(slot)
        mgr.delete_command(cmd.id)
        assert len(received) == 1

    def test_commands_changed_emitted_on_move(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd1 = VoiceCommand.new("A", "exact", "a", "key", "")
        cmd2 = VoiceCommand.new("B", "exact", "b", "key", "")
        mgr.add_command(cmd1)
        mgr.add_command(cmd2)
        received = []

        def slot():
            received.append(True)

        mgr.commands_changed.connect(slot)
        mgr.move_up(cmd2.id)
        assert len(received) == 1

    def test_commands_changed_emitted_on_set_enabled(self, isolated_mgr):
        mgr, _ = isolated_mgr
        cmd = VoiceCommand.new("test", "exact", "enable", "key", "")
        mgr.add_command(cmd)
        received = []

        def slot():
            received.append(True)

        mgr.commands_changed.connect(slot)
        mgr.set_enabled(cmd.id, False)
        assert len(received) == 1


class TestCommandsManagerSingleton:
    """单例行为测试"""

    def test_singleton(self):
        mgr1 = CommandsManager()
        mgr2 = CommandsManager()
        assert mgr1 is mgr2

    def test_reset_instance_allows_new(self, isolated_mgr):
        # isolated_mgr 会重置单例，这里验证手动重置有效
        mgr1 = CommandsManager()
        first_id = id(mgr1)
        CommandsManager._instance = None
        mgr2 = CommandsManager()
        assert id(mgr2) != first_id


class TestVoiceCommandHelper:
    """VoiceCommand 数据类辅助方法测试"""

    def test_new_creates_unique_id(self):
        cmd1 = VoiceCommand.new("A", "exact", "a", "key", "enter")
        cmd2 = VoiceCommand.new("B", "exact", "b", "key", "enter")
        assert cmd1.id != cmd2.id
        assert cmd1.enabled is True

    def test_to_dict_and_from_dict_roundtrip(self):
        original = VoiceCommand(
            id="fixed_id",
            name="测试",
            matchType="exact",
            matchPattern="hello",
            actionType="key",
            actionParams="ctrl+c",
            enabled=False,
        )
        as_dict = original.to_dict()
        reconstructed = VoiceCommand.from_dict(as_dict)
        assert reconstructed == original

    def test_from_dict_missing_fields_use_defaults(self):
        minimal_dict = {
            "name": "minimal",
            "matchType": "prefix",
            "matchPattern": "test",
            "actionType": "exec",
            "actionParams": "echo",
        }
        cmd = VoiceCommand.from_dict(minimal_dict)
        assert cmd.id is not None  # 会自动生成 id
        assert cmd.enabled is True  # 默认启用
        assert cmd.name == "minimal"


# ----------------------------------------------------------------------
# 如果需要运行此文件独立测试，可以添加 main 入口
# ----------------------------------------------------------------------
if __name__ == "__main__":
    pytest.main([__file__, "-v"])