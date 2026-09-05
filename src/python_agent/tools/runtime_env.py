"""发现并只读挂载受信任的宿主开发工具链。

Bubblewrap 默认只挂载基础系统目录和 workspace，因此用户宿主环境中的 Conda、NVM
或 ``/opt`` 工具链不会自动出现在 Shell 内。本模块只根据 Agent 启动时的宿主环境发现
少量明确的运行时根目录，并以只读方式挂载它们；它不会挂载整个 ``/home`` 或宿主根目录，
也不会把宿主环境变量整体传入沙箱。
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_COMMANDS = (
    "conda",
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "cmake",
    "java",
    "javac",
    "node",
    "npm",
    "npx",
    "gcc",
    "g++",
    "make",
    "ninja",
    "cargo",
    "rustc",
    "go",
    "ruby",
    "mvn",
    "gradle",
)
_SYSTEM_ROOTS = tuple(Path(value) for value in ("/usr", "/bin", "/sbin", "/lib", "/lib64"))
_CONDA_NAMES = frozenset(
    {
        "miniconda",
        "miniconda2",
        "miniconda3",
        "anaconda",
        "anaconda2",
        "anaconda3",
        "mambaforge",
        "miniforge",
        "miniforge3",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """一次 Shell 启动可使用的只读 runtime 挂载和最小环境变量。"""

    mounts: tuple[Path, ...] = ()
    path_entries: tuple[Path, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()

    @classmethod
    def discover(
        cls,
        *,
        commands: Iterable[str] = _DEFAULT_COMMANDS,
        include_current_python: bool = True,
    ) -> RuntimeEnvironment:
        """从当前宿主进程发现 Conda/NVM/opt 工具链，不执行外部命令。"""

        executable_paths: list[Path] = []
        if include_current_python:
            executable_paths.append(Path(sys.executable))
        for command in commands:
            executable = shutil.which(command)
            if executable:
                executable_paths.append(Path(executable))

        mounts: list[Path] = []
        path_entries: list[Path] = []
        conda_roots: list[Path] = []
        configured_conda_prefix = cls._env_path("CONDA_PREFIX")
        conda_exe = cls._env_path("CONDA_EXE")
        conda_python = cls._env_path("CONDA_PYTHON_EXE")
        conda_prefixes: list[Path] = []
        for candidate in (Path(sys.prefix), configured_conda_prefix):
            if candidate is not None:
                prefix = cls._conda_prefix(candidate)
                if prefix is not None:
                    cls._add_path(conda_prefixes, prefix)
        for candidate in (Path(sys.prefix), configured_conda_prefix, conda_exe, conda_python):
            if candidate is not None:
                root = cls._conda_root(candidate)
                if root is not None:
                    conda_roots.append(root)

        for path in executable_paths:
            resolved = path.expanduser().resolve()
            if cls._inside_system_root(resolved):
                if cls._uses_alternatives(path):
                    cls._add_mount(mounts, Path("/etc/alternatives"))
                continue
            conda_root = cls._conda_root(resolved)
            if conda_root is not None:
                conda_roots.append(conda_root)
                prefix = cls._conda_prefix(resolved)
                if prefix is not None:
                    cls._add_path(conda_prefixes, prefix)
                continue
            nvm_root = cls._nvm_version_root(resolved)
            if nvm_root is not None:
                cls._add_mount(mounts, nvm_root)
                cls._add_path(path_entries, nvm_root / "bin")
                continue
            generic_root = cls._generic_runtime_root(resolved)
            if generic_root is not None:
                cls._add_mount(mounts, generic_root)
                cls._add_path(path_entries, path.parent)

        for prefix in conda_prefixes:
            root = cls._conda_root(prefix)
            if root is not None:
                conda_roots.append(root)
        active_conda_prefix = conda_prefixes[0] if conda_prefixes else configured_conda_prefix
        for prefix in conda_prefixes:
            cls._add_mount(mounts, prefix)
            cls._add_path(path_entries, prefix / "bin")
        for root in conda_roots:
            cls._add_mount(mounts, root)
            cls._add_path(path_entries, root / "bin")

        environment: list[tuple[str, str]] = [
            # 包管理器的缓存和全局前缀必须落在沙箱临时目录或 workspace，不能写宿主 Home。
            ("CONDA_PKGS_DIRS", "/tmp/conda-pkgs"),
            ("PIP_CACHE_DIR", "/tmp/pip-cache"),
            ("npm_config_cache", "/tmp/npm-cache"),
            ("npm_config_userconfig", "/tmp/npmrc"),
            ("npm_config_prefix", "/workspace/.npm-global"),
            ("CARGO_HOME", "/workspace/.cargo"),
            ("GOPATH", "/workspace/.go"),
        ]
        for name, value in (
            ("CONDA_EXE", conda_exe),
            ("CONDA_PYTHON_EXE", conda_python),
            ("CONDA_PREFIX", active_conda_prefix),
        ):
            if value is not None:
                environment.append((name, str(value)))
        default_env = active_conda_prefix.name if active_conda_prefix is not None else None
        if default_env is not None:
            environment.append(("CONDA_DEFAULT_ENV", default_env))
        for root in conda_roots:
            profile_script = root / "etc" / "profile.d" / "conda.sh"
            if profile_script.is_file():
                # BASH_ENV 只引用已只读挂载的 Conda 初始化脚本，支持在沙箱内使用
                # ``conda activate``；它不会把宿主 Shell 配置或其他环境变量整体带入。
                environment.append(("BASH_ENV", str(profile_script)))
                break
        env_shlvl = os.environ.get("CONDA_SHLVL")
        if env_shlvl and "=" not in env_shlvl and "\x00" not in env_shlvl:
            environment.append(("CONDA_SHLVL", env_shlvl))

        # 稳定顺序便于 capability fingerprint 和测试结果复现。
        return cls(
            mounts=tuple(sorted(mounts, key=str)),
            path_entries=tuple(path_entries),
            environment=tuple(environment),
        )

    @staticmethod
    def _env_path(name: str) -> Path | None:
        value = os.environ.get(name)
        if not value or "\x00" in value:
            return None
        return Path(value).expanduser()

    @staticmethod
    def _inside_system_root(path: Path) -> bool:
        return any(path == root or root in path.parents for root in _SYSTEM_ROOTS)

    @classmethod
    def _conda_root(cls, path: Path) -> Path | None:
        """识别 Conda base root；不会把任意 Home 子目录当成 runtime。"""

        candidate = path.expanduser().resolve()
        if candidate.is_file():
            candidate = candidate.parent
        for ancestor in (candidate, *candidate.parents):
            if ancestor.name.lower() in _CONDA_NAMES or (ancestor / "condabin").is_dir():
                return cls._safe_directory(ancestor)
        return None

    @classmethod
    def _conda_prefix(cls, path: Path) -> Path | None:
        """识别当前 Python/Conda 环境 prefix，而不是只识别 base root。"""

        candidate = path.expanduser().resolve()
        if candidate.is_file():
            candidate = candidate.parent
        for ancestor in (candidate, *candidate.parents):
            if (ancestor / "conda-meta").is_dir():
                return cls._safe_directory(ancestor)
        return None

    @staticmethod
    def _nvm_version_root(path: Path) -> Path | None:
        parts = path.expanduser().resolve().parts
        try:
            index = parts.index(".nvm")
            if parts[index + 1 : index + 4][0:2] != ("versions", "node"):
                return None
            if len(parts) <= index + 3:
                return None
            root = Path(*parts[: index + 4])
        except (ValueError, IndexError):
            return None
        return RuntimeEnvironment._safe_directory(root)

    @classmethod
    def _generic_runtime_root(cls, path: Path) -> Path | None:
        """只接受 Home 用户目录的单层 bin 或 /opt 下的具体工具目录。"""

        home = Path.home().expanduser().resolve()
        try:
            relative = path.relative_to(home)
        except ValueError:
            relative = None
        if relative is not None and relative.parts:
            parent = path.parent
            if parent != home:
                return cls._safe_directory(parent)
            return None
        opt = Path("/opt")
        try:
            relative_opt = path.relative_to(opt)
        except ValueError:
            relative_opt = None
        if relative_opt is not None and relative_opt.parts:
            return cls._safe_directory(opt / relative_opt.parts[0])
        return None

    @staticmethod
    def _safe_directory(path: Path) -> Path | None:
        resolved = path.expanduser().resolve()
        if resolved == Path("/") or not resolved.is_dir():
            return None
        if resolved in {Path.home().expanduser().resolve(), Path("/home"), Path("/root")}:
            return None
        return resolved

    @staticmethod
    def _uses_alternatives(path: Path) -> bool:
        current = path.expanduser()
        for _ in range(8):
            if not current.is_symlink():
                return False
            raw_target = Path(os.readlink(current))
            current = raw_target if raw_target.is_absolute() else current.parent / raw_target
            if current == Path("/etc/alternatives") or Path("/etc/alternatives") in current.parents:
                return True
        return False

    @staticmethod
    def _add_mount(values: list[Path], path: Path | None) -> None:
        if path is None:
            return
        resolved = path.expanduser().resolve()
        if not resolved.is_dir() or resolved == Path("/"):
            return
        if any(item == resolved or item in resolved.parents for item in values):
            return
        values[:] = [item for item in values if resolved not in item.parents]
        values.append(resolved)

    @staticmethod
    def _add_path(values: list[Path], path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved.is_dir() and resolved not in values:
            values.append(resolved)


__all__ = ["RuntimeEnvironment"]
