"""Claude Code 风格默认入口和全屏 TUI 渲染测试。"""

from __future__ import annotations

from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import DummyInput
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from python_agent.cli import _normalize_cli_argv, build_parser
from python_agent.core.agent import Agent
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.session.events import SessionEvent
from python_agent.terminal_ui import FullScreenTerminalUI


def _ui() -> FullScreenTerminalUI:
    """创建不访问真实终端的 TUI。"""

    return FullScreenTerminalUI(input=DummyInput(), output=DummyOutput())


def _click_line(ui: FullScreenTerminalUI, line_number: int) -> None:
    """向会话控制器发送一次真实鼠标抬起事件。"""

    ui.conversation_window.content.mouse_handler(
        MouseEvent(
            position=Point(x=0, y=line_number),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )


def test_no_subcommand_defaults_to_chat() -> None:
    """验证直接启动、直接任务和显式管理命令的参数兼容。"""

    assert _normalize_cli_argv([]) == ["chat"]
    assert _normalize_cli_argv(["--provider", "fake"]) == ["chat", "--provider", "fake"]
    assert _normalize_cli_argv(["检查项目", "--provider", "fake"]) == [
        "chat",
        "检查项目",
        "--provider",
        "fake",
    ]
    assert _normalize_cli_argv(["run", "hello"]) == ["run", "hello"]
    assert _normalize_cli_argv(["sessions"]) == ["sessions"]
    assert _normalize_cli_argv(["--help"]) == ["--help"]


def test_direct_task_parses_as_chat_initial_prompt() -> None:
    """验证改写后的直接任务进入 chat.prompt 而不是一次性 run。"""

    args = build_parser().parse_args(
        _normalize_cli_argv(["检查当前项目", "--provider", "fake", "--plain"])
    )
    assert args.command == "chat"
    assert args.prompt == "检查当前项目"
    assert args.provider == "fake"
    assert args.plain is True


def test_full_screen_ui_formats_markdown_tools_and_activity_without_ansi_text() -> None:
    """验证 Markdown、工具和模型活动进入单一会话 Buffer，不含裸 ANSI。"""

    ui = _ui()
    ui.handle_event(
        "model/request_start",
        {"turn": 1, "step": 2, "provider": "deepseek", "model": "qwen"},
    )
    ui.handle_event(
        "model/request_end",
        {"status": "completed", "duration_ms": 1234, "finish_reason": "stop"},
    )
    ui.handle_event(
        "assistant/message",
        {"content": "# 标题\n\n- **完成项**\n- 第二项", "streamed": True},
    )
    ui.handle_event("tool/call", {"name": "read_file", "arguments": {"path": "README.md"}})
    ui.handle_event(
        "tool/result",
        {"name": "read_file", "content": "file content", "is_error": False},
    )
    rendered = ui.conversation_buffer.text

    assert "Thinking · Turn 1 / Step 2 · deepseek/qwen" in rendered
    assert "Thought for 1.2s · stop" in rendered
    assert "▌ 标题" in rendered
    assert "• 完成项" in rendered
    assert "**完成项**" not in rendered
    assert "read_file" in rendered
    assert "README.md" in rendered
    assert "⚙ ▸" in rendered
    assert "file content" not in rendered
    assert "\x1b[" not in rendered

    tool_row = next(
        index for index, line in enumerate(rendered.splitlines()) if "read_file" in line
    )
    _click_line(ui, tool_row)
    expanded = ui.conversation_buffer.text
    assert "⚙ ▾" in expanded
    assert "file content" in expanded
    assert "\x1b[" not in expanded


def test_full_screen_ui_bounds_large_tool_values() -> None:
    """验证长工具参数和结果不会淹没中间滚动区。"""

    ui = _ui()
    ui.handle_event(
        "tool/call",
        {
            "call_id": "large-call",
            "name": "write_file",
            "arguments": {"path": "large.txt", "content": "y" * 3000},
        },
    )
    ui.handle_event(
        "tool/result",
        {
            "call_id": "large-call",
            "name": "write_file",
            "content": "x" * 15_000,
            "is_error": False,
        },
    )
    assert "x" * 100 not in ui.conversation_buffer.text
    ui.toggle_tool_details()
    rendered = ui.conversation_buffer.text
    assert "terminal preview truncated" in rendered
    assert len(rendered) < 14_000


def test_markdown_code_block_and_help_are_structured() -> None:
    """验证代码块、帮助和输入记录使用可着色稳定前缀。"""

    ui = _ui()
    ui.show_initial_prompt("分析项目")
    ui.handle_event(
        "assistant/message",
        {"content": "## 结果\n```python\nprint('ok')\n```", "streamed": True},
    )
    ui.show_help()
    rendered = ui.conversation_buffer.text
    assert "❯ 分析项目" in rendered
    assert "▌ 结果" in rendered
    assert "    print('ok')" in rendered
    assert "▌ Commands" in rendered


def test_resume_replays_all_messages_tool_calls_and_bash_output() -> None:
    """验证恢复 Session 时旧对话、真实命令和结果都会重新进入中间消息区。"""

    ui = _ui()
    events = [
        SessionEvent(seq=0, type="user/message", data={"content": "第一轮问题"}),
        SessionEvent(
            seq=1,
            type="assistant/message",
            data={"content": "第一轮回答", "tool_calls": []},
        ),
        SessionEvent(
            seq=2,
            type="tool/call",
            data={
                "call_id": "call-bash",
                "name": "bash",
                "arguments": {"command": "printf 'history-ok'", "cwd": "."},
            },
        ),
        SessionEvent(
            seq=3,
            type="tool/result",
            data={
                "call_id": "call-bash",
                "name": "bash",
                "content": {
                    "command": "printf 'history-ok'",
                    "cwd": ".",
                    "returncode": 0,
                    "stdout": "history-ok",
                    "stderr": "",
                },
                "is_error": False,
            },
        ),
        SessionEvent(seq=4, type="user/message", data={"content": "第二轮问题"}),
        SessionEvent(
            seq=5,
            type="assistant/message",
            data={"content": "第二轮回答", "tool_calls": []},
        ),
    ]

    ui.load_session_history(events)
    rendered = ui.conversation_buffer.text

    assert rendered.index("第一轮问题") < rendered.index("第二轮问题")
    assert "第一轮回答" in rendered
    assert "第二轮回答" in rendered
    assert "Bash [bash](printf 'history-ok')" in rendered
    assert "exit 0 · cwd ." not in rendered

    tool_row = next(index for index, line in enumerate(rendered.splitlines()) if "Bash" in line)
    _click_line(ui, tool_row)
    expanded = ui.conversation_buffer.text
    assert "exit 0 · cwd ." in expanded
    assert "history-ok" in expanded


def test_manual_history_scroll_is_sticky_until_explicit_jump_to_bottom() -> None:
    """验证向上阅读时新消息不会抢走位置，并显示未读更新数量。"""

    ui = _ui()
    for index in range(30):
        ui.handle_event("assistant/message", {"content": f"历史消息 {index}"})

    ui.jump_to_top()
    assert ui._follow_tail is False
    assert ui.conversation_buffer.document.cursor_position_row == 0

    ui.handle_event("assistant/message", {"content": "刚刚到达的新消息"})
    assert ui._follow_tail is False
    assert ui.conversation_buffer.document.cursor_position_row == 0
    assert ui._unseen_updates == 1
    assert "刚刚到达的新消息" in ui.conversation_buffer.text

    ui.jump_to_bottom()
    assert ui._follow_tail is True
    assert ui._unseen_updates == 0
    assert ui.conversation_buffer.cursor_position == len(ui.conversation_buffer.text)


def test_mouse_wheel_falls_back_safely_before_first_render() -> None:
    """验证首次 render 前没有视口快照时仍不误走隐藏游标。"""

    ui = _ui()
    for index in range(30):
        ui.handle_event("assistant/message", {"content": f"历史消息 {index}"})
    cursor_before = ui.conversation_buffer.cursor_position

    result = ui.conversation_window.content.mouse_handler(
        MouseEvent(
            position=Point(x=0, y=25),
            event_type=MouseEventType.SCROLL_UP,
            button=MouseButton.NONE,
            modifiers=frozenset(),
        )
    )

    assert result is NotImplemented
    assert ui._follow_tail is False
    # 控制器不再先走隐藏游标；返回 NotImplemented 后由所属 Window 直接修改视口。
    assert ui.conversation_buffer.cursor_position == cursor_before


def test_each_mouse_wheel_event_moves_window_viewport_three_rows() -> None:
    """验证一格真实滚轮会立即推动消息 Window 三行，不需要累计多次事件。"""

    ui = _ui()

    class RenderInfo:
        content_height = 100
        window_height = 20

        @staticmethod
        def get_height_for_line(_line: int) -> int:
            return 1

    ui.conversation_window.render_info = RenderInfo()  # type: ignore[assignment]
    ui.conversation_window.vertical_scroll = 50
    for index in range(100):
        ui.handle_event("assistant/message", {"content": f"viewport-line-{index}"})
    ui.conversation_window.vertical_scroll = 50
    result = ui.conversation_window.content.mouse_handler(
        MouseEvent(
            position=Point(x=0, y=25),
            event_type=MouseEventType.SCROLL_UP,
            button=MouseButton.NONE,
            modifiers=frozenset(),
        )
    )

    assert result is None
    assert ui.conversation_window.vertical_scroll == 47
    assert ui.conversation_buffer.document.cursor_position_row == 47
    assert ui._follow_tail is False


def test_expanded_wrapped_content_can_scroll_past_native_logical_line_boundary() -> None:
    """验证自动折成两屏行时可以滚到真实最后一屏，不会在旧边界 80 提前停止。"""

    ui = _ui()
    for index in range(100):
        ui.handle_event("assistant/message", {"content": f"wrapped-line-{index}"})

    class RenderInfo:
        content_height = 100
        window_height = 20

        @staticmethod
        def get_height_for_line(_line: int) -> int:
            return 2

    ui.conversation_window.render_info = RenderInfo()  # type: ignore[assignment]
    ui.conversation_window.vertical_scroll = 80
    ui._follow_tail = False
    for _ in range(5):
        ui.conversation_window.content.mouse_handler(
            MouseEvent(
                position=Point(x=0, y=10),
                event_type=MouseEventType.SCROLL_DOWN,
                button=MouseButton.NONE,
                modifiers=frozenset(),
            )
        )

    assert ui.conversation_window.vertical_scroll == 90
    assert ui.conversation_buffer.document.cursor_position_row == 90
    assert ui._follow_tail is True


def test_large_resumed_multi_turn_history_can_reach_the_first_turn() -> None:
    """验证多轮长回答恢复后全部保留，并可从末尾逐页回到第一轮。"""

    ui = _ui()
    events: list[SessionEvent] = []
    sequence = 0
    for turn in range(1, 7):
        events.append(
            SessionEvent(
                seq=sequence,
                type="user/message",
                data={"content": f"RESUME_TURN_{turn}_USER"},
            )
        )
        sequence += 1
        answer = "\n".join(f"TURN_{turn}_LINE_{line:02d}" for line in range(1, 31))
        events.append(
            SessionEvent(
                seq=sequence,
                type="assistant/message",
                data={"content": answer, "tool_calls": []},
            )
        )
        sequence += 1

    ui.load_session_history(events)
    assert "RESUME_TURN_1_USER" in ui.conversation_buffer.text
    assert "TURN_1_LINE_01" in ui.conversation_buffer.text
    assert "TURN_6_LINE_30" in ui.conversation_buffer.text

    for _ in range(30):
        ui.scroll_page(-1)

    assert ui.conversation_buffer.document.cursor_position_row == 0
    assert ui._follow_tail is False


def test_tool_output_is_collapsed_by_default_and_title_click_toggles_it() -> None:
    """验证命令标题始终可见，结果只在点击该工具后展开。"""

    ui = _ui()
    output = "\n".join(f"output-line-{index}" for index in range(30))
    ui.handle_event(
        "tool/call",
        {
            "call_id": "call-1",
            "name": "bash",
            "arguments": {"command": "find . -type f", "cwd": "."},
        },
    )
    ui.handle_event(
        "tool/result",
        {
            "call_id": "call-1",
            "name": "bash",
            "content": {
                "command": "find . -type f",
                "cwd": ".",
                "returncode": 0,
                "stdout": output,
                "stderr": "",
            },
            "is_error": False,
        },
    )

    collapsed = ui.conversation_buffer.text
    assert "⚙ ▸ Bash [bash](find . -type f)" in collapsed
    assert "output-line-0" not in collapsed
    assert "output-line-29" not in collapsed

    tool_row = next(index for index, line in enumerate(collapsed.splitlines()) if "Bash" in line)
    _click_line(ui, tool_row)
    expanded = ui.conversation_buffer.text
    assert "⚙ ▾ Bash [bash](find . -type f)" in expanded
    assert "output-line-0" in expanded
    assert "output-line-29" in expanded

    _click_line(ui, tool_row)
    collapsed_again = ui.conversation_buffer.text
    assert "⚙ ▸ Bash [bash](find . -type f)" in collapsed_again
    assert "output-line-0" not in collapsed_again


def test_banner_scrolls_with_history_and_input_uses_compact_claude_layout() -> None:
    """验证横幅不是固定 Window，空输入框一行且只固定底部四行结构。"""

    ui = _ui()
    agent = Agent(FakeAdapter())
    ui.agent = agent
    ui.load_session_history([])

    rendered = ui.conversation_buffer.text
    assert "python-agent  v0.1.0" in rendered
    assert f"session {agent.id}" in rendered
    assert not hasattr(ui, "header")
    assert not hasattr(ui, "sticky_prompt")
    assert ui.input.window.height.min == 1
    assert ui.input.window.height.max == 5
    assert ui.input.window.dont_extend_height() is True

    root = ui.application.layout.container
    assert len(root.children) == 5
    assert root.children[0] is ui.conversation_window
    assert root.children[2] is ui.input.window
    assert root.children[4] is ui.status
