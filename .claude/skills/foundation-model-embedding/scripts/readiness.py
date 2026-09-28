"""Report which foundation models this host can actually run, and on what.

Three things are independent and all three decide whether a tool call will work:
the `biorun` package must import, each model's optional extra must be installed,
and its weights must already be in the local cache -- nothing downloads
implicitly. Reporting them here means the agent learns what is available at
session assembly instead of discovering it from a failed twenty-minute job.

Standard library only: this runs in the control plane.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

PROBE = r"""
import json
from biorun.registry import list_cards

rows = []
for card in list_cards():
    rows.append(
        {
            "id": card.id,
            "tasks": list(card.tasks),
            "availability": card.availability(),
            "licence": (card.weights_license or "unrecorded"),
            "input": card.input_expectation or "",
        }
    )
print(json.dumps(rows))
"""


def _python(environment: dict[str, str] | None) -> str | None:
    resolved = environment or {}
    return resolved.get("BIORUN_PYTHON") or os.environ.get("BIORUN_PYTHON")


def probe(environment: dict[str, str] | None = None) -> dict[str, Any]:
    interpreter = _python(environment)
    if not interpreter:
        return {
            "status": "unavailable",
            "summary": "BIORUN_PYTHON is not configured for this host",
            "details": [
                "Set BIORUN_PYTHON in the biorun capability's environment block to the "
                "interpreter that has `biorun` installed.",
            ],
        }
    try:
        completed = subprocess.run(
            [interpreter, "-c", PROBE],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "unavailable",
            "summary": f"could not run {interpreter}",
            "details": [str(exc)],
        }
    if completed.returncode != 0:
        return {
            "status": "unavailable",
            "summary": "biorun does not import in the configured interpreter",
            "details": [completed.stderr.strip()[-800:]],
        }

    cards = json.loads(completed.stdout.strip().splitlines()[-1])
    available = [c for c in cards if c["availability"] == "available"]
    blocked = [c for c in cards if c["availability"] != "available"]
    cell_models = [c for c in available if "embed" in c["tasks"] and c["id"] != "baseline-only"]

    details = [f"{c['id']}: {c['availability']}; {c['licence']}" for c in cards]
    details.append(
        "Input expectations differ per model and are enforced, not coerced: run "
        "check_dataset_for_fm before embedding."
    )
    details.append(
        "Weights are never downloaded implicitly. A model listed as unavailable names the "
        "command that would fetch it."
    )
    if not cell_models:
        status, summary = "partial", "biorun imports, but no cell model is runnable here"
    elif blocked:
        status = "partial"
        summary = (
            f"{len(cell_models)} cell models runnable; {len(blocked)} unavailable "
            f"({', '.join(c['id'] for c in blocked[:3])})"
        )
    else:
        status = "ready"
        summary = f"{len(cell_models)} cell models runnable with a PCA-50 baseline"
    return {"status": status, "summary": summary, "details": details}
