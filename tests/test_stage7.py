"""阶段七上下文压缩、Session fork、按需 Skills 和 SQLite 检索测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from python_agent.config import AgentPreset
from python_agent.core.agent_manager import AgentManager
from python_agent.errors import SkillError
from python_agent.ids import SessionId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.session.compaction import CallbackSummaryProvider, ContextCompactor
from python_agent.session.events import SessionHeader
from python_agent.session.jsonl_store import JsonlSessionStore
from python_agent.session.session import Session
from python_agent.session.sqlite_index import SqliteSessionIndex
from python_agent.skills.registry import SkillRegistry
from python_agent.skills.tool import ListSkillsTool, LoadSkillTool
from python_agent.tools.builtins import EchoTool
from python_agent.tools.registry import ToolRegistry


def _append_turn(session: Session, turn: int, user: str, assistant: str) -> None:
    """向测试 Session 追加一个完整、可压缩 Turn。"""

    step = session.next_step()
    session.append("turn/start", {"turn": turn})
    session.append("step/start", {"turn": turn, "step": step})
    session.append("user/message", {"message_id": f"u-{turn}", "content": user})
    session.append("assistant/message", {"content": assistant, "tool_calls": []})
    session.append("step/end", {"turn": turn, "step": step, "reason": "completed"})
    session.append("turn/end", {"turn": turn, "reason": "completed"})


async def test_context_compaction_replaces_old_turns_without_deleting_events() -> None:
    """验证摘要出现在旧消息原位置，原始事件仍完整保留。"""

    session = Session.new()
    _append_turn(session, 1, "问题一", "回答一")
    _append_turn(session, 2, "问题二", "回答二")
    _append_turn(session, 3, "问题三", "回答三")
    original_count = len(session.events)
    seen_transcript: list[str] = []

    async def summarize(transcript: str) -> str:
        seen_transcript.append(transcript)
        return "前两轮讨论了问题一和问题二。"

    result = await ContextCompactor().compact(
        session,
        CallbackSummaryProvider(summarize),
        keep_recent_turns=1,
    )

    assert result is not None
    assert result.replaced_turns == (1, 2)
    assert len(session.events) == original_count + 1
    assert "问题一" in seen_transcript[0]
    assert "问题二" in seen_transcript[0]
    messages = session.messages()
    assert messages == [
        {
            "role": "user",
            "content": "[Earlier conversation summary]\n前两轮讨论了问题一和问题二。",
        },
        {"role": "user", "content": "问题三"},
        {"role": "assistant", "content": "回答三"},
    ]
    assert any(
        event.type == "user/message" and event.data["content"] == "问题一"
        for event in session.events
    )


async def test_repeated_compaction_is_nested_and_replay_deterministic(tmp_path: Path) -> None:
    """验证后续 summary 可以替换旧 summary，多次投影和磁盘恢复结果一致。"""

    store = JsonlSessionStore(tmp_path / "state")
    session_id = SessionId("compact-replay")
    session = await store.create(SessionHeader(id=session_id, cwd=tmp_path))
    _append_turn(session, 1, "一", "答一")
    _append_turn(session, 2, "二", "答二")
    _append_turn(session, 3, "三", "答三")
    compactor = ContextCompactor()
    first = await compactor.compact(
        session,
        CallbackSummaryProvider(lambda text: "一和二的摘要"),
        keep_recent_turns=1,
    )
    assert first is not None

    _append_turn(session, 4, "四", "答四")
    second = await compactor.compact(
        session,
        CallbackSummaryProvider(lambda text: "一、二、三的新版摘要"),
        keep_recent_turns=1,
    )
    assert second is not None
    assert second.replaced_turns == (1, 2, 3)
    assert (
        await compactor.compact(
            session,
            CallbackSummaryProvider(lambda text: "不应调用"),
            keep_recent_turns=1,
        )
        is None
    )

    expected = session.messages()
    restored = await store.load(session_id)
    assert restored.messages() == expected
    assert expected == [
        {
            "role": "user",
            "content": "[Earlier conversation summary]\n一、二、三的新版摘要",
        },
        {"role": "user", "content": "四"},
        {"role": "assistant", "content": "答四"},
    ]


async def test_manager_fork_creates_independent_branch_with_correct_lineage(tmp_path: Path) -> None:
    """验证 fork 不再冒充子 Agent，分支继续运行也不会修改来源。"""

    store = JsonlSessionStore(tmp_path / "state")
    manager = AgentManager(session_store=store)
    config = AgentPreset(id="fork-preset", workspace=tmp_path)
    source = await manager.create(FakeAdapter(), config=config)
    await source.run("来源任务")

    forked = await manager.fork_session(source.id, SessionId("branch"))
    assert forked.header.parent_session_id is None
    assert forked.header.forked_from_session_id == source.id
    assert forked.messages() == source.session.messages()

    branch_agent = await manager.resume(
        forked.id,
        FakeAdapter(),
        config=config,
    )
    await branch_agent.run("分支任务")
    source_reloaded = await store.load(source.id)
    assert [
        message["content"] for message in source_reloaded.messages() if message["role"] == "user"
    ] == ["来源任务"]
    assert [
        message["content"]
        for message in branch_agent.session.messages()
        if message["role"] == "user"
    ] == [
        "来源任务",
        "分支任务",
    ]
    await manager.shutdown()


def _write_skill(root: Path, name: str, *, allowed_tools: str = '["echo"]') -> None:
    """创建声明式测试 Skill。"""

    directory = root / name
    directory.mkdir(parents=True)
    (directory / "skill.toml").write_text(
        f'name = "{name}"\n'
        f'description = "{name} description"\n'
        'instructions_file = "SKILL.md"\n'
        f"allowed_tools = {allowed_tools}\n",
        encoding="utf-8",
    )
    (directory / "SKILL.md").write_text(
        f"# {name}\n\n请按照 {name} 的步骤工作。\n",
        encoding="utf-8",
    )


def test_skill_registry_lists_metadata_and_loads_instructions_on_demand(tmp_path: Path) -> None:
    """验证 list 只返回元数据，load 才读取完整指令并检查工具授权。"""

    root = tmp_path / "skills"
    _write_skill(root, "review")
    registry = SkillRegistry(root)

    metadata = registry.list()
    assert [item.name for item in metadata] == ["review"]
    assert not hasattr(metadata[0], "instructions")
    loaded = registry.load("review", available_tools={"echo"})
    assert "请按照 review" in loaded.instructions
    assert loaded.allowed_tools == ("echo",)

    with pytest.raises(SkillError, match="unavailable tools"):
        registry.load("review", available_tools={"read_file"})
    with pytest.raises(SkillError, match="invalid skill name"):
        registry.load("../escape")


async def test_model_can_list_and_load_skill_through_recorded_tool_result(tmp_path: Path) -> None:
    """验证 Skill 动态内容先记录为 tool/result，下一模型 Step 才能看到。"""

    root = tmp_path / "skills"
    _write_skill(root, "review")
    skills = SkillRegistry(root)
    tools = ToolRegistry([EchoTool()])
    tools.register(ListSkillsTool(skills))
    tools.register(LoadSkillTool(skills, tools))
    adapter = FakeAdapter(
        [
            {
                "tool_calls": [
                    {"id": "load", "name": "load_skill", "arguments": {"name": "review"}}
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "已经使用 review skill"},
        ]
    )
    result = await AgentManager().create_agent(adapter, tools)
    answer = await result.run("加载审查技能")

    assert answer.answer == "已经使用 review skill"
    tool_message = adapter.requests[1].messages[-1]
    assert tool_message["role"] == "tool"
    assert "请按照 review" in tool_message["content"]
    await result.dispose()


async def test_sqlite_index_rebuilds_and_searches_across_sessions(tmp_path: Path) -> None:
    """验证 JSONL 可重建 SQLite 索引、跨 Session 搜索和 Session 过滤。"""

    store = JsonlSessionStore(tmp_path / "state")
    first = await store.create(SessionHeader(id=SessionId("first"), cwd=tmp_path))
    second = await store.create(SessionHeader(id=SessionId("second"), cwd=tmp_path))
    first.append("user/message", {"message_id": "f", "content": "讨论 durable inbox"})
    first.append("assistant/message", {"content": "使用 JSONL", "tool_calls": []})
    second.append("user/message", {"message_id": "s", "content": "检查 durable session"})
    second.append("assistant/message", {"content": "使用 SQLite 派生索引", "tool_calls": []})

    index = SqliteSessionIndex(tmp_path / "index.sqlite3")
    assert await index.rebuild(store) == 2
    hits = index.search("durable")
    assert {hit.session_id for hit in hits} == {SessionId("first"), SessionId("second")}
    filtered = index.search("durable", session_id=SessionId("first"))
    assert [hit.session_id for hit in filtered] == [SessionId("first")]

    second.append("user/message", {"message_id": "s2", "content": "新增百分号 100%"})
    index.index_session(second)
    literal = index.search("100%")
    assert len(literal) == 1
    assert literal[0].session_id == SessionId("second")
