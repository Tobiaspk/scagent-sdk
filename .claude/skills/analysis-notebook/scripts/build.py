"""Build a readable analysis notebook from whatever the session has committed so far.

This is deliberately a requested capability rather than a side effect of finalization. An analysis
does not have one ending: a user may want a walkthrough mid-run, after annotation, and again after
further work. Binding the notebook to `finalize_analysis` would have made it available only once
the finalization floors already pass, which is both too late and only once.

The document is rebuilt from committed provenance every time, so a later request simply covers the
steps added since. Nothing is overwritten: each build commits its own immutable artifact and the
`reports/analysis-notebook.ipynb` view points at the newest one.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notebook  # noqa: E402  (sibling module; path inserted above)

_MEGABYTE = 1024 * 1024


def _capability_history(session_dir: Path) -> list[dict[str, Any]]:
    """Order committed capability results by their commit event.

    ``state.json`` holds the records but not their order; the event log owns sequence. A record
    with no commit event sorts first under sequence 0, which only happens for hand-edited state.
    """

    state_path = session_dir / "state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict):
        return []
    sequences: dict[str, int] = {}
    events_path = session_dir / "events.jsonl"
    if events_path.is_file():
        with events_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("kind") != "capability.result_committed":
                    continue
                execution_id = event.get("payload", {}).get("execution_id")
                if isinstance(execution_id, str):
                    sequences[execution_id] = int(event.get("sequence", 0))
    history: list[dict[str, Any]] = []
    for execution_id, raw in artifacts.items():
        if not isinstance(raw, dict):
            continue
        history.append(
            {
                "sequence": sequences.get(str(execution_id), 0),
                "execution_id": str(execution_id),
                "skill_id": raw.get("skill_id"),
                "skill_version": raw.get("skill_version"),
                "tool_name": raw.get("tool_name"),
                "arguments": raw.get("arguments", {}),
                "summary": raw.get("summary", ""),
                "files": raw.get("files", []),
                # The committed dir is named <action>--<execution_id>, so the record's own path is
                # authoritative; reconstructing it from execution_id alone is stale.
                "path": raw.get("path"),
            }
        )
    return sorted(history, key=lambda item: (item["sequence"], item["execution_id"]))


def _nested_int(facts: dict[str, Any], *keys: str) -> int | None:
    node: Any = facts
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return int(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else None


def _current_shape(facts: dict[str, Any]) -> tuple[int, int] | None:
    """Prefer the revision facts, which track the working object rather than the original input."""

    cells = _nested_int(facts, "analysis", "dataset_revision", "n_cells")
    genes = _nested_int(facts, "analysis", "dataset_revision", "n_genes")
    if cells is None or genes is None:
        cells = _nested_int(facts, "dataset_contents", "shape", "n_obs")
        genes = _nested_int(facts, "dataset_contents", "shape", "n_vars")
    return (cells, genes) if cells is not None and genes is not None else None


def _dataset_path(facts: dict[str, Any], history: list[dict[str, Any]]) -> str:
    dataset = facts.get("dataset")
    if isinstance(dataset, dict) and isinstance(dataset.get("path"), str):
        return str(dataset["path"])
    for item in history:
        arguments = item.get("arguments")
        if isinstance(arguments, dict) and isinstance(arguments.get("path"), str):
            return str(arguments["path"])
    return "dataset"


def _final_label_rows(session_dir: Path, facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the committed label table when the session is finalized.

    Parsed with the ``csv`` module rather than pandas so this capability keeps running in the
    control plane and needs no compute runtime.
    """

    finalization = facts.get("finalization")
    if not isinstance(finalization, dict) or finalization.get("status") != "complete":
        return []
    report_path = finalization.get("report_path")
    if not isinstance(report_path, str):
        return []
    candidate = (session_dir / Path(report_path).parent / "final-labels.csv").resolve()
    try:
        candidate.relative_to(session_dir.resolve())
    except ValueError:
        return []
    if not candidate.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with candidate.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                record = dict(row)
                try:
                    record["n_cells"] = int(str(row.get("n_cells", "0")) or 0)
                except ValueError:
                    record["n_cells"] = 0
                rows.append(record)
    except OSError:
        return []
    return rows


def build_analysis_notebook(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    session_dir = Path(context.session_dir)
    history = _capability_history(session_dir)
    if not history:
        raise ValueError(
            "no committed capability results yet; run at least one analysis step before "
            "requesting a notebook"
        )
    facts = context.state_facts if isinstance(context.state_facts, dict) else {}
    finalization = facts.get("finalization")
    finalized = isinstance(finalization, dict) and finalization.get("status") == "complete"

    dataset_path = _dataset_path(facts, history)
    requested_title = arguments.get("title")
    title = (
        str(requested_title).strip()
        if isinstance(requested_title, str) and str(requested_title).strip()
        else f"Single-cell analysis — {Path(dataset_path).stem}"
    )
    caveats = [
        str(item)
        for item in (arguments.get("caveats") or [])
        if isinstance(item, str) and item.strip()
    ]
    budget = arguments.get("max_embedded_megabytes")
    embed_budget = (
        int(budget * _MEGABYTE) if isinstance(budget, (int, float)) and budget > 0 else None
    )

    document = notebook.build_analysis_notebook(
        title=title,
        dataset_path=dataset_path,
        history=history,
        session_dir=session_dir,
        shape=_current_shape(facts),
        n_clusters=_nested_int(facts, "analysis", "clustering", "n_clusters"),
        finalized=finalized,
        label_rows=_final_label_rows(session_dir, facts),
        caveats=caveats,
        embed_budget_bytes=embed_budget,
    )
    target = context.staging_dir / "analysis-notebook.ipynb"
    target.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    embedded = sum(len(cell.get("attachments") or {}) for cell in document["cells"])
    stage = "a finalized" if finalized else "an in-progress"
    return {
        "summary": (
            f"Built {stage} analysis notebook covering {len(history)} committed step(s) "
            f"with {embedded} embedded figure(s)."
        ),
        "details": {
            "steps": len(history),
            "finalized": finalized,
            "cells": len(document["cells"]),
            "embedded_figures": embedded,
            "size_bytes": target.stat().st_size,
            "regenerated_from": "committed session provenance",
        },
        "artifacts": [
            {
                "name": "analysis-notebook",
                "relative_path": "analysis-notebook.ipynb",
                "media_type": "application/x-ipynb+json",
            }
        ],
    }
