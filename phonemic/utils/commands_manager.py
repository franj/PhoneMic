import json
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Literal, Optional
from PySide6.QtCore import QObject, Signal
from PySide6.QtCore import QStandardPaths

from phonemic.utils.paths import get_config_dir

@dataclass
class VoiceCommand:
    id: str
    name: str
    matchType: Literal["exact", "prefix", "regex"]
    matchPattern: str
    actionType: Literal["key", "exec"]
    actionParams: str
    enabled: bool

    @staticmethod
    def new(name: str, match_type: str, pattern: str, action_type: str, params: str) -> "VoiceCommand":
        return VoiceCommand(
            id=uuid.uuid4().hex,
            name=name,
            matchType=match_type,  # type: ignore
            matchPattern=pattern,
            actionType=action_type,  # type: ignore
            actionParams=params,
            enabled=True
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "VoiceCommand":
        # 兼容旧格式缺失字段
        return VoiceCommand(
            id=data.get("id", uuid.uuid4().hex),
            name=data["name"],
            matchType=data["matchType"],
            matchPattern=data["matchPattern"],
            actionType=data["actionType"],
            actionParams=data["actionParams"],
            enabled=data.get("enabled", True)
        )


class CommandsManager(QObject):
    commands_changed = Signal()

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    @classmethod
    def instance(cls) -> "CommandsManager":
        """获取单例实例（等价于直接调用构造函数）"""
        return cls()

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        super().__init__()
        self._initialized = True
        self._commands: List[VoiceCommand] = []
        self._load()

    def _get_file_path(self) -> Path:
        return get_config_dir() / "commands.json"

    def _load(self):
        file_path = self._get_file_path()
        if not file_path.exists():
            self._commands = []
            self.save()
            return
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Root element must be a list")
            self._commands = [VoiceCommand.from_dict(item) for item in data]

            # ------------------------------
            # 重复 ID 修复逻辑
            # ------------------------------
            seen = set()
            need_save = False
            for cmd in self._commands:
                while cmd.id in seen:
                    old_id = cmd.id
                    cmd.id = uuid.uuid4().hex
                    print(f"[CommandsManager] 发现重复 ID '{old_id}'，已重新分配为 '{cmd.id}'")
                    need_save = True
                seen.add(cmd.id)

            if need_save:
                self.save()   # 将修复后的数据写回文件
        except Exception as e:
            # 备份损坏文件
            backup = file_path.with_suffix(".json.bak")
            file_path.rename(backup)
            print(f"[CommandsManager] Failed to load commands.json, backup created: {backup}, error: {e}")
            self._commands = []
            self.save()

    def save(self):
        # 1. 构建数据
        try:
            data_to_write = [cmd.to_dict() for cmd in self._commands]
        except Exception as e:
            print(f"构建命令数据失败: {e}")
            raise

        # 2. 先转为 JSON 字符串（检测序列化错误）
        try:
            json_str = json.dumps(data_to_write, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"JSON 序列化失败: {e}")
            raise

        # 3. 确保目录存在
        file_path = self._get_file_path()
        get_config_dir().mkdir(parents=True, exist_ok=True)

        # 4. 写入临时文件并原子替换
        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(json_str)
        temp_path.replace(file_path)   # Windows 上也支持 replace（Python 3.3+）

    def _emit_and_save(self):
        self.commands_changed.emit()
        self.save()

    # Public API
    def get_commands(self) -> List[VoiceCommand]:
        return self._commands.copy()  # 返回副本防止外部修改

    def add_command(self, cmd: VoiceCommand) -> None:
        if not isinstance(cmd, VoiceCommand):
            raise TypeError(f"Expected VoiceCommand or dict, got {type(cmd)}")
        self._commands.append(cmd)
        self._emit_and_save()

    def update_command(self, cmd_id: str, **updates) -> None:
        for i, cmd in enumerate(self._commands):
            if cmd.id == cmd_id:
                # 更新允许的字段
                allowed = {"name", "matchType", "matchPattern", "actionType", "actionParams", "enabled"}
                for key, value in updates.items():
                    if key in allowed:
                        setattr(cmd, key, value)
                self._emit_and_save()
                return
        raise ValueError(f"Command with id {cmd_id} not found")

    def delete_command(self, cmd_id: str) -> None:
        self._commands = [cmd for cmd in self._commands if cmd.id != cmd_id]
        self._emit_and_save()

    def move_up(self, cmd_id: str) -> None:
        idx = self._find_index(cmd_id)
        if idx > 0:
            self._commands[idx], self._commands[idx-1] = self._commands[idx-1], self._commands[idx]
            self._emit_and_save()

    def move_down(self, cmd_id: str) -> None:
        idx = self._find_index(cmd_id)
        if idx < len(self._commands) - 1:
            self._commands[idx], self._commands[idx+1] = self._commands[idx+1], self._commands[idx]
            self._emit_and_save()

    def set_enabled(self, cmd_id: str, enabled: bool) -> None:
        for cmd in self._commands:
            if cmd.id == cmd_id:
                cmd.enabled = enabled
                self._emit_and_save()
                return
        raise ValueError(f"Command with id {cmd_id} not found")

    def _find_index(self, cmd_id: str) -> int:
        for i, cmd in enumerate(self._commands):
            if cmd.id == cmd_id:
                return i
        raise ValueError(f"Command with id {cmd_id} not found")