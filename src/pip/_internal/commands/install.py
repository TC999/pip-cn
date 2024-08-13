import errno
import json
import operator
import os
import shutil
import site
from optparse import SUPPRESS_HELP, Values
from typing import List, Optional

from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.rich import print_json

from pip._internal.cache import WheelCache
from pip._internal.cli import cmdoptions
from pip._internal.cli.cmdoptions import make_target_python
from pip._internal.cli.req_command import (
    RequirementCommand,
    with_cleanup,
)
from pip._internal.cli.status_codes import ERROR, SUCCESS
from pip._internal.exceptions import CommandError, InstallationError
from pip._internal.locations import get_scheme
from pip._internal.metadata import get_environment
from pip._internal.models.installation_report import InstallationReport
from pip._internal.operations.build.build_tracker import get_build_tracker
from pip._internal.operations.check import ConflictDetails, check_install_conflicts
from pip._internal.req import install_given_reqs
from pip._internal.req.req_install import (
    InstallRequirement,
    check_legacy_setup_py_options,
)
from pip._internal.utils.compat import WINDOWS
from pip._internal.utils.filesystem import test_writable_dir
from pip._internal.utils.logging import getLogger
from pip._internal.utils.misc import (
    check_externally_managed,
    ensure_dir,
    get_pip_version,
    protect_pip_from_modification_on_windows,
    warn_if_run_as_root,
    write_output,
)
from pip._internal.utils.temp_dir import TempDirectory
from pip._internal.utils.virtualenv import (
    running_under_virtualenv,
    virtualenv_no_global,
)
from pip._internal.wheel_builder import build, should_build_for_install_command

logger = getLogger(__name__)


