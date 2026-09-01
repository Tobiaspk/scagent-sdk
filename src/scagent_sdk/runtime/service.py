"""Bind model turns to the durable scientific session."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from scagent_sdk.errors import ContextRolloverRequired, RuntimeExecutionError
from scagent_sdk.models.limits import ModelLimits
from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.observer import NullRuntimeObserver, RuntimeObserver
from scagent_sdk.runtime.protocol import RuntimeBackend, RuntimeRequest, RuntimeResponse
from scagent_sdk.runtime.resume import (
    ResumeMode,
    ResumePreference,
    checkpoint_reference,
    resume_context,
)
from scagent_sdk.session import AnalysisSession

_CONTEXT_ERROR_MARKERS = (
    "contextwindowexceeded",
    "context window exceeded",
    "maximum context length",
    "maximum context window",
    "prompt is too long",
    "too many input tokens",
)
MAX_CONTEXT_ROLLOVERS_PER_TURN = 8


class AgentRuntimeService:
    def __init__(
        self,
        backend: RuntimeBackend,
        *,
        model_limits: ModelLimits | None = None,
        resume_preference: ResumePreference = ResumePreference.AUTO,
        observer: RuntimeObserver | None = None,
    ):
        self.backend = backend
        self.model_limits = model_limits or ModelLimits(None, None, "runtime:unknown")
        self.resume_preference = resume_preference
        self.observer = observer or NullRuntimeObserver()
        self._first_turn = True

    @staticmethod
    def _effective_prompt(user_prompt: str, context: str, mode: ResumeMode) -> str:
        if mode is ResumeMode.FRESH:
            # New session, nothing to resume: hand the model its request directly, with no
            # checkpoint preamble. Skill/capability instructions are injected separately.
            return user_prompt.strip()
        if mode is ResumeMode.FORK:
            continuation = (
                "This is a fork of an earlier model conversation. Preserve recorded scientific "
                "facts, but treat subsequent reasoning as a new branch."
            )
        elif mode is ResumeMode.EXACT:
            continuation = (
                "The model conversation is being resumed exactly. Reconcile its history with "
                "the current durable scientific checkpoint."
            )
        else:
            continuation = (
                "The previous model conversation is unavailable, incompatible, or intentionally "
                "compacted. Continue from the durable scientific checkpoint without repeating "
                "completed work."
            )
        return f"{continuation}\n\n{context}\n\nNew user request:\n{user_prompt.strip()}"

    @staticmethod
    def _is_context_error(response: RuntimeResponse) -> bool:
        if not response.is_error:
            return False
        text = f"{response.subtype}\n{response.final_text}".casefold().replace("_", " ")
        return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)

    def _request(
        self,
        *,
        session: AnalysisSession,
        profile: ModelProfile,
        cwd: str | Path,
        prompt: str,
        runtime_session_id: str | None,
        fork_session: bool,
    ) -> RuntimeRequest:
        return RuntimeRequest(
            prompt=prompt,
            profile=profile,
            scientific_session_id=session.session_id,
            scientific_session_dir=session.directory,
            cwd=Path(cwd).expanduser().resolve(),
            resume_session_id=runtime_session_id,
            fork_session=fork_session,
            context_window_tokens=self.model_limits.context_window_tokens,
            max_output_tokens=self.model_limits.max_output_tokens,
            context_limit_source=self.model_limits.source,
        )

    def _record_rollover(
        self,
        session: AnalysisSession,
        *,
        turn_id: str,
        profile: ModelProfile,
        old_runtime_session_id: str | None,
        reason: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        state = session.store.state
        session.store.record(
            "runtime.context_rolled_over",
            payload={
                "turn_id": turn_id,
                "reason": reason,
                "runtime": self.backend.runtime_name,
                "model_profile": profile.name,
                "old_runtime_session_id": old_runtime_session_id,
                "scientific_state_revision": state.revision,
                "scientific_last_event_sequence": state.last_event_sequence,
                "model_limits": self.model_limits.to_dict(),
                "details": dict(details or {}),
                "policy": (
                    "same scientific session; fresh model-runtime conversation reconstructed "
                    "from durable state"
                ),
            },
        )

    def _notify_rollover(
        self,
        *,
        reason: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Tell an interactive surface that the live conversation is being rebuilt."""

        values = details or {}
        total = values.get("total_tokens")
        context_window = values.get("context_window_tokens")
        self.observer.on_context_rollover(
            reason=reason,
            total_tokens=(
                total if isinstance(total, int) and not isinstance(total, bool) else None
            ),
            context_window_tokens=(
                context_window
                if isinstance(context_window, int) and not isinstance(context_window, bool)
                else self.model_limits.context_window_tokens
            ),
        )

    def _reconstructed_request(
        self,
        session: AnalysisSession,
        *,
        user_prompt: str,
        profile: ModelProfile,
        cwd: str | Path,
    ) -> RuntimeRequest:
        context = resume_context(
            session.store.metadata,
            session.store.state,
            events=session.store.events(),
            session_dir=session.directory,
        )
        prompt = self._effective_prompt(user_prompt, context, ResumeMode.RECONSTRUCTED)
        return self._request(
            session=session,
            profile=profile,
            cwd=cwd,
            prompt=prompt,
            runtime_session_id=None,
            fork_session=False,
        )

    def _record_interrupted(
        self,
        session: AnalysisSession,
        *,
        turn_id: str,
        profile: ModelProfile,
        runtime_session_id: str | None,
        forced: bool,
        response: RuntimeResponse | None = None,
    ) -> None:
        """Record a user-stopped turn as its own outcome, keeping the session resumable."""

        if runtime_session_id:
            session.bind_runtime(
                runtime=self.backend.runtime_name,
                runtime_session_id=runtime_session_id,
                model_profile=profile.name,
                model_profile_fingerprint=profile.fingerprint,
                transport=profile.transport,
                model=profile.model,
            )
        payload: dict[str, object] = {
            "turn_id": turn_id,
            "runtime": self.backend.runtime_name,
            "model_profile": profile.name,
            "runtime_session_id": runtime_session_id,
            "forced": forced,
        }
        if response is not None:
            payload["response"] = response.to_dict()
        session.store.record("runtime.turn_interrupted", payload=payload, actor="user")

    async def run_turn(
        self,
        session: AnalysisSession,
        *,
        user_prompt: str,
        profile: ModelProfile,
        cwd: str | Path,
    ) -> RuntimeResponse:
        if not user_prompt.strip():
            raise ValueError("user_prompt must not be empty")
        if profile.runtime != self.backend.runtime_name:
            raise RuntimeExecutionError(
                f"profile runtime {profile.runtime!r} does not match backend "
                f"{self.backend.runtime_name!r}"
            )
        preference = self.resume_preference if self._first_turn else ResumePreference.AUTO
        plan = session.plan_resume(
            runtime=self.backend.runtime_name,
            model_profile=profile.name,
            model_profile_fingerprint=profile.fingerprint,
            preference=preference,
        )
        turn_id = str(uuid4())
        if not self._first_turn and plan.mode is ResumeMode.EXACT:
            context = checkpoint_reference(
                session.store.metadata,
                session.store.state,
                session_dir=session.directory,
            )
        else:
            context = plan.context
        effective_prompt = self._effective_prompt(user_prompt, context, plan.mode)
        session.store.record(
            "runtime.turn_started",
            payload={
                "turn_id": turn_id,
                "user_prompt": user_prompt,
                "resume_mode": plan.mode.value,
                "resume_preference": preference.value,
                "runtime_session_id": plan.runtime_session_id,
                "runtime": self.backend.runtime_name,
                "model_profile": profile.name,
                "model_profile_fingerprint": profile.fingerprint,
                "model_limits": self.model_limits.to_dict(),
            },
            actor="user",
        )
        request = self._request(
            session=session,
            profile=profile,
            cwd=cwd,
            prompt=effective_prompt,
            runtime_session_id=plan.runtime_session_id,
            fork_session=plan.mode is ResumeMode.FORK,
        )
        try:
            rollover_count = 0
            while True:
                try:
                    response = await self.backend.execute(request)
                except ContextRolloverRequired as exc:
                    if preference is ResumePreference.EXACT:
                        raise RuntimeExecutionError(
                            f"{exc}. Exact resume was selected; reopen the session and choose "
                            "automatic or reconstructed resume."
                        ) from exc
                    if rollover_count >= MAX_CONTEXT_ROLLOVERS_PER_TURN:
                        raise RuntimeExecutionError(
                            "the model conversation repeatedly exhausted its context window "
                            f"during one turn ({MAX_CONTEXT_ROLLOVERS_PER_TURN} rollovers)"
                        ) from exc
                    reason = "runtime context reached its reserve during the active turn"
                    old_runtime_session_id = (
                        getattr(self.backend, "last_runtime_session_id", None)
                        or request.resume_session_id
                    )
                    self._record_rollover(
                        session,
                        turn_id=turn_id,
                        profile=profile,
                        old_runtime_session_id=old_runtime_session_id,
                        reason=reason,
                        details=exc.to_dict(),
                    )
                    self._notify_rollover(reason=reason, details=exc.to_dict())
                    rollover_count += 1
                    request = self._reconstructed_request(
                        session,
                        user_prompt=user_prompt,
                        profile=profile,
                        cwd=cwd,
                    )
                    continue

                if not self._is_context_error(response):
                    break
                if preference is ResumePreference.EXACT:
                    raise RuntimeExecutionError(
                        "the model rejected the turn because its context window is full. "
                        "Exact resume was selected; reopen the session and choose automatic "
                        "or reconstructed resume."
                    )
                if rollover_count >= MAX_CONTEXT_ROLLOVERS_PER_TURN:
                    raise RuntimeExecutionError(
                        "the model repeatedly rejected reconstructed conversations as over its "
                        f"context window ({MAX_CONTEXT_ROLLOVERS_PER_TURN} rollovers)"
                    )
                reason = "provider rejected the active transcript as over its context window"
                old_runtime_session_id = (
                    response.runtime_session_id
                    or getattr(self.backend, "last_runtime_session_id", None)
                    or request.resume_session_id
                )
                details = {
                    "subtype": response.subtype,
                    "error": response.final_text[-4000:],
                }
                self._record_rollover(
                    session,
                    turn_id=turn_id,
                    profile=profile,
                    old_runtime_session_id=old_runtime_session_id,
                    reason=reason,
                    details=details,
                )
                self._notify_rollover(reason=reason, details=details)
                rollover_count += 1
                request = self._reconstructed_request(
                    session,
                    user_prompt=user_prompt,
                    profile=profile,
                    cwd=cwd,
                )
        except asyncio.CancelledError:
            self._record_interrupted(
                session,
                turn_id=turn_id,
                profile=profile,
                runtime_session_id=getattr(self.backend, "last_runtime_session_id", None),
                forced=True,
            )
            raise
        except Exception as exc:
            session.store.record(
                "runtime.turn_failed",
                payload={
                    "turn_id": turn_id,
                    "runtime": self.backend.runtime_name,
                    "model_profile": profile.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            if isinstance(exc, RuntimeExecutionError):
                raise
            raise RuntimeExecutionError(str(exc)) from exc

        if response.interrupted:
            self._record_interrupted(
                session,
                turn_id=turn_id,
                profile=profile,
                runtime_session_id=response.runtime_session_id or None,
                forced=False,
                response=response,
            )
            self._first_turn = False
            return response

        if response.is_error:
            session.store.record(
                "runtime.turn_failed",
                payload={
                    "turn_id": turn_id,
                    "runtime": self.backend.runtime_name,
                    "model_profile": profile.name,
                    "response": response.to_dict(),
                },
            )
            raise RuntimeExecutionError(
                f"model runtime returned an error result ({response.subtype}): "
                f"{response.final_text}"
            )

        session.bind_runtime(
            runtime=self.backend.runtime_name,
            runtime_session_id=response.runtime_session_id,
            model_profile=profile.name,
            model_profile_fingerprint=profile.fingerprint,
            transport=profile.transport,
            model=profile.model,
        )
        session.store.record(
            "runtime.turn_completed",
            payload={
                "turn_id": turn_id,
                "runtime": self.backend.runtime_name,
                "model_profile": profile.name,
                "response": response.to_dict(),
            },
        )
        self._first_turn = False
        return response
