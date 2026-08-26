"""Auditable custom Python for trusted in-process Iris execution."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Any


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import anndata as ad
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scanpy as sc

    code = str(arguments["code"])
    purpose = str(arguments["purpose"]).strip()
    if not purpose:
        raise ValueError("purpose must not be empty")
    compiled = compile(code, "analysis.py", "exec")
    script_path = context.staging_dir / "analysis.py"
    script_path.write_text(code.rstrip() + "\n", encoding="utf-8")
    registered: list[dict[str, str]] = []

    def register_artifact(
        path: str | Path, name: str, media_type: str = "application/octet-stream"
    ) -> None:
        target = Path(path).resolve()
        target.relative_to(context.staging_dir.resolve())
        if not target.is_file():
            raise FileNotFoundError(target)
        registered.append(
            {
                "name": str(name),
                "relative_path": str(target.relative_to(context.staging_dir)),
                "media_type": str(media_type),
            }
        )

    namespace = {
        "np": np,
        "pd": pd,
        "sc": sc,
        "ad": ad,
        "plt": plt,
        "Path": Path,
        "workspace": context.staging_dir,
        "session_dir": context.session_dir,
        "register_artifact": register_artifact,
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exec(compiled, namespace, namespace)
    (context.staging_dir / "analysis.stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
    (context.staging_dir / "analysis.stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
    artifacts = [
        {"name": "analysis-code", "relative_path": "analysis.py", "media_type": "text/x-python"},
        {
            "name": "analysis-stdout",
            "relative_path": "analysis.stdout.txt",
            "media_type": "text/plain",
        },
        {
            "name": "analysis-stderr",
            "relative_path": "analysis.stderr.txt",
            "media_type": "text/plain",
        },
        *registered,
    ]
    return {
        "summary": f"Custom analysis code completed for: {purpose}",
        "details": {
            "purpose": purpose,
            "stdout": stdout.getvalue()[-20000:],
            "registered_artifacts": registered,
        },
        "facts_patch": {
            "custom_analysis": {
                context.execution_id: {
                    "purpose": purpose,
                    "status": "complete",
                    "code_path": f"{context.artifact_relative_path}/analysis.py",
                }
            }
        },
        "artifacts": artifacts,
    }
