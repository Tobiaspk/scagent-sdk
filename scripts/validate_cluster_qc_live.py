"""Run a bounded inspect/prepare/cluster-QC acceptance session on Iris.

Exercises the restored three-axis cluster QC (metric + DEG-identity + covariance/coherence,
technical Moran's I, correlation heatmaps, bounded convergent cleanup) end to end through the
real environment broker and two-phase commit.
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
    return next(package for package in packages if package.manifest.skill_id == skill_id)


def _tool(package: SkillPackage, name: str) -> Any:
    return next(tool for tool in package.manifest.tools if tool.name == name)


async def _execute(
    executor: CapabilityExecutor,
    package: SkillPackage,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = await executor.execute(package, _tool(package, tool_name), arguments)
    if response.get("is_error"):
        raise RuntimeError(str(response["content"][0]["text"]))
    envelope = response["structuredContent"]
    executor.commit(str(envelope["scagent_execution_id"]))
    return envelope


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    sessions_root = args.sessions_root.resolve()
    session = AnalysisSession.create(sessions_root, title=args.title)
    packages = CapabilityRegistry(root / ".claude" / "skills").discover()
    inspector = _package(packages, "inspect-dataset")
    cluster_qc = _package(packages, "cluster-qc")
    executor = CapabilityExecutor(
        session,
        environment_broker=EnvironmentBroker(
            EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
        ),
    )
    source = args.input.expanduser().resolve()

    await _execute(
        executor, inspector, "inspect_dataset", {"path": str(source), "hash_mode": "full"}
    )
    prepared, prepared_path = await build_standard_fixture(
        execute=_execute,
        executor=executor,
        packages=packages,
        session_dir=session.directory,
        source=source,
        random_seed=args.random_seed,
    )

    qc = await _execute(
        executor,
        cluster_qc,
        "evaluate_cluster_qc",
        {
            "path": str(prepared_path),
            "auto_remove_convergent": not args.no_remove,
            "random_seed": args.random_seed,
        },
    )
    details = qc["structuredContent"] if "structuredContent" in qc else qc
    state = session.store.state
    return {
        "scientific_session_id": session.session_id,
        "session_directory": str(session.directory),
        "prepared_path": str(prepared_path),
        "fixture_summary": prepared["summary"],
        "cluster_qc_summary": qc["summary"],
        "cluster_qc_details": details.get("details") if isinstance(details, dict) else None,
        "cluster_qc_artifacts": qc.get("artifact_path"),
        "cluster_qc_fact": state.facts.get("cluster_qc"),
        "analysis_after": state.facts.get("analysis"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--sessions-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--title", default="Cluster QC live acceptance")
    parser.add_argument("--no-remove", action="store_true", help="Report-only; remove nothing.")
    parser.add_argument("--random-seed", type=int, default=0)
    return parser


def main() -> int:
    print(
        json.dumps(asyncio.run(_run(_parser().parse_args())), indent=2, sort_keys=True, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
