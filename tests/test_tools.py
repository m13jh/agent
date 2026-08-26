from pathlib import Path

from python_agent.ids import SessionId
from python_agent.llm.types import ToolCall
from python_agent.tools.builtins import ReadFileTool
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext


async def test_read_file_rejects_path_outside_workspace(tmp_path: Path) -> None:
    context = ToolContext(session_id=SessionId("session"), workspace=tmp_path)
    result = await ToolRuntime(ToolRegistry([ReadFileTool()])).execute(
        ToolCall(id="call", name="read_file", arguments={"path": "../secret.txt"}),
        context,
    )
    assert result.is_error is True
    assert "outside workspace" in result.content
