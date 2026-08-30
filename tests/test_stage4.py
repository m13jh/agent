"""阶段四 JSONL 持久化、恢复、修复与 transcript 导出测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from python_agent.config import AgentPreset
from python_agent.core.agent_manager import AgentManager
from python_agent.core.inbox import Inbox
from python_agent.errors import (
    ConfigurationError,
    SessionFormatError,
    SessionRepairRequired,
)
from python_agent.ids import SessionId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.session.events import SessionEvent, SessionHeader
from python_agent.session.jsonl_store import JsonlSessionStore
from python_agent.session.session import Session


def _header(
    session_id: str,
    workspace: Path,
    *,
    preset: str = "stage4",
) -> SessionHeader:
    """创建测试使用的稳定 Header，避免每个用例重复样板。"""

    return SessionHeader(
        id=SessionId(session_id),
        cwd=workspace,
        agent_preset=preset,
    )


def test_session_does_not_update_memory_when_durable_writer_fails() -> None:
    """验证“先落盘、后入内存”：writer 失败时事件列表保持原样。"""

    session = Session.new()

    def fail_write(event: SessionEvent) -> None:
        """模拟磁盘满或 fsync 失败。"""

        raise OSError(f"cannot persist seq {event.seq}")

    session.set_event_writer(fail_write)
    with pytest.raises(OSError, match="cannot persist seq 0"):
        session.append("user/message", {"content": "不能只留在内存"})

    assert session.events == []
    assert session.messages() == []


async def test_jsonl_store_round_trip_and_inbox_replay(tmp_path: Path) -> None:
    """验证 Header、事件、消息投影与两个 Inbox 队列均可从 JSONL 恢复。"""

    store = JsonlSessionStore(tmp_path / "state")
    session_id = SessionId("round-trip")
    session = await store.create(_header(str(session_id), tmp_path))
    inbox = Inbox(session)
    followup_id = inbox.append("下一轮任务", "followup")
    inject_id = inbox.append("静默背景", "inject")
    session.append(
        "user/message",
        {"message_id": "already-visible", "content": "已经进入历史"},
    )

    restored = await store.load(session_id)
    restored_inbox = Inbox(restored, replay=True)

    assert restored.header == session.header
    assert restored.messages() == [{"role": "user", "content": "已经进入历史"}]
    assert [item.message_id for item in restored_inbox.pending("next_turn")] == [followup_id]
    assert [item.message_id for item in restored_inbox.pending("next_step")] == [inject_id]
    assert [event.seq for event in restored.events] == list(range(len(restored.events)))

    event_lines = (
        (tmp_path / "state" / "sessions" / str(session_id) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(event_lines) == len(session.events)
    assert all(json.loads(line)["version"] == 1 for line in event_lines)


async def test_manager_resume_recovers_claimed_but_uncommitted_message(tmp_path: Path) -> None:
    """验证进程在 claim 后崩溃时，恢复不会永久丢失这条 followup。"""

    store = JsonlSessionStore(tmp_path / "state")
    config = AgentPreset(id="resume-preset", workspace=tmp_path)
    session = await store.create(_header("resume-claim", tmp_path, preset=config.id))
    inbox = Inbox(session)
    message_id = inbox.append("必须继续执行的任务", "followup")
    claimed = inbox.claim_next_turn()
    assert claimed is not None
    assert claimed.message_id == message_id
    # 不追加 user/message，精确模拟进程在 Inbox 出队后、进入模型历史前退出。

    adapter = FakeAdapter()
    manager = AgentManager(session_store=store)
    agent = await manager.resume(
        SessionId("resume-claim"),
        adapter,
        config=config,
    )
    await agent.when_idle()

    assert len(adapter.requests) == 1
    assert adapter.requests[0].messages[-1]["content"] == "必须继续执行的任务"
    committed = [event for event in agent.session.events if event.type == "user/message"]
    assert committed[-1].data["message_id"] == str(message_id)
    assert agent.inbox.pending() == ()
    await manager.shutdown()


async def test_resume_requires_the_recorded_preset(tmp_path: Path) -> None:
    """验证未注册 preset 时默认拒绝恢复，防止能力集合静默变化。"""

    store = JsonlSessionStore(tmp_path / "state")
    await store.create(_header("preset-check", tmp_path, preset="original"))
    manager = AgentManager(session_store=store)

    with pytest.raises(ConfigurationError, match="is not registered"):
        await manager.resume(SessionId("preset-check"), FakeAdapter())

    wrong = AgentPreset(id="different", workspace=tmp_path)
    with pytest.raises(ConfigurationError, match="requires preset"):
        await manager.resume(SessionId("preset-check"), FakeAdapter(), config=wrong)


async def test_truncated_final_jsonl_line_requires_explicit_repair(tmp_path: Path) -> None:
    """验证半截最后一行会先失败；显式修复会备份并只截掉该尾部。"""

    store = JsonlSessionStore(tmp_path / "state")
    session_id = SessionId("truncated-tail")
    session = await store.create(_header(str(session_id), tmp_path))
    session.append("user/message", {"message_id": "one", "content": "完整事件"})
    events_path = tmp_path / "state" / "sessions" / str(session_id) / "events.jsonl"
    with events_path.open("ab") as file:
        file.write(b'{"version":1,"seq":1,"type":"user/message"')

    with pytest.raises(SessionRepairRequired, match="non-terminated"):
        await store.load(session_id)

    restored = await store.load(session_id, repair=True)
    report = store.last_repair_report(session_id)

    assert [event.data["content"] for event in restored.events] == ["完整事件"]
    assert report is not None
    assert report.tail.action == "truncate_partial_line"
    assert report.tail.backup_path is not None
    assert report.tail.backup_path.read_bytes().endswith(b'"type":"user/message"')
    assert events_path.read_bytes().endswith(b"\n")

    # 修复后的 writer 必须能继续使用正确的下一个 seq，而不是永远停留在损坏位置。
    restored.append("user/message", {"message_id": "two", "content": "修复后继续"})
    reloaded = await store.load(session_id)
    assert [message["content"] for message in reloaded.messages()] == ["完整事件", "修复后继续"]


async def test_valid_json_without_newline_is_repaired_by_adding_newline(tmp_path: Path) -> None:
    """验证完整 JSON 只缺提交换行时不会丢事件，而是补齐行边界。"""

    store = JsonlSessionStore(tmp_path / "state")
    session_id = SessionId("missing-newline")
    session = await store.create(_header(str(session_id), tmp_path))
    session.append("user/message", {"message_id": "one", "content": "保留我"})
    events_path = tmp_path / "state" / "sessions" / str(session_id) / "events.jsonl"
    events_path.write_bytes(events_path.read_bytes().removesuffix(b"\n"))

    restored = await store.load(session_id, repair=True)
    report = store.last_repair_report(session_id)

    assert restored.messages()[0]["content"] == "保留我"
    assert report is not None
    assert report.tail.action == "add_newline"
    assert events_path.read_bytes().endswith(b"\n")


async def test_repair_refuses_corruption_in_a_complete_jsonl_line(tmp_path: Path) -> None:
    """验证已换行的坏记录被视为中间损坏，repair 也不能静默删除。"""

    store = JsonlSessionStore(tmp_path / "state")
    session_id = SessionId("middle-corruption")
    session = await store.create(_header(str(session_id), tmp_path))
    session.append("user/message", {"message_id": "one", "content": "有效"})
    events_path = tmp_path / "state" / "sessions" / str(session_id) / "events.jsonl"
    with events_path.open("ab") as file:
        file.write(b"{this is not json}\n")

    with pytest.raises(SessionFormatError, match="repairable tail"):
        await store.load(session_id, repair=True)

    assert events_path.read_bytes().endswith(b"{this is not json}\n")


async def test_semantic_repair_does_not_reexecute_unfinished_tool(tmp_path: Path) -> None:
    """验证未完成工具被补错误结果并关闭边界，而不是在恢复时重复执行。"""

    store = JsonlSessionStore(tmp_path / "state")
    session_id = SessionId("semantic-tail")
    session = await store.create(_header(str(session_id), tmp_path))
    session.append("turn/start", {"turn": 1})
    session.append("step/start", {"turn": 1, "step": 1})
    session.append("user/message", {"message_id": "task", "content": "执行写操作"})
    session.append(
        "assistant/message",
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "write-call",
                    "name": "write_file",
                    "arguments": {"path": "note.txt", "content": "内容"},
                }
            ],
            "finish_reason": "tool_calls",
            "usage": {},
        },
    )
    session.append(
        "tool/call",
        {
            "turn": 1,
            "step": 1,
            "call_id": "write-call",
            "name": "write_file",
            "arguments": {"path": "note.txt", "content": "内容"},
        },
    )

    with pytest.raises(SessionRepairRequired, match="unfinished calls write-call"):
        await store.load(session_id)

    restored = await store.load(session_id, repair=True)
    event_types = [event.type for event in restored.events]
    tool_results = [event for event in restored.events if event.type == "tool/result"]
    report = store.last_repair_report(session_id)

    assert not (tmp_path / "note.txt").exists()
    assert event_types[-4:] == ["session/recovery", "tool/result", "step/end", "turn/end"]
    assert tool_results[-1].data["is_error"] is True
    assert "没有自动重放" in tool_results[-1].data["content"]
    assert restored.messages()[-1]["role"] == "tool"
    assert report is not None
    assert report.semantic.recovered_call_ids == ("write-call",)
    assert report.semantic.closed_step == (1, 1)
    assert report.semantic.closed_turn == 1

    # 补偿事件本身也已落盘；再次严格加载不再要求 repair，且不会重复追加结果。
    clean_reload = await store.load(session_id)
    assert [event.type for event in clean_reload.events].count("tool/result") == 1


async def test_store_rejects_unsupported_header_and_event_versions(tmp_path: Path) -> None:
    """验证 Header 与单条 Event 的版本边界分别独立生效。"""

    store = JsonlSessionStore(tmp_path / "state")
    with pytest.raises(SessionFormatError, match="Header version 2"):
        await store.create(
            SessionHeader(
                version=2,
                id=SessionId("future-header"),
                cwd=tmp_path,
            )
        )

    session_id = SessionId("future-event")
    await store.create(_header(str(session_id), tmp_path))
    with pytest.raises(SessionFormatError, match="Event version 2"):
        await store.append(
            session_id,
            SessionEvent(
                version=2,
                seq=0,
                type="user/message",
                data={"content": "future"},
            ),
        )


async def test_load_rejects_sequence_gap_and_unknown_required_event(tmp_path: Path) -> None:
    """验证外部篡改后的 seq 断裂和未知上下文事件都不会被静默跳过。"""

    store = JsonlSessionStore(tmp_path / "state")
    gap_id = SessionId("sequence-gap")
    gap_session = await store.create(_header(str(gap_id), tmp_path))
    gap_session.append("user/message", {"message_id": "one", "content": "一"})
    gap_session.append("user/message", {"message_id": "two", "content": "二"})
    gap_path = tmp_path / "state" / "sessions" / str(gap_id) / "events.jsonl"
    gap_lines = [json.loads(line) for line in gap_path.read_text(encoding="utf-8").splitlines()]
    gap_lines[1]["seq"] = 7
    gap_path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in gap_lines),
        encoding="utf-8",
    )

    with pytest.raises(SessionFormatError, match="expected 1, got 7"):
        await JsonlSessionStore(tmp_path / "state").load(gap_id)

    unknown_id = SessionId("unknown-event")
    unknown = await store.create(_header(str(unknown_id), tmp_path))
    unknown.append("future/context", {"content": "不能消失"})
    with pytest.raises(SessionFormatError, match="unknown non-ignorable"):
        await JsonlSessionStore(tmp_path / "state").load(unknown_id)


async def test_transcript_export_list_and_fork_are_deterministic(tmp_path: Path) -> None:
    """验证导出内容、Session 列表和 fork 的事件投影保持确定性。"""

    store = JsonlSessionStore(tmp_path / "state")
    source_id = SessionId("source")
    source = await store.create(_header(str(source_id), tmp_path))
    source.append("user/message", {"message_id": "u1", "content": "问题"})
    source.append("assistant/message", {"content": "回答", "tool_calls": []})

    destination = tmp_path / "exports" / "source.txt"
    exported = await store.export_transcript(source_id, destination)
    assert exported == destination.resolve()
    assert destination.read_text(encoding="utf-8") == source.transcript()

    forked = await store.fork(source_id, SessionId("forked"))
    assert forked.header.parent_session_id is None
    assert forked.header.forked_from_session_id == source_id
    assert forked.messages() == source.messages()
    assert [event.model_dump() for event in forked.events] == [
        event.model_dump() for event in source.events
    ]

    forked.append("user/message", {"message_id": "u2", "content": "分支问题"})
    source_reload = await store.load(source_id)
    assert len(source_reload.events) == 2
    assert len(forked.events) == 3

    listed = await store.list()
    assert {header.id for header in listed} == {source_id, SessionId("forked")}
