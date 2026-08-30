"""JSONL 物理尾部与 Session 语义尾部的保守修复策略。

修复被刻意限制在“可以证明是崩溃留下的最后一段”这一小类问题：

* 物理层只处理最后一行没有换行的完整 JSON，或最后一段未写完的 JSON；
* 语义层只处理最后一个未闭合 Turn/Step，以及其中没有结果的工具调用；
* 中间行损坏、已结束 Turn 中缺失结果等情况一律拒绝，不能通过删日志来掩盖。

因此调用方可以显式选择 repair，而不会让正常 load 静默改变权威事件历史。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from python_agent.errors import SessionFormatError
from python_agent.session.session import Session

TailRepairAction = Literal["none", "add_newline", "truncate_partial_line"]


@dataclass(frozen=True, slots=True)
class JsonlTailRepairReport:
    """一次 JSONL 物理尾部检查/修复的结果。"""

    path: Path
    action: TailRepairAction
    original_size: int
    repaired_size: int
    backup_path: Path | None = None

    @property
    def changed(self) -> bool:
        """是否真的改写了 events.jsonl。"""

        return self.action != "none"


def _validate_complete_lines(data: bytes, path: Path) -> None:
    """校验一组均以换行结束的 JSONL 数据，不允许空行或非对象值。

    这里不负责 Pydantic 字段校验；它只用于判断“损坏发生在中间还是最后一段”。字段、
    版本和 seq 会在 JsonlSessionStore 加载事件时进行更严格的领域校验。
    """

    if not data:
        return
    if not data.endswith(b"\n"):
        raise AssertionError("complete JSONL prefix must end with a newline")
    for line_number, raw_line in enumerate(data.split(b"\n")[:-1], 1):
        if not raw_line:
            raise SessionFormatError(f"blank JSONL line at {path}:{line_number}")
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionFormatError(
                f"corrupt JSONL data before the repairable tail at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise SessionFormatError(f"JSONL event must be an object at {path}:{line_number}")


def _next_backup_path(path: Path) -> Path:
    """选择不会覆盖旧备份的确定性备份路径。"""

    base = path.with_name(f"{path.name}.repair-backup")
    if not base.exists():
        return base
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.repair-backup.{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _write_bytes_durably(path: Path, data: bytes) -> None:
    """通过同目录临时文件、fsync 和原子替换提交修复后的字节。"""

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".repair.tmp",
            delete=False,
        ) as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    except OSError:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise


def repair_jsonl_tail(path: Path, *, create_backup: bool = True) -> JsonlTailRepairReport:
    """检查并修复 events.jsonl 最后一段，其他损坏保持严格失败。

    正常 append 会把“一条 JSON + 换行”作为一个提交单元，但进程可能在系统调用中间
    退出。若最后一段本身是合法 JSON，只补换行；若它不是合法 JSON，则只截掉这段。
    任何已经以换行结束的坏记录都被视为持久化数据损坏，而不是可推断的崩溃尾部。
    修改前默认保存原始字节备份，便于人工审计或恢复。
    """

    resolved = path.expanduser().resolve()
    try:
        original = resolved.read_bytes()
    except OSError as exc:
        raise SessionFormatError(f"cannot read JSONL file {resolved}: {exc}") from exc
    original_size = len(original)
    if not original:
        return JsonlTailRepairReport(resolved, "none", 0, 0)

    # 文件以换行结束时不存在“半行”概念，因此必须逐行严格校验，不能删除最后一条坏记录。
    if original.endswith(b"\n"):
        _validate_complete_lines(original, resolved)
        return JsonlTailRepairReport(resolved, "none", original_size, original_size)

    last_newline = original.rfind(b"\n")
    prefix = original[: last_newline + 1] if last_newline >= 0 else b""
    tail = original[last_newline + 1 :]
    _validate_complete_lines(prefix, resolved)

    try:
        tail_value = json.loads(tail.decode("utf-8"))
        if not isinstance(tail_value, dict):
            raise SessionFormatError(f"JSONL event must be an object in final line of {resolved}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        # 只有没有换行的最后一段允许被截去；prefix 的每一行已经在上方验证为完整 JSON。
        repaired = prefix
        action: TailRepairAction = "truncate_partial_line"
    else:
        repaired = original + b"\n"
        action = "add_newline"

    backup_path: Path | None = None
    if create_backup:
        backup_path = _next_backup_path(resolved)
        try:
            descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as backup:
                backup.write(original)
                backup.flush()
                os.fsync(backup.fileno())
        except OSError as exc:
            raise SessionFormatError(f"cannot create repair backup {backup_path}: {exc}") from exc

    try:
        _write_bytes_durably(resolved, repaired)
    except OSError as exc:
        raise SessionFormatError(f"cannot commit JSONL tail repair for {resolved}: {exc}") from exc
    return JsonlTailRepairReport(
        path=resolved,
        action=action,
        original_size=original_size,
        repaired_size=len(repaired),
        backup_path=backup_path,
    )


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    """恢复分析中发现的、尚无 tool/result 的调用。"""

    call_id: str
    name: str
    arguments: dict[str, Any]
    turn: int | None
    step: int | None
    source_seq: int
    has_call_event: bool


@dataclass(frozen=True, slots=True)
class IncompleteSessionState:
    """Session 尾部的未闭合状态，只描述事实而不执行修改。"""

    open_turn: int | None
    open_step: tuple[int, int] | None
    pending_tool_calls: tuple[PendingToolCall, ...]

    @property
    def needs_repair(self) -> bool:
        """是否需要追加恢复事件才能安全继续会话。"""

        return bool(
            self.open_turn is not None or self.open_step is not None or self.pending_tool_calls
        )


@dataclass(frozen=True, slots=True)
class SemanticRepairReport:
    """语义尾部修复追加了哪些事实。"""

    repaired: bool
    recovered_call_ids: tuple[str, ...] = ()
    closed_step: tuple[int, int] | None = None
    closed_turn: int | None = None
    appended_event_seqs: tuple[int, ...] = ()


def analyze_incomplete_session(session: Session) -> IncompleteSessionState:
    """验证 Turn/Step 配对，并找出最后一个崩溃尾部中的未完成工具调用。

    该分析器不尝试容忍任意历史损坏。嵌套 Turn、Step 跨 Turn 关闭、重复 call id 等都说明
    问题不再局限于尾部，必须交给人工检查。只有一个仍然打开的末尾 Turn/Step 才能自动
    关闭，以免“修复”错误地重写真实对话语义。
    """

    open_turn: int | None = None
    open_step: tuple[int, int] | None = None
    calls: dict[str, PendingToolCall] = {}
    result_ids: set[str] = set()

    for event in session.events:
        data = event.data
        if event.type == "turn/start":
            turn = data.get("turn")
            if not isinstance(turn, int):
                raise SessionFormatError(f"turn/start at seq {event.seq} has no integer turn")
            if open_turn is not None:
                raise SessionFormatError(
                    f"nested turn/start at seq {event.seq}; turn {open_turn} is still open"
                )
            open_turn = turn
        elif event.type == "turn/end":
            turn = data.get("turn")
            if open_turn is None or turn != open_turn:
                raise SessionFormatError(f"turn/end at seq {event.seq} does not match an open turn")
            if open_step is not None:
                raise SessionFormatError(
                    f"turn/end at seq {event.seq} appears before step {open_step[1]} is closed"
                )
            open_turn = None
        elif event.type == "step/start":
            turn = data.get("turn")
            step = data.get("step")
            if not isinstance(turn, int) or not isinstance(step, int):
                raise SessionFormatError(f"step/start at seq {event.seq} has invalid turn/step")
            if open_turn != turn or open_step is not None:
                raise SessionFormatError(
                    f"step/start at seq {event.seq} is outside its active turn"
                )
            open_step = (turn, step)
        elif event.type == "step/end":
            marker = (data.get("turn"), data.get("step"))
            if open_step is None or marker != open_step:
                raise SessionFormatError(f"step/end at seq {event.seq} does not match an open step")
            open_step = None
        elif event.type == "assistant/message":
            raw_calls = data.get("tool_calls", []) or []
            if not isinstance(raw_calls, list):
                raise SessionFormatError(
                    f"assistant/message at seq {event.seq} has invalid tool_calls"
                )
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    raise SessionFormatError(
                        f"assistant/message tool call at seq {event.seq} must be an object"
                    )
                call_id = raw_call.get("id", raw_call.get("call_id"))
                name = raw_call.get("name")
                arguments = raw_call.get("arguments", {})
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(arguments, dict)
                ):
                    raise SessionFormatError(
                        f"assistant/message tool call at seq {event.seq} is incomplete"
                    )
                if call_id in calls:
                    raise SessionFormatError(f"duplicate assistant tool call id: {call_id}")
                calls[call_id] = PendingToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    turn=open_turn,
                    step=open_step[1] if open_step is not None else None,
                    source_seq=event.seq,
                    has_call_event=False,
                )
        elif event.type == "tool/call":
            call_id = data.get("call_id", data.get("id"))
            name = data.get("name")
            arguments = data.get("arguments", {})
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
                raise SessionFormatError(f"tool/call at seq {event.seq} is incomplete")
            existing = calls.get(call_id)
            if existing is None:
                if not isinstance(arguments, dict):
                    raise SessionFormatError(f"tool/call {call_id} arguments must be an object")
                calls[call_id] = PendingToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    turn=data.get("turn") if isinstance(data.get("turn"), int) else open_turn,
                    step=data.get("step") if isinstance(data.get("step"), int) else None,
                    source_seq=event.seq,
                    has_call_event=True,
                )
            elif existing.has_call_event:
                raise SessionFormatError(f"duplicate tool/call event for call id: {call_id}")
            else:
                calls[call_id] = PendingToolCall(
                    call_id=existing.call_id,
                    name=existing.name,
                    arguments=existing.arguments,
                    turn=existing.turn,
                    step=existing.step,
                    source_seq=existing.source_seq,
                    has_call_event=True,
                )
        elif event.type == "tool/result":
            call_id = data.get("call_id", data.get("id"))
            if not isinstance(call_id, str) or not call_id:
                raise SessionFormatError(f"tool/result at seq {event.seq} has no call id")
            if call_id in result_ids:
                raise SessionFormatError(f"duplicate tool/result event for call id: {call_id}")
            result_ids.add(call_id)

    pending = tuple(call for call_id, call in calls.items() if call_id not in result_ids)
    if pending:
        # 缺失结果只能属于仍打开的最后一个 Step。若 Turn 已结束或已经进入后续 Step，说明
        # 损坏位于日志中间，自动补一个错误结果会改变后续模型请求的历史顺序，因此拒绝。
        if open_turn is None or open_step is None:
            raise SessionFormatError("unfinished tool call is not inside the final open step")
        for call in pending:
            if call.turn != open_turn or call.step != open_step[1]:
                raise SessionFormatError(
                    f"unfinished tool call {call.call_id} is outside the final open step"
                )

    return IncompleteSessionState(
        open_turn=open_turn,
        open_step=open_step,
        pending_tool_calls=pending,
    )


def repair_incomplete_session(session: Session) -> SemanticRepairReport:
    """通过追加补偿事件安全关闭崩溃时未完成的语义尾部。

    工具是否已经产生外部副作用通常无法在重启后证明，因此绝不自动重放。对每个缺失
    ``tool/result`` 的调用追加一个明确错误结果，让模型在下一 Turn 看见失败并自行检查；
    随后关闭 Step 和 Turn。整个过程仍走 Session.append，所以每条补偿事件也先持久化。
    """

    state = analyze_incomplete_session(session)
    if not state.needs_repair:
        return SemanticRepairReport(repaired=False)

    appended: list[int] = []
    recovery_event = session.append(
        "session/recovery",
        {
            "reason": "process_crash",
            "open_turn": state.open_turn,
            "open_step": state.open_step[1] if state.open_step is not None else None,
            "pending_tool_call_ids": [call.call_id for call in state.pending_tool_calls],
        },
        # 这是审计事实，不改变模型语义；未来旧版本投影器也可以安全跳过。
        ignorable=True,
    )
    appended.append(recovery_event.seq)

    for call in state.pending_tool_calls:
        if not call.has_call_event:
            call_event = session.append(
                "tool/call",
                {
                    "turn": call.turn,
                    "step": call.step,
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "recovered_without_execution": True,
                },
            )
            appended.append(call_event.seq)
        result_event = session.append(
            "tool/result",
            {
                "call_id": call.call_id,
                "name": call.name,
                "content": (
                    "进程在工具调用完成前退出。为避免重复产生副作用，恢复过程没有自动重放"
                    "该工具；请先检查当前工作区状态，再决定是否重新执行。"
                ),
                "is_error": True,
                "concludes_turn": False,
                "recovered": True,
            },
        )
        appended.append(result_event.seq)

    if state.open_step is not None:
        turn, step = state.open_step
        step_event = session.append(
            "step/end",
            {"turn": turn, "step": step, "reason": "crash_recovered"},
        )
        appended.append(step_event.seq)
    if state.open_turn is not None:
        turn_event = session.append(
            "turn/end",
            {"turn": state.open_turn, "reason": "crash_recovered"},
        )
        appended.append(turn_event.seq)

    return SemanticRepairReport(
        repaired=True,
        recovered_call_ids=tuple(call.call_id for call in state.pending_tool_calls),
        closed_step=state.open_step,
        closed_turn=state.open_turn,
        appended_event_seqs=tuple(appended),
    )


__all__ = [
    "IncompleteSessionState",
    "JsonlTailRepairReport",
    "PendingToolCall",
    "SemanticRepairReport",
    "analyze_incomplete_session",
    "repair_incomplete_session",
    "repair_jsonl_tail",
]
