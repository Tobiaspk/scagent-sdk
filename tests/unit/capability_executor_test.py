from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.session import AnalysisSession


def test_capability_result_is_staged_then_committed_by_hook(tmp_path: Path) -> None:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        item
        for item in CapabilityRegistry(skills_root).discover()
        if item.manifest.skill_id == "inspect-dataset"
    )
    tool = package.manifest.tools[0]
    dataset = tmp_path / "sample.h5ad"
    dataset.write_bytes(b"\x89HDF\r\n\x1a\n" + b"data")
    session = AnalysisSession.create(tmp_path / "sessions", title="capability")
    executor = CapabilityExecutor(session)

    response = asyncio.run(
        executor.execute(package, tool, {"path": str(dataset), "hash_mode": "full"})
    )

    envelope = response["structuredContent"]
    execution_id = envelope["scagent_execution_id"]
    assert envelope["status"] == "validated"
    assert envelope["state_commit"] == "PostToolUse"
    assert envelope["artifact_relative_path"] == f"artifacts/capabilities/{execution_id}"
    assert envelope["artifact_path"] == str(
        (session.directory / envelope["artifact_relative_path"]).resolve()
    )
    assert envelope["files"][0]["path"] == str(
        Path(envelope["artifact_path"]) / envelope["files"][0]["relative_path"]
    )
    assert session.store.state.facts == {}
    assert (executor.pending_root / execution_id / "inspection.json").is_file()

    assert executor.commit_from_hook({"tool_response": response}) is True
    assert session.store.state.facts["dataset"]["format"]["byte_signature"] == "hdf5"
    artifact = session.store.state.artifacts[execution_id]
    assert artifact["skill_id"] == "inspect-dataset"
    assert artifact["arguments"] == {"path": str(dataset), "hash_mode": "full"}
    assert (session.directory / artifact["path"] / "inspection.json").is_file()
    kinds = [event.kind for event in session.store.events()]
    assert kinds[-2:] == ["capability.result_staged", "capability.result_committed"]


def test_session_relative_artifact_path_is_resolved_for_the_next_tool(
    tmp_path: Path,
) -> None:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    packages = {
        item.manifest.skill_id: item for item in CapabilityRegistry(skills_root).discover()
    }
    inspect_package = packages["inspect-dataset"]
    inspect_tool = inspect_package.manifest.tools[0]
    workspace_package = packages["analysis-workspace"]
    read_tool = next(
        tool for tool in workspace_package.manifest.tools if tool.name == "read_text_file"
    )
    dataset = tmp_path / "sample.h5ad"
    dataset.write_bytes(b"\x89HDF\r\n\x1a\n" + b"data")
    session = AnalysisSession.create(tmp_path / "sessions", title="artifact handoff")
    executor = CapabilityExecutor(session)

    produced = asyncio.run(
        executor.execute(
            inspect_package,
            inspect_tool,
            {"path": str(dataset), "hash_mode": "full"},
        )
    )
    assert executor.commit_from_hook({"tool_response": produced}) is True
    portable_path = produced["structuredContent"]["files"][0]["session_relative_path"]

    consumed = asyncio.run(
        executor.execute(workspace_package, read_tool, {"path": portable_path})
    )

    assert consumed.get("is_error") is not True
    details = consumed["structuredContent"]["details"]
    assert details["path"] == produced["structuredContent"]["files"][0]["path"]


