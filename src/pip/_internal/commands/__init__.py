"""
Package containing all pip commands
"""

import importlib
from collections import namedtuple
from typing import Any, Dict, Optional

from pip._internal.cli.base_command import Command

CommandInfo = namedtuple("CommandInfo", "module_path, class_name, summary")

# This dictionary does a bunch of heavy lifting for help output:
# - Enables avoiding additional (costly) imports for presenting `--help`.
# - The ordering matters for help display.
#
# Even though the module path starts with the same "pip._internal.commands"
# prefix, the full path makes testing easier (specifically when modifying
# `commands_dict` in test setup / teardown).
commands_dict: Dict[str, CommandInfo] = {
    "install": CommandInfo(
        "pip._internal.commands.install",
        "InstallCommand",
        "安装软件包。",
    ),
    "download": CommandInfo(
        "pip._internal.commands.download",
        "DownloadCommand",
        "下载软件包。",
    ),
    "uninstall": CommandInfo(
        "pip._internal.commands.uninstall",
        "UninstallCommand",
        "卸载软件包。",
    ),
    "freeze": CommandInfo(
        "pip._internal.commands.freeze",
        "FreezeCommand",
        "以需求格式输出已安装的软件包。",
    ),
    "inspect": CommandInfo(
        "pip._internal.commands.inspect",
        "InspectCommand",
        "检查 python 环境。",
    ),
    "list": CommandInfo(
        "pip._internal.commands.list",
        "ListCommand",
        "列出已安装软件包。",
    ),
    "show": CommandInfo(
        "pip._internal.commands.show",
        "ShowCommand",
        "显示已安装软件包的信息。",
    ),
    "check": CommandInfo(
        "pip._internal.commands.check",
        "CheckCommand",
        "验证已安装的软件包是否具有兼容的依赖关系。",
    ),
    "config": CommandInfo(
        "pip._internal.commands.configuration",
        "ConfigurationCommand",
        "管理本地和全局配置。",
    ),
    "search": CommandInfo(
        "pip._internal.commands.search",
        "SearchCommand",
        "搜索 PyPI 软件包。",
    ),
    "cache": CommandInfo(
        "pip._internal.commands.cache",
        "CacheCommand",
        "检查和管理 pip 的 wheel 缓存.",
    ),
    "index": CommandInfo(
        "pip._internal.commands.index",
        "IndexCommand",
        "检查软件包索引中提供的信息。",
    ),
    "wheel": CommandInfo(
        "pip._internal.commands.wheel",
        "WheelCommand",
        "从你的需求中生成 wheel 包。",
    ),
    "hash": CommandInfo(
        "pip._internal.commands.hash",
        "HashCommand",
        "计算包存档的哈希值。",
    ),
    "completion": CommandInfo(
        "pip._internal.commands.completion",
        "CompletionCommand",
        "用于命令补全的辅助命令。",
    ),
    "debug": CommandInfo(
        "pip._internal.commands.debug",
        "DebugCommand",
        "显示对调试有用的信息。",
    ),

    "help": CommandInfo(
        "pip._internal.commands.help",
        "HelpCommand",
        "显示命令帮助。",
    ),
}


def create_command(name: str, **kwargs: Any) -> Command:
    """
    Create an instance of the Command class with the given name.
    """
    module_path, class_name, summary = commands_dict[name]
    module = importlib.import_module(module_path)
    command_class = getattr(module, class_name)
    command = command_class(name=name, summary=summary, **kwargs)

    return command


def get_similar_commands(name: str) -> Optional[str]:
    """Command name auto-correct."""
    from difflib import get_close_matches

    name = name.lower()

    close_commands = get_close_matches(name, commands_dict.keys())

    if close_commands:
        return close_commands[0]
    else:
        return None
