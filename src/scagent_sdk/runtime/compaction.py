"""Legacy-style live compaction of the replayed model transcript.

A long scientific session accumulates one tool-result envelope and, more expensively, one or
more figure images per step. The Claude Agent SDK replays the entire transcript on every
resume, so without trimming the model's input eventually exceeds the context window (observed:
a 40.5 MiB transcript, 39.6 MiB of it base64 across 127 image blocks, overflowing a 262 K
window).

This module compacts the *copy of the transcript handed back to the model* — the return value
of :meth:`ScientificSessionTranscriptStore.load`, which the SDK writes out as the transcript
the CLI subprocess resumes from. The append-only ``transcript.jsonl`` on disk and the durable
scientific state (``state.json``, events, artifacts) are never touched; only what the model
replays is trimmed. This is the SessionStore-transport equivalent of the legacy ``scagent``
message trimmer: three tiers, largest-first, with a protected recent tail and a durable
state object the placeholders point back to.

The one deliberate departure from a literal legacy port is image handling. Legacy kept every
image and only estimated it cheaply; here images dominate the payload, so aged figures beyond
a recent window are replaced with a text placeholder naming the saved figure path (the pixels
remain on disk and can be re-read). Everything is pure Python over transcript dicts so it is
unit-testable outside the compute runtimes.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

# --- Token estimation ------------------------------------------------------------
# Base64 image payloads dominate a scientific transcript's bytes but not its model-token cost,
# so they are stripped from the char estimate and each image is counted as a flat figure cost.
_BASE64_DATA = re.compile(r'"data"\s*:\s*"[A-Za-z0-9+/=]+"')
# A long base64 blob (used to scrub the non-model-facing metadata copies of figures).
_LONG_BASE64 = re.compile(r"^[A-Za-z0-9+/=]{200,}$")
# A figure path. The path characters are matched with a bounded, whitespace/quote-free run so
# the pattern cannot backtrack catastrophically on a huge text block that contains no path.
_FIGURE_PATH = re.compile(r"[^\s\"']{1,256}\.(?:png|jpg|jpeg|svg|pdf|tif|tiff)", re.IGNORECASE)
# Figure paths appear at the head of a tool-result envelope; only that prefix is searched so the
# cost of the scan stays bounded regardless of how large the result body is.
MAX_FIGURE_SEARCH_CHARS = 8192
CHARS_PER_TOKEN = 4
DEFAULT_IMAGE_TOKEN_ESTIMATE = 1500

# --- Budget ----------------------------------------------------------------------
DEFAULT_OUTPUT_RESERVE_TOKENS = 32_000
MIN_SAFETY_MARGIN_TOKENS = 2_000
SAFETY_MARGIN_FRACTION = 0.03
TRIM_TARGET_FRACTION = 0.85
MIN_AVAILABLE_TOKENS = 4_000

# --- Trim policy -----------------------------------------------------------------
DEFAULT_KEEP_TAIL_ENTRIES = 8
DEFAULT_MAX_CONTEXT_IMAGES = 8
EMERGENCY_MAX_CONTEXT_IMAGES = 1
TRIM_MIN_CHARS = 300
MAX_ASSISTANT_CHARS = 600
EMERGENCY_ASSISTANT_CHARS = 200
CALIBRATION_MIN = 1.0
CALIBRATION_MAX = 4.0

_RESULT_PLACEHOLDER = (
    "[trimmed — tool result already processed; the current analysis state is persisted in the "
    "session state.json and artifact index. Read them if this result is needed again.]"
)
_ARGS_PLACEHOLDER: dict[str, Any] = {"_trimmed": True}


def _image_placeholder(figure_ref: str | None) -> dict[str, Any]:
    where = f" saved at {figure_ref}" if figure_ref else ""
    return {
        "type": "text",
        "text": (
            f"[figure aged out of the replayed context to fit the model window{where}. "
            "Re-read it from disk if it is needed again; analysis state is persisted in "
            "state.json.]"
        ),
    }


# --- Estimation ------------------------------------------------------------------


def _model_facing_chars(entry: dict[str, Any]) -> int:
    """Serialized size of the part of an entry the model actually receives.

    Only ``entry["message"]`` becomes an API message on replay; sibling fields such as
    ``toolUseResult`` are Claude Code's own transcript metadata (and duplicate the figures the
    model already sees in ``message.content``), so they are excluded from the token estimate.
    Entries without a ``message`` (queue/attachment/last-prompt bookkeeping) are small metadata
    and contribute nothing to the model context.
    """

    message = entry.get("message")
    if not isinstance(message, dict):
        return 0
    blob = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    return len(_BASE64_DATA.sub('"data":""', blob))


def estimate_tokens(
    entries: list[dict[str, Any]],
    *,
    calibration: float = 1.0,
    image_token_estimate: int = DEFAULT_IMAGE_TOKEN_ESTIMATE,
) -> int:
    """Char-based token estimate of the model-facing transcript.

    Base64 is stripped from the char count and each model-facing image (``message.content``,
    including images nested in a ``tool_result``) is counted as a flat figure cost. Mirrors the
    legacy estimator: cheap, deterministic, and safe to run before every turn. A calibration
    factor (ratcheted upward against real usage) corrects tokenizers that pack more tokens per
    character than the 4:1 heuristic.
    """

    chars = sum(_model_facing_chars(entry) for entry in entries)
    images = len(_iter_image_slots(entries))
    base = (chars // CHARS_PER_TOKEN) + images * image_token_estimate
    return int(base * calibration)


@dataclass(frozen=True)
class Budget:
    trim_target: int
    hard_limit: int


def compute_budget(
    context_limit: int,
    *,
    output_reserve: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    overhead_tokens: int = 0,
) -> Budget:
    """Available history budget after fixed overhead; trim proactively at 85%, hard-cap at 100%."""

    safety = max(MIN_SAFETY_MARGIN_TOKENS, int(context_limit * SAFETY_MARGIN_FRACTION))
    available = context_limit - output_reserve - overhead_tokens - safety
    available = max(available, MIN_AVAILABLE_TOKENS)
    return Budget(trim_target=int(available * TRIM_TARGET_FRACTION), hard_limit=available)


# --- Configuration & stats -------------------------------------------------------


@dataclass(frozen=True)
class CompactionConfig:
    context_limit: int | None = None
    output_reserve: int = DEFAULT_OUTPUT_RESERVE_TOKENS
    overhead_tokens: int = 0
    image_token_estimate: int = DEFAULT_IMAGE_TOKEN_ESTIMATE
    keep_tail_entries: int = DEFAULT_KEEP_TAIL_ENTRIES
    max_context_images: int = DEFAULT_MAX_CONTEXT_IMAGES
    enabled: bool = True

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.context_limit and self.context_limit > 0)


@dataclass
class CompactionStats:
    triggered: bool = False
    emergency: bool = False
    tokens_before: int = 0
    tokens_after: int = 0
    entries: int = 0
    images_evicted: int = 0
    images_kept: int = 0
    results_trimmed: int = 0
    args_trimmed: int = 0
    assistant_truncated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "emergency": self.emergency,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "entries": self.entries,
            "images_evicted": self.images_evicted,
            "images_kept": self.images_kept,
            "results_trimmed": self.results_trimmed,
            "args_trimmed": self.args_trimmed,
            "assistant_truncated": self.assistant_truncated,
        }


# --- Block walkers ---------------------------------------------------------------


def _content(entry: dict[str, Any]) -> list[Any] | None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, list) else None


def _figure_ref_from(blocks: list[Any]) -> str | None:
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            match = _FIGURE_PATH.search(str(block.get("text", ""))[:MAX_FIGURE_SEARCH_CHARS])
            if match:
                return match.group(0)
    return None


def _iter_image_slots(
    entries: list[dict[str, Any]],
) -> list[tuple[list[Any], int, int, str | None]]:
    """Every image block as (parent_list, index_in_parent, entry_index, figure_ref)."""

    slots: list[tuple[list[Any], int, int, str | None]] = []
    for entry_index, entry in enumerate(entries):
        content = _content(entry)
        if content is None:
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                slots.append((content, block_index, entry_index, None))
            elif block.get("type") == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    ref = _figure_ref_from(inner)
                    for sub_index, sub in enumerate(inner):
                        if isinstance(sub, dict) and sub.get("type") == "image":
                            slots.append((inner, sub_index, entry_index, ref))
    return slots


def _block_tokens(value: Any, calibration: float) -> int:
    rendered = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return int((len(rendered) // CHARS_PER_TOKEN) * calibration)


def _scrub_base64_data(value: Any) -> Any:
    """Empty any long base64 ``data`` string, recursively; preserve structure otherwise."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "data" and isinstance(item, str) and _LONG_BASE64.match(item):
                result[key] = ""
            else:
                result[key] = _scrub_base64_data(item)
        return result
    if isinstance(value, list):
        return [_scrub_base64_data(item) for item in value]
    return value


