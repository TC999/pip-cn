import logging
from optparse import Values
from typing import List

from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.cli import cmdoptions
from pip._internal.cli.base_command import Command
from pip._internal.cli.index_command import SessionCommandMixin
from pip._internal.cli.status_codes import SUCCESS
from pip._internal.exceptions import InstallationError
from pip._internal.req import parse_requirements
from pip._internal.req.constructors import (
    install_req_from_line,
    install_req_from_parsed_requirement,
)
from pip._internal.utils.misc import (
    check_externally_managed,
    protect_pip_from_modification_on_windows,
    warn_if_run_as_root,
)

logger = logging.getLogger(__name__)


class UninstallCommand(Command, SessionCommandMixin):
    """
    卸载软件包

    pip 可以卸载大部分已安装的软件包。已知的例外情况包括

    - 使用 ``python setup.py install`` 安装的纯 distutils 软件包。
      它不会留下元数据来确定安装了哪些文件。
    - 通过 ``python setup.py develop`` 安装的脚本封装包。
    """

    usage = """
      %prog [options] <package> ...
      %prog [options] -r <requirements file> ..."""

    def add_options(self) -> None:
        self.cmd_opts.add_option(
            "-r",
            "--requirement",
            dest="requirements",
            action="append",
            default=[],
            metavar="file",
            help=(
                "卸载给定需求文件中列出的所有软件包。此选项可以多次使用。"
            ),
        )
        self.cmd_opts.add_option(
            "-y",
            "--yes",
            dest="yes",
            action="store_true",
            help="卸载或删除时不要求确认。",
        )
        self.cmd_opts.add_option(cmdoptions.root_user_action())
        self.cmd_opts.add_option(cmdoptions.override_externally_managed())
        self.parser.insert_option_group(0, self.cmd_opts)

    def run(self, options: Values, args: List[str]) -> int:
        session = self.get_default_session(options)

        reqs_to_uninstall = {}
        for name in args:
            req = install_req_from_line(
                name,
                isolated=options.isolated_mode,
            )
            if req.name:
                reqs_to_uninstall[canonicalize_name(req.name)] = req
            else:
                logger.warning(
                    "无效的需求：%r 被忽略 -"
                    "卸载命令需要指定的需求名称:",
                    name,
                )
        for filename in options.requirements:
            for parsed_req in parse_requirements(
                filename, options=options, session=session
            ):
                req = install_req_from_parsed_requirement(
                    parsed_req, isolated=options.isolated_mode
                )
                if req.name:
                    reqs_to_uninstall[canonicalize_name(req.name)] = req
        if not reqs_to_uninstall:
            raise InstallationError(
                f"您必须为 {self.name} 提供至少一项要求 (使用 "
                f'"pip help {self.name}")'
            )

        if not options.override_externally_managed:
            check_externally_managed()

        protect_pip_from_modification_on_windows(
            modifying_pip="pip" in reqs_to_uninstall
        )

        for req in reqs_to_uninstall.values():
            uninstall_pathset = req.uninstall(
                auto_confirm=options.yes,
                verbose=self.verbosity > 0,
            )
            if uninstall_pathset:
                uninstall_pathset.commit()
        if options.root_user_action == "warn":
            warn_if_run_as_root()
        return SUCCESS
