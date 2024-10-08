import os
import textwrap
from optparse import Values
from typing import Any, List

from pip._internal.cli.base_command import Command
from pip._internal.cli.status_codes import ERROR, SUCCESS
from pip._internal.exceptions import CommandError, PipError
from pip._internal.utils import filesystem
from pip._internal.utils.logging import getLogger

logger = getLogger(__name__)


class CacheCommand(Command):
    """
    检查和管理 pip 的 wheel 缓存。

    子命令：

    - dir: 显示缓存目录。
    - info: 显示缓存的信息。
    - list: 列出存储在缓存中的包文件名。
    - remove: 从缓存中移除一个或多个包。
    - purge: 移除缓存中的所有项目。

    ``<pattern>`` 可以是一个 glob 表达式或包名。
    """

    ignore_require_venv = True
    usage = """
        %prog dir
        %prog info
        %prog list [<pattern>] [--format=[human, abspath]]
        %prog remove <pattern>
        %prog purge
    """

    def add_options(self) -> None:
        self.cmd_opts.add_option(
            "--format",
            action="store",
            dest="list_format",
            default="human",
            choices=("human", "abspath"),
            help="从以下格式中选择输出格式： human (默认) or abspath",
        )

        self.parser.insert_option_group(0, self.cmd_opts)

    def run(self, options: Values, args: List[str]) -> int:
        handlers = {
            "dir": self.get_cache_dir,
            "info": self.get_cache_info,
            "list": self.list_cache_items,
            "remove": self.remove_cache_items,
            "purge": self.purge_cache,
        }

        if not options.cache_dir:
            logger.error("pip 缓存已禁用，缓存命令无法运行。")
            return ERROR

        # Determine action
        if not args or args[0] not in handlers:
            logger.error(
                "需要执行一个操作 (%s)。",
                ", ".join(sorted(handlers)),
            )
            return ERROR

        action = args[0]

        # Error handling happens here, not in the action-handlers.
        try:
            handlers[action](options, args[1:])
        except PipError as e:
            logger.error(e.args[0])
            return ERROR

        return SUCCESS

    def get_cache_dir(self, options: Values, args: List[Any]) -> None:
        if args:
            raise CommandError("太多冲突")

        logger.info(options.cache_dir)

    def get_cache_info(self, options: Values, args: List[Any]) -> None:
        if args:
            raise CommandError("太多冲突")

        num_http_files = len(self._find_http_files(options))
        num_packages = len(self._find_wheels(options, "*"))

        http_cache_location = self._cache_dir(options, "http-v2")
        old_http_cache_location = self._cache_dir(options, "http")
        wheels_cache_location = self._cache_dir(options, "wheels")
        http_cache_size = filesystem.format_size(
            filesystem.directory_size(http_cache_location)
            + filesystem.directory_size(old_http_cache_location)
        )
        wheels_cache_size = filesystem.format_directory_size(wheels_cache_location)

        message = (
            textwrap.dedent(
                """
                    软件包索引页面缓存位置(pip v23.3+): {http_cache_location}
                    软件包索引页面缓存位置(旧版 pip): {old_http_cache_location}
                    软件包索引页面缓存大小: {http_cache_size}
                    HTTP 文件数量: {num_http_files}
                    本地构建的 whl 文件位置: {wheels_cache_location}
                    本地构建的 whl 文件大小: {wheels_cache_size}
                    本地构建的 whl 文件数量: {package_count}

                """  # noqa: E501
            )
            .format(
                http_cache_location=http_cache_location,
                old_http_cache_location=old_http_cache_location,
                http_cache_size=http_cache_size,
                num_http_files=num_http_files,
                wheels_cache_location=wheels_cache_location,
                package_count=num_packages,
                wheels_cache_size=wheels_cache_size,
            )
            .strip()
        )

        logger.info(message)

    def list_cache_items(self, options: Values, args: List[Any]) -> None:
        if len(args) > 1:
            raise CommandError("太多冲突")

        if args:
            pattern = args[0]
        else:
            pattern = "*"

        files = self._find_wheels(options, pattern)
        if options.list_format == "human":
            self.format_for_human(files)
        else:
            self.format_for_abspath(files)

    def format_for_human(self, files: List[str]) -> None:
        if not files:
            logger.info("没有本地构建的 whl 缓存。")
            return

        results = []
        for filename in files:
            wheel = os.path.basename(filename)
            size = filesystem.format_file_size(filename)
            results.append(f" - {wheel} ({size})")
        logger.info("缓存内容：\n")
        logger.info("\n".join(sorted(results)))

    def format_for_abspath(self, files: List[str]) -> None:
        if files:
            logger.info("\n".join(sorted(files)))

    def remove_cache_items(self, options: Values, args: List[Any]) -> None:
        if len(args) > 1:
            raise CommandError("太多冲突")

        if not args:
            raise CommandError("请提供模式")

        files = self._find_wheels(options, args[0])

        no_matching_msg = "无匹配软件包"
        if args[0] == "*":
            # Only fetch http files if no specific pattern given
            files += self._find_http_files(options)
        else:
            # Add the pattern to the log message
            no_matching_msg += f' for pattern "{args[0]}"'

        if not files:
            logger.warning(no_matching_msg)

        for filename in files:
            os.unlink(filename)
            logger.verbose("删除 %s", filename)
        logger.info("文件删除: %s", len(files))

    def purge_cache(self, options: Values, args: List[Any]) -> None:
        if args:
            raise CommandError("太多冲突")

        return self.remove_cache_items(options, ["*"])

    def _cache_dir(self, options: Values, subdir: str) -> str:
        return os.path.join(options.cache_dir, subdir)

    def _find_http_files(self, options: Values) -> List[str]:
        old_http_dir = self._cache_dir(options, "http")
        new_http_dir = self._cache_dir(options, "http-v2")
        return filesystem.find_files(old_http_dir, "*") + filesystem.find_files(
            new_http_dir, "*"
        )

    def _find_wheels(self, options: Values, pattern: str) -> List[str]:
        wheel_dir = self._cache_dir(options, "wheels")

        # The wheel filename format, as specified in PEP 427, is:
        #     {distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl
        #
        # Additionally, non-alphanumeric values in the distribution are
        # normalized to underscores (_), meaning hyphens can never occur
        # before `-{version}`.
        #
        # Given that information:
        # - If the pattern we're given contains a hyphen (-), the user is
        #   providing at least the version. Thus, we can just append `*.whl`
        #   to match the rest of it.
        # - If the pattern we're given doesn't contain a hyphen (-), the
        #   user is only providing the name. Thus, we append `-*.whl` to
        #   match the hyphen before the version, followed by anything else.
        #
        # PEP 427: https://www.python.org/dev/peps/pep-0427/
        pattern = pattern + ("*.whl" if "-" in pattern else "-*.whl")

        return filesystem.find_files(wheel_dir, pattern)
