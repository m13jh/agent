"""完整审计发现的敏感路径、参数约束、搜索上限和文件权限回归测试。"""

from __future__ import annotations

import os
from pathlib import Path

from python_agent.config import AgentPreset
from python_agent.core.agent_manager import AgentManager
from python_agent.ids import SessionId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.llm.types import ToolCall
from python_agent.session.events import SessionHeader
from python_agent.session.jsonl_store import JsonlSessionStore
from python_agent.session.sqlite_index import SqliteSessionIndex
from python_agent.tools.builtins import ListFilesTool, ReadFileTool, SearchTextTool
from python_agent.tools.definition import FunctionTool
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext


async def test_sensitive_credentials_are_hidden_and_direct_read_is_denied(tmp_path: Path) -> None:
    """验证 .env/私钥不出现在 list/search 中，直接读取也在工具主体前失败。"""

    (tmp_path / ".env").write_text("SECRET_MARK=value\n", encoding="utf-8")
    (tmp_path / "private.pem").write_text("SECRET_MARK\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SAFE_EXAMPLE=value\n", encoding="utf-8")
    (tmp_path / "normal.txt").write_text("NORMAL_MARK\n", encoding="utf-8")
    runtime = ToolRuntime(ToolRegistry([ReadFileTool(), ListFilesTool(), SearchTextTool()]))
    context = ToolContext(session_id=SessionId("sensitive"), workspace=tmp_path)

    denied = await runtime.execute(
        ToolCall(id="read", name="read_file", arguments={"path": ".env"}),
        context,
    )
    listed = await runtime.execute(
        ToolCall(id="list", name="list_files", arguments={"max_results": 100}),
        context,
    )
    searched = await runtime.execute(
        ToolCall(id="search", name="search_text", arguments={"query": "SECRET_MARK"}),
        context,
    )

    assert denied.is_error is True
    assert "sensitive credential" in str(denied.content)
    assert ".env" not in listed.content
    assert "private.pem" not in listed.content
    assert ".env.example" in listed.content
    assert "normal.txt" in listed.content
    assert "SECRET_MARK" not in searched.content


async def test_agent_automatically_excludes_custom_session_store_root(tmp_path: Path) -> None:
    """验证 workspace/state 自定义存储不会回流到 list_files 工具结果。"""

    store = JsonlSessionStore(tmp_path / "state")
    adapter = FakeAdapter(
        [
            {
                "tool_calls": [{"id": "list", "name": "list_files", "arguments": {}}],
                "finish_reason": "tool_calls",
            },
            {"content": "done"},
        ]
    )
    manager = AgentManager(session_store=store)
    agent = await manager.create(
        adapter,
        ToolRegistry([ListFilesTool()]),
        config=AgentPreset(id="excluded-store", workspace=tmp_path),
    )
    await agent.run("list")

    tool_result = next(event for event in agent.session.events if event.type == "tool/result")
    assert not any(path.startswith("state/") for path in tool_result.data["content"])
    assert store.root in agent.loop.excluded_paths
    await manager.shutdown()


async def test_json_schema_ranges_and_nested_array_items_are_enforced(tmp_path: Path) -> None:
    """验证 minimum/maximum 和递归 items 在工具主体前生效。"""

    called = False

    async def body(arguments: dict, context: ToolContext) -> str:
        nonlocal called
        del arguments, context
        called = True
        return "unexpected"

    tool = FunctionTool(
        name="schema",
        description="schema constraints",
        parameters={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "names": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 2},
                },
            },
            "required": ["count", "names"],
            "additionalProperties": False,
        },
        body=body,
    )
    runtime = ToolRuntime(ToolRegistry([tool]))
    context = ToolContext(session_id=SessionId("schema"), workspace=tmp_path)

    too_small = await runtime.execute(
        ToolCall(id="small", name="schema", arguments={"count": 0, "names": ["ok"]}),
        context,
    )
    bad_item = await runtime.execute(
        ToolCall(id="item", name="schema", arguments={"count": 2, "names": ["x"]}),
        context,
    )
    assert too_small.is_error is True
    assert ">= 1" in str(too_small.content)
    assert bad_item.is_error is True
    assert "length >= 2" in str(bad_item.content)
    assert called is False


async def test_search_text_max_results_is_global(tmp_path: Path) -> None:
    """验证多个文件命中时仍只返回全局 max_results 条。"""

    (tmp_path / "a.txt").write_text("GLOBAL_MARK\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("GLOBAL_MARK\n", encoding="utf-8")
    runtime = ToolRuntime(ToolRegistry([SearchTextTool()]))
    context = ToolContext(session_id=SessionId("search"), workspace=tmp_path)
    for index in range(5):
        result = await runtime.execute(
            ToolCall(
                id=f"search-{index}",
                name="search_text",
                arguments={"query": "GLOBAL_MARK", "max_results": 1},
            ),
            context,
        )
        assert result.is_error is False
        assert len(result.content.splitlines()) == 1


async def test_repair_backup_and_sqlite_index_are_mode_600(tmp_path: Path) -> None:
    """验证包含完整历史的派生文件不会继承宽松 umask。"""

    store = JsonlSessionStore(tmp_path / "state")
    session_id = SessionId("permissions")
    session = await store.create(SessionHeader(id=session_id, cwd=tmp_path))
    session.append("user/message", {"message_id": "u", "content": "private history"})
    events_path = tmp_path / "state" / "sessions" / str(session_id) / "events.jsonl"
    with events_path.open("ab") as file:
        file.write(b'{"version":1')

    await store.load(session_id, repair=True)
    report = store.last_repair_report(session_id)
    assert report is not None and report.tail.backup_path is not None
    assert os.stat(report.tail.backup_path).st_mode & 0o777 == 0o600

    index = SqliteSessionIndex(tmp_path / "index.sqlite3")
    await index.rebuild(store)
    assert os.stat(index.path).st_mode & 0o777 == 0o600
