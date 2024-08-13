import logging
import os
import subprocess
from optparse import Values
from typing import Any, List, Optional

from pip._internal.cli.base_command import Command
from pip._internal.cli.status_codes import ERROR, SUCCESS
from pip._internal.configuration import (
    Configuration,
    Kind,
    get_configuration_files,
    kinds,
)
from pip._internal.exceptions import PipError
from pip._internal.utils.logging import indent_log
from pip._internal.utils.misc import get_prog, write_output

logger = logging.getLogger(__name__)


class ConfigurationCommand(Command):
    """
    管理本地和全局配置。

    子命令：

    - list：列出活动配置（或指定文件中的配置）
    - edit：在编辑器中编辑配置文件
    - get：获取与 command.option 关联的值
    - set：设置 command.option=value
    - unset：取消与 command.option 关联的值
    - debug：列出配置文件及其定义的值
    
    配置键应由命令和选项名称组成，使用点号分隔，并且具有特殊前缀 "global" 以影响所有命令。例如，"pip config set global.index-url https://example.org/" 将为所有命令配置索引 URL，而 "pip config set download.timeout 10" 仅为 "pip download" 命令配置 10 秒超时。
    
    如果没有传递 --user、--global 和 --site 选项，则会使用虚拟环境配置文件（如果它是活动的且文件存在）。否则，默认情况下所有修改都会应用于用户文件。
    """

    ignore_require_venv = True
    usage = """
        %prog [<file-option>] list
        %prog [<file-option>] [--editor <editor-path>] edit

        %prog [<file-option>] get command.option
        %prog [<file-option>] set command.option value
        %prog [<file-option>] unset command.option
        %prog [<file-option>] debug
    """

    def add_options(self) -> None:
        self.cmd_opts.add_option(
            "--editor",
            dest="editor",
            action="store",
            default=None,
            help=(
                "用于编辑文件的编辑器。如果未提供，将使用 VISUAL 或 EDITOR 环境变量。"
            ),
        )

        self.cmd_opts.add_option(
            "--global",
            dest="global_file",
            action="store_true",
            default=False,
            help="仅使用全系统配置文件",
        )

        self.cmd_opts.add_option(
            "--user",
            dest="user_file",
            action="store_true",
            default=False,
            help="仅使用用户配置文件",
        )

        self.cmd_opts.add_option(
            "--site",
            dest="site_file",
            action="store_true",
            default=False,
            help="仅使用当前环境配置文件",
        )

        self.parser.insert_option_group(0, self.cmd_opts)

    def run(self, options: Values, args: List[str]) -> int:
        handlers = {
            "list": self.list_values,
            "edit": self.open_in_editor,
            "get": self.get_name,
            "set": self.set_name_value,
            "unset": self.unset_name,
            "debug": self.list_config_values,
        }

        # Determine action
        if not args or args[0] not in handlers:
            logger.error(
                "需要执行一个操作 (%s)。",
                ", ".join(sorted(handlers)),
            )
            return ERROR

        action = args[0]

        # Determine which configuration files are to be loaded
        #    Depends on whether the command is modifying.
        try:
            load_only = self._determine_file(
                options, need_value=(action in ["get", "set", "unset", "edit"])
            )
        except PipError as e:
            logger.error(e.args[0])
            return ERROR

        # Load a new configuration
        self.configuration = Configuration(
            isolated=options.isolated_mode, load_only=load_only
        )
        self.configuration.load()

        # Error handling happens here, not in the action-handlers.
        try:
            handlers[action](options, args[1:])
        except PipError as e:
            logger.error(e.args[0])
            return ERROR

        return SUCCESS

    def _determine_file(self, options: Values, need_value: bool) -> Optional[Kind]:
        file_options = [
            key
            for key, value in (
                (kinds.USER, options.user_file),
                (kinds.GLOBAL, options.global_file),
                (kinds.SITE, options.site_file),
            )
            if value
        ]

        if not file_options:
            if not need_value:
                return None
            # Default to user, unless there's a site file.
            elif any(
                os.path.exists(site_config_file)
                for site_config_file in get_configuration_files()[kinds.SITE]
            ):
                return kinds.SITE
            else:
                return kinds.USER
        elif len(file_options) == 1:
            return file_options[0]

        raise PipError(
            "需要指定一个文件进行操作 (--user、--site、--global)。"
        )

    def list_values(self, options: Values, args: List[str]) -> None:
        self._get_n_args(args, "list", n=0)

        for key, value in sorted(self.configuration.items()):
            write_output("%s=%r", key, value)

    def get_name(self, options: Values, args: List[str]) -> None:
        key = self._get_n_args(args, "get [name]", n=1)
        value = self.configuration.get_value(key)

        write_output("%s", value)

    def set_name_value(self, options: Values, args: List[str]) -> None:
        key, value = self._get_n_args(args, "set [name] [value]", n=2)
        self.configuration.set_value(key, value)

        self._save_configuration()

    def unset_name(self, options: Values, args: List[str]) -> None:
        key = self._get_n_args(args, "unset [name]", n=1)
        self.configuration.unset_value(key)

        self._save_configuration()

    def list_config_values(self, options: Values, args: List[str]) -> None:
        """列出不同配置文件中的配置键值对"""
        self._get_n_args(args, "debug", n=0)

        self.print_env_var_values()
        # Iterate over config files and print if they exist, and the
        # key-value pairs present in them if they do
        for variant, files in sorted(self.configuration.iter_config_files()):
            write_output("%s:", variant)
            for fname in files:
                with indent_log():
                    file_exists = os.path.exists(fname)
                    write_output("%s, exists: %r", fname, file_exists)
                    if file_exists:
                        self.print_config_file_values(variant)

    def print_config_file_values(self, variant: Kind) -> None:
        """从变量文件中获取键值对"""
        for name, value in self.configuration.get_values_in_config(variant).items():
            with indent_log():
                write_output("%s: %s", name, value)

    def print_env_var_values(self) -> None:
        """获取作为环境变量存在的键值对"""
        write_output("%s:", "env_var")
        with indent_log():
            for key, value in sorted(self.configuration.get_environ_vars()):
                env_var = f"PIP_{key.upper()}"
                write_output("%s=%r", env_var, value)

    def open_in_editor(self, options: Values, args: List[str]) -> None:
        editor = self._determine_editor(options)

        fname = self.configuration.get_file_to_edit()
        if fname is None:
            raise PipError("无法确定合适的文件。")
        elif '"' in fname:
            # This shouldn't happen, unless we see a username like that.
            # If that happens, we'd appreciate a pull request fixing this.
            raise PipError(
                f'无法为包含以下内容的文件名打开编辑器 "\n{fname}'
            )

        try:
            subprocess.check_call(f'{editor} "{fname}"', shell=True)
        except FileNotFoundError as e:
            if not e.filename:
                e.filename = editor
            raise
        except subprocess.CalledProcessError as e:
            raise PipError(f"编辑器子进程以退出代码退出 {e.returncode}")

    def _get_n_args(self, args: List[str], example: str, n: int) -> Any:
        """帮助确保命令获得正确的参数数"""
        if len(args) != n:
            msg = (
                f"参数数量出乎意料，预计为 {n}。 "
                f'(示例: "{get_prog()} config {example}")'
            )
            raise PipError(msg)

        if n == 1:
            return args[0]
        else:
            return args

    def _save_configuration(self) -> None:
        # We successfully ran a modifying command. Need to save the
        # configuration.
        try:
            self.configuration.save()
        except Exception:
            logger.exception(
                "无法保存配置。请将此作为错误报告。"
            )
            raise PipError("内部错误。")

    def _determine_editor(self, options: Values) -> str:
        if options.editor is not None:
            return options.editor
        elif "VISUAL" in os.environ:
            return os.environ["VISUAL"]
        elif "EDITOR" in os.environ:
            return os.environ["EDITOR"]
        else:
            raise PipError("无法确定使用的编辑器。")