def _scrub_duplicate_media(entry: dict[str, Any]) -> None:
    """Drop base64 figure bytes from an entry's non-model-facing metadata fields, in place.

    Claude Code mirrors each tool result (figures included) into ``toolUseResult`` alongside the
    model-facing ``message``. That copy is never sent to the model but doubles the transcript's
    on-disk/materialized bytes, so it is emptied in the replay copy while the durable append-only
    transcript keeps it intact. ``message`` is left untouched — its images are governed by the
    eviction pass so the model still sees the most recent figures.
    """

    for key in list(entry.keys()):
        if key == "message":
            continue
        entry[key] = _scrub_base64_data(entry[key])


# --- Passes ----------------------------------------------------------------------


def _evict_images(
    working: list[dict[str, Any]],
    trimmable: set[int],
    max_images: int,
    calibration: float,
    image_token_estimate: int,
    stats: CompactionStats,
    current: int,
) -> int:
    slots = _iter_image_slots(working)
    keep_from = max(0, len(slots) - max_images)
    placeholder_tokens = _block_tokens(_image_placeholder(None), calibration)
    per_image = int(image_token_estimate * calibration)
    for order, (parent, index, entry_index, ref) in enumerate(slots):
        recent_enough = order >= keep_from
        if recent_enough or entry_index not in trimmable:
            stats.images_kept += 1
            continue
        parent[index] = _image_placeholder(ref)
        stats.images_evicted += 1
        current -= max(0, per_image - placeholder_tokens)
    return current


