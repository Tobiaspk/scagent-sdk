"""Render committed session provenance as a human-readable Jupyter notebook.

The session already carries an exact machine record (``analysis-recipe.py``) and a narrative
report (``analysis-report.md``). Neither is pleasant to read step by step: the recipe is a call
list, and the report's workflow table holds raw JSON arguments with session-absolute paths.

This module produces the reading surface instead -- ordered steps, resolved arguments, full
decision rationales, and the figures each step actually produced, inline. Three deliberate
choices:

* **Chronological, not regrouped.** A real analysis revisits phases (cluster, QC, cluster again).
  Sorting steps into phase buckets would misrepresent the order, so phase headings are emitted
  when the phase changes and marked ``(continued)`` when one recurs.
* **Illustrative code, not runnable science.** Capability calls execute inside the scagent-sdk
  runtime with floors, staging, and artifact commits; they are not standalone scanpy. The code
  cells record each call faithfully and the ``call`` stub raises an explanatory error, so pressing
  shift-enter produces an explanation rather than a ``NameError``.
* **Only stdlib.** This runs inside the compute runtime but carries no scientific dependency, so
  it stays unit-testable from the control plane.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

NOTEBOOK_FORMAT = 4
NOTEBOOK_FORMAT_MINOR = 5

# Figures a step emits in a nested subdirectory are a per-item diagnostic series (one panel per
# cluster); the shallowest group is the step's overview. Embedding the series would dominate the
# file, so only the overview is inlined and the series is linked.
_EMBED_PER_STEP = 6
# Counted against source PNG bytes; base64 inflates roughly a third, so this lands near 8 MB.
_EMBED_TOTAL_BYTES = 6 * 1024 * 1024
_INLINE_STRING_MAXLEN = 200
_IMAGE_PREFIX = "image/"

_PHASE_BY_SKILL = {
    "inspect-dataset": "Dataset identity and contents",
    "single-cell-counts": "Raw counts",
    "cellbender-background-removal": "Ambient RNA removal",
    "single-cell-qc": "Cell-level quality control",
    "doublet-evidence": "Doublet evidence",
    "expression-preprocessing": "Normalization and feature selection",
    "dimensionality-reduction": "Representation",
    "scvi-integration": "Representation",
    "single-cell-clustering": "Clustering",
    "cluster-qc": "Cluster quality control",
    "batch-investigation": "Batch investigation",
    "celltypist-annotation": "Annotation",
    "scimilarity-annotation": "Annotation",
    "marker-annotation": "Annotation",
    "visualize-single-cell": "Figures",
    "inspect-media": "Figure review",
    "research-literature": "Literature and web evidence",
    "research-web": "Literature and web evidence",
    "analysis-workspace": "Direct inspection",
    "finalize-analysis": "Finalization",
}
_DEFAULT_PHASE = "Other steps"

# A committed artifact path carries the execution id, which is how an input is traced back to the
# step that produced it.
_ARTIFACT_PATH = re.compile(
    r"(?:[^\s\"']*/)?artifacts/capabilities/([0-9a-fA-F][0-9a-fA-F-]{7,})/([^\s\"']+)"
)
_DECISION_KEYS = ("decision", "disposition", "verdict")
_RATIONALE_KEYS = ("rationale", "justification", "override_justification")


def _phase(skill_id: Any) -> str:
    return _PHASE_BY_SKILL.get(str(skill_id), _DEFAULT_PHASE)


def _steps_by_execution(history: list[dict[str, Any]]) -> dict[str, tuple[int, str]]:
    """Map each execution id to its step number and tool, for resolving inputs to producers."""

    mapping: dict[str, tuple[int, str]] = {}
    for index, item in enumerate(history, start=1):
        execution_id = item.get("execution_id")
        if isinstance(execution_id, str):
            mapping[execution_id] = (index, str(item.get("tool_name", "capability")))
    return mapping


def _describe_path(text: str, steps: dict[str, tuple[int, str]]) -> tuple[str, str | None]:
    """Reduce a committed artifact path to its filename plus the step that produced it."""

    match = _ARTIFACT_PATH.search(text)
    if match is None:
        return text, None
    execution_id, remainder = match.group(1), match.group(2)
    filename = remainder.rsplit("/", 1)[-1]
    produced = steps.get(execution_id)
    if produced is None:
        return filename, None
    number, tool = produced
    return filename, f"output of step {number} \u00b7 {tool}"


def _humanize(value: Any, steps: dict[str, tuple[int, str]]) -> Any:
    """Rewrite committed artifact paths anywhere inside an argument structure."""

    if isinstance(value, str):
        return _describe_path(value, steps)[0]
    if isinstance(value, dict):
        return {key: _humanize(item, steps) for key, item in value.items()}
    if isinstance(value, list):
        return [_humanize(item, steps) for item in value]
    return value


def _is_long_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) > _INLINE_STRING_MAXLEN


def _split_arguments(
    arguments: dict[str, Any], steps: dict[str, tuple[int, str]]
) -> tuple[list[tuple[str, str, str | None]], list[tuple[str, str]], list[tuple[str, Any]]]:
    """Separate arguments into inline scalars, long prose, and structured values.

    Prose and structures are what make the existing report table unreadable, so they are lifted
    out of the call signature and rendered as their own prose or JSON blocks.
    """

    inline: list[tuple[str, str, str | None]] = []
    prose: list[tuple[str, str]] = []
    structured: list[tuple[str, Any]] = []
    for key in sorted(arguments):
        value = arguments[key]
        if _is_long_text(value):
            prose.append((key, value))
        elif isinstance(value, (dict, list)) and value:
            structured.append((key, _humanize(value, steps)))
        elif isinstance(value, str):
            short, note = _describe_path(value, steps)
            inline.append((key, repr(short), note))
        else:
            inline.append((key, repr(value), None))
    return inline, prose, structured


def _decision_of(arguments: dict[str, Any]) -> str | None:
    for key in _DECISION_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _rationales_of(arguments: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for key in _RATIONALE_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            found.append((key, value.strip()))
    return found


def _quote(text: str) -> list[str]:
    """Render prose as a blockquote, preserving paragraphs and never truncating."""

    lines: list[str] = []
    for paragraph in text.strip().splitlines():
        stripped = paragraph.strip()
        lines.append(f"> {stripped}" if stripped else ">")
    return lines


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").strip()

    lines = [
        "| " + " | ".join(cell(header) for header in headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return lines


def _markdown_cell(lines: list[str], attachments: dict[str, Any] | None = None) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "cell_type": "markdown",
        "metadata": {},
        "source": _as_source(lines),
    }
    if attachments:
        cell["attachments"] = attachments
    return cell


def _code_cell(lines: list[str]) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": _as_source(lines),
    }


def _as_source(lines: list[str]) -> list[str]:
    """nbformat stores source as a list of lines that each keep their trailing newline."""

    if not lines:
        return []
    return [f"{line}\n" for line in lines[:-1]] + [lines[-1]]


def _figure_files(item: dict[str, Any]) -> list[dict[str, Any]]:
    files = item.get("files")
    if not isinstance(files, list):
        return []
    return [
        raw
        for raw in files
        if isinstance(raw, dict)
        and str(raw.get("media_type", "")).startswith(_IMAGE_PREFIX)
        and isinstance(raw.get("relative_path"), str)
    ]


def _plan_figures(
    figures: list[dict[str, Any]], remaining_bytes: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Choose which of a step's figures to inline: the shallowest group, within budget."""

    if not figures:
        return [], []
    shallowest = min(raw["relative_path"].count("/") for raw in figures)
    overview = [raw for raw in figures if raw["relative_path"].count("/") == shallowest]
    series = [raw for raw in figures if raw["relative_path"].count("/") > shallowest]
    embedded: list[dict[str, Any]] = []
    linked: list[dict[str, Any]] = list(series)
    for raw in sorted(overview, key=lambda value: str(value["relative_path"])):
        size = int(raw.get("size_bytes") or 0)
        if len(embedded) >= _EMBED_PER_STEP or size > remaining_bytes:
            linked.append(raw)
            continue
        embedded.append(raw)
        remaining_bytes -= size
    return embedded, linked


