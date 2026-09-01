"""Report which CellTypist classifiers are cached on this host.

CellTypist cannot download models implicitly here, so model availability — and specifically
*which* references are cached — decides whether the skill can run and which model is defensible
for a given tissue. Standard library only: this runs in the control plane at session assembly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MAX_LISTED_MODELS = 80


def models_directory(environment: dict[str, str]) -> Path:
    """Resolve the same models directory `celltypist.models` will compute at run time."""

    folder = environment.get("CELLTYPIST_FOLDER") or os.environ.get("CELLTYPIST_FOLDER")
    if folder:
        root = Path(folder).expanduser()
    else:
        home = environment.get("HOME") or os.environ.get("HOME") or "~"
        root = Path(home).expanduser() / ".celltypist"
    return root / "data" / "models"


def probe(environment: dict[str, str] | None = None) -> dict[str, Any]:
    directory = models_directory(environment or {})
    if not directory.is_dir():
        return {
            "status": "unavailable",
            "summary": f"no CellTypist model cache at {directory}",
            "details": ["Models must be cached locally first; downloads are never implicit."],
        }
    models = sorted(item.name for item in directory.glob("*.pkl") if item.is_file())
    if not models:
        return {
            "status": "unavailable",
            "summary": f"CellTypist model cache is empty: {directory}",
            "details": ["Models must be cached locally first; downloads are never implicit."],
        }
    listed = models[:MAX_LISTED_MODELS]
    details = [f"models directory: {directory}", f"cached models: {', '.join(listed)}"]
    if len(models) > len(listed):
        details.append(f"{len(models) - len(listed)} further cached models not listed")
    details.append(
        "Pass the closest organism/tissue classifier explicitly as `model`; there is no generic "
        "default. Uncached models cannot be downloaded."
    )
    return {
        "status": "ready",
        "summary": f"{len(models)} cached classifiers available",
        "details": details,
    }
