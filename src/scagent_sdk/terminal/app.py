"""Interactive Rich REPL over a resumable scientific session."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from scagent_sdk.errors import RuntimeExecutionError
from scagent_sdk.models.limits import ModelLimits
from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.interrupts import TurnInterrupter
from scagent_sdk.runtime.resume import ResumePreference
from scagent_sdk.runtime.service import AgentRuntimeService
from scagent_sdk.session import AnalysisSession
from scagent_sdk.terminal.context import context_toolbar_text
from scagent_sdk.terminal.input import read_user_input
from scagent_sdk.terminal.interrupts import TurnInterruptController, keyboard_interrupts


@dataclass
class RichInteractiveAgent:
    service: AgentRuntimeService
    session: AnalysisSession
    profile: ModelProfile
    cwd: Path
    console: Console
    skills: tuple[dict[str, object], ...] = ()
    interrupter: TurnInterrupter | None = None
    model_limits: ModelLimits | None = None
    resume_preference: ResumePreference | None = None
    _output_folder_shown: bool = False
    _last_context_usage: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Restore the last known bar so it is visible before a resumed session's first turn."""

        for event in reversed(self.session.store.events()):
            if event.kind != "runtime.turn_completed":
                continue
            response = event.payload.get("response")
            usage = response.get("context_usage") if isinstance(response, dict) else None
            if isinstance(usage, dict) and usage:
                self._last_context_usage = self._normalize_context_usage(usage)
                break

    def _normalize_context_usage(self, usage: dict[str, object]) -> dict[str, object]:
        normalized = dict(usage)
        limits = self.model_limits
        discovered = limits.context_window_tokens if limits is not None else None
        total = normalized.get("total_tokens")
        if discovered and limits is not None:
            previous = normalized.get("context_window_tokens")
            if isinstance(previous, int) and previous != discovered:
                normalized["sdk_raw_context_window_tokens"] = previous
            normalized["context_window_tokens"] = discovered
            normalized["context_window_source"] = limits.source
            if isinstance(total, int) and not isinstance(total, bool):
                normalized["percentage"] = 100.0 * total / discovered
        return normalized

    def _context_toolbar(self, columns: int) -> str:
        return context_toolbar_text(self._last_context_usage, columns=columns)

    def welcome(self, *, gateway_managed: bool) -> None:
        state = self.session.store.state
        dataset = state.facts.get("dataset") if isinstance(state.facts, dict) else None
        dataset_path = dataset.get("path") if isinstance(dataset, dict) else "none loaded yet"
        skill_names = ", ".join(str(skill["id"]) for skill in self.skills) or "none"
        gateway = self.profile.base_url or "provider default"
        if gateway_managed:
            gateway += " (started for this session)"
        if self.model_limits and self.model_limits.context_window_tokens:
            context = (
                f"{self.model_limits.context_window_tokens:,} tokens "
                f"({self.model_limits.source})"
            )
        else:
            context = "runtime-reported when available"
        resume = self.resume_preference.value if self.resume_preference else "new session"
        # Show the live upstream model the gateway resolves to (e.g. "Qwen3.8-27B"),
        # not the static profile alias — the profile is deliberately model-agnostic.
        advertised = self.model_limits.advertised_model if self.model_limits else None
        model_label = (advertised or self.profile.model).rsplit("/", 1)[-1]
        body = Text.assemble(
            ("scagent-sdk", "bold cyan"),
            " — skill-driven single-cell analysis agent\n\n",
            ("  Session:  ", "dim"),
            (self.session.session_id, "white"),
            "\n",
            ("  Model:    ", "dim"),
            (model_label, "white"),
            "\n",
            ("  Gateway:  ", "dim"),
            (gateway, "green"),
            "\n",
            ("  Context:  ", "dim"),
            (context, "white"),
            "\n",
            ("  Resume:   ", "dim"),
            (resume, "white"),
            "\n",
            ("  Work dir: ", "dim"),
            (str(self.cwd), "white"),
            "\n",
            ("  Data:     ", "dim"),
            (str(dataset_path), "white"),
            "\n",
            ("  Skills:   ", "dim"),
            (skill_names, "white"),
            "\n\n",
            (
                "Enter submits · Esc+Enter adds a newline · Esc or Ctrl+C stops a running "
                "turn · /help shows commands",
                "dim",
            ),
        )
        self.console.print(Panel(body, border_style="cyan", padding=(0, 1)))

    def _render_turn_header(self) -> None:
        """Separate user input from agent output with a blue rule.

        On the first analysis turn of this REPL — whether the session is new or
        resumed — the session's output folder is named just below the rule as
        dimmed text, so a user always knows where this run's artifacts land.
        """

        self.console.rule(style="blue")
        if not self._output_folder_shown:
            folder = self.session.store.session_dir.name
            self.console.print(f"[dim]📁 Output: {folder}[/dim]")
            self._output_folder_shown = True

    async def run(self, *, initial_prompt: str | None = None) -> int:
        if initial_prompt:
            self._render_turn_header()
            await self._turn(initial_prompt)
        exit_armed = False
        while True:
            self.console.print()
            try:
                prompt = (
                    await read_user_input("> ", bottom_toolbar=self._context_toolbar)
                ).strip()
            except KeyboardInterrupt:
                # At the prompt there is no turn to stop. A single reflexive Ctrl+C — often
                # aimed at a turn that has just ended — should not end the session silently.
                if exit_armed:
                    self.console.print("\n[dim]Session ended. State is preserved.[/dim]")
                    return 0
                exit_armed = True
                self.console.print("\n[dim]Press Ctrl+C again to exit, or type /exit.[/dim]")
                continue
            except EOFError:
                self.console.print("\n[dim]Session ended. State is preserved.[/dim]")
                return 0
            exit_armed = False
            if not prompt:
                continue
            lowered = prompt.casefold()
            if lowered in {"exit", "quit", "q", "done", "/exit", "/quit"}:
                self.console.print("[dim]Session ended. State is preserved.[/dim]")
                return 0
            if prompt == "/help":
                self.console.print(
                    Markdown(
                        "**Commands:** `/state`, `/session`, `/skills`, `/help`, `/exit`\n\n"
                        "**Esc** or **Ctrl+C** while a turn is running stops it and keeps the "
                        "session; press again to stop immediately.\n\n"
                        "All other text is sent to the analysis agent."
                    )
                )
                continue
            if prompt == "/state":
                self.console.print_json(json.dumps(self.session.store.state.to_dict()))
                continue
            if prompt == "/session":
                self.console.print_json(json.dumps(self.session.summary()))
                continue
            if prompt == "/skills":
                self.console.print_json(json.dumps(list(self.skills)))
                continue
            self._render_turn_header()
            await self._turn(prompt)

    def _interrupt_notice(self, message: str) -> None:
        self.console.print(f"[yellow]⏹ {message}[/yellow]")

    def _report_interrupted(self) -> None:
        self.console.print(
            "\n[yellow]Turn stopped — everything recorded so far is preserved.[/yellow]\n"
            "[dim]Enter another request, or /exit.[/dim]"
        )

    async def _turn(self, prompt: str) -> None:
        """Run one analysis turn that the user can stop without losing the session.

        The turn runs as its own task so a keypress can act on it; a stop is reported as an
        interruption rather than a failure, because nothing about the durable session is wrong.
        """

        task = asyncio.get_running_loop().create_task(
            self.service.run_turn(
                self.session,
                user_prompt=prompt,
                profile=self.profile,
                cwd=self.cwd,
            )
        )
        controller = TurnInterruptController(
            task, interrupter=self.interrupter, notify=self._interrupt_notice
        )
        try:
            with keyboard_interrupts(controller.request):
                response = await task
            if response.context_usage:
                self._last_context_usage = self._normalize_context_usage(
                    dict(response.context_usage)
                )
            if response.interrupted:
                self._report_interrupted()
        except asyncio.CancelledError:
            if not controller.stopped:
                raise  # this REPL is being shut down, not the turn
            self._report_interrupted()
        except KeyboardInterrupt:
            # Only reachable where the loop cannot own SIGINT. Stop the turn explicitly rather
            # than returning to a prompt with work still running behind it.
            task.cancel()
            self._report_interrupted()
        except RuntimeExecutionError as exc:
            self.console.print(
                f"\n[red]Turn failed:[/red] {exc}\n"
                "[dim]The scientific session is still resumable; "
                "correct the issue and retry.[/dim]",
                markup=False,
            )
        finally:
            controller.close()