def _attach(source: Path, name: str) -> dict[str, Any] | None:
    try:
        payload = base64.b64encode(source.read_bytes()).decode("ascii")
    except OSError:
        return None
    media = "image/png" if source.suffix.lower() == ".png" else "image/jpeg"
    return {name: {media: payload}}


def _step_heading(number: int, item: dict[str, Any]) -> str:
    tool = str(item.get("tool_name", "capability"))
    skill = str(item.get("skill_id", "?"))
    version = str(item.get("skill_version", "?"))
    return f"### Step {number} \u00b7 `{tool}`\n\n<sub>{skill} {version}</sub>"


def _render_step(
    *,
    number: int,
    item: dict[str, Any],
    steps: dict[str, tuple[int, str]],
    embedded: list[tuple[str, dict[str, Any], str]],
    linked: list[str],
) -> list[dict[str, Any]]:
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    inline, prose, structured = _split_arguments(dict(arguments), steps)
    lines = [_step_heading(number, item), ""]

    decision = _decision_of(dict(arguments))
    if decision is not None:
        lines.extend([f"**Decision: `{decision}`**", ""])
    summary = str(item.get("summary", "")).strip()
    if summary:
        lines.extend([summary, ""])
    for key, text in _rationales_of(dict(arguments)):
        lines.extend([f"*{key.replace('_', ' ').capitalize()}*", ""])
        lines.extend(_quote(text))
        lines.append("")
    for key, text in prose:
        if key in _RATIONALE_KEYS:
            continue
        lines.extend([f"*{key.replace('_', ' ').capitalize()}*", ""])
        lines.extend(_quote(text))
        lines.append("")
    for key, value in structured:
        lines.extend([f"*{key.replace('_', ' ')}*", "", "```json"])
        lines.extend(json.dumps(value, indent=2, sort_keys=True).splitlines())
        lines.extend(["```", ""])

    attachments: dict[str, Any] = {}
    for name, attachment, caption in embedded:
        attachments.update(attachment)
        lines.extend([f"![{caption}](attachment:{name})", "", f"<sub>{caption}</sub>", ""])
    if linked:
        lines.extend(
            [
                f"<details><summary>{len(linked)} further diagnostic figure(s)</summary>",
                "",
            ]
        )
        lines.extend(f"- [{Path(target).name}]({target})" for target in linked)
        lines.extend(["", "</details>", ""])

    cells = [_markdown_cell(lines, attachments or None)]
    call_lines = [f"call({str(item.get('tool_name', 'capability'))!r}"]
    for key, value, note in inline:
        comment = f"  # {note}" if note else ""
        call_lines.append(f"     {key}={value},{comment}")
    for key, _ in prose:
        call_lines.append(f"     {key}=...,  # see above")
    for key, _ in structured:
        call_lines.append(f"     {key}=...,  # see above")
    call_lines[0] = call_lines[0] + ("," if len(call_lines) > 1 else "")
    call_lines.append(")")
    cells.append(_code_cell(call_lines))
    return cells


