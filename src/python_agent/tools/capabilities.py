"""能力、权限等级和网络模式的统一定义。

工具名不是安全边界：同一个工具在不同调用参数、权限 Profile 或网络 Scope 下可能
拥有不同的实际能力。本模块提供一组小而稳定的值对象，供 Tool Gateway、Sandbox
和恢复指纹共同使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

PermissionLevelName = Literal["L0", "L1", "L2", "L3", "L4"]
NetworkModeName = Literal["disabled", "setup-approved", "allowlist", "full"]
CapabilityName = str


class PermissionLevel(str, Enum):
    """SANBOX 文档定义的单调权限等级。"""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    READ_ONLY = "L0"
    WORKSPACE_WRITE = "L1"
    WORKSPACE_NETWORK = "L2"
    CONTAINER_ADMIN = "L3"
    HOST_ADMIN = "L4"


class NetworkMode(str, Enum):
    """网络权限；它与 workspace 读写权限保持独立。"""

    DISABLED = "disabled"
    SETUP_APPROVED = "setup-approved"
    ALLOWLIST = "allowlist"
    FULL = "full"
    OFF = "disabled"
    ON = "full"


class Capability(str, Enum):
    """工具和 Profile 可以声明的稳定 Capability 名称。"""

    FILESYSTEM_WORKSPACE_READ = "filesystem.workspace.read"
    FILESYSTEM_WORKSPACE_WRITE = "filesystem.workspace.write"
    NETWORK_INTERNET = "network.internet"
    PROCESS_READONLY = "process.readonly"
    PROCESS_EXECUTE = "process.execute"
    CONTAINER_EXECUTE = "container.execute"
    CONTAINER_ADMIN = "container.admin"
    HOST_ADMIN = "host.admin"
    EXTERNAL_READ = "external.read"
    EXTERNAL_WRITE = "external.write"
    EXTERNAL_DELETE = "external.delete"


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    """权限等级与独立网络模式组成的一次不可变执行 Profile。"""

    level: PermissionLevel
    network_mode: NetworkMode = NetworkMode.DISABLED

    @classmethod
    def from_values(
        cls,
        permission_mode: str,
        *,
        level: str | PermissionLevel | None = None,
        network_mode: str | NetworkMode | None = None,
    ) -> PermissionProfile:
        """从旧 filesystem 模式和新字段构造 Profile。"""

        resolved_network = coerce_network_mode(network_mode) or NetworkMode.DISABLED
        return cls(permission_level_for(permission_mode, level), resolved_network)

    @property
    def capabilities(self) -> frozenset[str]:
        """返回本 Profile 可授予工具 Gateway 的 Capability 快照。"""

        return capabilities_for_level(self.level, network_mode=self.network_mode)


# 也提供字符串常量，方便不希望依赖 Enum 的扩展工具声明能力。
FILESYSTEM_WORKSPACE_READ = Capability.FILESYSTEM_WORKSPACE_READ.value
FILESYSTEM_WORKSPACE_WRITE = Capability.FILESYSTEM_WORKSPACE_WRITE.value
NETWORK_INTERNET = Capability.NETWORK_INTERNET.value
PROCESS_READONLY = Capability.PROCESS_READONLY.value
PROCESS_EXECUTE = Capability.PROCESS_EXECUTE.value
CONTAINER_EXECUTE = Capability.CONTAINER_EXECUTE.value
CONTAINER_ADMIN = Capability.CONTAINER_ADMIN.value
HOST_ADMIN = Capability.HOST_ADMIN.value
EXTERNAL_READ = Capability.EXTERNAL_READ.value
EXTERNAL_WRITE = Capability.EXTERNAL_WRITE.value
EXTERNAL_DELETE = Capability.EXTERNAL_DELETE.value


_LEVEL_CAPABILITIES: dict[PermissionLevel, frozenset[str]] = {
    PermissionLevel.L0: frozenset(
        {
            FILESYSTEM_WORKSPACE_READ,
            PROCESS_READONLY,
        }
    ),
    PermissionLevel.L1: frozenset(
        {
            FILESYSTEM_WORKSPACE_READ,
            FILESYSTEM_WORKSPACE_WRITE,
            PROCESS_READONLY,
            PROCESS_EXECUTE,
        }
    ),
    PermissionLevel.L2: frozenset(
        {
            FILESYSTEM_WORKSPACE_READ,
            FILESYSTEM_WORKSPACE_WRITE,
            PROCESS_READONLY,
            PROCESS_EXECUTE,
            NETWORK_INTERNET,
        }
    ),
    PermissionLevel.L3: frozenset(
        {
            FILESYSTEM_WORKSPACE_READ,
            FILESYSTEM_WORKSPACE_WRITE,
            PROCESS_READONLY,
            PROCESS_EXECUTE,
            NETWORK_INTERNET,
            CONTAINER_EXECUTE,
            CONTAINER_ADMIN,
        }
    ),
    PermissionLevel.L4: frozenset(
        {
            FILESYSTEM_WORKSPACE_READ,
            FILESYSTEM_WORKSPACE_WRITE,
            PROCESS_READONLY,
            PROCESS_EXECUTE,
            NETWORK_INTERNET,
            CONTAINER_EXECUTE,
            CONTAINER_ADMIN,
            HOST_ADMIN,
        }
    ),
}


def coerce_permission_level(value: str | PermissionLevel) -> PermissionLevel:
    """把外部配置转换成严格的权限等级。"""

    if isinstance(value, PermissionLevel):
        return value
    try:
        return PermissionLevel(value)
    except ValueError as exc:
        raise ValueError(f"unknown permission level: {value!r}") from exc


def coerce_network_mode(value: str | NetworkMode | None) -> NetworkMode | None:
    """把可选网络模式转换成枚举；None 表示旧 API 的兼容模式。"""

    if value is None or isinstance(value, NetworkMode):
        return value
    try:
        return NetworkMode(value)
    except ValueError as exc:
        raise ValueError(f"unknown network mode: {value!r}") from exc


def permission_level_for(
    permission_mode: str,
    explicit: str | PermissionLevel | None = None,
) -> PermissionLevel:
    """根据旧的 filesystem 模式和可选显式等级取得 Profile 等级。

    旧调用方只传 ``read-only``/``workspace-write`` 时仍分别映射为 L0/L1；显式等级
    用于新 API，实际 Capability 检查会继续单独考虑 network_mode。
    """

    if explicit is not None:
        return coerce_permission_level(explicit)
    if permission_mode == "read-only":
        return PermissionLevel.L0
    if permission_mode == "workspace-write":
        return PermissionLevel.L1
    raise ValueError(f"unknown filesystem permission mode: {permission_mode!r}")


def capabilities_for_level(
    level: str | PermissionLevel,
    *,
    network_mode: str | NetworkMode | None = None,
) -> frozenset[str]:
    """返回一个 Profile 的 Capability 快照。

    ``network_mode=disabled`` 会从等级快照中移除网络能力；这样网络打开不会隐式改变
    workspace 或 system 能力，符合文档中“网络、文件、系统权限相互独立”的原则。
    """

    resolved_level = coerce_permission_level(level)
    resolved_network = coerce_network_mode(network_mode)
    capabilities = set(_LEVEL_CAPABILITIES[resolved_level])
    if resolved_network == NetworkMode.DISABLED:
        capabilities.discard(NETWORK_INTERNET)
    elif resolved_network is not None and is_at_least(resolved_level, PermissionLevel.L2):
        capabilities.add(NETWORK_INTERNET)
    else:
        # L0/L1 的最终权限矩阵明确禁止网络，即使调用方误把 network_mode 打开，也不能
        # 通过独立网络字段越过基础权限等级。
        capabilities.discard(NETWORK_INTERNET)
    return frozenset(capabilities)


def is_at_least(
    actual: str | PermissionLevel,
    required: str | PermissionLevel,
) -> bool:
    """判断权限等级是否满足另一个等级的全部基础权限。"""

    order = {
        PermissionLevel.L0: 0,
        PermissionLevel.L1: 1,
        PermissionLevel.L2: 2,
        PermissionLevel.L3: 3,
        PermissionLevel.L4: 4,
    }
    return order[coerce_permission_level(actual)] >= order[coerce_permission_level(required)]


__all__ = [
    "Capability",
    "CapabilityName",
    "CONTAINER_ADMIN",
    "CONTAINER_EXECUTE",
    "EXTERNAL_DELETE",
    "EXTERNAL_READ",
    "EXTERNAL_WRITE",
    "FILESYSTEM_WORKSPACE_READ",
    "FILESYSTEM_WORKSPACE_WRITE",
    "HOST_ADMIN",
    "NETWORK_INTERNET",
    "NetworkMode",
    "NetworkModeName",
    "PermissionLevel",
    "PermissionLevelName",
    "PermissionProfile",
    "PROCESS_EXECUTE",
    "PROCESS_READONLY",
    "capabilities_for_level",
    "coerce_network_mode",
    "coerce_permission_level",
    "is_at_least",
    "permission_level_for",
]