class InstallCommand(RequirementCommand):
    """
    从以下来源安装软件包：
    
    - 使用需求规范从 PyPI（及其他索引）安装。
    - 从版本控制系统（VCS）项目网址安装。
    - 从本地项目目录安装。
    - 从本地或远程的源代码压缩包安装。
    
    pip 还支持从“需求文件”进行安装，这提供了一种简单的方法来指定要安装的整个环境。
    """

    usage = """
      %prog [options] <requirement specifier> [package-index-options] ...
      %prog [options] -r <requirements file> [package-index-options] ...
      %prog [options] [-e] <vcs project url> ...
      %prog [options] [-e] <local project path> ...
      %prog [options] <archive url/path> ..."""

    def add_options(self) -> None:
        self.cmd_opts.add_option(cmdoptions.requirements())
        self.cmd_opts.add_option(cmdoptions.constraints())
        self.cmd_opts.add_option(cmdoptions.no_deps())
        self.cmd_opts.add_option(cmdoptions.pre())

        self.cmd_opts.add_option(cmdoptions.editable())
        self.cmd_opts.add_option(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            default=False,
            help=(
                "实际上不安装任何东西，只输出将要安装的内容。"
                "可以与 --ignore-installed 选项结合使用，以‘解析’需求。"
            ),
        )
        self.cmd_opts.add_option(
            "-t",
            "--target",
            dest="target_dir",
            metavar="dir",
            default=None,
            help=(
                "将软件包安装到 `<dir>` 目录中。"
                "默认情况下，这不会替换 `<dir>` 中现有的文件/文件夹。"
                "使用 `--upgrade` 选项可以用新版本替换 `<dir>` 中现有的软件包。"
            ),
        )
        cmdoptions.add_target_python_options(self.cmd_opts)

        self.cmd_opts.add_option(
            "--user",
            dest="use_user_site",
            action="store_true",
            help=(
                "安装到您所在平台的 Python 用户安装目录中。"
                "通常为 `~/.local/`，或在 Windows 上为 `%APPDATA%\\Python`。"
                "（有关详细信息，请参阅 Python 文档中的 `site.USER_BASE`。）"
            ),
        )
        self.cmd_opts.add_option(
            "--no-user",
            dest="use_user_site",
            action="store_false",
            help=SUPPRESS_HELP,
        )
        self.cmd_opts.add_option(
            "--root",
            dest="root_path",
            metavar="dir",
            default=None,
            help="将所有内容安装到该备用根目录。",
        )
        self.cmd_opts.add_option(
            "--prefix",
            dest="prefix_path",
            metavar="dir",
            default=None,
            help=(
                "安装前缀，用于放置 `lib`、`bin` 和其他顶级文件夹。"
                "请注意，生成的安装内容可能包含引用 pip 的 Python 解释器"
                "而不是 `--prefix` 指定的解释器的脚本和其他资源。"
                "如果打算将软件包安装到另一个（可能没有 pip 的）环境中，"
                "请参阅 `--python` 选项。"
            ),
        )

        self.cmd_opts.add_option(cmdoptions.src())

        self.cmd_opts.add_option(
            "-U",
            "--upgrade",
            dest="upgrade",
            action="store_true",
            help=(
                "将所有指定的软件包升级到可用的最新版本。"
                "依赖项的处理取决于所使用的升级策略。" # upgrade-strategy
            ),
        )

        self.cmd_opts.add_option(
            "--upgrade-strategy",
            dest="upgrade_strategy",
            default="only-if-needed",
            choices=["only-if-needed", "eager"],
            help=(
                "确定依赖关系升级的处理方式 [默认值：%default]。"
                "- “eager” - 不论当前安装的版本是否满足升级包的要求，都会升级依赖关系。"
                "- “only-if-needed” - 仅在当前安装的版本不满足升级包的要求时才会升级依赖关系。"
            ),
        )

        self.cmd_opts.add_option(
            "--force-reinstall",
            dest="force_reinstall",
            action="store_true",
            help="重新安装所有软件包，即使它们已经是最新。",
        )

        self.cmd_opts.add_option(
            "-I",
            "--ignore-installed",
            dest="ignore_installed",
            action="store_true",
            help=(
                "忽略已安装的软件包，覆盖它们。如果现有的软件包版本不同"
                "或使用不同的包管理器安装，这可能会破坏您的系统！"
            ),
        )

        self.cmd_opts.add_option(cmdoptions.ignore_requires_python())
        self.cmd_opts.add_option(cmdoptions.no_build_isolation())
        self.cmd_opts.add_option(cmdoptions.use_pep517())
        self.cmd_opts.add_option(cmdoptions.no_use_pep517())
        self.cmd_opts.add_option(cmdoptions.check_build_deps())
        self.cmd_opts.add_option(cmdoptions.override_externally_managed())

        self.cmd_opts.add_option(cmdoptions.config_settings())
        self.cmd_opts.add_option(cmdoptions.global_options())

        self.cmd_opts.add_option(
            "--compile",
            action="store_true",
            dest="compile",
            default=True,
            help="将 Python 源文件编译成字节码",
        )

        self.cmd_opts.add_option(
            "--no-compile",
            action="store_false",
            dest="compile",
            help="不要将 Python 源文件编译成字节码",
        )

        self.cmd_opts.add_option(
            "--no-warn-script-location",
            action="store_false",
            dest="warn_script_location",
            default=True,
            help="在 PATH 以外安装脚本时不发出警告",
        )
        self.cmd_opts.add_option(
            "--no-warn-conflicts",
            action="store_false",
            dest="warn_about_conflicts",
            default=True,
            help="不对已损坏的依赖发出警告",
        )
        self.cmd_opts.add_option(cmdoptions.no_binary())
        self.cmd_opts.add_option(cmdoptions.only_binary())
        self.cmd_opts.add_option(cmdoptions.prefer_binary())
        self.cmd_opts.add_option(cmdoptions.require_hashes())
        self.cmd_opts.add_option(cmdoptions.progress_bar())
        self.cmd_opts.add_option(cmdoptions.root_user_action())

        index_opts = cmdoptions.make_option_group(
            cmdoptions.index_group,
            self.parser,
        )

        self.parser.insert_option_group(0, index_opts)
        self.parser.insert_option_group(0, self.cmd_opts)

        self.cmd_opts.add_option(
            "--report",
            dest="json_report_file",
            metavar="file",
            default=None,
            help=(
                "生成一个 JSON 文件，描述 pip 为安装提供的需求所执行的操作。"
                "可以与 `--dry-run` 和 `--ignore-installed` 选项结合使用，以‘解析’需求。"
                "当文件名为 `-` 时，输出到标准输出（stdout）。"
                "当写入标准输出时，请结合使用 `--quiet` 选项，以避免将 pip 的日志输出与 JSON 输出混合。"
            ),
        )

    @with_cleanup
    def run(self, options: Values, args: List[str]) -> int:
        if options.use_user_site and options.target_dir is not None:
            raise CommandError("Can not combine '--user' and '--target'")

        # Check whether the environment we're installing into is externally
        # managed, as specified in PEP 668. Specifying --root, --target, or
        # --prefix disables the check, since there's no reliable way to locate
        # the EXTERNALLY-MANAGED file for those cases. An exception is also
        # made specifically for "--dry-run --report" for convenience.
        installing_into_current_environment = (
            not (options.dry_run and options.json_report_file)
            and options.root_path is None
            and options.target_dir is None
            and options.prefix_path is None
        )
        if (
            installing_into_current_environment
            and not options.override_externally_managed
        ):
            check_externally_managed()

        upgrade_strategy = "to-satisfy-only"
        if options.upgrade:
            upgrade_strategy = options.upgrade_strategy

        cmdoptions.check_dist_restriction(options, check_target=True)

        logger.verbose("Using %s", get_pip_version())
        options.use_user_site = decide_user_install(
            options.use_user_site,
            prefix_path=options.prefix_path,
            target_dir=options.target_dir,
            root_path=options.root_path,
            isolated_mode=options.isolated_mode,
        )

        target_temp_dir: Optional[TempDirectory] = None
        target_temp_dir_path: Optional[str] = None
        if options.target_dir:
            options.ignore_installed = True
            options.target_dir = os.path.abspath(options.target_dir)
            if (
                # fmt: off
                os.path.exists(options.target_dir) and
                not os.path.isdir(options.target_dir)
                # fmt: on
            ):
                raise CommandError(
                    "目标路径存在但不是目录，不会继续。"
                )

            # Create a target directory for using with the target option
            target_temp_dir = TempDirectory(kind="target")
            target_temp_dir_path = target_temp_dir.path
            self.enter_context(target_temp_dir)

        global_options = options.global_options or []

        session = self.get_default_session(options)

        target_python = make_target_python(options)
        finder = self._build_package_finder(
            options=options,
            session=session,
            target_python=target_python,
            ignore_requires_python=options.ignore_requires_python,
        )
        build_tracker = self.enter_context(get_build_tracker())

        directory = TempDirectory(
            delete=not options.no_clean,
            kind="install",
            globally_managed=True,
        )

        try:
            reqs = self.get_requirements(args, options, finder, session)
            check_legacy_setup_py_options(options, reqs)

            wheel_cache = WheelCache(options.cache_dir)

            # Only when installing is it permitted to use PEP 660.
            # In other circumstances (pip wheel, pip download) we generate
            # regular (i.e. non editable) metadata and wheels.
            for req in reqs:
                req.permit_editable_wheels = True

            preparer = self.make_requirement_preparer(
                temp_build_dir=directory,
                options=options,
                build_tracker=build_tracker,
                session=session,
                finder=finder,
                use_user_site=options.use_user_site,
                verbosity=self.verbosity,
            )
            resolver = self.make_resolver(
                preparer=preparer,
                finder=finder,
                options=options,
                wheel_cache=wheel_cache,
                use_user_site=options.use_user_site,
                ignore_installed=options.ignore_installed,
                ignore_requires_python=options.ignore_requires_python,
                force_reinstall=options.force_reinstall,
                upgrade_strategy=upgrade_strategy,
                use_pep517=options.use_pep517,
                py_version_info=options.python_version,
            )

            self.trace_basic_info(finder)

            requirement_set = resolver.resolve(
                reqs, check_supported_wheels=not options.target_dir
            )

            if options.json_report_file:
                report = InstallationReport(requirement_set.requirements_to_install)
                if options.json_report_file == "-":
                    print_json(data=report.to_dict())
                else:
                    with open(options.json_report_file, "w", encoding="utf-8") as f:
                        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

            if options.dry_run:
                would_install_items = sorted(
                    (r.metadata["name"], r.metadata["version"])
                    for r in requirement_set.requirements_to_install
                )
                if would_install_items:
                    write_output(
                        "将安装 %s",
                        " ".join("-".join(item) for item in would_install_items),
                    )
                return SUCCESS

            try:
                pip_req = requirement_set.get_requirement("pip")
            except KeyError:
                modifying_pip = False
            else:
                # If we're not replacing an already installed pip,
                # we're not modifying it.
                modifying_pip = pip_req.satisfied_by is None
                if modifying_pip:
                    # Eagerly import this module to avoid crashes. Otherwise, this
                    # module would be imported *after* pip was replaced, resulting in
                    # crashes if the new self_outdated_check module was incompatible
                    # with the rest of pip that's already imported.
                    import pip._internal.self_outdated_check  # noqa: F401
            protect_pip_from_modification_on_windows(modifying_pip=modifying_pip)

            reqs_to_build = [
                r
                for r in requirement_set.requirements.values()
                if should_build_for_install_command(r)
            ]

            _, build_failures = build(
                reqs_to_build,
                wheel_cache=wheel_cache,
                verify=True,
                build_options=[],
                global_options=global_options,
            )

            if build_failures:
                raise InstallationError(
                    "错误：构建某些基于 `pyproject.toml` 的项目的可安装"
                    "whl 文件失败({})".format(
                        ", ".join(r.name for r in build_failures)  # type: ignore
                    )
                )

            to_install = resolver.get_installation_order(requirement_set)

            # Check for conflicts in the package set we're installing.
            conflicts: Optional[ConflictDetails] = None
            should_warn_about_conflicts = (
                not options.ignore_dependencies and options.warn_about_conflicts
            )
            if should_warn_about_conflicts:
                conflicts = self._determine_conflicts(to_install)

            # Don't warn about script install locations if
            # --target or --prefix has been specified
            warn_script_location = options.warn_script_location
            if options.target_dir or options.prefix_path:
                warn_script_location = False

            installed = install_given_reqs(
                to_install,
                global_options,
                root=options.root_path,
                home=target_temp_dir_path,
                prefix=options.prefix_path,
                warn_script_location=warn_script_location,
                use_user_site=options.use_user_site,
                pycompile=options.compile,
            )

            lib_locations = get_lib_location_guesses(
                user=options.use_user_site,
                home=target_temp_dir_path,
                root=options.root_path,
                prefix=options.prefix_path,
                isolated=options.isolated_mode,
            )
            env = get_environment(lib_locations)

            # Display a summary of installed packages, with extra care to
            # display a package name as it was requested by the user.
            installed.sort(key=operator.attrgetter("name"))
            summary = []
            installed_versions = {}
            for distribution in env.iter_all_distributions():
                installed_versions[distribution.canonical_name] = distribution.version
            for package in installed:
                display_name = package.name
                version = installed_versions.get(canonicalize_name(display_name), None)
                if version:
                    text = f"{display_name}-{version}"
                else:
                    text = display_name
                summary.append(text)

            if conflicts is not None:
                self._warn_about_conflicts(
                    conflicts,
                    resolver_variant=self.determine_resolver_variant(options),
                )

            installed_desc = " ".join(summary)
            if installed_desc:
                write_output(
                    "成功安装 %s",
                    installed_desc,
                )
        except OSError as error:
            show_traceback = self.verbosity >= 1

            message = create_os_error_message(
                error,
                show_traceback,
                options.use_user_site,
            )
            logger.error(message, exc_info=show_traceback)

            return ERROR

        if options.target_dir:
            assert target_temp_dir
            self._handle_target_dir(
                options.target_dir, target_temp_dir, options.upgrade
            )
        if options.root_user_action == "warn":
            warn_if_run_as_root()
        return SUCCESS

    def _handle_target_dir(
        self, target_dir: str, target_temp_dir: TempDirectory, upgrade: bool
    ) -> None:
        ensure_dir(target_dir)

        # Checking both purelib and platlib directories for installed
        # packages to be moved to target directory
        lib_dir_list = []

        # Checking both purelib and platlib directories for installed
        # packages to be moved to target directory
        scheme = get_scheme("", home=target_temp_dir.path)
        purelib_dir = scheme.purelib
        platlib_dir = scheme.platlib
        data_dir = scheme.data

        if os.path.exists(purelib_dir):
            lib_dir_list.append(purelib_dir)
        if os.path.exists(platlib_dir) and platlib_dir != purelib_dir:
            lib_dir_list.append(platlib_dir)
        if os.path.exists(data_dir):
            lib_dir_list.append(data_dir)

        for lib_dir in lib_dir_list:
            for item in os.listdir(lib_dir):
                if lib_dir == data_dir:
                    ddir = os.path.join(data_dir, item)
                    if any(s.startswith(ddir) for s in lib_dir_list[:-1]):
                        continue
                target_item_dir = os.path.join(target_dir, item)
                if os.path.exists(target_item_dir):
                    if not upgrade:
                        logger.warning(
                            "目标目录 %s 已经存在。请指定 --upgrade 以强制替换。",
                            target_item_dir,
                        )
                        continue
                    if os.path.islink(target_item_dir):
                        logger.warning(
                            "目标目录 %s 已经存在且是一个链接。"
                            "pip 不会自动替换链接，如果需要替换，请手动删除。",
                            target_item_dir,
                        )
                        continue
                    if os.path.isdir(target_item_dir):
                        shutil.rmtree(target_item_dir)
                    else:
                        os.remove(target_item_dir)

                shutil.move(os.path.join(lib_dir, item), target_item_dir)

    def _determine_conflicts(
        self, to_install: List[InstallRequirement]
    ) -> Optional[ConflictDetails]:
        try:
            return check_install_conflicts(to_install)
        except Exception:
            logger.exception(
                "检查冲突时出错。请在 pip 的问题追踪器上提交问题："
                "https://github.com/pypa/pip/issues/new"
            )
            return None

    def _warn_about_conflicts(
        self, conflict_details: ConflictDetails, resolver_variant: str
    ) -> None:
        package_set, (missing, conflicting) = conflict_details
        if not missing and not conflicting:
            return

        parts: List[str] = []
        if resolver_variant == "legacy":
            parts.append(
                "pip 的遗留依赖解析器在选择软件包时不会考虑依赖冲突。"
                "这种行为是以下依赖冲突的根源。"
            )
        else:
            assert resolver_variant == "resolvelib"
            parts.append(
                "pip 的依赖解析器目前没有考虑所有已安装的软件包。"
                "这种行为是以下依赖冲突的根源。"
            )

        # NOTE: There is some duplication here, with commands/check.py
        for project_name in missing:
            version = package_set[project_name][0]
            for dependency in missing[project_name]:
                message = (
                    f"{project_name} {version} 需要 {dependency[1]}, "
                    "尚未安装。"
                )
                parts.append(message)

        for project_name in conflicting:
            version = package_set[project_name][0]
            for dep_name, dep_version, req in conflicting[project_name]:
                message = (
                    "{name} {version} 需要 {requirement}, 但 {you} 存在 "
                    "{dep_name} {dep_version} 是不兼容的。"
                ).format(
                    name=project_name,
                    version=version,
                    requirement=req,
                    dep_name=dep_name,
                    dep_version=dep_version,
                    you=("you" if resolver_variant == "resolvelib" else "you'll"),
                )
                parts.append(message)

        logger.critical("\n".join(parts))


