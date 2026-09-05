"""工具执行的 Pre、Execute、Post 策略及其共享上下文。"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from python_agent.approval.service import ApprovalRequest, ApprovalService
from python_agent.errors import ToolError, ToolValidationError
from python_agent.hooks.waterfall import Waterfall
from python_agent.llm.types import ToolCall
from python_agent.tools.builtins._paths import safe_path, workspace_root
from python_agent.tools.capabilities import (
    FILESYSTEM_WORKSPACE_READ,
    HOST_ADMIN,
    NETWORK_INTERNET,
    PROCESS_READONLY,
    NetworkMode,
    coerce_network_mode,
)
from python_agent.tools.command_risk import CommandRiskAnalyzer
from python_agent.tools.definition import ToolCapabilities, ToolDefinition
from python_agent.tools.delete_policy import DeletePolicyEngine
from python_agent.tools.serialization import JsonSerializationError, to_json_safe
from python_agent.tools.types import ToolContext, ToolResult


@dataclass(slots=True)
class ToolInvocation:
    """把一次 ToolCall、工具定义和执行上下文绑定在一起。"""

    call: ToolCall
    tool: ToolDefinition
    context: ToolContext


@dataclass(slots=True)
class ToolResultEnvelope:
    """供 Post 策略继续加工的工具结果包。"""

    invocation: ToolInvocation
    result: ToolResult


PreHandler = Callable[
    [ToolInvocation, Callable[[ToolInvocation], Awaitable[ToolResult | None]]],
    Awaitable[ToolResult | None],
]
ExecuteHandler = Callable[
    [ToolInvocation, Callable[[ToolInvocation], Awaitable[Any]]], Awaitable[Any]
]
PostHandler = Callable[
    [ToolResultEnvelope, Callable[[ToolResultEnvelope], Awaitable[ToolResult]]],
    Awaitable[ToolResult],
]


def _matches_type(value: Any, expected: str) -> bool:
    """检查当前阶段需要的 JSON Schema 基础类型。"""

    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_arguments(tool: ToolDefinition, arguments: dict[str, Any]) -> None:
    """递归校验工具实际使用的 JSON Schema 关键字。

    外部模型参数在任何路径解析或副作用之前经过类型、范围、长度、枚举、数组 items、
    object required/properties 和 additionalProperties 校验。未知注释关键字仍可保留，
    但已声明的约束绝不能只展示给模型而不在运行时执行。
    """

    schema = tool.parameters
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise ToolValidationError(f"tool {tool.name} parameters must describe an object")
    _validate_schema_value(arguments, schema, path=f"arguments for {tool.name}")


def _validate_schema_value(value: Any, schema: dict[str, Any], *, path: str) -> None:
    """校验一个 JSON 值及其递归子结构。"""

    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        raise ToolValidationError(f"{path} must be {expected}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ToolValidationError(f"{path} must be one of {enum!r}")
    if "const" in schema and value != schema["const"]:
        raise ToolValidationError(f"{path} must equal {schema['const']!r}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or not all(isinstance(name, str) for name in required)
        ):
            raise ToolValidationError(f"{path} has an invalid object schema")
        missing = [name for name in required if name not in value]
        if missing:
            raise ToolValidationError(f"{path} is missing required fields: {', '.join(missing)}")
        additional = schema.get("additionalProperties", True)
        unknown = sorted(set(value) - set(properties))
        if additional is False and unknown:
            raise ToolValidationError(f"{path} has unknown fields: {', '.join(unknown)}")
        for name, item in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                _validate_schema_value(item, child_schema, path=f"{path}.{name}")
            elif isinstance(additional, dict):
                _validate_schema_value(item, additional, path=f"{path}.{name}")
        return

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise ToolValidationError(f"{path} must contain at least {minimum_items} items")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise ToolValidationError(f"{path} must contain at most {maximum_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, path=f"{path}[{index}]")
        return

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise ToolValidationError(f"{path} must have length >= {minimum_length}")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise ToolValidationError(f"{path} must have length <= {maximum_length}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value)
            except re.error as exc:
                raise ToolValidationError(f"{path} has invalid schema pattern: {exc}") from exc
            if matched is None:
                raise ToolValidationError(f"{path} does not match required pattern")
        return

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        exclusive_minimum = schema.get("exclusiveMinimum")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ToolValidationError(f"{path} must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ToolValidationError(f"{path} must be <= {maximum}")
        if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
            raise ToolValidationError(f"{path} must be > {exclusive_minimum}")
        if isinstance(exclusive_maximum, (int, float)) and value >= exclusive_maximum:
            raise ToolValidationError(f"{path} must be < {exclusive_maximum}")


def _error(invocation: ToolInvocation, message: str) -> ToolResult:
    """生成不执行工具主体的错误结果。"""

    return ToolResult(
        call_id=invocation.call.id,
        name=invocation.call.name,
        content=message,
        is_error=True,
    )


class ArgumentValidationPolicy:
    """Pre 策略：在任何工具副作用之前校验参数。"""

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        """校验成功才向后委托；失败直接构造错误结果并短路执行链。"""

        try:
            validate_arguments(invocation.tool, invocation.call.arguments)
        except ToolValidationError as exc:
            return _error(invocation, f"{type(exc).__name__}: {exc}")
        return await next_handler(invocation)


def _patch_paths(patch: str) -> list[str]:
    """提取 apply_patch 常见格式中的目标路径。"""

    paths: list[str] = []
    for line in patch.splitlines():
        for prefix in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
            if line.startswith(prefix):
                paths.append(line.removeprefix(prefix).strip())
        if line.startswith("+++ b/"):
            paths.append(line.removeprefix("+++ b/").strip())
    return paths


class WorkspacePathPolicy:
    """Pre 策略：提前验证所有工具路径仍在 workspace 内。"""

    _path_tools = {"read_file", "write_file", "list_files", "search_text"}

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        """检查普通 path、Bash cwd 和 patch 中的所有目标路径。"""

        try:
            arguments = invocation.call.arguments
            if invocation.tool.name in self._path_tools and "path" in arguments:
                safe_path(str(arguments["path"]), invocation.context)
            if invocation.tool.name == "bash" and "cwd" in arguments:
                safe_path(str(arguments["cwd"]), invocation.context)
            if invocation.tool.name == "apply_patch":
                for path in _patch_paths(str(arguments.get("patch", ""))):
                    safe_path(path, invocation.context)
        except (ToolError, ValueError) as exc:
            return _error(invocation, f"{type(exc).__name__}: {exc}")
        return await next_handler(invocation)


class PermissionPolicy:
    """Pre 策略：阻止只读 Agent 使用写入或 Shell 工具。"""

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        """只在 workspace-write 模式下放行明确声明会产生副作用的工具。

        工具名称不能作为安全边界：调用方可以注册任意工具。因此缺少或格式错误的能力声明
        会采用 ``ToolCapabilities`` 的危险默认值，并在只读模式下被拒绝。
        """

        raw_capabilities = getattr(invocation.tool, "capabilities", None)
        capabilities = (
            raw_capabilities
            if isinstance(raw_capabilities, ToolCapabilities)
            else ToolCapabilities()
        )
        required = capabilities.declared_capabilities()
        if invocation.tool.name == "bash" and (
            CommandRiskAnalyzer.is_explicit_read_only(
                str(invocation.call.arguments.get("command", ""))
            )
        ):
            # 只读 Shell 只需要 L0 的 workspace read/process readonly；写 Shell 仍然按
            # 工具的静态危险能力要求 L1 workspace write 和 process.execute。
            required = frozenset({FILESYSTEM_WORKSPACE_READ, PROCESS_READONLY})
        granted = invocation.context.effective_capabilities()
        missing = sorted(required - granted)
        if HOST_ADMIN in required and not (
            invocation.context.metadata.get("host_admin_executor", False)
            and invocation.context.metadata.get("host_admin_approved", False)
        ):
            return _error(
                invocation,
                "permission denied: host.admin is not available to ordinary Agent tools",
            )
        if missing:
            return _error(
                invocation,
                "permission denied: "
                f"{invocation.tool.name} requires capabilities {', '.join(sorted(required))}; "
                f"missing {', '.join(missing)}",
            )
        return await next_handler(invocation)


class NetworkPolicy:
    """Pre 策略：为声明 network.internet 的非 Shell 工具管理短期网络 Scope。"""

    @staticmethod
    async def _approve_scope(invocation: ToolInvocation, reason: str) -> bool:
        service = invocation.context.approval_service
        if service is None:
            return False
        request = ApprovalRequest(
            call_id=invocation.call.id,
            tool_name=invocation.tool.name,
            arguments=invocation.call.arguments,
            reason=reason,
        )
        try:
            approved = bool(await service.request(request))
        except Exception:
            return False
        if approved:
            invocation.context.metadata["network_scope_approved"] = True
            skipped = invocation.context.metadata.setdefault(
                "__network_policy_skip_approval__", set()
            )
            if isinstance(skipped, set):
                skipped.add(str(invocation.call.id))
            approved_tools = invocation.context.metadata.setdefault(
                "__approved_tool_names__", set()
            )
            if isinstance(approved_tools, set):
                approved_tools.add(invocation.tool.name)
        return approved

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        raw_capabilities = getattr(invocation.tool, "capabilities", None)
        capabilities = (
            raw_capabilities
            if isinstance(raw_capabilities, ToolCapabilities)
            else ToolCapabilities()
        )
        if NETWORK_INTERNET not in capabilities.declared_capabilities():
            return await next_handler(invocation)
        mode = coerce_network_mode(invocation.context.network_mode)
        if mode in {None, NetworkMode.DISABLED}:
            return _error(
                invocation,
                "network blocked: this Agent is running with network=disabled",
            )
        if mode == NetworkMode.SETUP_APPROVED and not invocation.context.metadata.get(
            "setup_runner", False
        ):
            return _error(
                invocation,
                "network blocked: setup-approved is only available inside a setup runner",
            )
        if mode == NetworkMode.ALLOWLIST:
            return _error(
                invocation,
                "network unavailable: allowlist mode requires a broker-backed executor",
            )
        if not invocation.context.metadata.get("network_scope_approved", False):
            if not await self._approve_scope(
                invocation,
                f"network Scope required for {mode.value} tool {invocation.tool.name}",
            ):
                return _error(
                    invocation,
                    f"network blocked: {mode.value} requires an explicit approved scope",
                )
        return await next_handler(invocation)


class ModificationRiskPolicy:
    """Pre 策略：对一次补丁大量覆盖已有文件要求额外审批。"""

    _bulk_update_threshold = 5

    @staticmethod
    async def _request_approval(invocation: ToolInvocation, paths: list[str]) -> bool:
        service = invocation.context.approval_service
        if service is None:
            return False
        request = ApprovalRequest(
            call_id=invocation.call.id,
            tool_name=invocation.tool.name,
            arguments={"paths": paths, "operation": "bulk-overwrite"},
            reason=(f"patch overwrites {len(paths)} existing files; explicit approval is required"),
        )
        try:
            return bool(await service.request(request))
        except Exception:
            return False

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        if invocation.tool.name != "apply_patch":
            return await next_handler(invocation)
        paths = [
            line.removeprefix("*** Update File: ").strip()
            for line in str(invocation.call.arguments.get("patch", "")).splitlines()
            if line.startswith("*** Update File: ")
        ]
        paths = sorted(set(paths))
        if len(paths) < self._bulk_update_threshold:
            return await next_handler(invocation)
        if not await self._request_approval(invocation, paths):
            return _error(
                invocation,
                "approval denied: bulk overwrite of existing workspace files",
            )
        return await next_handler(invocation)


class CommandRiskPolicy:
    """Pre 策略：阻止 Shell 绕过独立的网络和宿主机权限边界。"""

    @staticmethod
    async def _approve_scope(invocation: ToolInvocation, reason: str) -> bool:
        """为一次具体网络命令请求短生命周期批准。"""

        service = invocation.context.approval_service
        if service is None:
            return False
        request = ApprovalRequest(
            call_id=invocation.call.id,
            tool_name=invocation.tool.name,
            arguments=invocation.call.arguments,
            reason=reason,
        )
        try:
            approved = bool(await service.request(request))
        except Exception:
            return False
        if approved:
            invocation.context.metadata["network_scope_approved"] = True
            skipped = invocation.context.metadata.setdefault(
                "__network_policy_skip_approval__", set()
            )
            if isinstance(skipped, set):
                skipped.add(str(invocation.call.id))
            approved_tools = invocation.context.metadata.setdefault(
                "__approved_tool_names__", set()
            )
            if isinstance(approved_tools, set):
                approved_tools.add(invocation.tool.name)
        return approved

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        """分析 Bash 命令；网络 Scope 不满足时在进入 Sandbox 前拒绝。"""

        if invocation.tool.name not in {"bash", "container_exec", "docker_exec"}:
            return await next_handler(invocation)
        command = str(invocation.call.arguments.get("command", ""))
        risk = CommandRiskAnalyzer.analyze(command)
        if risk.reserved_path:
            return _error(
                invocation,
                "permission denied: agent infrastructure paths are not available to Shell",
            )
        if risk.host_admin and invocation.tool.name == "bash":
            return _error(
                invocation,
                "host-admin command denied: ordinary Agent tools cannot modify the host; "
                "use the dedicated L4 workflow",
            )
        if (
            invocation.tool.name == "bash"
            and risk.read_only
            and CommandRiskAnalyzer.is_explicit_read_only(command)
        ):
            skipped = invocation.context.metadata.setdefault(
                "__readonly_bash_skip_approval__", set()
            )
            if isinstance(skipped, set):
                skipped.add(str(invocation.call.id))
        if risk.network_required:
            mode = coerce_network_mode(invocation.context.network_mode)
            if mode in {None, NetworkMode.DISABLED}:
                return _error(
                    invocation,
                    "network blocked: this Agent is running with network=disabled",
                )
            if mode == NetworkMode.SETUP_APPROVED and not invocation.context.metadata.get(
                "setup_runner", False
            ):
                return _error(
                    invocation,
                    "network blocked: setup-approved is only available inside a setup runner",
                )
            if mode == NetworkMode.ALLOWLIST:
                return _error(
                    invocation,
                    "network unavailable: allowlist mode requires a broker-backed executor",
                )
            if not invocation.context.metadata.get("network_scope_approved", False):
                approved = await self._approve_scope(
                    invocation,
                    f"network Scope required for {mode.value} command",
                )
                if not approved:
                    return _error(
                        invocation,
                        f"network blocked: {mode.value} requires an explicit approved scope",
                    )
        return await next_handler(invocation)


class DeletePolicy:
    """Pre 策略：让文件、补丁和 Shell 删除共享 DeletePolicyEngine。"""

    def __init__(self, engine: DeletePolicyEngine | None = None) -> None:
        self.engine = engine

    def _engine_for(self, context: ToolContext) -> DeletePolicyEngine:
        """优先使用调用方绑定的引擎，否则按本次 workspace 创建受控实例。"""

        if self.engine is not None:
            return self.engine
        candidate = context.delete_policy
        if isinstance(candidate, DeletePolicyEngine):
            return candidate
        return DeletePolicyEngine(manifest=context.task_manifest)

    @staticmethod
    def _decision_store(context: ToolContext) -> dict[str, Any]:
        store = context.metadata.setdefault("__delete_decisions__", {})
        return store if isinstance(store, dict) else {}

    @staticmethod
    def _skip_approval_store(context: ToolContext) -> set[str]:
        store = context.metadata.setdefault("__delete_policy_skip_approval__", set())
        if not isinstance(store, set):
            store = set()
            context.metadata["__delete_policy_skip_approval__"] = store
        return store

    async def _authorize(
        self,
        invocation: ToolInvocation,
        paths: str | Path | Iterable[str | Path],
        *,
        recursive: bool = False,
        batch: bool = False,
        source: str,
    ) -> ToolResult | None:
        engine = self._engine_for(invocation.context)
        decision = await engine.authorize(
            paths,
            context=invocation.context,
            call_id=invocation.call.id,
            tool_name=invocation.tool.name,
            arguments=invocation.call.arguments,
            recursive=recursive,
            batch=batch,
            source=source,
        )
        if not decision.allowed:
            return _error(invocation, f"delete denied: {decision.reason}")
        decisions = self._decision_store(invocation.context)
        for path in decision.paths:
            decisions[str(path.expanduser().resolve())] = decision
        if invocation.tool.name != "container_exec" and invocation.tool.name != "docker_exec":
            self._skip_approval_store(invocation.context).add(str(invocation.call.id))
        if invocation.tool.name in {"bash"}:
            approved_tools = invocation.context.metadata.setdefault(
                "__approved_tool_names__", set()
            )
            if isinstance(approved_tools, set):
                approved_tools.add(invocation.tool.name)
        return None

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        """只对明确识别的删除操作做决策，普通读写工具直接继续。"""

        name = invocation.tool.name
        arguments = invocation.call.arguments
        if name == "delete_file":
            result = await self._authorize(
                invocation,
                str(arguments.get("path", "")),
                source="delete_file",
            )
            return result if result is not None else await next_handler(invocation)
        if name == "delete_directory":
            result = await self._authorize(
                invocation,
                str(arguments.get("path", "")),
                recursive=True,
                batch=True,
                source="delete_directory",
            )
            return result if result is not None else await next_handler(invocation)
        if name == "apply_patch":
            patch = str(arguments.get("patch", ""))
            paths = [
                line.removeprefix("*** Delete File: ").strip()
                for line in patch.splitlines()
                if line.startswith("*** Delete File: ")
            ]
            if paths:
                result = await self._authorize(
                    invocation,
                    paths,
                    batch=len(paths) > 1,
                    source="apply_patch",
                )
                return result if result is not None else await next_handler(invocation)
        if name in {"bash", "container_exec", "docker_exec"}:
            risk = CommandRiskAnalyzer.analyze(str(arguments.get("command", "")))
            if risk.unknown_delete_scope:
                return _error(
                    invocation,
                    "delete denied: Shell deletion target scope is not fully determinable; "
                    "use delete_file/delete_directory with explicit paths",
                )
            if risk.delete_paths:
                try:
                    command_cwd = safe_path(str(arguments.get("cwd", ".")), invocation.context)
                except (ToolError, ValueError) as exc:
                    return _error(invocation, f"{type(exc).__name__}: {exc}")
                resolved_paths = [
                    (
                        str(workspace_root(invocation.context) / Path(*Path(path).parts[2:]))
                        if name in {"container_exec", "docker_exec"}
                        and Path(path).parts[:2] == ("/", "workspace")
                        else path
                        if Path(path).is_absolute()
                        else str(command_cwd / path)
                    )
                    for path in risk.delete_paths
                ]
                result = await self._authorize(
                    invocation,
                    resolved_paths,
                    recursive=risk.recursive,
                    batch=risk.batch,
                    source="bash",
                )
                return result if result is not None else await next_handler(invocation)
        return await next_handler(invocation)


class ApprovalPolicy:
    """Pre 策略：对高风险工具要求显式批准，缺少服务时默认拒绝。"""

    def __init__(
        self,
        approval_service: ApprovalService | None = None,
        required_tools: set[str] | frozenset[str] = frozenset({"bash"}),
    ) -> None:
        """保存审批服务和工具白名单；空集合表示不要求额外审批。"""

        self.approval_service = approval_service
        self.required_tools = frozenset(required_tools)

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        """构造最小审批请求；审批失败或服务异常都按拒绝处理。"""

        skipped = invocation.context.metadata.get("__delete_policy_skip_approval__", set())
        if isinstance(skipped, set) and str(invocation.call.id) in skipped:
            return await next_handler(invocation)
        network_skipped = invocation.context.metadata.get("__network_policy_skip_approval__", set())
        if isinstance(network_skipped, set) and str(invocation.call.id) in network_skipped:
            return await next_handler(invocation)
        readonly_skipped = invocation.context.metadata.get("__readonly_bash_skip_approval__", set())
        if isinstance(readonly_skipped, set) and str(invocation.call.id) in readonly_skipped:
            return await next_handler(invocation)

        raw_capabilities = getattr(invocation.tool, "capabilities", None)
        capabilities = (
            raw_capabilities
            if isinstance(raw_capabilities, ToolCapabilities)
            else ToolCapabilities()
        )
        if invocation.tool.name not in self.required_tools and not capabilities.requires_approval:
            return await next_handler(invocation)
        service = self.approval_service or invocation.context.approval_service
        if service is None:
            return _error(
                invocation, f"approval denied: no approval service for {invocation.tool.name}"
            )
        request = ApprovalRequest(
            call_id=invocation.call.id,
            tool_name=invocation.tool.name,
            arguments=invocation.call.arguments,
            reason=f"tool {invocation.tool.name} requires explicit approval",
        )
        try:
            approved = await service.request(request)
        except Exception as exc:
            return _error(invocation, f"approval denied: {type(exc).__name__}: {exc}")
        if not approved:
            return _error(invocation, f"approval denied for tool {invocation.tool.name}")
        approved_tools = invocation.context.metadata.setdefault("__approved_tool_names__", set())
        if isinstance(approved_tools, set):
            approved_tools.add(invocation.tool.name)
        return await next_handler(invocation)


class TimeoutPolicy:
    """Execute 策略：为每个工具主体提供独立超时。"""

    async def __call__(
        self, invocation: ToolInvocation, next_handler: Callable[..., Awaitable[Any]]
    ) -> Any:
        """用工具定义的 timeout_seconds 包裹下一层，超时由 Runtime 规范化。"""

        if getattr(invocation.tool, "handles_own_timeout", False):
            # Bash 需要在自己的 deadline 中先终止整个进程组，再返回包含秒数的领域错误；
            # 外层 wait_for 与它使用同一超时会抢先取消，退化成没有上下文的 TimeoutError。
            return await next_handler(invocation)
        timeout = invocation.tool.timeout_seconds
        if timeout is None:
            return await next_handler(invocation)
        return await asyncio.wait_for(next_handler(invocation), timeout=timeout)


def _serialized(value: Any) -> str:
    """把工具结果转换成可裁剪、可写入 spill 文件的文本。"""

    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class OutputPolicy:
    """Post 策略：限制模型上下文中的结果大小，超长结果写入 spill 文件。"""

    def __init__(self, max_chars: int = 12000, spill_directory: Path | None = None) -> None:
        """设置模型上下文预览上限和可选 spill 目录。"""

        self.max_chars = max_chars
        self.spill_directory = spill_directory

    async def __call__(
        self, envelope: ToolResultEnvelope, next_handler: Callable[..., Awaitable[Any]]
    ) -> ToolResult:
        """执行后端结果归一化；超限时保存完整文本，只把摘要交给模型。"""

        result = cast(ToolResult, await next_handler(envelope))
        try:
            safe_content = to_json_safe(result.content)
        except JsonSerializationError as exc:
            return ToolResult(
                call_id=envelope.invocation.call.id,
                name=envelope.invocation.call.name,
                content=f"JsonSerializationError: {exc}",
                is_error=True,
            )
        result = result.model_copy(update={"content": safe_content})
        text = _serialized(safe_content)
        if len(text) <= self.max_chars:
            return result

        preview = text[: self.max_chars]
        spill_path = self._spill(envelope.invocation, text)
        content: dict[str, Any] = {
            "truncated": True,
            "total_characters": len(text),
            "preview": preview,
        }
        if spill_path is not None:
            content["spilled_path"] = str(spill_path)
            content["message"] = "完整工具结果已写入文件，请按需读取。"
        else:
            content["message"] = "工具结果过长，spill 文件写入失败，仅保留预览。"
        return result.model_copy(update={"content": content})

    def _spill(self, invocation: ToolInvocation, text: str) -> Path | None:
        """在 workspace 私有目录或系统临时目录保存完整结果。"""

        if self.spill_directory is not None:
            directory = self.spill_directory
        elif invocation.context.workspace is not None:
            directory = workspace_root(invocation.context) / ".python-agent" / "tool-output"
        else:
            directory = Path(tempfile.gettempdir()) / "python-agent-tool-output"
        safe_call_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(invocation.call.id))
        path = directory / f"{safe_call_id}.txt"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            return None
        return path


def build_pre_waterfall(
    approval_service: ApprovalService | None,
    approval_required: set[str] | frozenset[str],
    custom: tuple[PreHandler, ...] = (),
    delete_policy_engine: DeletePolicyEngine | None = None,
) -> Waterfall:
    """创建默认 Pre 策略链，并把调用方策略追加到内置安全检查之后。"""

    return Waterfall(
        [
            ArgumentValidationPolicy(),
            WorkspacePathPolicy(),
            PermissionPolicy(),
            NetworkPolicy(),
            ModificationRiskPolicy(),
            CommandRiskPolicy(),
            DeletePolicy(delete_policy_engine),
            ApprovalPolicy(approval_service, approval_required),
            *custom,
        ]
    )