def _trim_results(
    working: list[dict[str, Any]],
    trimmable: set[int],
    min_chars: int,
    calibration: float,
    stats: CompactionStats,
    current: int,
    target: int,
) -> int:
    candidates: list[tuple[int, int, int]] = []
    for entry_index in trimmable:
        content = _content(working[entry_index])
        if content is None:
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if isinstance(inner, str):  # already trimmed
                continue
            size = len(json.dumps(inner, separators=(",", ":"), ensure_ascii=False))
            if size > min_chars:
                candidates.append((size, entry_index, block_index))
    candidates.sort(reverse=True)
    for _size, entry_index, block_index in candidates:
        if current <= target:
            break
        block = _content(working[entry_index])[block_index]  # type: ignore[index]
        before = _block_tokens(block.get("content"), calibration)
        block["content"] = _RESULT_PLACEHOLDER
        after = _block_tokens(_RESULT_PLACEHOLDER, calibration)
        current -= max(0, before - after)
        stats.results_trimmed += 1
    return current


def _trim_args(
    working: list[dict[str, Any]],
    trimmable: set[int],
    min_chars: int,
    calibration: float,
    stats: CompactionStats,
    current: int,
    target: int,
) -> int:
    candidates: list[tuple[int, int, int]] = []
    for entry_index in trimmable:
        content = _content(working[entry_index])
        if content is None:
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            args = block.get("input")
            if args == _ARGS_PLACEHOLDER:
                continue
            size = len(json.dumps(args, separators=(",", ":"), ensure_ascii=False))
            if size > min_chars:
                candidates.append((size, entry_index, block_index))
    candidates.sort(reverse=True)
    for _size, entry_index, block_index in candidates:
        if current <= target:
            break
        block = _content(working[entry_index])[block_index]  # type: ignore[index]
        before = _block_tokens(block.get("input"), calibration)
        block["input"] = dict(_ARGS_PLACEHOLDER)
        after = _block_tokens(_ARGS_PLACEHOLDER, calibration)
        current -= max(0, before - after)
        stats.args_trimmed += 1
    return current


def _truncate_assistant_text(
    working: list[dict[str, Any]],
    trimmable: set[int],
    cap: int,
    calibration: float,
    stats: CompactionStats,
    current: int,
    target: int,
) -> int:
    for entry_index in trimmable:
        if current <= target:
            break
        entry = working[entry_index]
        if entry.get("type") != "assistant":
            continue
        content = _content(entry)
        if content is None:
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text", ""))
            if len(text) <= cap:
                continue
            before = _block_tokens(text, calibration)
            block["text"] = text[:cap] + " …[truncated]"
            after = _block_tokens(block["text"], calibration)
            current -= max(0, before - after)
            stats.assistant_truncated += 1
    return current