def get_lib_location_guesses(
    user: bool = False,
    home: Optional[str] = None,
    root: Optional[str] = None,
    isolated: bool = False,
    prefix: Optional[str] = None,
) -> List[str]:
    scheme = get_scheme(
        "",
        user=user,
        home=home,
        root=root,
        isolated=isolated,
        prefix=prefix,
    )
    return [scheme.purelib, scheme.platlib]


def site_packages_writable(root: Optional[str], isolated: bool) -> bool:
    return all(
        test_writable_dir(d)
        for d in set(get_lib_location_guesses(root=root, isolated=isolated))
    )


def decide_user_install(
    use_user_site: Optional[bool],
    prefix_path: Optional[str] = None,
    target_dir: Optional[str] = None,
    root_path: Optional[str] = None,
    isolated_mode: bool = False,
) -> bool:
    """根据输入选项确定是否进行用户安装。

    如果 use_user_site 为 False，则不会进行额外检查。
    如果 use_user_site 为 True，则会检查是否与其他
    选项的兼容性。
    如果 use_user_site 为 None，默认行为取决于环境、
    由其他参数提供。
    """
    # In some cases (config from tox), use_user_site can be set to an integer
    # rather than a bool, which 'use_user_site is False' wouldn't catch.
    if (use_user_site is not None) and (not use_user_site):
        logger.debug("Non-user install by explicit request")
        return False

    if use_user_site:
        if prefix_path:
            raise CommandError(
                "无法同时使用 --user 和 --prefix，因为它们表示不同的安装位置。"
            )
        if virtualenv_no_global():
            raise InstallationError(
                "无法执行 --user 安装。用户的 site-packages 在这个虚拟环境中不可见。"
            )
        logger.debug("User install by explicit request")
        return True

    # If we are here, user installs have not been explicitly requested/avoided
    assert use_user_site is None

    # user install incompatible with --prefix/--target
    if prefix_path or target_dir:
        logger.debug("Non-user install due to --prefix or --target option")
        return False

    # If user installs are not enabled, choose a non-user install
    if not site.ENABLE_USER_SITE:
        logger.debug("Non-user install because user site-packages disabled")
        return False

    # If we have permission for a non-user install, do that,
    # otherwise do a user install.
    if site_packages_writable(root=root_path, isolated=isolated_mode):
        logger.debug("Non-user install because site-packages writeable")
        return False

    logger.info(
        "Defaulting to user installation because normal site-packages "
        "is not writeable"
    )
    return True


