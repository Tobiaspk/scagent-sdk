"""Provider-neutral runtime activity events for terminals and logs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scagent_sdk.runtime.protocol import RuntimeMessage, RuntimeRequest, RuntimeResponse


@dataclass(frozen=True)
class ToolActivity:
    tool_name: str
    label: str
    skill_id: str | None = None


class RuntimeObserver(Protocol):
    def on_runtime_started(self, request: RuntimeRequest) -> None: ...

    def on_context_rollover(
        self,
        *,
        reason: str,
        total_tokens: int | None,
        context_window_tokens: int | None,
    ) -> None: ...

    def on_context_compacted(
        self,
        *,
        summary: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None: ...

    def on_message(self, message: RuntimeMessage) -> None: ...

    def on_tool_started(self, activity: ToolActivity) -> None: ...

    def on_tool_progress(self, activity: ToolActivity, text: str) -> None: ...

    def on_tool_finished(self, activity: ToolActivity, summary: str | None) -> None: ...

    def on_tool_failed(self, activity: ToolActivity, error: str) -> None: ...

    def on_runtime_finished(self, response: RuntimeResponse) -> None: ...

    def on_runtime_failed(self, error: str) -> None: ...

    def on_runtime_interrupted(self, *, forced: bool) -> None: ...


class NullRuntimeObserver:
    def on_runtime_started(self, request: RuntimeRequest) -> None:
        pass

    def on_context_rollover(
        self,
        *,
        reason: str,
        total_tokens: int | None,
        context_window_tokens: int | None,
    ) -> None:
        pass

    def on_context_compacted(
        self,
        *,
        summary: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        pass

    def on_message(self, message: RuntimeMessage) -> None:
        pass

    def on_tool_started(self, activity: ToolActivity) -> None:
        pass

    def on_tool_progress(self, activity: ToolActivity, text: str) -> None:
        pass

    def on_tool_finished(self, activity: ToolActivity, summary: str | None) -> None:
        pass

    def on_tool_failed(self, activity: ToolActivity, error: str) -> None:
        pass

    def on_runtime_finished(self, response: RuntimeResponse) -> None:
        pass

    def on_runtime_failed(self, error: str) -> None:
        pass

    def on_runtime_interrupted(self, *, forced: bool) -> None:
        pass
