from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from scagent_sdk.runtime.observer import ToolActivity
from scagent_sdk.runtime.protocol import RuntimeMessage, RuntimeResponse
from scagent_sdk.terminal.rendering import (
    ElapsedStatus,
    RichRuntimeObserver,
    _LeftAlignedMarkdown,
    delatex,
    format_elapsed,
)


def test_headings_render_left_aligned_without_banner_box() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=60)
    console.print(_LeftAlignedMarkdown("# Dataset Summary\n\nbody text here"))
    rendered = output.getvalue()
    heading_line = next(line for line in rendered.splitlines() if "Dataset Summary" in line)
    # Heading starts at the left margin (Rich centers by default) and draws no box border.
    assert heading_line.startswith("Dataset Summary")
    assert "━" not in rendered  # heavy box character Rich uses to panel H1


def test_delatex_converts_common_inline_math() -> None:
    assert delatex(r"Result: $151 \times 2 \approx 302$ cells") == "Result: 151 × 2 ≈ 302 cells"


def test_rich_observer_distinguishes_thinking_text_and_tool_progress() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    observer = RichRuntimeObserver(console)
    activity = ToolActivity("run_code", "Running code", "general-code")

    observer.on_message(RuntimeMessage("thinking", "check assumptions"))
    observer.on_message(RuntimeMessage("text", "## Result\n\n**Ready**"))
    observer.on_tool_started(activity)
    observer.on_tool_finished(activity, "Created one artifact.")
    observer.on_runtime_finished(
        RuntimeResponse(
            runtime_session_id="sdk",
            messages=(),
            final_text="## Result\n\n**Ready**",
            stop_reason="end_turn",
            is_error=False,
            subtype="success",
        )
    )

    rendered = output.getvalue()
    assert "check assumptions" in rendered
    assert "Result" in rendered and "Ready" in rendered
    assert "▶ Running code..." in rendered
    assert "✓ Running code done" in rendered
    assert "Created one artifact." in rendered


def test_reasoning_embedded_in_text_and_final_is_not_rendered_twice() -> None:
    # Streaming delivers <think> inside the text block AND again in the final result text;
    # the same reasoning fragment must be shown only once per turn.
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    observer = RichRuntimeObserver(console)

    observer.on_runtime_started(SimpleNamespace())
    observer.on_message(RuntimeMessage("text", "<think>weigh the options</think>The answer is 42."))
    observer.on_runtime_finished(
        RuntimeResponse(
            runtime_session_id="sdk",
            messages=(),
            final_text="<think>weigh the options</think>The answer is 42.",
            stop_reason="end_turn",
            is_error=False,
            subtype="success",
        )
    )

    rendered = output.getvalue()
    assert rendered.count("weigh the options") == 1
    assert rendered.count("The answer is 42.") == 1


def _dummy_request():
    return SimpleNamespace()


def test_reasoning_is_saved_even_when_display_is_off(tmp_path) -> None:
    log = tmp_path / "logs" / "reasoning.log"
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    observer = RichRuntimeObserver(console, show_thinking=False, reasoning_log=log)

    observer.on_runtime_started(_dummy_request())
    observer.on_message(RuntimeMessage("thinking", "weigh the evidence"))
    observer.on_message(RuntimeMessage("text", "<think>hidden step</think>Here is the answer."))

    saved = log.read_text(encoding="utf-8")
    # Both the native thinking block and the embedded <think> are persisted...
    assert "weigh the evidence" in saved
    assert "hidden step" in saved
    # ...under a single dated per-turn header...
    assert saved.count("# ") == 1
    # ...while the terminal shows neither reasoning fragment (display off).
    rendered = output.getvalue()
    assert "weigh the evidence" not in rendered
    assert "hidden step" not in rendered
    assert "Here is the answer." in rendered


def test_no_reasoning_log_when_turn_has_no_reasoning(tmp_path) -> None:
    log = tmp_path / "logs" / "reasoning.log"
    observer = RichRuntimeObserver(
        Console(file=StringIO(), force_terminal=False), reasoning_log=log
    )
    observer.on_runtime_started(_dummy_request())
    observer.on_message(RuntimeMessage("text", "plain answer, no reasoning"))
    # A turn without reasoning writes no header and creates no file.
    assert not log.exists()


def _observer() -> RichRuntimeObserver:
    return RichRuntimeObserver(Console(file=StringIO(), force_terminal=False, width=100))


def test_wait_indicator_survives_every_streamed_block() -> None:
    # A silent terminal is indistinguishable from a hung one, so generation time between
    # blocks must always be covered by a running spinner.
    observer = _observer()
    observer.on_runtime_started(_dummy_request())
    assert observer._status_label == "Thinking"

    observer.on_message(RuntimeMessage("text", "Let me start."))
    assert observer._status_label == "Thinking"

    observer.on_message(RuntimeMessage("thinking", "weigh the options"))
    assert observer._status_label == "Thinking"

    # An uninstrumented tool (a built-in such as Skill) only ever reports its tool_use block.
    observer.on_message(RuntimeMessage("tool_use", {"name": "Skill", "input": {}}))
    assert observer._status_label == "Working"


