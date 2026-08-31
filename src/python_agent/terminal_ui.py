"""Claude Code 风格的单 renderer 全屏终端应用。

这个模块只负责“怎样在终端里展示和输入”，不参与 Agent 推理。Claude Code 的启动
横幅并不是固定头部，而是会随着旧消息一起滚走；真正固定的只有底部输入框和状态栏。
因此界面只保留两个布局区域：占据绝大部分空间的历史区，以及最下方紧凑输入区。

这里刻意不混用 ``print``、Rich Live 和 prompt_toolkit。整个全屏期间只有
prompt_toolkit 一个 renderer，因而不会把 ANSI 控制字符当普通文本打印出来。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import (
    StyleAndTextTuples,
    fragment_list_to_text,
    fragment_list_width,
)
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    Dimension,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output.base import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from python_agent.core.agent import Agent

DispatchLine = Callable[[str], Awaitable[bool]]


class TerminalUI(Protocol):
    """chat 命令分发层需要的最小终端接口。"""

    def print_notice(self, message: str, *, style: str = "class:notice") -> None: ...

    def show_help(self) -> None: ...

    def show_status(self, agent: Agent) -> None: ...

    def show_transcript(self, transcript: str) -> None: ...

    def toggle_tool_details(self) -> bool: ...


@dataclass(slots=True)
class _TextBlock:
    """一段已经完成轻量 Markdown 转换的普通会话文本。"""

    lines: list[str]


@dataclass(slots=True)
class _ToolBlock:
    """一次工具调用及其随后到达的结果。

    工具调用和结果在事件流中是两条独立事件。将它们合并成一个结构化块后，结果到达时
    可以原位更新，而不是把参数、输出散落成无法折叠的普通字符串。
    """

    call_id: str
    name: str
    arguments: dict[str, Any]
    has_result: bool = False
    result: Any = None
    is_error: bool = False
    # 每个工具独立维护折叠状态。默认 False，只有点击标题或执行“展开全部”才显示结果。
    expanded: bool = False


_ConversationBlock = _TextBlock | _ToolBlock


class _ConversationLexer(Lexer):
    """根据稳定行前缀给会话、活动、工具和代码分配样式。"""

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        def get_line(lineno: int) -> StyleAndTextTuples:
            line = document.lines[lineno]
            if line.startswith("❯ "):
                return [("class:conversation.user", line)]
            if line.startswith(("  ◆◆", " ◆  ◆", "   ◆", "         Type ")):
                return [("class:conversation.banner", line)]
            if line.startswith("● ") or line.startswith("↻ "):
                return [("class:conversation.activity", line)]
            if line.startswith("✓ "):
                return [("class:conversation.success", line)]
            if line.startswith("× ") or line.startswith("■ "):
                return [("class:conversation.error", line)]
            if line.startswith("⚙ ▾"):
                return [("class:conversation.tool-expanded", line)]
            if line.startswith("⚙ ▸"):
                return [("class:conversation.tool", line)]
            if line.startswith(("  │ ", "  └ ")):
                return [("class:conversation.tool-output-expanded", line)]
            if line.startswith("◇ ") or line.startswith("◆ "):
                return [("class:conversation.child", line)]
            if line.startswith("▌ "):
                return [("class:conversation.heading", line)]
            if line.startswith("    "):
                return [("class:conversation.code", line)]
            return [("class:conversation.assistant", line)]

        return get_line


class _BlockBackgroundProcessor(Processor):
    """把用户提示条和展开工具块的背景补齐到消息区右边缘。

    Lexer 只会给真实文字着色，默认不会覆盖行尾空白；Claude Code 截图中的用户提示和
    展开工具却是完整横条。Processor 能拿到当前实际视口宽度，所以比在 Buffer 文本里
    硬编码空格更可靠，终端缩放后也会在下一次渲染时自动使用新宽度。
    """

    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
        fragments = list(transformation_input.fragments)
        text = fragment_list_to_text(fragments)
        if text.startswith("❯ "):
            padding_style = "class:conversation.user"
        elif text.startswith("⚙ ▾"):
            padding_style = "class:conversation.tool-expanded"
        elif text.startswith(("  │ ", "  └ ")):
            padding_style = "class:conversation.tool-output-expanded"
        else:
            return Transformation(fragments)

        # 终端最后一列写满后部分仿真器会自动换到下一行，产生“每行之间多一空行”的
        # 假象，因此保留最右一列给 renderer/滚动条，而不是把可写宽度精确填满。
        target_width = max(0, transformation_input.width - 1)
        padding = max(0, target_width - fragment_list_width(fragments))
        if padding:
            fragments.append((padding_style, " " * padding))
        return Transformation(fragments)


class _HistoryBufferControl(BufferControl):
    """为只读会话 Buffer 截获鼠标滚轮。

    prompt_toolkit 默认会把键盘焦点留在输入框；普通 ``BufferControl`` 在未获得焦点时
    不会改变会话游标。此外，默认 Window 滚动也不知道应用的 sticky-follow 状态。
    此控制器只截获滚轮并交给 UI 的显式历史滚动函数，其余点击/选择行为仍沿用父类。
    """

    def __init__(
        self,
        *args: Any,
        on_mouse_scroll: Callable[[int], bool],
        on_tool_click: Callable[[int], bool],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_mouse_scroll = on_mouse_scroll
        self._on_tool_click = on_tool_click

    def mouse_handler(self, mouse_event: MouseEvent) -> Any:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            # 回调直接推动所属消息 Window 的视口；返回 None 防止 Window 再重复滚一格。
            return None if self._on_mouse_scroll(-3) else NotImplemented
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            return None if self._on_mouse_scroll(3) else NotImplemented
        # Window 已经把屏幕坐标转换成 Buffer 的真实逻辑行号，因此即使工具标题已经
        # 滚到视口中间，这里仍能精确找到对应 call_id。返回 None 会保留输入框焦点。
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            if self._on_tool_click(mouse_event.position.y):
                return None
        return super().mouse_handler(mouse_event)


class FullScreenTerminalUI:
    """横幅随历史滚动、底部输入固定，并能回放完整 Session 的全屏 TUI。"""

    _TOOL_LABELS = {
        "read_file": "Read",
        "list_files": "List",
        "search_text": "Search",
        "bash": "Bash",
        "write_file": "Write",
        "apply_patch": "ApplyPatch",
        "echo": "Echo",
        "spawn_agent": "SpawnAgent",
        "subagent_followup": "SubagentFollowup",
        "subagent_interrupt": "SubagentInterrupt",
        "list_subagents": "ListSubagents",
        "list_skills": "ListSkills",
        "load_skill": "LoadSkill",
    }

    def __init__(self, *, input: Input | None = None, output: Output | None = None) -> None:
        self.agent: Agent | None = None
        self._dispatch: DispatchLine | None = None

        # ``_blocks`` 是完整会话真相的 UI 投影；``_lines`` 是当前折叠模式下的渲染缓存。
        # 保留结构化块是实现工具详情切换的关键，否则折叠后便无法无损展开。
        self._blocks: list[_ConversationBlock] = []
        self._lines: list[str] = []
        self._tools_by_id: dict[str, _ToolBlock] = {}
        # 只把工具标题行映射到 call_id。鼠标点击输出正文仍可正常选择文本，不会误折叠。
        self._tool_header_rows: dict[int, str] = {}
        self._history_loaded = False

        # sticky-follow=True 表示新内容到达时自动贴住底部。用户一旦向上滚动便关闭它，
        # 此后新消息不会抢走阅读位置，直到 PageDown 到底或按 Ctrl+End。
        self._follow_tail = True
        self._unseen_updates = 0
        self._tool_details_expanded = False

        self.conversation_buffer = Buffer(read_only=True)
        conversation_control = _HistoryBufferControl(
            buffer=self.conversation_buffer,
            lexer=_ConversationLexer(),
            input_processors=[_BlockBackgroundProcessor()],
            focusable=False,
            on_mouse_scroll=self._scroll_with_mouse,
            on_tool_click=self.toggle_tool_at_line,
        )
        self.conversation_window = Window(
            conversation_control,
            wrap_lines=True,
            right_margins=[ScrollbarMargin(display_arrows=True)],
            allow_scroll_beyond_bottom=False,
            style="class:conversation",
        )
        self.input = TextArea(
            # Claude Code 空输入框只有一行；输入换行或自动折行后再按内容增长，最多五行，
            # 因而短输入不会永久浪费三行会话空间，长输入又仍然可编辑。
            height=Dimension(min=1, max=5),
            dont_extend_height=True,
            prompt=[("class:input.prompt", "❯ ")],
            multiline=True,
            wrap_lines=True,
            style="class:input",
        )
        self.status = Window(
            FormattedTextControl(self._status_text),
            height=1,
            wrap_lines=False,
            style="class:status",
        )

        def separator() -> Window:
            return Window(height=1, char="─", style="class:separator")

        # 启动横幅已经成为 conversation 的第一个普通块，所以这里只固定 Claude Code
        # 真正固定的底部区域：上边线、动态单行输入、下边线和一行状态。空输入时总共
        # 只占四行，其余终端高度全部交给历史消息。
        root = HSplit(
            [
                self.conversation_window,
                separator(),
                self.input,
                separator(),
                self.status,
            ]
        )
        self.key_bindings = self._key_bindings()
        self.application: Application[None] = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=self.key_bindings,
            style=self._style(),
            full_screen=True,
            mouse_support=True,
            enable_page_navigation_bindings=False,
            refresh_interval=0.25,
            input=input,
            output=output,
        )

    @staticmethod
    def _style() -> Style:
        """使用终端主题背景，只设置前景和少量固定条背景。"""

        return Style.from_dict(
            {
                "separator": "#666666",
                "conversation": "#ffffff",
                "conversation.banner": "#d58aa4 bold",
                "conversation.user": "bg:#3a3a3a #ffffff bold",
                "conversation.activity": "#a0a0a0",
                "conversation.success": "#70d7a7",
                "conversation.error": "#ff6b6b bold",
                "conversation.tool": "#64b5f6 bold",
                "conversation.tool-expanded": "bg:#414141 #64b5f6 bold",
                "conversation.tool-output-expanded": "bg:#414141 #e0e0e0",
                "conversation.child": "#ba8cff",
                "conversation.heading": "#ffffff bold",
                "conversation.code": "#d0d0d0",
                "conversation.assistant": "#ffffff",
                "input": "#ffffff",
                "input.prompt": "#ffffff bold",
                "status": "#a0a0a0",
                "status.running": "#ffd166 bold",
                "notice": "#a0a0a0",
                "notice.warning": "#ffd166",
                "notice.error": "#ff6b6b",
                "notice.success": "#70d7a7",
            }
        )

    @staticmethod
    def _banner_lines(agent: Agent) -> list[str]:
        """生成会随历史滚走的启动横幅，而不是固定 Window。"""

        workspace = str(agent.session.header.cwd or "-")
        return [
            "",
            "  ◆◆    python-agent  v0.1.0",
            f" ◆  ◆   {agent.config.provider}/{agent.config.model}  ·  "
            f"{agent.config.permission_mode}",
            f"  ◆◆    {workspace}",
            f"   ◆    session {agent.id}",
            "         Type /help for commands",
            "",
        ]

    def _status_text(self) -> StyleAndTextTuples:
        """用一行显示 Agent 状态；不把工具模式等次要信息塞满底栏。"""

        if self.agent is None:
            return [("class:status", " manual mode on · /help")]

        scroll = ""
        if not self._follow_tail:
            current = self.conversation_buffer.document.cursor_position_row + 1
            total = max(1, len(self._lines))
            unseen = f" · {self._unseen_updates} new" if self._unseen_updates else ""
            scroll = f" · history {current}/{total}{unseen} · Ctrl+End ↓"

        request = self.agent.active_request
        queued = len(self.agent.inbox.pending("next_turn"))
        if request is not None:
            text = (
                f" working · {request.provider}/{request.model} · Turn {request.turn}/"
                f"Step {request.step} · {request.elapsed_seconds:.1f}s · queued {queued}{scroll}"
            )
            return [("class:status.running", text)]
        return [
            (
                "class:status",
                f" manual mode on · {self.agent.status} · queued {queued} · "
                f"/help · Ctrl+D exit{scroll}",
            )
        ]

    def _invalidate(self) -> None:
        """在 Application 运行内外都安全请求重绘。"""

        try:
            get_app().invalidate()
        except RuntimeError:
            pass

    def _render_blocks(self) -> list[str]:
        """将结构化块投影成当前工具详情模式下的稳定逻辑行。"""

        rendered: list[str] = []
        self._tool_header_rows.clear()
        for block in self._blocks:
            if isinstance(block, _TextBlock):
                rendered.extend(block.lines)
            else:
                # 标题永远是工具块的第一行。保存其最终逻辑行号，供鼠标 MOUSE_UP 精确
                # 定位；折叠/展开后下一次渲染会完整重建映射，不会留下过期坐标。
                self._tool_header_rows[len(rendered)] = block.call_id
                rendered.extend(self._render_tool(block))
        return rendered

    def _sync_buffer(self, *, content_update: bool = False) -> None:
        """重建只读 Buffer，同时保持用户的历史阅读锚点。

        follow-tail 打开时游标永远放在文末，Window 因此自动展示最新内容。关闭时按逻辑
        行保存游标，而不是保存旧字符偏移；工具块原位展开导致前文长度改变时也不会突然
        跳到一个完全不同的远端位置。
        """

        old_row = self.conversation_buffer.document.cursor_position_row
        self._lines = self._render_blocks()
        text = "\n".join(self._lines)
        if self._follow_tail:
            cursor = len(text)
        else:
            document = Document(text)
            target_row = min(old_row, max(0, len(document.lines) - 1))
            cursor = document.translate_row_col_to_index(target_row, 0)
            if content_update:
                self._unseen_updates += 1
        self.conversation_buffer.set_document(
            Document(text, cursor_position=cursor),
            bypass_readonly=True,
        )
        self._invalidate()

    def _append_block(self, block: _ConversationBlock) -> None:
        self._blocks.append(block)
        self._sync_buffer(content_update=True)

    def _append(self, text: str = "") -> None:
        """追加一段普通文本；一个块可包含多行。"""

        self._append_block(_TextBlock(text.splitlines() or [""]))

    @staticmethod
    def _markdown_lines(content: str) -> list[str]:
        """把常见 Markdown 转成适合 TUI 的无转义纯文本结构。"""

        rendered: list[str] = []
        in_code = False
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                if in_code:
                    rendered.append("    ── code ──")
                continue
            line = raw_line
            if in_code:
                rendered.append("    " + line)
                continue
            heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
            if heading:
                line = "▌ " + heading.group(1)
            elif re.match(r"^[-*+]\s+", stripped):
                line = "  • " + re.sub(r"^[-*+]\s+", "", stripped)
            elif re.match(r"^\d+\.\s+", stripped):
                line = "  " + stripped
            elif stripped in {"---", "***", "___"}:
                line = "  " + "─" * 32
            line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
            line = re.sub(r"__(.*?)__", r"\1", line)
            line = re.sub(r"`([^`]*)`", r"\1", line)
            rendered.append(line)
        return rendered

    @staticmethod
    def _one_line(value: Any, maximum: int = 260) -> str:
        """把工具主题压成一行，避免大参数把调用标题本身淹没。"""

        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = " ↵ ".join(part.strip() for part in text.splitlines() if part.strip())
        return text if len(text) <= maximum else text[: maximum - 1] + "…"

    @classmethod
    def _tool_invocation(cls, block: _ToolBlock) -> str:
        """从不同工具参数中抽取最有辨识度的信息，尤其保证 Bash 命令始终可见。"""

        name = block.name
        arguments = block.arguments
        label = cls._TOOL_LABELS.get(name, name)
        if name == "bash":
            subject = arguments.get("command", "")
        elif name in {"read_file", "list_files", "write_file"}:
            subject = arguments.get("path", ".")
        elif name == "search_text":
            query = cls._one_line(arguments.get("query", ""), 120)
            subject = f"{query!r} in {arguments.get('path', '.')}"
        elif name == "apply_patch":
            patch = str(arguments.get("patch", ""))
            files = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
            subject = ", ".join(files) or "workspace patch"
        elif name == "spawn_agent":
            subject = arguments.get("description") or arguments.get("task") or arguments
        else:
            subject = arguments
        # 同时保留面向人的短标签和注册表里的精确工具名，便于用户审计实际调用。
        exact_name = f" [{name}]" if label != name else ""
        return f"{label}{exact_name}({cls._one_line(subject)})"

    @staticmethod
    def _result_text(block: _ToolBlock) -> str:
        """把结果转换为可读文本；Bash 特别拆出退出码、stdout 和 stderr。"""

        value = block.result
        if block.name == "bash" and isinstance(value, dict):
            header = f"exit {value.get('returncode', '?')} · cwd {value.get('cwd', '.')}"
            sections = [header]
            stdout = value.get("stdout")
            stderr = value.get("stderr")
            if isinstance(stdout, str) and stdout:
                sections.extend(["stdout:", stdout.rstrip("\n")])
            if isinstance(stderr, str) and stderr:
                sections.extend(["stderr:", stderr.rstrip("\n")])
            return "\n".join(sections)
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

    def _render_tool(self, block: _ToolBlock) -> list[str]:
        """默认只渲染标题；单个工具展开后才渲染结果正文。"""

        state = "…" if not block.has_result else ("failed" if block.is_error else "done")
        disclosure = "▾" if block.expanded else "▸"
        lines = [f"⚙ {disclosure} {self._tool_invocation(block)} · {state}"]

        # 折叠态连预览都不显示，避免 list/search/bash 的长输出占满聊天窗口。工具名、
        # 精确注册名和 Bash 命令仍保留在标题上，点击这一行即可原位展开。
        if not block.expanded:
            return lines
        if not block.has_result:
            return [*lines, "  └ running…"]

        text = self._result_text(block)
        if not text:
            return [*lines, "  └ completed (no output)"]

        # 展开态仍设置一个仅针对终端的安全上限；Session 中的原始 tool/result 从不改变。
        maximum_chars = 12_000
        maximum_lines = 120
        original_length = len(text)
        bounded = text[:maximum_chars]
        # prompt_toolkit 对“一个逻辑行自身高于整个窗口”的滚动支持有限。命令输出偶尔会
        # 返回数千字符却没有换行；先按显示安全长度切成若干逻辑行，避免展开后困在这个
        # 超高逻辑行里。这里只改变终端投影，Session 原始结果仍然完整保留。
        source_lines: list[str] = []
        for raw_line in bounded.splitlines() or [""]:
            if not raw_line:
                source_lines.append("")
                continue
            source_lines.extend(
                raw_line[index : index + 240] for index in range(0, len(raw_line), 240)
            )
        visible = source_lines[:maximum_lines]
        for index, line in enumerate(visible):
            lines.append(("  └ " if index == 0 else "  │ ") + line)

        hidden_lines = max(0, len(source_lines) - len(visible))
        was_truncated = original_length > len(bounded) or hidden_lines > 0
        if was_truncated:
            lines.append("  │ … terminal preview truncated；完整结果已保存在 Session")
        return lines

    def toggle_tool_at_line(self, line_number: int) -> bool:
        """切换被点击标题对应的单个工具块；返回该行是否确实是工具标题。"""

        call_id = self._tool_header_rows.get(line_number)
        if call_id is None:
            return False
        block = self._tools_by_id.get(call_id)
        if block is None:
            return False
        block.expanded = not block.expanded
        self._tool_details_expanded = bool(self._tools_by_id) and all(
            item.expanded for item in self._tools_by_id.values()
        )
        self._sync_buffer()
        return True

    def _find_tool_for_result(self, data: dict[str, Any]) -> _ToolBlock | None:
        """按 call_id 关联结果；兼容早期无 call_id 的测试/旧事件。"""

        call_id = data.get("call_id")
        if call_id is not None:
            found = self._tools_by_id.get(str(call_id))
            if found is not None:
                return found
        name = data.get("name")
        return next(
            (
                block
                for block in reversed(self._blocks)
                if isinstance(block, _ToolBlock)
                and not block.has_result
                and (name is None or block.name == name)
            ),
            None,
        )

    def _record_tool_call(self, data: dict[str, Any]) -> None:
        raw_arguments = data.get("arguments", {})
        arguments = (
            dict(raw_arguments) if isinstance(raw_arguments, dict) else {"value": raw_arguments}
        )
        call_id = str(data.get("call_id") or f"ui-tool-{len(self._tools_by_id) + 1}")
        # Session 回放或异常重发不应生成两个相同工具块。
        if call_id in self._tools_by_id:
            return
        block = _ToolBlock(
            call_id=call_id,
            name=str(data.get("name") or "unknown_tool"),
            arguments=arguments,
            expanded=self._tool_details_expanded,
        )
        self._tools_by_id[call_id] = block
        self._append_block(block)

    def _record_tool_result(self, data: dict[str, Any]) -> None:
        block = self._find_tool_for_result(data)
        if block is None:
            # 损坏或旧版日志可能只剩结果。仍然创建明确的孤立块，避免审计信息静默消失。
            self._record_tool_call(
                {
                    "call_id": data.get("call_id") or f"orphan-result-{len(self._tools_by_id) + 1}",
                    "name": data.get("name") or "unknown_tool",
                    "arguments": {},
                }
            )
            block = self._find_tool_for_result(data)
        if block is None:
            return
        block.has_result = True
        block.result = data.get("content")
        block.is_error = bool(data.get("is_error"))
        self._sync_buffer(content_update=True)

    def _scroll_with_mouse(self, direction: int) -> bool:
        """按实际渲染高度推动消息视口，展开长工具时也不会提前卡住。

        prompt_toolkit 原生 ``_scroll_down`` 用 ``逻辑行数 - 窗口屏幕行数`` 估算底部。
        当一个工具输出行自动折成多行时，这两个单位不同，原生实现会过早停止。这里用
        ``render_info.get_height_for_line`` 计算每个逻辑行真实占用的屏幕高度，并自行求出
        最后一屏允许的起始行。
        """

        info = self.conversation_window.render_info
        if info is None:
            if direction < 0:
                self._follow_tail = False
            return False

        maximum_top = self._maximum_viewport_top(info)
        current_top = max(0, min(self.conversation_window.vertical_scroll, maximum_top))
        target_top = current_top
        moved_rows = 0

        if direction < 0:
            self._follow_tail = False
            while target_top > 0 and moved_rows < abs(direction):
                target_top -= 1
                moved_rows += max(1, info.get_height_for_line(target_top))
        else:
            while target_top < maximum_top and moved_rows < direction:
                moved_rows += max(1, info.get_height_for_line(target_top))
                target_top += 1
            if target_top >= maximum_top:
                self._follow_tail = True
                self._unseen_updates = 0

        # 同时移动 Window 起点和只读 Buffer 游标。下一次 render 的“保证游标可见”逻辑
        # 因而不会把我们刚设置的视口又拉回旧位置。
        self.conversation_window.vertical_scroll = target_top
        self.conversation_window.vertical_scroll_2 = 0
        document = self.conversation_buffer.document
        self.conversation_buffer.cursor_position = document.translate_row_col_to_index(
            target_top, 0
        )
        self._invalidate()
        return True

    @staticmethod
    def _maximum_viewport_top(info: Any) -> int:
        """返回仍能让最后一行出现在窗口内的最大逻辑起始行。"""

        content_height = int(info.content_height)
        remaining_height = max(1, int(info.window_height))
        top = content_height
        while top > 0:
            candidate = top - 1
            line_height = max(1, int(info.get_height_for_line(candidate)))
            if line_height > remaining_height and top < content_height:
                break
            top = candidate
            remaining_height -= min(line_height, remaining_height)
            if remaining_height <= 0:
                break
        return max(0, min(top, content_height - 1))

    def _scroll_history(self, delta_lines: int) -> None:
        """按逻辑行滚动历史；负数向上、正数向下。"""

        if not self._lines:
            return
        current_row = self.conversation_buffer.document.cursor_position_row
        target_row = max(0, min(len(self._lines) - 1, current_row + delta_lines))
        if target_row >= len(self._lines) - 1:
            self.jump_to_bottom()
            return

        self._follow_tail = False
        document = self.conversation_buffer.document
        self.conversation_buffer.cursor_position = document.translate_row_col_to_index(
            target_row, 0
        )
        self._invalidate()

    def scroll_page(self, direction: int) -> None:
        """滚动一页，供按键绑定和确定性测试共同使用。"""

        render_info = self.conversation_window.render_info
        page = max(3, (render_info.window_height - 2) if render_info is not None else 12)
        self._scroll_history(page if direction > 0 else -page)

    def jump_to_bottom(self) -> None:
        """重新开启自动跟随并清除“新消息”计数。"""

        self._follow_tail = True
        self._unseen_updates = 0
        self.conversation_buffer.cursor_position = len(self.conversation_buffer.text)
        self._invalidate()

    def jump_to_top(self) -> None:
        """跳到完整 Session 的第一行并暂停自动跟随。"""

        if not self._lines:
            return
        self._follow_tail = False
        self.conversation_buffer.cursor_position = 0
        self._invalidate()

    def print_notice(self, message: str, *, style: str = "class:notice") -> None:
        """把交互命令反馈写入会话区；style 编码成稳定行前缀。"""

        if "error" in style or style == "red":
            prefix = "■ "
        elif "warning" in style or style == "yellow":
            prefix = "● "
        elif "success" in style or style == "green":
            prefix = "✓ "
        else:
            prefix = "  "
        self._append(prefix + message)

    def show_help(self) -> None:
        """在会话区显示命令和滚动快捷键。"""

        self._append("▌ Commands")
        rows = [
            ("普通文本", "followup；运行中排队"),
            ("/steer 内容", "下一个 Step 纠偏"),
            ("/inject 内容", "静默写入上下文"),
            ("/cancel [keep]", "取消；可选择保留 Inbox"),
            ("/status", "Agent/队列/请求状态"),
            ("/transcript", "Session 审计记录"),
            ("/tools", "展开/折叠全部工具输出"),
            ("/wait", "等待 idle"),
            ("/exit", "退出"),
        ]
        for command, description in rows:
            self._append(f"    {command:<18} {description}")
        self._append("    PageUp/PageDown 或滚轮浏览 · Ctrl+Home 顶部 · Ctrl+End 底部")
        self._append("    点击工具标题单独展开/折叠 · Ctrl+O 切换全部 · Alt+Enter 换行")

    def show_status(self, agent: Agent) -> None:
        """把当前状态快照写入会话区。"""

        self._append("▌ Status")
        self._append(f"    status      {agent.status}")
        self._append(f"    next_turn   {len(agent.inbox.pending('next_turn'))}")
        self._append(f"    next_step   {len(agent.inbox.pending('next_step'))}")
        expanded = sum(block.expanded for block in self._tools_by_id.values())
        self._append(f"    tools       expanded {expanded}/{len(self._tools_by_id)}")
        request = agent.active_request
        if request is None:
            self._append("    request     none")
        else:
            self._append(
                f"    request     Turn {request.turn} / Step {request.step} · "
                f"{request.provider}/{request.model} · {request.elapsed_seconds:.1f}s"
            )

    def show_transcript(self, transcript: str) -> None:
        """显示审计 transcript；长行由会话 Window 自动换行。"""

        self._append("▌ Transcript")
        for line in (transcript or "（当前没有 transcript）").splitlines():
            self._append("    " + line)

    def show_initial_prompt(self, prompt: str, *, update_sticky: bool = True) -> None:
        """把用户输入作为普通历史块显示；它不会再占用固定顶部区域。"""

        # 保留关键字仅为兼容调用方；当前布局已经没有 sticky prompt。
        del update_sticky
        self.jump_to_bottom()
        if self._blocks:
            self._append()
        self._append("❯ " + prompt)

    def toggle_tool_details(self) -> bool:
        """批量展开或折叠全部工具；鼠标点击则只影响单个工具。"""

        blocks = list(self._tools_by_id.values())
        if not blocks:
            self.print_notice("当前会话还没有工具调用。")
            return False
        self._tool_details_expanded = not all(block.expanded for block in blocks)
        for block in blocks:
            block.expanded = self._tool_details_expanded
        self._sync_buffer()
        mode = "全部展开" if self._tool_details_expanded else "全部折叠"
        self.print_notice(f"工具调用已{mode}；也可以点击单个工具标题切换。")
        return self._tool_details_expanded

    def load_session_history(self, events: Iterable[Any]) -> None:
        """从持久 Session 回放用户、回答和工具记录。

        旧实现只订阅“进程启动之后”的实时事件，``--resume`` 后自然看不到过去的消息和
        命令。这里将 Session Event 作为事实源重新投影；控制类事件仍留在 transcript，
        不挤占主要对话视图。
        """

        self._blocks.clear()
        self._tools_by_id.clear()
        self._tool_header_rows.clear()
        self._follow_tail = True
        self._unseen_updates = 0
        self._tool_details_expanded = False

        # 横幅是可滚动历史的第一个块。对话足够长时它会自然离开视口，不再永久侵占空间。
        if self.agent is not None:
            self._blocks.append(_TextBlock(self._banner_lines(self.agent)))

        for event in events:
            event_type = getattr(event, "type", None)
            data = getattr(event, "data", None)
            if not isinstance(event_type, str) or not isinstance(data, dict):
                continue
            if event_type == "user/message":
                content = data.get("content")
                if isinstance(content, str) and content:
                    self.show_initial_prompt(content)
            elif event_type == "assistant/message":
                content = data.get("content")
                if isinstance(content, str) and content:
                    self._append_block(_TextBlock([*self._markdown_lines(content), ""]))
            elif event_type == "tool/call":
                self._record_tool_call(data)
            elif event_type == "tool/result":
                self._record_tool_result(data)
            elif event_type == "agent/limit":
                self._append(f"■ budget · {data.get('reason')} · {data.get('message')}")

        self._history_loaded = True
        self.jump_to_bottom()
        self._sync_buffer()

    def handle_event(self, event_type: str, data: dict[str, Any]) -> None:
        """把 Agent 实时事件转换为结构化会话块。"""

        if event_type == "model/request_start":
            self._append(
                f"● Thinking · Turn {data.get('turn')} / Step {data.get('step')} · "
                f"{data.get('provider')}/{data.get('model')}"
            )
        elif event_type == "assistant/delta":
            # 增量不直接写入 Session 视图，完成后的 assistant/message 一次写入，避免每个
            # token 都重建大 Buffer；底栏仍通过 active_request 显示持续耗时。
            return
        elif event_type == "assistant/message":
            content = data.get("content")
            if isinstance(content, str) and content:
                self._append_block(_TextBlock([*self._markdown_lines(content), ""]))
        elif event_type == "model/request_end":
            duration = data.get("duration_ms", 0)
            seconds = float(duration) / 1000 if isinstance(duration, (int, float)) else 0.0
            status = data.get("status")
            icon = "✓" if status == "completed" else "×"
            reason = data.get("finish_reason") or data.get("error_type")
            self._append(f"{icon} Thought for {seconds:.1f}s · {reason}")
        elif event_type == "model/request_error" and data.get("will_retry"):
            self._append(f"↻ Retry in {data.get('delay_seconds', 0)}s · {data.get('error_type')}")
        elif event_type == "tool/call":
            self._record_tool_call(data)
        elif event_type == "tool/result":
            self._record_tool_result(data)
        elif event_type == "agent/limit":
            self._append(f"■ budget · {data.get('reason')} · {data.get('message')}")
        elif event_type == "agent/error":
            self._append(f"■ {data.get('error_type')}: {data.get('message')}")
        elif event_type == "subagent/created":
            self._append(
                f"◇ child {data.get('child_id')} · depth {data.get('delegation_depth')} · "
                f"{data.get('description')}"
            )
        elif event_type == "subagent/settled":
            self._append(f"◆ child {data.get('child_id')} settled · {data.get('finish_reason')}")
        elif event_type == "subagent/error":
            self._append(f"■ child {data.get('child_id')} · {data.get('message')}")

    def _key_bindings(self) -> KeyBindings:
        """定义提交、取消、工具展开和独立历史滚动快捷键。"""

        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event: Any) -> None:
            text = self.input.text.strip()
            dispatch_handler = self._dispatch
            if not text or dispatch_handler is None:
                return
            self.input.buffer.reset()
            self.show_initial_prompt(text, update_sticky=not text.startswith("/"))

            async def dispatch() -> None:
                try:
                    should_exit = await dispatch_handler(text)
                except Exception as exc:  # pragma: no cover - 防止真实插件异常杀死 renderer
                    self.print_notice(f"命令执行失败：{type(exc).__name__}: {exc}", style="red")
                    return
                if should_exit:
                    event.app.exit()

            event.app.create_background_task(dispatch())

        @bindings.add("escape", "enter")
        def newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("c-c")
        def cancel(event: Any) -> None:
            if self.input.text:
                self.input.buffer.reset()
                return
            if (
                self._dispatch is not None
                and self.agent is not None
                and self.agent.status == "running"
            ):
                event.app.create_background_task(self._dispatch("/cancel"))
            else:
                self.print_notice("输入 /exit 或按 Ctrl+D 退出。")

        @bindings.add("c-d")
        def exit_app(event: Any) -> None:
            event.app.exit()

        # eager=True 让这些全局滚动键优先于输入 TextArea 的默认绑定；滚动时输入焦点和
        # 尚未提交的文字完全不变，这正是消息区与输入区解耦的行为。
        @bindings.add("pageup", eager=True)
        def page_up(event: Any) -> None:
            del event
            self.scroll_page(-1)

        @bindings.add("pagedown", eager=True)
        def page_down(event: Any) -> None:
            del event
            self.scroll_page(1)

        @bindings.add("c-home", eager=True)
        def jump_top(event: Any) -> None:
            del event
            self.jump_to_top()

        @bindings.add("c-end", eager=True)
        def jump_bottom(event: Any) -> None:
            del event
            self.jump_to_bottom()

        @bindings.add("c-o", eager=True)
        def toggle_tools(event: Any) -> None:
            del event
            self.toggle_tool_details()

        return bindings

    async def run(
        self,
        agent: Agent,
        dispatch: DispatchLine,
        *,
        initial_prompt: str | None = None,
    ) -> None:
        """回放完整 Session 后运行全屏 Application，并可提交首个任务。"""

        self.agent = agent
        self._dispatch = dispatch
        if not self._history_loaded:
            self.load_session_history(agent.session.events)
        self._invalidate()

        def pre_run() -> None:
            if initial_prompt:
                self.show_initial_prompt(initial_prompt)

                async def submit_initial() -> None:
                    try:
                        should_exit = await dispatch(initial_prompt)
                    except Exception as exc:  # pragma: no cover - 真实 Provider 防御边界
                        self.print_notice(
                            f"初始任务提交失败：{type(exc).__name__}: {exc}", style="red"
                        )
                        return
                    if should_exit:
                        self.application.exit()

                self.application.create_background_task(submit_initial())

        await self.application.run_async(pre_run=pre_run)

    def show_goodbye(self) -> None:
        """全屏 renderer 退出并恢复终端后输出一条收敛确认。"""

        print("Session saved. All agent tasks settled.")


__all__ = ["FullScreenTerminalUI", "TerminalUI"]
