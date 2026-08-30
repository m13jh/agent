"""只追加、可重放的上下文 summary/surface replacement。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from python_agent.errors import SessionError
from python_agent.session.events import SessionEvent
from python_agent.session.projection import render_transcript
from python_agent.session.session import Session


@runtime_checkable
class SummaryProvider(Protocol):
    """把旧对话 transcript 压缩成模型可见摘要的异步能力边界。"""

    async def summarize(self, transcript: str) -> str:
        """返回非空摘要；Provider 不负责修改 Session。"""

        ...


SummaryCallback = Callable[[str], str | Awaitable[str]]


class CallbackSummaryProvider:
    """把同步或异步应用回调适配成 SummaryProvider。"""

    def __init__(self, callback: SummaryCallback) -> None:
        self.callback = callback

    async def summarize(self, transcript: str) -> str:
        """执行回调并统一等待 Awaitable。"""

        result = self.callback(transcript)
        if inspect.isawaitable(result):
            result = await result
        return result


class StaticSummaryProvider:
    """使用调用方已经审阅的静态摘要，适合 CLI 手工压缩。"""

    def __init__(self, summary: str) -> None:
        self.summary = summary

    async def summarize(self, transcript: str) -> str:
        """忽略原文并返回静态摘要；原文参数保留统一接口。"""

        del transcript
        return self.summary


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """一次压缩追加的 summary 事件和被替换范围。"""

    event: SessionEvent
    replaced_turns: tuple[int, ...]
    replaced_event_count: int


class ContextCompactor:
    """只压缩完整旧 Turn，并通过新事件替换模型表面，不删除原始日志。"""

    @staticmethod
    def _completed_turns(session: Session) -> list[tuple[int, int, int]]:
        """返回 ``(turn, start_seq, end_seq)``，忽略仍开放的崩溃尾部。"""

        completed: list[tuple[int, int, int]] = []
        open_turn: tuple[int, int] | None = None
        for event in session.events:
            if event.type == "turn/start":
                turn = event.data.get("turn")
                if not isinstance(turn, int) or open_turn is not None:
                    raise SessionError("cannot compact malformed or nested Turn history")
                open_turn = (turn, event.seq)
            elif event.type == "turn/end":
                turn = event.data.get("turn")
                if open_turn is None or turn != open_turn[0]:
                    raise SessionError("cannot compact unmatched turn/end")
                completed.append((turn, open_turn[1], event.seq))
                open_turn = None
        return completed

    @staticmethod
    def _active_summary_coverages(session: Session) -> list[tuple[SessionEvent, set[int]]]:
        """展开没有被后续 summary 替换的摘要来源集合。"""

        summaries = {
            event.seq: event for event in session.events if event.type == "context/summary"
        }
        referenced = {
            source
            for event in summaries.values()
            for source in event.source_event_seqs or []
            if source in summaries
        }

        def expand(seq: int, seen: set[int]) -> set[int]:
            if seq in seen:
                raise SessionError("context summary source graph contains a cycle")
            event = summaries.get(seq)
            if event is None:
                if seq < 0 or seq >= len(session.events):
                    raise SessionError(f"context summary references unknown seq {seq}")
                return {seq}
            sources = event.source_event_seqs or []
            if not sources:
                raise SessionError(f"context summary at seq {seq} has no sources")
            result: set[int] = set()
            for source in sources:
                result.update(expand(source, {*seen, seq}))
            return result

        return [
            (event, expand(event.seq, set()))
            for seq, event in sorted(summaries.items())
            if seq not in referenced
        ]

    async def compact(
        self,
        session: Session,
        provider: SummaryProvider,
        *,
        keep_recent_turns: int = 2,
    ) -> CompactionResult | None:
        """用一条 context/summary 替换完整旧 Turn 的模型可见表面。

        原 user/assistant/tool 事件永远保留，summary 只追加到日志末尾。投影器根据
        ``source_event_seqs`` 在原位置插入摘要并隐藏来源，因此恢复、fork 和重复投影完全
        确定。没有新增可压缩 Turn 时返回 None，避免重复生成等价摘要。
        """

        if keep_recent_turns < 0:
            raise ValueError("keep_recent_turns must be non-negative")
        completed = self._completed_turns(session)
        target_turns = (
            completed[: len(completed) - keep_recent_turns] if keep_recent_turns else completed
        )
        if not target_turns:
            return None
        target_seqs = {seq for _, start, end in target_turns for seq in range(start, end + 1)}
        active_summaries = self._active_summary_coverages(session)
        covered: set[int] = set()
        source_summary_events: list[SessionEvent] = []
        transcript_units: list[tuple[int, str]] = []
        for summary_event, coverage in active_summaries:
            intersection = coverage & target_seqs
            if not intersection:
                continue
            if not coverage <= target_seqs:
                raise SessionError("compaction target would split an existing summary surface")
            covered.update(coverage)
            source_summary_events.append(summary_event)
            transcript_units.append((min(coverage), str(summary_event.data.get("content", ""))))

        uncovered = target_seqs - covered
        if not uncovered:
            return None
        for turn, start, end in target_turns:
            turn_events = [
                event for event in session.events[start : end + 1] if event.seq in uncovered
            ]
            if turn_events:
                transcript_units.append((start, render_transcript(tuple(turn_events))))

        transcript = "\n\n".join(text for _, text in sorted(transcript_units) if text)
        summary_text = (await provider.summarize(transcript)).strip()
        if not summary_text:
            raise SessionError("summary provider returned empty content")
        sources = sorted(
            [event.seq for event in source_summary_events] + [seq for seq in uncovered]
        )
        turns = tuple(turn for turn, _, _ in target_turns)
        event = session.append(
            "context/summary",
            {
                "role": "user",
                "content": "[Earlier conversation summary]\n" + summary_text,
                "replaced_turns": list(turns),
                "replaced_event_count": len(target_seqs),
            },
            source_event_seqs=sources,
        )
        return CompactionResult(
            event=event,
            replaced_turns=turns,
            replaced_event_count=len(target_seqs),
        )


__all__ = [
    "CallbackSummaryProvider",
    "CompactionResult",
    "ContextCompactor",
    "StaticSummaryProvider",
    "SummaryProvider",
]
