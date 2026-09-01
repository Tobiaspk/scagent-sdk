"""Mirror Claude SDK transcripts inside the durable scientific session."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from scagent_sdk.runtime.compaction import (
    CompactionConfig,
    CompactionStats,
    compact_entries,
    estimate_tokens,
    ratchet_calibration,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ScientificSessionTranscriptStore:
    """A Claude Agent SDK SessionStore scoped to one scientific session directory.

    The transcript file on disk is append-only and complete. When a compaction config is
    supplied, :meth:`load` returns a *compacted copy* of the transcript — the version the SDK
    materializes for the CLI subprocess to replay — so a long session does not overflow the
    model context window. Below the trim target the returned entries are byte-identical to the
    stored ones, so short sessions and the SDK conformance suite are unaffected.
    """

    def __init__(
        self,
        scientific_session_dir: str | Path,
        *,
        compaction: CompactionConfig | None = None,
    ):
        self.root = Path(scientific_session_dir).resolve() / "runtime" / "claude-agent-sdk"
        self.root.mkdir(parents=True, exist_ok=True)
        self._compaction = compaction
        self._calibration_path = self.root / "compaction.json"
        # Estimate of the entries most recently returned by load(); calibrated against the real
        # prompt-token count the runtime observes after the turn.
        self._last_loaded_estimate: int = 0
        self.last_compaction: CompactionStats | None = None

    def _safe_component(self, value: str, *, name: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError(f"invalid {name}: {value!r}")
        return value

    def _path(self, key: dict[str, Any]) -> Path:
        session_id = self._safe_component(str(key["session_id"]), name="runtime session ID")
        directory = self.root / session_id
        subpath = key.get("subpath")
        if subpath is not None:
            parts = Path(str(subpath)).parts
            invalid = any(part in {"", ".", ".."} or not _SAFE_ID.fullmatch(part) for part in parts)
            if not parts or invalid:
                raise ValueError(f"invalid transcript subpath: {subpath!r}")
            directory = directory.joinpath(*parts)
        return directory / "transcript.jsonl"

    @staticmethod
    def _read_entries(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid SDK transcript {path}:{line_number}: {exc}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"SDK transcript entry is not an object: {path}:{line_number}")
                entries.append(value)
        return entries

    def _append_sync(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / ".transcript.lock"
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self._read_entries(path)
                known = {
                    str(entry["uuid"]) for entry in current if isinstance(entry.get("uuid"), str)
                }
                with path.open("a", encoding="utf-8") as handle:
                    for entry in entries:
                        item = deepcopy(entry)
                        uuid = item.get("uuid")
                        if isinstance(uuid, str) and uuid in known:
                            continue
                        encoded = json.dumps(item, separators=(",", ":"), allow_nan=False)
                        handle.write(encoded + "\n")
                        if isinstance(uuid, str):
                            known.add(uuid)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._append_sync, key, entries)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        path = self._path(key)
        entries = await asyncio.to_thread(self._read_entries, path)
        if not entries:
            return None
        return await asyncio.to_thread(self._compact_for_replay, entries)

    def _compact_for_replay(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        config = self._compaction
        if config is None or not config.active:
            self._last_loaded_estimate = estimate_tokens(entries)
            return entries
        calibration = self._read_calibration()
        compacted, stats = compact_entries(entries, config, calibration)
        self._last_loaded_estimate = stats.tokens_after
        if stats.triggered:
            self.last_compaction = stats
        return compacted

    def _read_calibration(self) -> float:
        try:
            data = json.loads(self._calibration_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 1.0
        value = data.get("calibration") if isinstance(data, dict) else None
        return float(value) if isinstance(value, (int, float)) and value > 0 else 1.0

    def update_calibration(self, actual_prompt_tokens: int | None) -> float | None:
        """Ratchet the estimator against the real prompt-token count of the last turn.

        Returns the new factor when it changes, else ``None``. Best-effort and never fatal:
        calibration only makes future estimates more conservative.
        """

        if not actual_prompt_tokens or self._last_loaded_estimate <= 0:
            return None
        current = self._read_calibration()
        updated = ratchet_calibration(current, self._last_loaded_estimate, actual_prompt_tokens)
        if abs(updated - current) < 1e-6:
            return None
        with contextlib.suppress(OSError):
            self._calibration_path.write_text(
                json.dumps({"calibration": updated}), encoding="utf-8"
            )
        return updated

    def take_last_compaction(self) -> CompactionStats | None:
        """Return and clear the most recent compaction stats, for the runtime to surface once."""

        stats = self.last_compaction
        self.last_compaction = None
        return stats

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        del project_key
        items: list[dict[str, Any]] = []
        for child in self.root.iterdir():
            transcript = child / "transcript.jsonl"
            if child.is_dir() and transcript.is_file():
                items.append(
                    {
                        "session_id": child.name,
                        "mtime": int(transcript.stat().st_mtime * 1000),
                    }
                )
        return items

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        session_id = self._safe_component(str(key["session_id"]), name="runtime session ID")
        directory = self.root / session_id
        if not directory.is_dir():
            return []
        return [
            str(path.parent.relative_to(directory))
            for path in directory.rglob("transcript.jsonl")
            if path.parent != directory
        ]