def _intro_cells(
    *,
    title: str,
    dataset_path: str,
    shape: tuple[int, int] | None,
    n_clusters: int | None,
    finalized: bool,
    total_steps: int,
) -> list[dict[str, Any]]:
    described = (
        f"{shape[0]:,} cells \u00d7 {shape[1]:,} genes" if shape is not None else "current state"
    )
    if n_clusters is not None:
        described += f", {n_clusters} clusters"
    # An unfinalized analysis is a snapshot, not a conclusion, and must not read like one.
    stage = (
        "The analysis is finalized."
        if finalized
        else "**The analysis is still in progress**, so this is a snapshot of the work committed "
        "so far rather than a finished result. Requesting the notebook again later covers "
        "whatever has been added since."
    )
    lines = [
        f"# {title}",
        "",
        f"**Current object** `{Path(dataset_path).name}` \u2014 {described}",
        "",
        f"This notebook records the {total_steps} capability calls this analysis actually "
        "committed, in the order they ran, with each step's arguments, result, decision "
        "rationale, and figures. " + stage,
        "",
        "**The code cells are a record, not a runnable pipeline.** Each capability executes "
        "inside the scagent-sdk runtime, which enforces scientific floors, stages outputs, and "
        "commits immutable artifacts with provenance. Reproducing the analysis means replaying "
        "those calls through that runtime rather than running these cells.",
        "",
    ]
    # Only advertise deliverables that finalization actually produced.
    if finalized:
        lines.extend(
            [
                "Companion deliverables in this session:",
                "",
                "- `reports/final-analysis-report.md` \u2014 narrative findings and caveats",
                "- `data/final-annotated.h5ad` \u2014 the final annotated object",
                "- `tables/final-labels.csv` \u2014 per-cluster labels and evidence",
                "- `code/analysis-recipe.py` \u2014 the exact call list for replay",
                "",
            ]
        )
    lines.append(
        "Linked figures resolve when this notebook is opened from the session's `reports/` "
        "directory. Inline figures are embedded and need no surrounding files."
    )
    stub = [
        "def call(tool, **arguments):",
        '    """Placeholder standing in for a scagent-sdk capability call.',
        "",
        "    The cells below record what ran. They are not executable science: capabilities need",
        "    the runtime's floors, staging, and artifact commits.",
        '    """',
        "    raise NotImplementedError(",
        '        f"{tool} runs inside the scagent-sdk capability runtime, not standalone. "',
        '        "Replay the recorded calls through the runtime to reproduce this analysis."',
        "    )",
    ]
    return [_markdown_cell(lines), _code_cell(stub)]


