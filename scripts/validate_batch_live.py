"""Bounded inspect/prepare/gene-first-batch-evidence/decision acceptance on Iris.

Drives the real broker through inspect -> prepare -> investigate_batch (gene-first evidence, no
decision) -> decide_batch_handling (identity-bound decision against the current evidence id).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from live_fixture_pipeline import build_standard_fixture

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.execution import EnvironmentBroker, EnvironmentRegistry
from scagent_sdk.session import AnalysisSession


def _package(packages: tuple[SkillPackage, ...], skill_id: str) -> SkillPackage:
    return next(p for p in packages if p.manifest.skill_id == skill_id)


def _tool(package: SkillPackage, name: str) -> Any:
    return next(tool for tool in package.manifest.tools if tool.name == name)


async def _execute(
    executor: Any, package: SkillPackage, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = await executor.execute(package, _tool(package, tool_name), arguments)
    if response.get("is_error"):
        raise RuntimeError(str(response["content"][0]["text"]))
    envelope = response["structuredContent"]
    executor.commit(str(envelope["scagent_execution_id"]))
    return envelope


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    session = AnalysisSession.create(args.sessions_root.resolve(), title=args.title)
    packages = CapabilityRegistry(root / ".claude" / "skills").discover()
    executor = CapabilityExecutor(
        session,
        environment_broker=EnvironmentBroker(
            EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
        ),
    )
    source = args.input.expanduser().resolve()

    await _execute(
        executor,
        _package(packages, "inspect-dataset"),
        "inspect_dataset",
        {"path": str(source), "hash_mode": "full"},
    )
    prepared, prepared_path = await build_standard_fixture(
        execute=_execute,
        executor=executor,
        packages=packages,
        session_dir=session.directory,
        source=source,
        random_seed=args.random_seed,
    )

    batch = _package(packages, "batch-investigation")
    evidence = await _execute(
        executor,
        batch,
        "investigate_batch",
        {
            "path": str(prepared_path),
            "batch_key": args.batch_key,
            "max_regions": args.max_regions,
            "random_seed": args.random_seed,
        },
    )
    evidence_id = evidence["details"]["evidence_id"]
    decision = await _execute(
        executor,
        batch,
        "decide_batch_handling",
        {
            "evidence_id": evidence_id,
            "decision": args.decision,
            "rationale": "Bounded acceptance: record the conservative decision here.",
        },
    )
    state = session.store.state
    return {
        "fixture_summary": prepared["summary"],
        "evidence_summary": evidence["summary"],
        "evidence": {
            k: evidence["details"].get(k)
            for k in (
                "gene_evidence",
                "design_interpretation",
                "recommendation",
                "n_enriched_regions",
                "n_supported_matches",
                "n_recurring_programs",
                "de_engine",
                "evidence_id",
            )
        },
        "decision_summary": decision["summary"],
        "batch_fact_keys": sorted((state.facts.get("batch") or {}).keys()),
        "recorded_decision": (state.facts.get("batch") or {}).get("decision", {}).get("decision"),
        "decision_matches_evidence": (state.facts.get("batch") or {})
        .get("decision", {})
        .get("evidence_id")
        == evidence_id,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--sessions-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--title", default="Gene-first batch live acceptance")
    parser.add_argument("--organism", choices=("human", "mouse"), default="human")
    parser.add_argument("--batch-key", default="sample")
    parser.add_argument("--decision", default="keep_uncorrected")
    parser.add_argument("--max-regions", type=int, default=20)
    parser.add_argument("--random-seed", type=int, default=0)
    return parser


def main() -> int:
    print(
        json.dumps(asyncio.run(_run(_parser().parse_args())), indent=2, sort_keys=True, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
