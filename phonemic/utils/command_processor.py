import subprocess
import logging
import re
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from phonemic.gui.keyboard import send_keys, flash_insert
from phonemic.utils.paths import get_exec_workdir
from phonemic.utils.input_parser import (
    parse_input_sequence,
    parse_exec_args,
    apply_template,
)
from .commands_manager import VoiceCommand, CommandsManager

logger = logging.getLogger(__name__)

def match_command(text: str, commands: List[VoiceCommand]) -> Optional[Tuple[VoiceCommand, str, str]]:
    for cmd in commands:
        if not cmd.enabled:
            continue
        match_type = cmd.matchType
        pattern = cmd.matchPattern
        if match_type == "exact":
            if text == pattern:
                return (cmd, text, "")
        elif match_type == "prefix":
            if text.startswith(pattern):
                return (cmd, pattern, text[len(pattern):])
    return None

def extract_cwd_from_command_str(command_str: str) -> Tuple[str, Optional[Path]]:
    """
    从命令字符串开头提取 {cwd:...} 指定的工作目录。
    返回 (剩余命令字符串, cwd_path 或 None)
    """
    pattern = re.compile(r'^\{cwd:([^\{\}]+?)\}\s*', re.IGNORECASE)
    m = pattern.match(command_str)
    if m:
        path_str = m.group(1).strip()
        # 去除首尾的引号（双引号或单引号）
        if (path_str.startswith('"') and path_str.endswith('"')) or \
           (path_str.startswith("'") and path_str.endswith("'")):
            path_str = path_str[1:-1]
        try:
            expanded = os.path.expandvars(path_str)
            cwd_path = Path(expanded).expanduser().resolve()
            remaining = command_str[m.end():].lstrip()
            return remaining, cwd_path
        except Exception as e:
            logger.warning(f"无效的路径格式: {path_str}, 错误: {e}")
            return command_str, None
    return command_str, None

# 此函数废弃，由于windows使用shlex.split的问题，cwd只能放在最前面
def extract_cwd_from_tokens(tokens: List[str]) -> Tuple[List[str], Optional[Path]]:
    """
    从 token 列表中提取 {cwd:...} 指定的工作目录。
    支持环境变量扩展（例如 %LOCALAPPDATA%、$HOME 等）。
    返回 (新的 token 列表, cwd_path 或 None)
    """
    pattern = re.compile(r'^\{cwd:(.+)\}$')
    for i, token in enumerate(tokens):
        m = pattern.match(token)
        if m:
            path_str = m.group(1).strip()
            # 去除首尾的引号（双引号或单引号）
            if (path_str.startswith('"') and path_str.endswith('"')) or \
               (path_str.startswith("'") and path_str.endswith("'")):
                path_str = path_str[1:-1]
            try:
                # 1. 扩展环境变量（如 %LOCALAPPDATA% 或 $HOME）
                expanded = os.path.expandvars(path_str)
                # 2. 扩展用户目录（~），然后解析为绝对路径
                cwd_path = Path(expanded).expanduser().resolve()
                # 移除该 token
                new_tokens = tokens[:i] + tokens[i+1:]
                return new_tokens, cwd_path
            except Exception as e:
                logger.warning(f"无效的路径格式: {path_str}, 错误: {e}")
                return tokens, None
    return tokens, None

def execute_command(cmd: VoiceCommand, all_text: str, prefix: str, content: str) -> None:
    action_type = cmd.actionType
    params = cmd.actionParams
    try:
        if action_type == "key":
            # 解析混合输入序列：按键段 + 粘贴文本段
            segments = parse_input_sequence(params)
            if not segments:
                logger.error("执行命令失败：按键序列为空")
                return
            for seg_type, seg_value in segments:
                if seg_type == "key":
                    send_keys(seg_value)
                elif seg_type == "text":
                    text = apply_template(seg_value, all_text=all_text, prefix=prefix, content=content)
                    flash_insert(text)
                time.sleep(0.05)
        elif action_type == "exec":
            # 1. 提取并移除 {cwd:...} token（在格式化之前，避免占位符破坏标记）
            remaining_cmd, custom_cwd = extract_cwd_from_command_str(params)
            if not remaining_cmd.strip():
                logger.error("执行命令失败：参数列表为空")
                return

            # 2. 使用 pyparsing 分词（替代 shlex，Windows 反斜杠友好）
            tokens = parse_exec_args(remaining_cmd)
            if not tokens:
                logger.error("执行命令失败：参数列表为空")
                return

            # 3. 对每个 token 独立替换占位符
            formatted_tokens = [
                apply_template(token, all_text=all_text, prefix=prefix, content=content)
                for token in tokens
            ]

            # 4. 确定工作目录
            workdir = custom_cwd if custom_cwd is not None else get_exec_workdir()
            logger.info(f"执行命令: {formatted_tokens}, cwd={workdir}")

            # 5. 无 shell 执行
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            subprocess.Popen(
                formatted_tokens,
                shell=False,
                creationflags=creationflags,
                cwd=str(workdir)
            )
        else:
            logger.error(f"未知动作类型: {action_type}")
    except Exception as e:
        logger.exception(f"执行命令失败: {cmd.id} - {e}")

class CommandInterceptor:
    """命令拦截器，维护缓存并提供 send 文本处理"""
    
    def __init__(self):
        self._cached_commands: List[VoiceCommand] = []
        self._manager = CommandsManager.instance()
        self._reload_cache()
        self._manager.commands_changed.connect(self._on_commands_changed)

    def _reload_cache(self):
        """从 manager 加载 enabled 命令，保持存储顺序"""
        all_cmds = self._manager.get_commands()
        self._cached_commands = [cmd for cmd in all_cmds if cmd.enabled]
        logging.info(f"[CommandInterceptor] Cache reloaded, {len(self._cached_commands)} commands")

    def _on_commands_changed(self):
        self._reload_cache()

    def process_send_text(self, text: str) -> bool:
        """
        处理发送文本，若匹配命令则执行并返回 True，否则返回 False
        """
        if not self._cached_commands:
            return False
        result = match_command(text, self._cached_commands)
        if result is None:
            return False
        cmd, prefix, content = result
        logging.info(f"[CommandInterceptor] Match: id={cmd.id}, name={cmd.name}, text={text}")
        execute_command(cmd, text, prefix, content)
        return True