def test_wait_indicator_tracks_tool_execution_and_recovery() -> None:
    observer = _observer()
    activity = ToolActivity("materialize_count_matrix", "Materializing raw counts", "counts")

    observer.on_tool_started(activity)
    assert observer._status_label == "Materializing raw counts"

    observer.on_tool_finished(activity, "Materialized X as raw counts.")
    assert observer._status_label == "Analyzing results"

    # A failed tool does not end the turn; the model still has to read the error.
    observer.on_tool_failed(activity, "boom")
    assert observer._status_label == "Thinking"


def test_context_rollover_is_visible_and_reconstruction_stays_live() -> None:
    output = StringIO()
    observer = RichRuntimeObserver(
        Console(file=output, force_terminal=False, width=100)
    )
    observer.on_runtime_started(_dummy_request())

    observer.on_context_rollover(
        reason="preflight usage reached the model's context reserve",
        total_tokens=233_051,
        context_window_tokens=262_144,
    )

    rendered = output.getvalue()
    assert "Compacting conversation context at 233K/262K" in rendered
    assert "scientific state and artifacts remain preserved" in rendered
    assert observer._status_label == "Reconstructing context"

    # Starting the fresh runtime retains the reconstruction-specific wait label until the
    # first model block proves that normal generation has resumed.
    observer.on_runtime_started(_dummy_request())
    assert observer._status_label == "Reconstructing context"
    observer.on_message(RuntimeMessage("text", "Continuing from the checkpoint."))
    assert observer._status_label == "Thinking"


def test_wait_indicator_clears_when_the_turn_ends() -> None:
    response = RuntimeResponse(
        runtime_session_id="sdk",
        messages=(),
        final_text="done",
        stop_reason="end_turn",
        is_error=False,
        subtype="success",
    )
    for end_turn in (
        lambda observer: observer.on_runtime_finished(response),
        lambda observer: observer.on_runtime_failed("gateway refused"),
        lambda observer: observer.on_runtime_interrupted(forced=False),
    ):
        observer = _observer()
        observer.on_runtime_started(_dummy_request())
        end_turn(observer)
        assert observer._status_label is None


def test_elapsed_status_reports_the_growing_wait() -> None:
    clock = [100.0]
    console = Console(file=StringIO(), force_terminal=False, width=60)
    label = ElapsedStatus("Training scVI", clock=lambda: clock[0])

    def rendered() -> str:
        with console.capture() as capture:
            console.print(label)
        return capture.get().strip()

    assert rendered() == "Training scVI... (0s)"
    clock[0] = 107.0
    # Rich re-renders spinner text on every refresh, so the same object reports a new duration.
    assert rendered() == "Training scVI... (7s)"


def test_elapsed_status_appends_live_progress_detail() -> None:
    clock = [100.0]
    detail: list[str | None] = [None]
    console = Console(file=StringIO(), force_terminal=False, width=80)
    label = ElapsedStatus("Training scVI", clock=lambda: clock[0], progress=lambda: detail[0])

    def rendered() -> str:
        with console.capture() as capture:
            console.print(label)
        return capture.get().strip()

    assert rendered() == "Training scVI... (0s)"
    detail[0] = "Epoch 3/200"
    # Same object, new epoch line: the spinner surfaces worker progress on the next refresh.
    assert rendered() == "Training scVI... (0s) Epoch 3/200"


def test_on_tool_progress_updates_the_spinner_and_clears_on_finish() -> None:
    observer = RichRuntimeObserver(Console(file=StringIO(), force_terminal=False))
    activity = ToolActivity(tool_name="train_scvi_latent", label="Training scVI latent model")
    observer.on_tool_started(activity)
    assert observer._progress_text is None
    observer.on_tool_progress(activity, "Epoch 12/200:  6%|  | 12/200")
    assert observer._progress_text == "Epoch 12/200:  6%|  | 12/200"
    observer.on_tool_finished(activity, summary=None)
    assert observer._progress_text is None


def test_format_elapsed_switches_to_minutes() -> None:
    assert format_elapsed(0) == "0s"
    assert format_elapsed(59) == "59s"
    assert format_elapsed(65) == "1m 05s"
    assert format_elapsed(3725) == "62m 05s"


def test_observer_without_log_does_not_crash_on_reasoning() -> None:
    observer = RichRuntimeObserver(Console(file=StringIO(), force_terminal=False))
    observer.on_runtime_started(_dummy_request())
    observer.on_message(RuntimeMessage("thinking", "no log configured"))