def _contents_cell(history: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[list[str]] = []
    previous = None
    for number, item in enumerate(history, start=1):
        phase = _phase(item.get("skill_id"))
        rows.append(
            [
                str(number),
                phase if phase != previous else "",
                f"`{item.get('tool_name', 'capability')}`",
            ]
        )
        previous = phase
    return _markdown_cell(["## Contents", "", *_table(["Step", "Phase", "Capability"], rows)])


def _outcome_cells(
    *, label_rows: list[dict[str, Any]], caveats: list[str]
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    if label_rows:
        ordered = sorted(
            label_rows,
            key=lambda row: (-int(row.get("n_cells") or 0), str(row.get("cluster", ""))),
        )
        rows = [
            [
                str(row.get("cluster", "")),
                str(row.get("cell_type", "")),
                f"{int(row.get('n_cells') or 0):,}",
                str(row.get("confidence", "")),
            ]
            for row in ordered
        ]
        lines = [
            "## Final labels",
            "",
            *_table(["Cluster", "Cell type", "Cells", "Confidence"], rows),
            "",
        ]
        for row in ordered:
            rationale = str(row.get("rationale", "")).strip()
            if not rationale:
                continue
            lines.extend([f"**Cluster {row.get('cluster', '')}**", ""])
            lines.extend(_quote(rationale))
            lines.append("")
        cells.append(_markdown_cell(lines))
    if caveats:
        lines = ["## Caveats and limitations", ""]
        lines.extend(f"- {caveat}" for caveat in caveats)
        cells.append(_markdown_cell(lines))
    return cells


def build_analysis_notebook(
    *,
    title: str,
    dataset_path: str,
    history: list[dict[str, Any]],
    session_dir: Path,
    shape: tuple[int, int] | None = None,
    n_clusters: int | None = None,
    finalized: bool = False,
    label_rows: list[dict[str, Any]] | None = None,
    caveats: list[str] | None = None,
    embed_budget_bytes: int | None = None,
) -> dict[str, Any]:
    """Assemble the notebook document from everything committed so far.

    Rebuilt from scratch on every request rather than appended to: the document is a pure function
    of committed provenance, so regenerating cannot drift, duplicate, or go stale, and a later
    build simply covers the steps that have since been added.
    """

    ordered = list(history)
    steps = _steps_by_execution(ordered)
    cells = _intro_cells(
        title=title,
        dataset_path=dataset_path,
        shape=shape,
        n_clusters=n_clusters,
        finalized=finalized,
        total_steps=len(ordered),
    )
    cells.append(_contents_cell(ordered))

    remaining = _EMBED_TOTAL_BYTES if embed_budget_bytes is None else max(0, embed_budget_bytes)
    seen_phases: set[str] = set()
    previous_phase: str | None = None
    for number, item in enumerate(ordered, start=1):
        phase = _phase(item.get("skill_id"))
        if phase != previous_phase:
            suffix = " (continued)" if phase in seen_phases else ""
            cells.append(_markdown_cell([f"## {phase}{suffix}"]))
            seen_phases.add(phase)
            previous_phase = phase

        figures = _figure_files(item)
        embed_plan, link_plan = _plan_figures(figures, remaining)
        # The record's committed path is <action>--<execution_id>; fall back to the bare id only for
        # legacy records written before descriptive directory naming.
        artifact_dir = item.get("path") or f"artifacts/capabilities/{item.get('execution_id', '')}"
        root = session_dir / artifact_dir
        embedded: list[tuple[str, dict[str, Any], str]] = []
        for raw in embed_plan:
            relative = str(raw["relative_path"])
            name = f"step-{number}-{Path(relative).name}"
            attachment = _attach(root / relative, name)
            if attachment is None:
                link_plan.append(raw)
                continue
            embedded.append((name, attachment, str(raw.get("name", Path(relative).stem))))
            remaining -= int(raw.get("size_bytes") or 0)
        links = [f"../{artifact_dir}/{raw['relative_path']}" for raw in link_plan]
        cells.extend(
            _render_step(
                number=number,
                item=item,
                steps=steps,
                embedded=embedded,
                linked=links,
            )
        )

    cells.extend(_outcome_cells(label_rows=label_rows or [], caveats=caveats or []))
    # nbformat 4.5 requires a unique cell id. Assigning positionally rather than randomly keeps
    # regenerating the same session's notebook a clean diff instead of a whole-file churn.
    for index, cell in enumerate(cells, start=1):
        cell["id"] = f"c{index:04d}"
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
            "scagent_sdk": {"generated_from": "committed session provenance"},
        },
        "nbformat": NOTEBOOK_FORMAT,
        "nbformat_minor": NOTEBOOK_FORMAT_MINOR,
    }