def compact(
    entries: list[dict[str, Any]],
    *,
    context_limit: int,
    output_reserve: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    overhead_tokens: int = 0,
    calibration: float = 1.0,
    image_token_estimate: int = DEFAULT_IMAGE_TOKEN_ESTIMATE,
    keep_tail_entries: int = DEFAULT_KEEP_TAIL_ENTRIES,
    max_context_images: int = DEFAULT_MAX_CONTEXT_IMAGES,
    force_emergency: bool = False,
) -> tuple[list[dict[str, Any]], CompactionStats]:
    """Return a compacted copy of ``entries`` and stats. A no-op below the trim target.

    Never mutates ``entries``. Preserves the first entry, the last ``keep_tail_entries``,
    every entry's ``uuid``/``type``, and the tool_use↔tool_result pairing (blocks are kept;
    only their payloads are replaced).
    """

    budget = compute_budget(
        context_limit, output_reserve=output_reserve, overhead_tokens=overhead_tokens
    )
    before = estimate_tokens(
        entries, calibration=calibration, image_token_estimate=image_token_estimate
    )
    stats = CompactionStats(tokens_before=before, tokens_after=before, entries=len(entries))
    if before <= budget.trim_target and not force_emergency:
        return list(entries), stats

    working = deepcopy(entries)
    stats.triggered = True
    # Drop the duplicated, non-model-facing figure bytes from every entry's metadata mirror so
    # the materialized replay file is not needlessly enormous. Does not affect model tokens.
    for entry in working:
        _scrub_duplicate_media(entry)
    count = len(working)
    target = budget.trim_target

    def _run_tier(trimmable: set[int], max_images: int, min_chars: int, cap: int) -> int:
        current = estimate_tokens(
            working, calibration=calibration, image_token_estimate=image_token_estimate
        )
        current = _evict_images(
            working, trimmable, max_images, calibration, image_token_estimate, stats, current
        )
        if current > target:
            current = _trim_results(
                working, trimmable, min_chars, calibration, stats, current, target
            )
        if current > target:
            current = _trim_args(
                working, trimmable, min_chars, calibration, stats, current, target
            )
        if current > target:
            current = _truncate_assistant_text(
                working, trimmable, cap, calibration, stats, current, target
            )
        return current

    # Normal tier: keep the recent tail and a recent-figure window; trim aged material.
    _run_tier(
        set(range(1, max(1, count - keep_tail_entries))),
        max_context_images,
        TRIM_MIN_CHARS,
        MAX_ASSISTANT_CHARS,
    )

    # Escalate only if the normal tier still leaves us above the hard ceiling: reach into the
    # tail, keep a single figure, and drop every size floor.
    recomputed = estimate_tokens(
        working, calibration=calibration, image_token_estimate=image_token_estimate
    )
    if force_emergency or recomputed > budget.hard_limit:
        stats.emergency = True
        _run_tier(
            set(range(0, max(0, count - 1))),
            EMERGENCY_MAX_CONTEXT_IMAGES,
            0,
            EMERGENCY_ASSISTANT_CHARS,
        )

    stats.tokens_after = estimate_tokens(
        working, calibration=calibration, image_token_estimate=image_token_estimate
    )
    return working, stats


def compact_entries(
    entries: list[dict[str, Any]],
    config: CompactionConfig,
    calibration: float = 1.0,
    *,
    force_emergency: bool = False,
) -> tuple[list[dict[str, Any]], CompactionStats]:
    """Convenience wrapper applying a :class:`CompactionConfig`."""

    if not config.active or config.context_limit is None:
        return list(entries), CompactionStats(
            tokens_before=estimate_tokens(
                entries, calibration=calibration, image_token_estimate=config.image_token_estimate
            ),
            entries=len(entries),
        )
    stats_before = estimate_tokens(
        entries, calibration=calibration, image_token_estimate=config.image_token_estimate
    )
    compacted, stats = compact(
        entries,
        context_limit=config.context_limit,
        output_reserve=config.output_reserve,
        overhead_tokens=config.overhead_tokens,
        calibration=calibration,
        image_token_estimate=config.image_token_estimate,
        keep_tail_entries=config.keep_tail_entries,
        max_context_images=config.max_context_images,
        force_emergency=force_emergency,
    )
    stats.tokens_before = stats_before
    return compacted, stats


def ratchet_calibration(
    current: float, estimated_tokens: int, actual_tokens: int
) -> float:
    """Raise the calibration factor when the estimate under-counts real usage (never lower it).

    Mirrors legacy: the char heuristic only ever needs to become *more* conservative, and a
    runaway factor is capped so one anomalous turn cannot over-trim every future turn.
    """

    if estimated_tokens <= 0 or actual_tokens <= 0:
        return _clamp_calibration(current)
    implied = current * (actual_tokens / estimated_tokens)
    return _clamp_calibration(max(current, implied))


def _clamp_calibration(value: float) -> float:
    if value < CALIBRATION_MIN:
        return CALIBRATION_MIN
    if value > CALIBRATION_MAX:
        return CALIBRATION_MAX
    return value
