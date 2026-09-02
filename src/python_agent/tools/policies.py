"""工具执行的 Pre、Execute、Post 策略及其共享上下文。"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from python_agent.approval.service import ApprovalRequest, ApprovalService
from python_agent.errors import ToolError, ToolValidationError
from python_agent.hooks.waterfall import Waterfall
from python_agent.llm.types import ToolCall
from python_agent.tools.builtins._paths import safe_path, workspace_root
from python_agent.tools.definition import ToolCapabilities, ToolDefinition
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
        if invocation.context.permission_mode == "read-only" and not capabilities.read_only:
            return _error(
                invocation,
                f"permission denied: {invocation.tool.name} is not read-only",
            )
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
) -> Waterfall:
    """创建默认 Pre 策略链，并把调用方策略追加到内置安全检查之后。"""

    return Waterfall(
        [
            ArgumentValidationPolicy(),
            WorkspacePathPolicy(),
            PermissionPolicy(),
            ApprovalPolicy(approval_service, approval_required),
            *custom,
        ]
    )