def create_os_error_message(
    error: OSError, show_traceback: bool, using_user_site: bool
) -> str:
    """格式化 OSError 的错误信息

    在执行安装命令的过程中，随时都可能发生。
    """
    parts = []

    # Mention the error if we are not going to show a traceback
    parts.append("由于 OSError，无法安装软件包")
    if not show_traceback:
        parts.append(": ")
        parts.append(str(error))
    else:
        parts.append(".")

    # Spilt the error indication from a helper message (if any)
    parts[-1] += "\n"

    # Suggest useful actions to the user:
    #  (1) using user site-packages or (2) verifying the permissions
    if error.errno == errno.EACCES:
        user_option_part = "考虑使用 `--user` 选项"
        permissions_part = "检查权限"

        if not running_under_virtualenv() and not using_user_site:
            parts.extend(
                [
                    user_option_part,
                    " 或 ",
                    permissions_part.lower(),
                ]
            )
        else:
            parts.append(permissions_part)
        parts.append(".\n")

    # Suggest the user to enable Long Paths if path length is
    # more than 260
    if (
        WINDOWS
        and error.errno == errno.ENOENT
        and error.filename
        and len(error.filename) > 260
    ):
        parts.append(
            "提示：此错误可能发生是因为系统未启用 Windows 长路径支持。"
            "你可以在以下网址找到启用该功能的信息："
            "https://pip.pypa.io/warnings/enable-long-paths\n"
        )

    return "".join(parts).strip() + "\n"