def test_large_details_spill_and_finalized_uncommitted_result_recovers(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skills" / "large-result"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: large-result\ndescription: test large results\n---\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(
        """\
schema_version: 1
skill: {id: large-result, version: "1", description: test}
tools:
  - name: produce_large
    description: produce large details
    entrypoint: scripts/run.py:run
    input_schema: {type: object}
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        "def run(arguments, context):\n"
        # Fact roots are registered in scagent_sdk.state.lineage and fail closed, so a fixture
        # cannot invent one; dataset_contents is session-scoped and adequate here.
        "    return {'summary': 'large', 'details': {'value': 'x' * 50000}, "
        "'facts_patch': {'dataset_contents': {'large': True}}}\n",
        encoding="utf-8",
    )
    package = CapabilityRegistry(skill.parent).discover()[0]
    session = AnalysisSession.create(tmp_path / "sessions", title="spill")
    executor = CapabilityExecutor(session)

    response = asyncio.run(executor.execute(package, package.manifest.tools[0], {}))
    execution_id = response["structuredContent"]["scagent_execution_id"]
    assert response["structuredContent"]["details"] is None
    assert (executor.pending_root / execution_id / "details.json").is_file()

    pending = executor.pending_root / execution_id
    final = executor.artifact_root / execution_id
    final.parent.mkdir(parents=True, exist_ok=True)
    pending.rename(final)
    assert executor.recover_pending() == [execution_id]
    assert session.store.state.facts["dataset_contents"]["large"] is True
    assert json.loads((final / "details.json").read_text())["value"].startswith("x")


def test_unresolved_environment_fails_closed(tmp_path: Path) -> None:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        item
        for item in CapabilityRegistry(skills_root).discover()
        if item.manifest.skill_id == "inspect-dataset"
    )
    tool = replace(package.manifest.tools[0], environment="gpu-singlecell")
    session = AnalysisSession.create(tmp_path / "sessions", title="environment")

    response = asyncio.run(CapabilityExecutor(session).execute(package, tool, {}))

    assert response["is_error"] is True
    assert "environment broker" in response["content"][0]["text"]
    assert session.store.events()[-1].kind == "capability.execution_failed"


def test_executor_enforces_scientific_floors_without_sdk_hook(tmp_path: Path) -> None:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        item
        for item in CapabilityRegistry(skills_root).discover()
        if item.manifest.skill_id == "finalize-analysis"
    )
    session = AnalysisSession.create(tmp_path / "sessions", title="defense in depth")

    response = asyncio.run(
        CapabilityExecutor(session).execute(
            package,
            package.manifest.tools[0],
            {"path": str(tmp_path / "unmaterialized.h5ad")},
        )
    )

    assert response["is_error"] is True
    # Full detail stays in the model-facing content...
    assert "scientific floor denied execution" in response["content"][0]["text"]
    # ...but the terminal gets a single concise line.
    summary = response["error_summary"]
    assert summary.startswith("finalize_analysis failed:")
    assert "\n" not in summary


def test_store_directory_artifact_is_sized_by_tree_and_committed_intact(tmp_path: Path) -> None:
    # A .zarr store is one artifact that is a directory, not a single file. The executor must
    # accept it, record its size as the sum of member files, and commit the whole tree intact.
    skill = tmp_path / "skills" / "storer"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: storer\ndescription: writes a store\n---\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(
        """\
schema_version: 1
skill: {id: storer, version: "1", description: test}
tools:
  - name: write_store
    description: write a zarr-like store directory
    entrypoint: scripts/run.py:run
    input_schema: {type: object}
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        "def run(arguments, context):\n"
        "    store = context.staging_dir / 'matrix.zarr'\n"
        "    (store / 'X').mkdir(parents=True)\n"
        "    (store / '.zgroup').write_bytes(b'0123456789')\n"      # 10 bytes
        "    (store / 'X' / '0.0').write_bytes(b'x' * 100)\n"       # 100 bytes
        "    return {'summary': 'wrote a store',\n"
        "            'details': {}, 'facts_patch': {},\n"
        "            'artifacts': [{'name': 'matrix',\n"
        "                           'relative_path': 'matrix.zarr',\n"
        "                           'media_type': 'application/octet-stream'}]}\n",
        encoding="utf-8",
    )
    package = CapabilityRegistry(skill.parent).discover()[0]
    session = AnalysisSession.create(tmp_path / "sessions", title="store")
    executor = CapabilityExecutor(session)

    response = asyncio.run(executor.execute(package, package.manifest.tools[0], {}))
    envelope = response["structuredContent"]
    assert envelope["status"] == "validated"
    produced = envelope["files"][0]
    assert produced["relative_path"] == "matrix.zarr"
    assert produced["size_bytes"] == 110  # summed over the tree, not a single stat()

    assert executor.commit_from_hook({"tool_response": response}) is True
    artifact = session.store.state.artifacts[envelope["scagent_execution_id"]]
    store = session.directory / artifact["path"] / "matrix.zarr"
    assert (store / ".zgroup").read_bytes() == b"0123456789"
    assert (store / "X" / "0.0").stat().st_size == 100


def test_committed_python_code_is_mirrored_into_code_dir(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "coder"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: coder\ndescription: writes code\n---\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(
        """\
schema_version: 1
skill: {id: coder, version: "1", description: test}
tools:
  - name: write_code
    description: write a python file
    entrypoint: scripts/run.py:run
    input_schema: {type: object}
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        "def run(arguments, context):\n"
        "    (context.staging_dir / 'analysis.py').write_text('print(1)\\n')\n"
        "    summary = 'Custom analysis code completed for: Inspect the Reyfman dataset'\n"
        "    return {'summary': summary,\n"
        "            'details': {}, 'facts_patch': {},\n"
        "            'artifacts': [{'name': 'analysis-code',\n"
        "                           'relative_path': 'analysis.py',\n"
        "                           'media_type': 'text/x-python'}]}\n",
        encoding="utf-8",
    )
    package = CapabilityRegistry(skill.parent).discover()[0]
    session = AnalysisSession.create(tmp_path / "sessions", title="mirror")
    executor = CapabilityExecutor(session)

    response = asyncio.run(executor.execute(package, package.manifest.tools[0], {}))
    assert executor.commit_from_hook({"tool_response": response}) is True

    mirrored = sorted((session.directory / "code").glob("*.py"))
    assert len(mirrored) == 1
    # The filename describes what the run did rather than being an opaque id.
    assert mirrored[0].name.startswith("inspect-the-reyfman-dataset-")
    assert mirrored[0].is_symlink()
    assert mirrored[0].read_text() == "print(1)\n"
