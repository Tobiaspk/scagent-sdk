"""Deterministic tests for the model-transcript compactor."""

from __future__ import annotations

import copy
from typing import Any

from scagent_sdk.runtime.compaction import (
    CompactionConfig,
    compact,
    compact_entries,
    compute_budget,
    estimate_tokens,
    ratchet_calibration,
)

_B64 = "A" * 4000  # stands in for a figure's base64 payload


def _assistant(uuid: str, text: str, tool: dict[str, Any] | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if tool is not None:
        content.append(tool)
    return {"type": "assistant", "uuid": uuid, "message": {"role": "assistant", "content": content}}


def _tool_use(tool_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def _tool_result(
    uuid: str,
    tool_id: str,
    text: str,
    *,
    with_image: bool = False,
    figure: str | None = None,
) -> dict[str, Any]:
    inner: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if with_image:
        inner.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _B64}}
        )
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": inner}
    entry: dict[str, Any] = {
        "type": "user",
        "uuid": uuid,
        "message": {"role": "user", "content": [block]},
    }
    if with_image:
        # The non-model-facing metadata mirror Claude Code writes alongside the message.
        mirror_image = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _B64},
        }
        entry["toolUseResult"] = [{"type": "text", "text": text}, mirror_image]
    if figure:
        inner[0]["text"] = f"{text} saved at {figure}"
    return entry


def _figure_session(num_steps: int) -> list[dict[str, Any]]:
    prompt = {
        "type": "user",
        "uuid": "prompt",
        "message": {"role": "user", "content": [{"type": "text", "text": "Analyze this dataset."}]},
    }
    entries: list[dict[str, Any]] = [prompt]
    for i in range(num_steps):
        tool = _tool_use(f"t{i}", "evaluate_cluster_qc", {"k": i, "big": "y" * 500})
        entries.append(_assistant(f"a{i}", f"Running step {i} " + "x" * 500, tool))
        entries.append(
            _tool_result(
                f"u{i}", f"t{i}", "R" * 2000, with_image=True, figure=f"figures/step_{i}.png"
            )
        )
    return entries


# --- estimation ------------------------------------------------------------------


def test_estimate_strips_base64_and_counts_model_facing_images() -> None:
    with_image = _tool_result("u", "t", "hello", with_image=True)
    without = _tool_result("u", "t", "hello", with_image=False)
    # The 4000-char base64 blob must not dominate the estimate; one image ~ image_token_estimate.
    delta = estimate_tokens([with_image], image_token_estimate=1500) - estimate_tokens(
        [without], image_token_estimate=1500
    )
    assert 1400 <= delta <= 1600


def test_estimate_ignores_non_model_facing_metadata_mirror() -> None:
    entry = _tool_result("u", "t", "hello", with_image=True)
    stripped = copy.deepcopy(entry)
    stripped.pop("toolUseResult")
    # toolUseResult is metadata, not sent to the model, so it must not change the estimate.
    assert estimate_tokens([entry]) == estimate_tokens([stripped])


# --- no-op below threshold -------------------------------------------------------


def test_below_target_is_a_noop_and_returns_input_unchanged() -> None:
    entries = _figure_session(2)
    out, stats = compact(entries, context_limit=1_000_000)
    assert stats.triggered is False
    assert out == entries


def test_disabled_config_is_a_noop() -> None:
    entries = _figure_session(30)
    out, stats = compact_entries(entries, CompactionConfig(context_limit=None))
    assert stats.triggered is False
    assert out == entries


# --- normal tier -----------------------------------------------------------------


def test_normal_tier_keeps_recent_figures_and_ages_out_old_ones() -> None:
    entries = _figure_session(40)  # 40 figures, far over budget
    before = estimate_tokens(entries)
    budget = compute_budget(120_000, output_reserve=16_000, overhead_tokens=4_000)
    out, stats = compact(
        entries,
        context_limit=120_000,
        output_reserve=16_000,
        overhead_tokens=4_000,
        max_context_images=8,
    )
    assert stats.triggered is True
    assert stats.images_kept == 8
    assert stats.images_evicted == 32
    assert stats.tokens_after <= budget.trim_target
    assert stats.tokens_after < before


def test_compaction_never_mutates_input() -> None:
    entries = _figure_session(40)
    snapshot = copy.deepcopy(entries)
    compact(entries, context_limit=120_000)
    assert entries == snapshot


def test_structure_and_pairing_preserved() -> None:
    entries = _figure_session(40)
    out, _ = compact(entries, context_limit=120_000)
    assert [e.get("uuid") for e in out] == [e.get("uuid") for e in entries]
    assert [e.get("type") for e in out] == [e.get("type") for e in entries]
    tool_use_ids = {
        b["id"]
        for e in out
        for b in e.get("message", {}).get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    }
    tool_result_ids = {
        b["tool_use_id"]
        for e in out
        for b in e.get("message", {}).get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    assert tool_result_ids.issubset(tool_use_ids)


def test_evicted_image_becomes_placeholder_naming_the_figure() -> None:
    entries = _figure_session(40)
    out, _ = compact(entries, context_limit=120_000)
    # The first tool-result's image is old and should be a text placeholder naming its path.
    first_result = out[2]["message"]["content"][0]
    assert first_result["type"] == "tool_result"
    kinds = [b.get("type") for b in first_result["content"]]
    assert "image" not in kinds
    texts = [b.get("text", "") for b in first_result["content"] if b["type"] == "text"]
    assert any("step_0.png" in text for text in texts)


def test_metadata_mirror_base64_is_scrubbed_in_replay_copy() -> None:
    entries = _figure_session(40)
    out, _ = compact(entries, context_limit=120_000)
    for entry in out:
        mirror = entry.get("toolUseResult")
        if isinstance(mirror, list):
            for block in mirror:
                if isinstance(block, dict) and block.get("type") == "image":
                    assert block["source"]["data"] == ""


# --- emergency tier --------------------------------------------------------------


def test_emergency_tier_engages_when_normal_is_insufficient() -> None:
    # A single enormous protected-tail result cannot be shed by the normal tier alone.
    entries = _figure_session(3)
    entries.append(_tool_result("huge", "th", "Z" * 4_000_000))
    out, stats = compact(entries, context_limit=120_000, output_reserve=16_000)
    assert stats.emergency is True


def test_force_emergency_trims_even_when_small() -> None:
    entries = _figure_session(5)
    out, stats = compact(entries, context_limit=1_000_000, force_emergency=True)
    assert stats.triggered is True
    assert stats.emergency is True


# --- calibration -----------------------------------------------------------------


def test_ratchet_only_increases_and_clamps() -> None:
    assert ratchet_calibration(1.0, 100, 200) == 2.0
    # Never lowers below the current factor.
    assert ratchet_calibration(2.0, 100, 100) == 2.0
    # Clamped at 4x.
    assert ratchet_calibration(1.0, 100, 1000) == 4.0
    # Degenerate inputs are safe.
    assert ratchet_calibration(1.5, 0, 200) == 1.5
