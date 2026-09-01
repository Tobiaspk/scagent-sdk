"""Rich rendering for streamed model and capability activity."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Heading, Markdown
from rich.status import Status
from rich.text import Text

from scagent_sdk.runtime.observer import ToolActivity
from scagent_sdk.runtime.protocol import RuntimeMessage, RuntimeRequest, RuntimeResponse


class _LeftHeading(Heading):
    """Left-aligned, colored section headings.

    Rich centers all headings and wraps H1 in a heavy border panel, which reads as a banner
    rather than a document heading. This renders headings left-aligned with a per-level color
    (matching the legacy terminal look) and a leading blank line for separation.
    """

    _COLORS = {
        "h1": "bold bright_cyan",
        "h2": "bold magenta",
        "h3": "bold cyan",
        "h4": "bold",
        "h5": "bold",
        "h6": "bold",
    }

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text
        text.justify = "left"
        text.stylize(self._COLORS.get(self.tag, "bold"))
        yield Text("")  # blank line before every heading for visual separation
        yield text


class _LeftAlignedMarkdown(Markdown):
    """Markdown renderer that uses :class:`_LeftHeading` for all heading levels."""

    elements = {**Markdown.elements, "heading_open": _LeftHeading}

_LATEX_COMMANDS = {
    r"\rightarrow": "→",
    r"\leftarrow": "←",
    r"\Rightarrow": "⇒",
    r"\leftrightarrow": "↔",
    r"\approx": "≈",
    r"\geq": "≥",
    r"\leq": "≤",
    r"\neq": "≠",
    r"\times": "×",
    r"\pm": "±",
    r"\cdot": "·",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\sigma": "σ",
    r"\infty": "∞",
    r"\sum": "Σ",
    r"\prod": "Π",
    r"\in": "∈",
    r"\notin": "∉",
}
_MODEL_ARTIFACTS = (
    re.compile(r"<think>(.*?)</think>\n?", re.DOTALL),
    re.compile(r"<\|channel\>[^\|]*\n(.*?)<channel\|>\n?", re.DOTALL),
)


# Wait labels. The terminal must never fall silent while the runtime is working, so every
# branch that stops the spinner to print something starts it again with the label describing
# what is being waited on next.
_THINKING = "Thinking"
_ANALYZING = "Analyzing results"
_WORKING = "Working"
_RECONSTRUCTING_CONTEXT = "Reconstructing context"


def format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


class ElapsedStatus:
    """Spinner text that reports how long the current wait has lasted.

    Rich re-renders a spinner's text renderable on every refresh, so reading the clock here
    turns a static status line into a live timer. A spinner alone proves the process is
    alive; only the timer distinguishes a step that has run for two seconds from one that
    has run for twenty minutes, which is what a user actually wants to know before deciding
    whether a long compute is stuck.
    """

    def __init__(
        self,
        label: str,
        *,
        clock: Callable[[], float] = monotonic,
        progress: Callable[[], str | None] | None = None,
    ) -> None:
        self.label = label
        self._clock = clock
        self._started = clock()
        # Read on every refresh so a long compute can report its own progress (e.g. scVI epochs)
        # in the same line as the elapsed timer, without any cross-thread rich manipulation: the
        # worker thread only writes a string, this render thread only reads it.
        self._progress = progress

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        elapsed = max(int(self._clock() - self._started), 0)
        text = Text.assemble(f"{self.label}...", (f" ({format_elapsed(elapsed)})", "dim"))
        detail = self._progress() if self._progress is not None else None
        if detail:
            text.append(f" {detail}", style="dim")
        yield text


def delatex(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group(1)
        for command, symbol in sorted(_LATEX_COMMANDS.items(), key=lambda item: -len(item[0])):
            value = value.replace(command, symbol)
        value = re.sub(r"\\(?:text|mathrm)\{([^}]*)\}", r"\1", value)
        value = re.sub(r"\{([^}]*)\}", r"\1", value)
        return re.sub(r"\\[A-Za-z]+", "", value).strip()

    return re.sub(r"\$([^$\n]+?)\$", replace, text) if "$" in text else text


def strip_model_artifacts(text: str) -> tuple[str, list[str]]:
    thinking: list[str] = []
    for pattern in _MODEL_ARTIFACTS:
        thinking.extend(match.strip() for match in pattern.findall(text) if match.strip())
        text = pattern.sub("", text)
    text = re.sub(r"^<\|[^|]+\|>\s*$", "", text, flags=re.MULTILINE)
    return text.strip(), thinking


class RichRuntimeObserver:
    def __init__(
        self,
        console: Console | None = None,
        *,
        show_thinking: bool = True,
        reasoning_log: Path | None = None,
    ):
        self.console = console or Console()
        self.show_thinking = show_thinking
        self.reasoning_log = reasoning_log
        self._status: Status | None = None
        self._status_label: str | None = None
        self._progress_text: str | None = None
        self._rendered_text: list[str] = []
        self._emitted_thoughts: list[str] = []
        self._turn_header_pending = False
        self._context_rollover_pending = False

    def _save_reasoning(self, text: str) -> None:
        """Append one chain-of-thought fragment to the session reasoning log.

        Persistence is independent of terminal display: reasoning is saved even
        when ``show_thinking`` is off. A dated per-turn header is written lazily,
        only once a turn actually produces reasoning, to avoid empty headers.
        """

        if self.reasoning_log is None:
            return
        cleaned = delatex(text.strip())
        if not cleaned:
            return
        self.reasoning_log.parent.mkdir(parents=True, exist_ok=True)
        with self.reasoning_log.open("a", encoding="utf-8") as handle:
            if self._turn_header_pending:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                handle.write(f"\n{'=' * 60}\n# {stamp}\n{'=' * 60}\n")
                self._turn_header_pending = False
            handle.write(cleaned.rstrip() + "\n")

    def _emit_thought(self, thought: str) -> None:
        """Persist and (when enabled) render one reasoning fragment, dimmed.

        Reasoning can reach us twice — once as a streamed message and again inside the final
        result text — so identical fragments are emitted only once per turn.
        """

        cleaned = thought.strip()
        if not cleaned or cleaned in self._emitted_thoughts:
            return
        self._emitted_thoughts.append(cleaned)
        self._save_reasoning(thought)
        if self.show_thinking:
            self.console.print("…", style="dim")
            self._render_markdown(thought, dim=True)

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
        self._status_label = None

    def _start_status(self, label: str = _THINKING) -> None:
        self._stop_status()
        self._status_label = label
        self._status = self.console.status(
            ElapsedStatus(label, progress=lambda: self._progress_text), spinner="dots"
        )
        self._status.start()

    def _render_markdown(self, text: str, *, dim: bool = False) -> None:
        text = delatex(text.strip())
        if not text:
            return
        self.console.print(_LeftAlignedMarkdown(text), style="dim" if dim else None)

    def on_runtime_started(self, request: RuntimeRequest) -> None:
        self._rendered_text.clear()
        self._emitted_thoughts.clear()
        self._turn_header_pending = True
        label = _RECONSTRUCTING_CONTEXT if self._context_rollover_pending else _THINKING
        self._context_rollover_pending = False
        self._start_status(label)

    def on_context_rollover(
        self,
        *,
        reason: str,
        total_tokens: int | None,
        context_window_tokens: int | None,
    ) -> None:
        """Make automatic compaction visible without disrupting the interactive prompt."""

        self._stop_status()
        if total_tokens is not None and context_window_tokens is not None:
            location = (
                f" at {total_tokens / 1000:.0f}K/{context_window_tokens / 1000:.0f}K"
            )
        else:
            location = ""
        self.console.print(
            "↻ Compacting conversation context"
            f"{location} — scientific state and artifacts remain preserved.",
            style="dim",
            markup=False,
        )
        self._context_rollover_pending = True
        self._start_status(_RECONSTRUCTING_CONTEXT)

    def on_context_compacted(
        self,
        *,
        summary: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        """Show that the replayed conversation was trimmed; durable state is untouched."""

        resumed = self._status is not None
        self._stop_status()
        self.console.print(
            f"⤵ Compacted conversation for replay ({tokens_before / 1000:.0f}K→"
            f"{tokens_after / 1000:.0f}K est. tokens): {summary}. "
            "Scientific state and artifacts remain preserved.",
            style="dim",
            markup=False,
        )
        if resumed:
            self._start_status(_THINKING)

    def on_message(self, message: RuntimeMessage) -> None:
        """Render one streamed block, then resume waiting visibly.

        A block arriving means the turn is still in flight, so every branch ends with a
        running spinner: text is emitted in pieces, and the gap between one block and the
        next is model generation time that would otherwise look like a hung terminal. Only
        the turn-ending callbacks clear it.
        """

        self._stop_status()
        if message.kind == "thinking":
            self._emit_thought(str(message.content))
            self._start_status()
            return
        if message.kind != "text":
            # A tool this terminal does not instrument — a built-in such as Skill — reports no
            # start or finish, so its tool_use block is the only signal that work has begun.
            self._start_status(_WORKING if message.kind == "tool_use" else _THINKING)
            return
        visible, embedded_thinking = strip_model_artifacts(str(message.content))
        for thought in embedded_thinking:
            self._emit_thought(thought)
        if visible:
            self._render_markdown(visible)
            self._rendered_text.append(visible)
        self._start_status()

    def on_tool_started(self, activity: ToolActivity) -> None:
        # The printed line is the durable record of what ran; the spinner below it carries the
        # elapsed time, which is the only feedback during a compute that lasts minutes.
        self._stop_status()
        self._progress_text = None
        self.console.print(f"[cyan]▶[/cyan] {activity.label}...")
        self._start_status(activity.label)

    def on_tool_progress(self, activity: ToolActivity, text: str) -> None:
        # Compute workers stream their own progress (scVI epochs, tqdm bars) from a subprocess
        # thread; store the latest line so the spinner's renderable picks it up on its next
        # refresh. A plain string write is all that crosses the thread boundary — no rich call.
        self._progress_text = text.strip() or None

    def on_tool_finished(self, activity: ToolActivity, summary: str | None) -> None:
        self._stop_status()
        self._progress_text = None
        self.console.print(f"[green]✓[/green] {activity.label} done")
        if summary:
            self.console.print(f"  ⤷ {summary}", style="dim", markup=False)
        # After a tool returns, the model is interpreting its result, not cold-starting.
        self._start_status(_ANALYZING)

    def on_tool_failed(self, activity: ToolActivity, error: str) -> None:
        self._stop_status()
        self._progress_text = None
        self.console.print(f"[red]✗[/red] {activity.label} failed")
        self.console.print(f"  {error}", style="red", markup=False)
        # The turn is not over: the model still has to read the failure and decide what to do.
        self._start_status()

    def on_runtime_finished(self, response: RuntimeResponse) -> None:
        self._stop_status()
        final, embedded_thinking = strip_model_artifacts(response.final_text)
        for thought in embedded_thinking:
            self._emit_thought(thought)
        if final and final not in self._rendered_text:
            self._render_markdown(final)

    def on_runtime_failed(self, error: str) -> None:
        self._stop_status()
        self.console.print(f"[red]✗ Runtime error:[/red] {error}", markup=False)

    def on_runtime_interrupted(self, *, forced: bool) -> None:
        # The spinner is a live display; leaving it running would keep repainting over the
        # message that explains why the turn ended.
        self._stop_status()
        detail = "stopped immediately" if forced else "stopped cleanly"
        self.console.print(f"[yellow]⏹ Model runtime {detail}.[/yellow]")
