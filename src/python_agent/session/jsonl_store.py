"""基于目录与 JSONL 文件的阶段四 SessionStore 实现。

磁盘布局固定为：

```
<root>/sessions/<session-id>/header.json
<root>/sessions/<session-id>/events.jsonl
```

Header 通过临时目录加原子 rename 创建；事件每次追加一整行并立即 flush/fsync。返回给
Agent 的 Session 绑定同步 writer，因此一次 ``Session.append`` 要么先成功落盘再进入内存，
要么抛错且内存完全不变。
"""

from __future__ import annotations

import builtins
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from python_agent.errors import (
    ProjectionError,
    SessionConflictError,
    SessionError,
    SessionFormatError,
    SessionNotFoundError,
    SessionRepairRequired,
)
from python_agent.ids import SessionId
from python_agent.session.events import (
    SESSION_EVENT_VERSION,
    SESSION_HEADER_VERSION,
    SessionEvent,
    SessionHeader,
    utc_now,
)
from python_agent.session.repair import (
    JsonlTailRepairReport,
    SemanticRepairReport,
    analyze_incomplete_session,
    repair_incomplete_session,
    repair_jsonl_tail,
)
from python_agent.session.session import Session

_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True, slots=True)
class SessionRepairReport:
    """一次 load(repair=True) 同时包含物理层和语义层的修复结果。"""

    tail: JsonlTailRepairReport
    semantic: SemanticRepairReport

    @property
    def changed(self) -> bool:
        """此次恢复是否对权威日志做了任何改动。"""

        return self.tail.changed or self.semantic.repaired


