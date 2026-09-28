"""Live Iris acceptance for the foundation-model-embedding skill, on real csgenetics data.

Three things are exercised against real assets, in the order a session would meet
them, and all three land in one durable session under `sessions/`:

1. **The input check refuses before anything expensive runs.** `check_dataset_for_fm`
   runs each model's validator with no weights and no GPU, on an ordinary PBMC
   count matrix, and reports which models accept it and exactly what the others
   need. This is the wedge argument in one tool call.
2. **The embedding is reported next to its baseline.** `embed_cells_with_fm` runs
   a foundation model and PCA-50 on the same cells, scored with the same metric
   code against CellTypist labels rather than clusters derived from a PCA
   pipeline, and the tool's own summary states both numbers.
3. **A refusal is an answer.** The first file is an untouched csgenetics h5ad --
   counts in X, symbols parked in `var['gene_name']`, var indexed by position --
   and the models say what each of them needs instead of failing later.

The driver is scripted rather than model-driven: it calls the skill's tools
through scagent's own capability executor, so the session, its artifacts, its
lineage and its environment provenance are produced by the real runtime. What it
does not exercise is the agent's judgement about *when* to reach for a foundation
model; that lives in SKILL.md and is a separate level of validation.

    .venv/bin/python scripts/validate_foundation_model_embedding_live.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.execution import EnvironmentBroker, EnvironmentRegistry
from scagent_sdk.session import AnalysisSession

SKILL_ID = "foundation-model-embedding"
#: An ordinary csgenetics file, exactly as it sits on disk: counts in X, gene
#: symbols in var['gene_name'], and var indexed by position. Nothing has been
#: prepared for any model. This is what step 1 checks.
RAW_INPUT = "/data1/peerd/ibrahih3/csgenetics/adata_10x.h5ad"
#: A PBMC 10k v3 subsample labelled by CellTypist Immune_All_Low rather than by
#: clustering, and carrying the columns each model needs. Written by
#: bio_models_inference/tests/reference/probe_hard_task.py.
DEFAULT_INPUT = "/data1/peerd/ibrahih3/bio_models_inference/data/hard_task/query.h5ad"
LABEL_KEY = "cell_type"
MODEL = "scgpt/human"


def _package(packages: list[SkillPackage], skill_id: str) -> SkillPackage:
    for package in packages:
        if package.manifest.skill_id == skill_id:
            return package
    raise SystemExit(
        f"skill {skill_id!r} not found; discovered: "
        f"{[p.manifest.skill_id for p in packages]}"
    )


def _verdicts(envelope: Any) -> dict[str, Any]:
    return {
        "summary": envelope["summary"],
        "verdicts": [
            {k: v for k, v in verdict.items() if k in ("model", "accepted", "check")}
            for verdict in envelope["details"]["verdicts"]
        ],
    }


def _tool(package: SkillPackage, name: str):
    for tool in package.manifest.tools:
        if tool.name == name:
            return tool
    raise SystemExit(f"tool {name!r} not in {package.manifest.skill_id}")


async def _execute(executor, package, tool_name, arguments) -> tuple[bool, Any]:
    response = await executor.execute(package, _tool(package, tool_name), arguments)
    if response.get("is_error"):
        return False, str(response["content"][0]["text"])
    envelope = response["structuredContent"]
    executor.commit(str(envelope["scagent_execution_id"]))
    return True, envelope


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    packages = CapabilityRegistry(root / ".claude" / "skills").discover()
    package = _package(packages, SKILL_ID)
    broker = EnvironmentBroker(
        EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
    )
    source = Path(args.input).expanduser()
    if not source.exists():
        source = Path(FALLBACK_INPUT)
        print(f"note: {args.input} absent, falling back to {source} (no independent labels)")
    source = source.resolve()

    raw = Path(args.raw_input).expanduser().resolve()
    session = AnalysisSession.create(
        args.sessions_root.resolve(),
        title="Foundation model embedding vs PCA-50 baseline, with the input check first",
    )
    executor = CapabilityExecutor(session, environment_broker=broker)
    results: dict[str, Any] = {
        "session": str(session.directory),
        "raw_input": str(raw),
        "input": str(source),
    }

    print("== 1. check_dataset_for_fm on an untouched csgenetics file")
    ok, check = await _execute(
        executor, package, "check_dataset_for_fm", {"path": str(raw), "model": "all"}
    )
    if not ok:
        print("  FAILED:", str(check)[:1500])
        results["raw_input_check"] = {"error": check}
        return results
    print("  ", check["summary"])
    results["raw_input_check"] = _verdicts(check)

    print(f"== 2. embed_cells_with_fm: {MODEL} against the PCA-50 baseline")
    ok, embedding = await _execute(
        executor,
        package,
        "embed_cells_with_fm",
        {
            # This file is not the one step 1 looked at, and only a tool that
            # transforms the matrix may adopt a new analysis root.
            "adopt_untracked": True,
            "path": str(source),
            "model": MODEL,
            "label_key": args.label_key,
            "max_cells": args.max_cells,
            "batch_size": args.batch_size,
        },
    )
    if not ok:
        print("  FAILED:", str(embedding)[:1500])
        results["embedding"] = {"error": embedding}
        return results
    print("  ", embedding["summary"])
    details = embedding["details"]
    results["embedding"] = {
        "summary": embedding["summary"],
        "model": details["model"],
        "embedding_dim": details["embedding_dim"],
        "pooling": details["pooling"],
        "label_key": details["label_key"],
        "subsampling": details["subsampling"],
        "metrics_model": details["metrics_model"],
        "metrics_baseline": details["metrics_baseline"],
        "comparison": details["comparison"],
        "provenance": details["provenance"],
    }

    print("== 3. check_dataset_for_fm on the session's own matrix, with no path")
    ok, check = await _execute(
        executor, package, "check_dataset_for_fm", {"model": "all"}
    )
    if not ok:
        print("  FAILED:", str(check)[:1500])
        results["input_check"] = {"error": check}
        return results
    print("  ", check["summary"])
    results["input_check"] = _verdicts(check)

    report = session.directory / "foundation-model-embedding-validation.json"
    report.write_text(json.dumps(results, indent=2, sort_keys=True, default=str) + "\n")
    print("\nwrote", report)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--raw-input", dest="raw_input", default=RAW_INPUT)
    parser.add_argument("--label-key", dest="label_key", default=LABEL_KEY)
    parser.add_argument("--max-cells", dest="max_cells", type=int, default=1500)
    parser.add_argument("--batch-size", dest="batch_size", type=int, default=16)
    parser.add_argument(
        "--sessions-root",
        dest="sessions_root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "sessions",
    )
    args = parser.parse_args()
    results = asyncio.run(_run(args))
    comparison = results.get("embedding", {}).get("comparison") or {}
    print("\n--- model vs baseline ---")
    for name, row in comparison.items():
        if isinstance(row, dict) and isinstance(row.get("model"), (int, float)):
            print(f"{name:>16}: model {row['model']:.4f} | baseline {row['baseline']:.4f}")


if __name__ == "__main__":
    main()
