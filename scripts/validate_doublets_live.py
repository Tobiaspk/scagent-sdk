"""Run a bounded inspect/prepare/doublet-evidence/review acceptance session on Iris."""

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
    session = (
        AnalysisSession.resume(sessions_root, args.resume_session)
        if args.resume_session
        else AnalysisSession.create(sessions_root, title=args.title)
    )
    packages = CapabilityRegistry(root / ".claude" / "skills").discover()
    inspector = _package(packages, "inspect-dataset")
    doublets = _package(packages, "doublet-evidence")
    executor = CapabilityExecutor(
        session,
        environment_broker=EnvironmentBroker(
            EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
        ),
    )
    source = args.input.expanduser().resolve()
    runs: list[dict[str, Any]] = []
    if args.resume_session:
        analysis = session.store.state.facts.get("analysis", {})
        revision = analysis.get("dataset_revision", {}) if isinstance(analysis, dict) else {}
        recorded = revision.get("prepared_path") if isinstance(revision, dict) else None
        if not isinstance(recorded, str):
            raise RuntimeError("resumed session has no prepared_path")
        prepared_path = Path(recorded)
        if not prepared_path.is_absolute():
            prepared_path = session.directory / prepared_path
    else:
        runs.append(
            await _execute(
                executor,
                inspector,
                "inspect_dataset",
                {"path": str(source), "hash_mode": "full"},
            )
        )
        prepared, prepared_path = await build_standard_fixture(
            execute=_execute,
            executor=executor,
            packages=packages,
            session_dir=session.directory,
            source=source,
            random_seed=args.random_seed,
        )
        runs.append(prepared)
    evidence = await _execute(
        executor,
        doublets,
        "evaluate_doublet_evidence",
        {
            "path": str(prepared_path),
            "batch_key": args.batch_key,
            "confirm_unstratified": args.batch_key is None,
            "random_seed": args.random_seed,
        },
    )
    runs.append(evidence)
    annotated_path = session.directory / str(evidence["artifact_path"]) / "doublet-annotated.h5ad"
    review = await _execute(
        executor,
        doublets,
        "review_doublet_evidence",
        {
            "path": str(annotated_path),
            "decision": "retain_for_cluster_review",
            "rationale": (
                "Bounded capability acceptance: retain calls for cluster, marker, library, and "
                "QC-context review; do not filter cells during validation."
            ),
        },
    )
    runs.append(review)
    state = session.store.state
    return {
        "scientific_session_id": session.session_id,
        "session_directory": str(session.directory),
        "prepared_path": str(prepared_path),
        "annotated_path": str(annotated_path),
        "state_revision": state.revision,
        "doublets": state.facts.get("doublets"),
        "analysis": state.facts.get("analysis"),
        "run_summaries": [run["summary"] for run in runs],
        "run_artifacts": [run["artifact_path"] for run in runs],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--sessions-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--title", default="Doublet evidence live acceptance")
    parser.add_argument("--resume-session")
    parser.add_argument("--batch-key")
    parser.add_argument("--random-seed", type=int, default=0)
    return parser


def main() -> int:
    print(json.dumps(asyncio.run(_run(_parser().parse_args())), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