class JsonlSessionStore:
    """使用一个 Header JSON 和一个只追加 JSONL 文件保存每个 Session。"""

    def __init__(self, root: Path) -> None:
        """记录存储根目录；目录只在第一次写操作时创建。

        每个 Session 使用独立 ``RLock``。阶段四的单次 Header/JSONL 操作很短，并且
        ``Session.append`` 本来就需要同步保证“落盘后才入内存”，因此 async 接口内部直接
        执行这段临界区，不创建无法归属的线程池任务。阶段四暂不承诺多进程同时写同一 ID。
        """

        self.root = root.expanduser().resolve()
        self.sessions_directory = self.root / "sessions"
        self._locks_guard = threading.Lock()
        self._locks: dict[SessionId, threading.RLock] = {}
        self._next_seq: dict[SessionId, int] = {}
        self._repair_reports: dict[SessionId, SessionRepairReport] = {}

    async def create(self, header: SessionHeader) -> Session:
        """原子创建空 Session，并返回已绑定持久化 writer 的对象。"""

        return self._create_sync(header, ())

    async def load(self, session_id: SessionId, *, repair: bool = False) -> Session:
        """从磁盘严格加载 Session，或在显式授权后修复可证明的崩溃尾部。"""

        return self._load_sync(session_id, repair)

    async def append(self, session_id: SessionId, event: SessionEvent) -> None:
        """直接追加已编号事件；主要供基础设施调用，Agent 通常使用 Session.append。"""

        self._append_event_sync(session_id, event)

    async def list(self) -> list[SessionHeader]:
        """列出所有完整 Session；任一正式目录损坏都会明确失败。"""

        return self._list_sync()

    async def fork(self, source_id: SessionId, target_id: SessionId) -> Session:
        """复制来源的精确事件快照，创建具有新 Header 的独立分支。"""

        source = await self.load(source_id)
        target_header = source.header.model_copy(
            update={
                "id": target_id,
                "created_at": utc_now(),
                # 记录分支来源，便于审计；fork 后的 events 是独立副本，后续追加互不影响。
                "parent_session_id": source.id,
            }
        )
        return self._create_sync(target_header, tuple(source.events))

    async def export_transcript(self, session_id: SessionId, path: Path) -> Path:
        """严格加载日志，并把同一事件投影原子写入 transcript 文件。"""

        session = await self.load(session_id)
        return session.export_transcript(path)

    async def repair(self, session_id: SessionId) -> SessionRepairReport:
        """显式修复并返回报告；无法安全推断的损坏仍会抛出 SessionFormatError。"""

        await self.load(session_id, repair=True)
        report = self._repair_reports.get(session_id)
        if report is None:  # pragma: no cover - 防御内部状态意外丢失
            raise SessionError(f"repair report was not recorded for session {session_id}")
        return report

    def last_repair_report(self, session_id: SessionId) -> SessionRepairReport | None:
        """返回当前 Store 实例最近一次显式修复报告，不触发文件 I/O。"""

        return self._repair_reports.get(session_id)

    def _lock_for(self, session_id: SessionId) -> threading.RLock:
        """线程安全地取得每个 Session 独占的可重入锁。"""

        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.RLock())

    @staticmethod
    def _validate_session_id(session_id: SessionId) -> str:
        """拒绝路径分隔符、``..`` 和过长 ID，避免目录穿越。"""

        value = str(session_id)
        if _SESSION_ID_PATTERN.fullmatch(value) is None or value in {".", ".."}:
            raise SessionFormatError(f"invalid session id: {value!r}")
        return value

    def _session_directory(self, session_id: SessionId) -> Path:
        """把已校验 ID 映射到固定 sessions 根下的目录。"""

        value = self._validate_session_id(session_id)
        return self.sessions_directory / value

    @staticmethod
    def _validate_header_version(header: SessionHeader) -> None:
        """只接受当前明确支持的 Header 版本，不猜测未来格式。"""

        if header.version != SESSION_HEADER_VERSION:
            raise SessionFormatError(
                f"unsupported Session Header version {header.version}; "
                f"supported version is {SESSION_HEADER_VERSION}"
            )

    @staticmethod
    def _validate_event_version(event: SessionEvent, *, line_number: int | None = None) -> None:
        """逐事件校验编码版本，并在错误中保留 JSONL 行号。"""

        if event.version == SESSION_EVENT_VERSION:
            return
        location = f" at JSONL line {line_number}" if line_number is not None else ""
        raise SessionFormatError(
            f"unsupported Session Event version {event.version}{location}; "
            f"supported version is {SESSION_EVENT_VERSION}"
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """尽力同步目录项，使原子 rename 在掉电后也更可靠。

        某些平台不支持打开目录做 fsync；阶段四仍保留跨平台回退，因此这些平台上的
        ``OSError`` 被忽略，文件内容本身的 fsync 仍然有效。
        """

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_text_file(path: Path, text: str) -> None:
        """创建一个新 UTF-8 文件并在返回前同步内容。"""

        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        try:
            path.chmod(0o600)
        except OSError:
            # chmod 在部分非 POSIX 文件系统不可用；不能因此把已完整写入的数据判为损坏。
            pass

    def _create_sync(
        self,
        header: SessionHeader,
        events: Iterable[SessionEvent],
    ) -> Session:
        """在单个临界区内原子创建目录、Header 与初始事件快照。"""

        self._validate_header_version(header)
        session_id = header.id
        target = self._session_directory(session_id)
        event_snapshot = tuple(events)
        for index, event in enumerate(event_snapshot):
            self._validate_event_version(event, line_number=index + 1)
            if event.seq != index:
                raise SessionFormatError(
                    f"cannot create session with non-contiguous event seq: "
                    f"expected {index}, got {event.seq}"
                )

        self.sessions_directory.mkdir(parents=True, exist_ok=True)
        lock = self._lock_for(session_id)
        with lock:
            if target.exists():
                raise SessionConflictError(f"session already exists: {session_id}")
            temporary = Path(
                tempfile.mkdtemp(prefix=f".creating-{session_id}-", dir=self.sessions_directory)
            )
            try:
                header_text = header.model_dump_json(indent=2) + "\n"
                self._write_text_file(temporary / "header.json", header_text)
                event_text = "".join(event.model_dump_json() + "\n" for event in event_snapshot)
                self._write_text_file(temporary / "events.jsonl", event_text)
                try:
                    os.replace(temporary, target)
                except FileExistsError as exc:
                    raise SessionConflictError(f"session already exists: {session_id}") from exc
                self._fsync_directory(self.sessions_directory)
            except Exception:
                # temporary 的名称和父目录都由本方法创建并精确掌握，清理不会触及用户目录。
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise

            self._next_seq[session_id] = len(event_snapshot)
            return self._bind_session(header, event_snapshot)

    def _bind_session(
        self,
        header: SessionHeader,
        events: Iterable[SessionEvent],
    ) -> Session:
        """创建 Session，并把后续 append 绑定到当前 Store 的同步提交函数。"""

        session_id = header.id

        def write_event(event: SessionEvent) -> None:
            """在事件进入 Session.events 前完成耐久追加。"""

            self._append_event_sync(session_id, event)

        return Session(header, events, event_writer=write_event)

    def _read_header(self, session_id: SessionId) -> SessionHeader:
        """读取、Pydantic 校验并核对请求 ID 与 Header ID。"""

        directory = self._session_directory(session_id)
        path = directory / "header.json"
        if not directory.is_dir():
            raise SessionNotFoundError(f"session does not exist: {session_id}")
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise SessionFormatError(f"Session Header must be a JSON object: {path}")
            header = SessionHeader.model_validate(raw)
        except SessionFormatError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise SessionFormatError(f"cannot load Session Header {path}: {exc}") from exc
        self._validate_header_version(header)
        if header.id != session_id:
            raise SessionFormatError(
                f"Session Header id {header.id!s} does not match directory id {session_id!s}"
            )
        return header

    def _read_events(self, session_id: SessionId) -> tuple[SessionEvent, ...]:
        """严格读取完整 JSONL，校验 UTF-8、对象结构、版本和连续 seq。"""

        path = self._session_directory(session_id) / "events.jsonl"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SessionFormatError(f"cannot read Session events {path}: {exc}") from exc
        if data and not data.endswith(b"\n"):
            raise SessionRepairRequired(
                f"events JSONL has a non-terminated final line: {path}; "
                "load again with repair=True to inspect and repair only that tail"
            )

        events: list[SessionEvent] = []
        for line_number, raw_line in enumerate(data.split(b"\n")[:-1], 1):
            if not raw_line:
                raise SessionFormatError(f"blank JSONL line at {path}:{line_number}")
            try:
                raw: Any = json.loads(raw_line.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise SessionFormatError(
                        f"Session event must be an object at {path}:{line_number}"
                    )
                event = SessionEvent.model_validate(raw)
            except SessionFormatError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                raise SessionFormatError(
                    f"cannot decode Session event at {path}:{line_number}: {exc}"
                ) from exc
            self._validate_event_version(event, line_number=line_number)
            expected = len(events)
            if event.seq != expected:
                raise SessionFormatError(
                    f"event sequence is not contiguous at {path}:{line_number}: "
                    f"expected {expected}, got {event.seq}"
                )
            events.append(event)
        return tuple(events)

    def _load_sync(self, session_id: SessionId, repair: bool) -> Session:
        """在同一 Session 锁内完成物理检查、领域校验、绑定和可选补偿。"""

        directory = self._session_directory(session_id)
        if not directory.is_dir():
            raise SessionNotFoundError(f"session does not exist: {session_id}")
        events_path = directory / "events.jsonl"
        lock = self._lock_for(session_id)
        with lock:
            if repair:
                tail_report = repair_jsonl_tail(events_path)
            else:
                # 未授权修复时仍构造一个“未改变”报告，便于内部逻辑保持单一路径。
                try:
                    size = events_path.stat().st_size
                except OSError as exc:
                    raise SessionFormatError(
                        f"cannot stat Session events {events_path}: {exc}"
                    ) from exc
                tail_report = JsonlTailRepairReport(
                    path=events_path.resolve(),
                    action="none",
                    original_size=size,
                    repaired_size=size,
                )

            header = self._read_header(session_id)
            events = self._read_events(session_id)
            unbound = Session(header, events)
            try:
                # 投影一次可同时验证未知必需事件、tool call/result 顺序和 JSON 可回放性。
                unbound.messages()
            except ProjectionError as exc:
                raise SessionFormatError(f"Session message projection failed: {exc}") from exc

            incomplete = analyze_incomplete_session(unbound)
            if incomplete.needs_repair and not repair:
                details = []
                if incomplete.open_turn is not None:
                    details.append(f"open turn {incomplete.open_turn}")
                if incomplete.open_step is not None:
                    details.append(f"open step {incomplete.open_step[1]}")
                if incomplete.pending_tool_calls:
                    details.append(
                        "unfinished calls "
                        + ", ".join(call.call_id for call in incomplete.pending_tool_calls)
                    )
                raise SessionRepairRequired(
                    "Session has an incomplete crash tail ("
                    + "; ".join(details)
                    + "); load again with repair=True"
                )

            self._next_seq[session_id] = len(events)
            session = self._bind_session(header, events)
            semantic_report = (
                repair_incomplete_session(session)
                if repair
                else SemanticRepairReport(repaired=False)
            )
            if repair:
                self._repair_reports[session_id] = SessionRepairReport(
                    tail=tail_report,
                    semantic=semantic_report,
                )
            return session

    def _append_event_sync(self, session_id: SessionId, event: SessionEvent) -> None:
        """把单条事件作为完整 JSONL 行写入，并在返回前完成 fsync。"""

        self._validate_event_version(event)
        path = self._session_directory(session_id) / "events.jsonl"
        lock = self._lock_for(session_id)
        with lock:
            if not path.is_file():
                raise SessionNotFoundError(f"session does not exist: {session_id}")
            expected = self._next_seq.get(session_id)
            if expected is None:
                # Store 实例可能在进程中途才接管既有 Session；严格读取一次即可恢复 next seq。
                expected = len(self._read_events(session_id))
                self._next_seq[session_id] = expected
            if event.seq != expected:
                raise SessionError(
                    f"event sequence must be contiguous for {session_id}: "
                    f"expected {expected}, got {event.seq}"
                )
            try:
                size = path.stat().st_size
                if size:
                    with path.open("rb") as check:
                        check.seek(-1, os.SEEK_END)
                        if check.read(1) != b"\n":
                            raise SessionRepairRequired(
                                f"cannot append to non-terminated JSONL tail: {path}"
                            )
                encoded = (event.model_dump_json() + "\n").encode("utf-8")
                with path.open("ab") as file:
                    file.write(encoded)
                    file.flush()
                    os.fsync(file.fileno())
            except SessionRepairRequired:
                raise
            except OSError as exc:
                raise SessionError(f"cannot append Session event to {path}: {exc}") from exc
            self._next_seq[session_id] = expected + 1

    def _list_sync(self) -> builtins.list[SessionHeader]:
        """读取所有正式目录的 Header，并按创建时间、ID 稳定排序。"""

        if not self.sessions_directory.exists():
            return []
        # 类中已有名为 list 的协议方法，显式写 builtins.list 可避免类型检查器把它误当方法。
        headers: builtins.list[SessionHeader] = []
        try:
            children = sorted(self.sessions_directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise SessionFormatError(
                f"cannot list Session directory {self.sessions_directory}: {exc}"
            ) from exc
        for child in children:
            # 原子 create 使用点号开头的临时目录；进程崩溃遗留它时不应伪装成正式 Session。
            if not child.is_dir() or child.name.startswith("."):
                continue
            session_id = SessionId(child.name)
            headers.append(self._read_header(session_id))
        return sorted(headers, key=lambda header: (header.created_at, str(header.id)))


__all__ = ["JsonlSessionStore", "SessionRepairReport"]
