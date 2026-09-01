"""The transcript store compacts what it replays, without touching the on-disk record."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from scagent_sdk.runtime.claude_store import ScientificSessionTranscriptStore
from scagent_sdk.runtime.compaction import CompactionConfig, estimate_tokens


def _image_result(uuid: str, tool_id: str) -> dict[str, Any]:
    b64 = "A" * 4000
    block = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [
            {"type": "text", "text": "figure saved at figures/f.png " + "R" * 2000},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
        ],
    }
    return {
        "type": "user",
        "uuid": uuid,
        "message": {"role": "user", "content": [block]},
        "toolUseResult": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
        ],
    }


def _big_session(steps: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for i in range(steps):
        content = [
            {"type": "text", "text": "step " + "x" * 400},
            {"type": "tool_use", "id": f"t{i}", "name": "evaluate_cluster_qc", "input": {"k": i}},
        ]
        entries.append(
            {
                "type": "assistant",
                "uuid": f"a{i}",
                "message": {"role": "assistant", "content": content},
            }
        )
        entries.append(_image_result(f"u{i}", f"t{i}"))
    return entries


def test_load_is_byte_identical_below_threshold(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = CompactionConfig(context_limit=1_000_000)
        store = ScientificSessionTranscriptStore(tmp_path, compaction=config)
        key = {"project_key": "p", "session_id": "s1"}
        entries = _big_session(3)
        await store.append(key, entries)
        loaded = await store.load(key)
        assert loaded == entries
        assert store.last_compaction is None

    asyncio.run(exercise())


def test_load_compacts_a_large_transcript_but_leaves_the_file_whole(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = CompactionConfig(
            context_limit=120_000, output_reserve=16_000, max_context_images=8
        )
        store = ScientificSessionTranscriptStore(tmp_path, compaction=config)
        key = {"project_key": "p", "session_id": "s1"}
        entries = _big_session(40)
        await store.append(key, entries)

        loaded = await store.load(key)
        assert loaded is not None
        # The replayed copy is compacted and fits the window.
        assert estimate_tokens(loaded) <= 120_000
        assert store.last_compaction is not None
        assert store.last_compaction.images_kept == 8

        # The append-only file on disk still holds every original entry, base64 included.
        raw = [
            json.loads(line)
            for line in (store.root / "s1" / "transcript.jsonl").read_text().splitlines()
        ]
        assert raw == entries

    asyncio.run(exercise())


def test_take_last_compaction_clears_after_read(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = CompactionConfig(context_limit=120_000, output_reserve=16_000)
        store = ScientificSessionTranscriptStore(tmp_path, compaction=config)
        key = {"project_key": "p", "session_id": "s1"}
        await store.append(key, _big_session(40))
        await store.load(key)
        assert store.take_last_compaction() is not None
        assert store.take_last_compaction() is None

    asyncio.run(exercise())


def test_calibration_ratchets_and_persists(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = CompactionConfig(context_limit=120_000, output_reserve=16_000)
        store = ScientificSessionTranscriptStore(tmp_path, compaction=config)
        key = {"project_key": "p", "session_id": "s1"}
        await store.append(key, _big_session(40))
        await store.load(key)
        estimate = store._last_loaded_estimate
        assert estimate > 0
        # Real usage came in at 1.5x the estimate → factor rises.
        updated = store.update_calibration(int(estimate * 1.5))
        assert updated is not None and updated > 1.0
        # Persisted for the next load.
        fresh = ScientificSessionTranscriptStore(tmp_path, compaction=config)
        assert fresh._read_calibration() == updated

    asyncio.run(exercise())
