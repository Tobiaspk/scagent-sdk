from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.output_view import OUTPUT_DIRECTORIES, refresh_output_view
from scagent_sdk.session import AnalysisSession


def _commit_artifact(
    session: AnalysisSession,
    *,
    execution_id: str,
    tool_name: str,
    summary: str,
    files: list[tuple[str, str, str, bytes | dict[str, bytes]]],
) -> Path:
    artifact_path = session.directory / "artifacts" / "capabilities" / execution_id
    artifact_path.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    for name, relative_path, media_type, content in files:
        path = artifact_path / relative_path
        if isinstance(content, dict):
            path.mkdir(parents=True, exist_ok=True)
            for member, value in content.items():
                member_path = path / member
                member_path.parent.mkdir(parents=True, exist_ok=True)
                member_path.write_bytes(value)
            size_bytes = sum(map(len, content.values()))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            size_bytes = len(content)
        records.append(
            {
                "name": name,
                "relative_path": relative_path,
                "media_type": media_type,
                "size_bytes": size_bytes,
            }
        )
    artifact_record = {
        "kind": "capability-result",
        "skill_id": "test-skill",
        "skill_version": "1",
        "skill_fingerprint": "sha256:test",
        "tool_name": tool_name,
        "environment": {"name": "current"},
        "input_state_revision": session.store.state.revision,
        "summary": summary,
        "path": f"artifacts/capabilities/{execution_id}",
        "files": records,
        "model_media": [],
    }
    session.store.record(
        "capability.result_committed",
        payload={"execution_id": execution_id, **artifact_record},
        state_patch={"artifacts": {execution_id: artifact_record}},
    )
    session.refresh_outputs()
    return artifact_path


def _shell_files(
    command: str,
    *,
    exit_code: int = 0,
    duration: float = 1.5,
    stdout: bytes = b"stdout text\n",
    stderr: bytes = b"",
    report: bytes | None = None,
) -> list[tuple[str, str, str, bytes]]:
    """The exact artifact shape ``run_shell_command`` commits."""

    if report is None:
        report = (
            json.dumps(
                {
                    "command": command,
                    "cwd": "/work",
                    "timeout_seconds": 600,
                    "exit_code": exit_code,
                    "duration_seconds": duration,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
    return [
        (
            "shell-command",
            "shell-command.sh",
            "text/x-shellscript",
            b"#!/usr/bin/env bash\nset -o pipefail\n" + command.encode() + b"\n",
        ),
        ("shell-stdout", "shell.stdout.txt", "text/plain", stdout),
        ("shell-stderr", "shell.stderr.txt", "text/plain", stderr),
        ("shell-run", "shell-run.json", "application/json", report),
    ]


def test_new_session_has_empty_browsable_output_surface(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="new")

    for directory in OUTPUT_DIRECTORIES:
        assert (session.directory / directory).is_dir()
    assert (session.directory / "data" / "intermediates").is_dir()
    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["generator"] == "scagent-sdk"
    assert all(not entries for entries in index["categories"].values())
    assert "# Session outputs" in (session.directory / "outputs.md").read_text()


def test_committed_files_are_projected_by_kind_without_copying_data(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="projection")
    execution_id = "aaaaaaaa-1111-2222-3333-444444444444"
    artifact_path = _commit_artifact(
        session,
        execution_id=execution_id,
        tool_name="finalize_analysis",
        summary="Custom analysis code completed for: Inspect the dataset",
        files=[
            ("analysis-code", "analysis.py", "text/x-python", b"print('ok')\n"),
            (
                "analysis-recipe",
                "analysis-recipe.py",
                "text/x-python",
                b"CAPABILITY_CALLS = []\n",
            ),
            ("umap", "embedding.png", "image/png", b"PNG"),
            ("analysis-report", "analysis-report.md", "text/markdown", b"# Report\n"),
            ("run-facts", "run.json", "application/json", b"{}\n"),
            ("final-labels", "final-labels.csv", "text/csv", b"cluster,label\n0,T\n"),
            ("final-anndata", "final-annotated.h5ad", "application/x-hdf5", b"H5AD"),
            ("analysis-stdout", "analysis.stdout.txt", "text/plain", b"not projected\n"),
        ],
    )

    # Two code files in one record: the summary-derived label is the same for both, so the
    # artifact name disambiguates them. Neither may fall through to the --view-N collision
    # guard, which exists for cross-run reuse rather than for naming.
    code_files = sorted(
        path.name for path in (session.directory / "code").glob("inspect-the-dataset-*.py")
    )
    assert code_files == [
        "inspect-the-dataset-analysis-code-aaaaaaaa.py",
        "inspect-the-dataset-analysis-recipe-aaaaaaaa.py",
    ]
    code = session.directory / "code" / code_files[0]
    figure = next((session.directory / "figures").glob("*.png"))
    reports = sorted((session.directory / "reports").glob("*--aaaaaaaa.*"))
    table = next((session.directory / "tables").glob("*--aaaaaaaa.csv"))
    data = next((session.directory / "data").glob("*--aaaaaaaa.h5ad"))
    assert code.is_symlink()
    assert figure.is_symlink()
    assert len(reports) == 1
    assert all(path.is_symlink() for path in reports)
    assert table.is_symlink()
    assert data.is_symlink()
    assert data.resolve() == artifact_path / "final-annotated.h5ad"
    assert data.stat().st_ino == (artifact_path / "final-annotated.h5ad").stat().st_ino
    assert not list((session.directory / "reports").glob("*stdout*"))

    assert (session.directory / "data" / "final-annotated.h5ad").resolve() == data.resolve()
    assert (
        session.directory / "reports" / "final-analysis-report.md"
    ).resolve() == artifact_path / "analysis-report.md"
    assert (
        session.directory / "tables" / "final-labels.csv"
    ).resolve() == artifact_path / "final-labels.csv"
    assert (
        session.directory / "code" / "analysis-recipe.py"
    ).resolve() == artifact_path / "analysis-recipe.py"

    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["authoritative_root"] == "artifacts/capabilities"
    assert len(index["aliases"]) == 4
    assert index["categories"]["figures"][0]["canonical_path"].endswith("/embedding.png")
    markdown = (session.directory / "outputs.md").read_text()
    assert "## Primary results" in markdown
    assert "final-annotated.h5ad" in markdown
    assert "`finalize_analysis`" in markdown


def test_single_code_artifact_keeps_the_plain_summary_label(tmp_path: Path) -> None:
    """The production shape: one code file per execution, named for what it does."""

    session = AnalysisSession.create(tmp_path / "sessions", title="one-code-file")
    _commit_artifact(
        session,
        execution_id="cccccccc-1111-2222-3333-444444444444",
        tool_name="run_analysis_code",
        summary="Custom analysis code completed for: Inspect the dataset",
        files=[("analysis-code", "analysis.py", "text/x-python", b"print('ok')\n")],
    )

    assert [path.name for path in (session.directory / "code").glob("*.py")] == [
        "inspect-the-dataset-cccccccc.py"
    ]


def test_multiple_code_artifacts_never_use_the_collision_guard(tmp_path: Path) -> None:
    """Two code files in one record must get distinct names, not name plus --view-2.

    The code label is derived from the record summary, so it is identical for every code file in
    an execution. Without disambiguation the second one lands on the --view-N fallback, which is
    reserved for reusing a name across runs and makes the pairing arbitrary.
    """

    session = AnalysisSession.create(tmp_path / "sessions", title="two-code-files")
    artifact_path = _commit_artifact(
        session,
        execution_id="dddddddd-1111-2222-3333-444444444444",
        tool_name="finalize_analysis",
        summary="Custom analysis code completed for: Inspect the dataset",
        files=[
            ("analysis-code", "analysis.py", "text/x-python", b"print('a')\n"),
            ("analysis-recipe", "analysis-recipe.py", "text/x-python", b"print('b')\n"),
        ],
    )

    projected = sorted(
        path.name for path in (session.directory / "code").glob("inspect-the-dataset-*.py")
    )
    assert projected == [
        "inspect-the-dataset-analysis-code-dddddddd.py",
        "inspect-the-dataset-analysis-recipe-dddddddd.py",
    ]
    assert not list((session.directory / "code").glob("*--view-*"))

    # Each name resolves to its own artifact, so the pairing is stable rather than filesystem order.
    code_dir = session.directory / "code"
    assert (code_dir / projected[0]).resolve() == artifact_path / "analysis.py"
    assert (code_dir / projected[1]).resolve() == artifact_path / "analysis-recipe.py"


def test_nested_artifact_groups_remain_meaningfully_grouped(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="nested")
    artifact_path = _commit_artifact(
        session,
        execution_id="a0a0a0a0-1111-2222-3333-444444444444",
        tool_name="evaluate_cluster_qc",
        summary="Evaluated cluster structure",
        files=[
            (
                "cluster-structure-10",
                "cluster-structure/cluster_10_correlation.png",
                "image/png",
                b"ten",
            ),
            (
                "cluster-structure-2",
                "cluster-structure/cluster_2_correlation.png",
                "image/png",
                b"two",
            ),
        ],
    )

    group = session.directory / "figures" / "cluster-structure"
    projected = sorted(group.glob("*.png"))
    assert len(projected) == 2
    assert {path.resolve() for path in projected} == {
        artifact_path / "cluster-structure" / "cluster_2_correlation.png",
        artifact_path / "cluster-structure" / "cluster_10_correlation.png",
    }
    index = json.loads((session.directory / "outputs.json").read_text())
    view_paths = [entry["view_path"] for entry in index["categories"]["figures"]]
    assert "cluster-structure-2" in view_paths[0]
    assert "cluster-structure-10" in view_paths[1]


def test_intermediate_data_is_separated_from_final_data(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="data")
    artifact_path = _commit_artifact(
        session,
        execution_id="bbbbbbbb-1111-2222-3333-444444444444",
        tool_name="compute_single_cell_umap",
        summary="Computed UMAP",
        files=[("umap-anndata", "umap.h5ad", "application/x-hdf5", b"intermediate")],
    )

    projected = next((session.directory / "data" / "intermediates").glob("*.h5ad"))
    assert projected.is_symlink()
    assert projected.resolve() == artifact_path / "umap.h5ad"
    assert not (session.directory / "data" / "final-annotated.h5ad").exists()


def test_zarr_store_is_projected_as_one_intermediate_data_artifact(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="zarr data")
    artifact_path = _commit_artifact(
        session,
        execution_id="12121212-1111-2222-3333-444444444444",
        tool_name="compute_single_cell_umap",
        summary="Computed UMAP",
        files=[
            (
                "umap-anndata",
                "umap.zarr",
                "application/vnd.zarr",
                {".zgroup": b"{}", "X/0.0": b"chunk"},
            )
        ],
    )

    projected = next((session.directory / "data" / "intermediates").glob("*.zarr"))
    assert projected.is_symlink()
    assert projected.resolve() == artifact_path / "umap.zarr"
    assert (projected / "X" / "0.0").read_bytes() == b"chunk"
    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["categories"]["data"][0]["media_type"] == "application/vnd.zarr"
    assert ".zarr" in (session.directory / "outputs.md").read_text()


def test_exported_h5ad_is_projected_as_a_user_facing_data_result(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="exported data")
    artifact_path = _commit_artifact(
        session,
        execution_id="34343434-1111-2222-3333-444444444444",
        tool_name="export_anndata",
        summary="Exported the current analysis as H5AD",
        files=[
            (
                "portable-anndata",
                "analysis-result.h5ad",
                "application/x-h5ad",
                b"H5AD",
            )
        ],
    )

    projected = next((session.directory / "data").glob("*.h5ad"))
    assert projected.is_symlink()
    assert projected.resolve() == artifact_path / "analysis-result.h5ad"
    assert not list((session.directory / "data" / "intermediates").glob("*.h5ad"))
    assert "`export_anndata`" in (session.directory / "outputs.md").read_text()


def test_repeated_names_remain_distinct_and_traceable(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="repeated")
    for execution_id, content in (
        ("11111111-aaaa-bbbb-cccc-000000000000", b"first"),
        ("22222222-aaaa-bbbb-cccc-000000000000", b"second"),
    ):
        _commit_artifact(
            session,
            execution_id=execution_id,
            tool_name="visualize_embedding",
            summary="Visualized UMAP",
            files=[("embedding", "embedding.png", "image/png", content)],
        )

    figures = sorted((session.directory / "figures").glob("*.png"))
    assert [path.name for path in figures] == [
        "embedding--visualize-embedding--11111111.png",
        "embedding--visualize-embedding--22222222.png",
    ]
    assert [path.read_bytes() for path in figures] == [b"first", b"second"]


def test_resume_rebuilds_a_deleted_projection_from_committed_state(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    session = AnalysisSession.create(sessions_root, title="resume", session_id="resume-view")
    artifact_path = _commit_artifact(
        session,
        execution_id="cccccccc-1111-2222-3333-444444444444",
        tool_name="visualize_qc",
        summary="Visualized QC",
        files=[("qc-distributions", "qc.png", "image/png", b"PNG")],
    )
    projected = next((session.directory / "figures").glob("*.png"))
    projected.unlink()
    assert not projected.exists()

    resumed = AnalysisSession.resume(sessions_root, "resume-view")

    rebuilt = next((resumed.directory / "figures").glob("*.png"))
    assert rebuilt.is_symlink()
    assert rebuilt.resolve() == artifact_path / "qc.png"


def test_relative_links_survive_moving_the_complete_session(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="portable")
    _commit_artifact(
        session,
        execution_id="dddddddd-1111-2222-3333-444444444444",
        tool_name="visualize_embedding",
        summary="Visualized UMAP",
        files=[("embedding", "embedding.png", "image/png", b"PNG")],
    )
    original_link = next((session.directory / "figures").glob("*.png"))
    assert not original_link.readlink().is_absolute()

    destination_root = tmp_path / "moved"
    destination_root.mkdir()
    moved = session.directory.rename(destination_root / session.directory.name)

    moved_link = next((moved / "figures").glob("*.png"))
    assert moved_link.read_bytes() == b"PNG"
    assert moved_link.resolve().is_relative_to(moved)


def test_staged_or_failed_outputs_never_enter_the_review_view(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="uncommitted")
    pending = (
        session.directory
        / "runtime"
        / "capabilities"
        / "pending"
        / "eeeeeeee-1111-2222-3333-444444444444"
    )
    pending.mkdir(parents=True)
    (pending / "uncommitted.png").write_bytes(b"PNG")
    session.store.record(
        "capability.execution_failed",
        payload={"execution_id": pending.name, "error": "failed"},
    )

    session.refresh_outputs()

    assert not list((session.directory / "figures").iterdir())
    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["categories"]["figures"] == []


def test_output_view_io_failure_does_not_invalidate_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="view failure")
    session.checkpoint_facts({"dataset": {"path": "/data/input.h5ad"}}, reason="test")

    def fail() -> dict[str, Any]:
        raise OSError("symlinks unavailable")

    monkeypatch.setattr(session, "refresh_outputs", fail)

    assert session.refresh_outputs_best_effort() is None
    assert session.store.state.facts["dataset"]["path"] == "/data/input.h5ad"
    assert session.store.events()[-1].kind == "session.output_view_refresh_failed"


def test_user_files_and_aliases_are_not_overwritten(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="preserve")
    user_report = session.directory / "reports" / "notes.md"
    user_report.write_text("mine\n")
    user_alias = session.directory / "data" / "final-annotated.h5ad"
    user_alias.write_bytes(b"user-owned")

    _commit_artifact(
        session,
        execution_id="ffffffff-1111-2222-3333-444444444444",
        tool_name="finalize_analysis",
        summary="Finalized analysis",
        files=[
            ("analysis-report", "notes.md", "text/markdown", b"generated\n"),
            ("final-anndata", "final-annotated.h5ad", "application/x-hdf5", b"canonical"),
        ],
    )

    assert user_report.read_text() == "mine\n"
    assert user_alias.read_bytes() == b"user-owned"
    generated_report = next((session.directory / "reports").glob("*--ffffffff.md"))
    assert generated_report.read_bytes() == b"generated\n"
    assert next((session.directory / "data").glob("*--ffffffff.h5ad")).read_bytes() == b"canonical"
    index = json.loads((session.directory / "outputs.json").read_text())
    assert "data/final-annotated.h5ad" not in {
        entry["view_path"] for entry in index["aliases"]
    }


def test_byte_identical_legacy_code_copy_is_migrated_but_edited_code_is_preserved(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="code migration")
    _commit_artifact(
        session,
        execution_id="abababab-1111-2222-3333-444444444444",
        tool_name="run_analysis_code",
        summary="Custom analysis code completed for: Inspect the dataset",
        files=[("analysis-code", "analysis.py", "text/x-python", b"print('canonical')\n")],
    )
    projected = next((session.directory / "code").glob("*.py"))
    projected.unlink()
    (session.directory / "outputs.json").unlink()
    projected.write_bytes(b"print('canonical')\n")

    session.refresh_outputs()

    assert projected.is_symlink()
    assert projected.read_bytes() == b"print('canonical')\n"

    projected.unlink()
    (session.directory / "outputs.json").unlink()
    projected.write_bytes(b"print('user edit')\n")
    session.refresh_outputs()

    assert not projected.is_symlink()
    assert projected.read_bytes() == b"print('user edit')\n"
    alternate = next((session.directory / "code").glob("*--view-2.py"))
    assert alternate.is_symlink()
    assert alternate.read_bytes() == b"print('canonical')\n"


def test_a_notebook_projects_as_a_report_with_a_stable_alias(tmp_path: Path) -> None:
    """The notebook is the headline reading surface, so it is a report, not code."""

    session = AnalysisSession.create(tmp_path / "sessions", title="notebook")
    artifact_path = _commit_artifact(
        session,
        execution_id="fe7aeed8-cf0e-46b2-b068-81426c7ca1a5",
        tool_name="finalize_analysis",
        summary="Finalized 29 cluster labels",
        files=[
            (
                "analysis-notebook",
                "analysis-notebook.ipynb",
                "application/x-ipynb+json",
                b'{"cells": [], "nbformat": 4, "nbformat_minor": 5}\n',
            ),
            ("analysis-recipe", "analysis-recipe.py", "text/x-python", b"CAPABILITY_CALLS = []\n"),
        ],
    )

    projected = next((session.directory / "reports").glob("*--fe7aeed8.ipynb"))
    assert projected.resolve() == artifact_path / "analysis-notebook.ipynb"
    assert (
        session.directory / "reports" / "analysis-notebook.ipynb"
    ).resolve() == artifact_path / "analysis-notebook.ipynb"
    # Its JSON body must not be mistaken for a script, and the recipe still lands in code/.
    assert not list((session.directory / "code").glob("*.ipynb"))
    assert (
        session.directory / "code" / "analysis-recipe.py"
    ).resolve() == artifact_path / "analysis-recipe.py"
    index = json.loads((session.directory / "outputs.json").read_text())
    assert "reports/analysis-notebook.ipynb" in {
        entry["view_path"] for entry in index["aliases"]
    }


def test_shell_runs_project_as_named_bundles_with_their_output(tmp_path: Path) -> None:
    """A command is only interpretable with its output, so the bundle is the unit of review."""

    session = AnalysisSession.create(tmp_path / "sessions", title="shell")
    artifact_path = _commit_artifact(
        session,
        execution_id="f513a638-fc33-4b52-8304-83bd52429563",
        tool_name="run_shell_command",
        summary="Shell command completed in 2.97s.",
        files=_shell_files(
            "tail -5 /home/user/projects/scagent-sdk/sessions/run_2026/events.jsonl",
            duration=2.97,
        ),
    )

    # The long absolute path collapses to its basename, which is what makes the name readable.
    bundle = session.directory / "shell" / "001-tail-5-events-jsonl-f513a638"
    assert bundle.is_dir()
    assert sorted(path.name for path in bundle.iterdir()) == [
        "command.sh",
        "run.json",
        "stderr.txt",
        "stdout.txt",
    ]
    assert all(path.is_symlink() for path in bundle.iterdir())
    assert (bundle / "command.sh").resolve() == artifact_path / "shell-command.sh"
    assert (bundle / "stdout.txt").resolve() == artifact_path / "shell.stdout.txt"
    assert (bundle / "stderr.txt").resolve() == artifact_path / "shell.stderr.txt"
    assert (bundle / "run.json").resolve() == artifact_path / "shell-run.json"
    assert (bundle / "stdout.txt").read_bytes() == b"stdout text\n"
    assert not (bundle / "command.sh").readlink().is_absolute()


def test_shell_index_entries_carry_run_facts_in_reading_order(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="shell facts")
    _commit_artifact(
        session,
        execution_id="470aee80-fa70-4fe4-924f-e5768d49972b",
        tool_name="run_shell_command",
        summary="Shell command completed in 2.83s.",
        files=_shell_files("wc -l notes.txt", duration=2.83),
    )

    index = json.loads((session.directory / "outputs.json").read_text())
    entries = index["categories"]["shell"]
    assert [Path(entry["view_path"]).name for entry in entries] == [
        "command.sh",
        "stdout.txt",
        "stderr.txt",
        "run.json",
    ]
    assert {entry["bundle_path"] for entry in entries} == {"shell/001-wc-l-notes-txt-470aee80"}
    assert entries[0]["command"] == "wc -l notes.txt"
    assert entries[0]["exit_code"] == 0
    assert entries[0]["duration_seconds"] == pytest.approx(2.83)
    assert entries[0]["canonical_path"].endswith("/shell-command.sh")


def test_shell_bundles_are_numbered_by_run_order_not_by_name(tmp_path: Path) -> None:
    """A name-sorted directory destroys run order; the ordinal prefix restores it."""

    session = AnalysisSession.create(tmp_path / "sessions", title="shell order")
    # Execution ids deliberately reverse-sort against commit order, so an ordinal taken from the
    # id rather than from the commit sequence would number these backwards.
    for execution_id, command in (
        ("cccccccc-0000-0000-0000-000000000000", "zcat archive.gz"),
        ("bbbbbbbb-0000-0000-0000-000000000000", "awk NR==1 table.csv"),
        ("aaaaaaaa-0000-0000-0000-000000000000", "md5sum matrix.h5ad"),
    ):
        _commit_artifact(
            session,
            execution_id=execution_id,
            tool_name="run_shell_command",
            summary="Shell command completed.",
            files=_shell_files(command),
        )

    assert sorted(path.name for path in (session.directory / "shell").iterdir()) == [
        "001-zcat-archive-gz-cccccccc",
        "002-awk-nr-1-table-csv-bbbbbbbb",
        "003-md5sum-matrix-h5ad-aaaaaaaa",
    ]


def test_multiline_inline_script_still_yields_a_recognizable_bundle_name(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="inline python")
    command = (
        'python3 -c "\nimport json\n'
        "with open('/home/user/sessions/run_2026/state.json') as handle:\n"
        '    state = json.load(handle)\n" 2>&1'
    )
    _commit_artifact(
        session,
        execution_id="b3516d16-80a2-4815-87fe-fbb28b96eac6",
        tool_name="run_shell_command",
        summary="Shell command completed in 2.67s.",
        files=_shell_files(command),
    )

    bundle = next((session.directory / "shell").iterdir())
    assert bundle.name.startswith("001-python3-c-import-json")
    assert bundle.name.endswith("-b3516d16")
    assert "home-user-sessions" not in bundle.name

    # Only the path substring is abbreviated, so the surrounding call still reads as code.
    row = next(
        line
        for line in (session.directory / "outputs.md").read_text().splitlines()
        if "b3516d16" in line
    )
    assert "open('state.json')" in row


def test_urls_and_relative_paths_are_left_intact_in_the_command_table(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="urls")
    _commit_artifact(
        session,
        execution_id="0ddba11c-0000-0000-0000-000000000000",
        tool_name="run_shell_command",
        summary="Shell command completed.",
        files=_shell_files("curl -s https://example.com/a/b > ./out/data.json"),
    )

    row = next(
        line
        for line in (session.directory / "outputs.md").read_text().splitlines()
        if "0ddba11c" in line
    )
    assert "https://example.com/a/b" in row
    assert "./out/data.json" in row


def test_a_trailing_separator_does_not_swallow_the_directory_name(tmp_path: Path) -> None:
    """``ls -la /long/path/to/target/`` must be named for target, not for the path prefix."""

    session = AnalysisSession.create(tmp_path / "sessions", title="trailing slash")
    _commit_artifact(
        session,
        execution_id="e7834fb1-bda3-4698-b4e0-c180b5bedfc4",
        tool_name="run_shell_command",
        summary="Shell command completed in 2.98s.",
        files=_shell_files("ls -la /home/user/projects/scagent-sdk/sessions/run_2026/figures/"),
    )

    bundle = next((session.directory / "shell").iterdir())
    assert bundle.name == "001-ls-la-figures-e7834fb1"


def test_shell_run_without_a_readable_report_falls_back_to_the_summary(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="no report")
    files = _shell_files("ls", report=b"{ this is not json")
    _commit_artifact(
        session,
        execution_id="dddddddd-0000-0000-0000-000000000000",
        tool_name="run_shell_command",
        summary="Inspected the staging directory",
        files=files,
    )

    bundle = next((session.directory / "shell").iterdir())
    assert bundle.name == "001-inspected-the-staging-directory-dddddddd"
    assert (bundle / "command.sh").is_symlink()
    index = json.loads((session.directory / "outputs.json").read_text())
    entry = index["categories"]["shell"][0]
    assert entry["command"] == ""
    assert entry["exit_code"] is None
    assert entry["duration_seconds"] is None


def test_the_summary_fallback_drops_its_boilerplate_clause(tmp_path: Path) -> None:
    """``run_shell_command`` now names the command in its summary; the prefix is not a label."""

    session = AnalysisSession.create(tmp_path / "sessions", title="summary fallback")
    _commit_artifact(
        session,
        execution_id="feedface-0000-0000-0000-000000000000",
        tool_name="run_shell_command",
        summary="Shell command completed in 2.83s: ls -la /home/user/sessions/run_2026/figures",
        files=_shell_files("ls", report=b"not json at all"),
    )

    bundle = next((session.directory / "shell").iterdir())
    assert bundle.name == "001-ls-la-figures-feedface"


def test_non_finite_report_numbers_keep_the_index_serializable(tmp_path: Path) -> None:
    """``json.loads`` accepts NaN, but the index is written with ``allow_nan=False``."""

    session = AnalysisSession.create(tmp_path / "sessions", title="nan duration")
    _commit_artifact(
        session,
        execution_id="eeeeeeee-0000-0000-0000-000000000000",
        tool_name="run_shell_command",
        summary="Shell command completed.",
        files=_shell_files(
            "true",
            report=b'{"command": "true", "duration_seconds": NaN, "exit_code": 0}\n',
        ),
    )

    index = json.loads((session.directory / "outputs.json").read_text())
    entry = index["categories"]["shell"][0]
    assert entry["duration_seconds"] is None
    assert entry["command"] == "true"


def test_other_outputs_of_a_shell_record_keep_their_own_category(tmp_path: Path) -> None:
    """A figure a command happens to write belongs in figures/, not buried in the bundle."""

    session = AnalysisSession.create(tmp_path / "sessions", title="mixed shell")
    artifact_path = _commit_artifact(
        session,
        execution_id="99999999-0000-0000-0000-000000000000",
        tool_name="run_shell_command",
        summary="Shell command completed.",
        files=[
            *_shell_files("gnuplot plot.gp"),
            ("plot", "plot.png", "image/png", b"PNG"),
        ],
    )

    bundle = next((session.directory / "shell").iterdir())
    assert sorted(path.name for path in bundle.iterdir()) == [
        "command.sh",
        "run.json",
        "stderr.txt",
        "stdout.txt",
    ]
    figure = next((session.directory / "figures").glob("*.png"))
    assert figure.resolve() == artifact_path / "plot.png"


def test_shell_section_of_the_markdown_index_is_browsable(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="shell markdown")
    _commit_artifact(
        session,
        execution_id="7a3d322e-b4f6-4c49-9a62-3e948d33feb2",
        tool_name="run_shell_command",
        summary="Shell command completed in 2.81s.",
        files=_shell_files("grep -c finalize /work/events.jsonl", duration=2.81),
    )

    markdown = (session.directory / "outputs.md").read_text()
    assert "## Shell commands" in markdown
    # Abbreviated in the table for readability; the note points at the exact text.
    assert "`grep -c finalize events.jsonl`" in markdown
    assert "`command.sh` holds the exact text that ran" in markdown
    assert "2.81s" in markdown
    assert "shell/001-grep-c-finalize-events-jsonl-7a3d322e" in markdown
    assert "[stdout.txt](shell/001-grep-c-finalize-events-jsonl-7a3d322e/stdout.txt)" in markdown


def test_a_backtick_command_cannot_break_the_markdown_table(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="backticks")
    _commit_artifact(
        session,
        execution_id="cafecafe-0000-0000-0000-000000000000",
        tool_name="run_shell_command",
        summary="Shell command completed.",
        files=_shell_files("echo `date` | tee out.txt"),
    )

    row = next(
        line
        for line in (session.directory / "outputs.md").read_text().splitlines()
        if "cafecafe" in line
    )
    assert row.count("`") == 2
    assert "\\|" in row


def test_an_empty_shell_surface_is_reported_rather_than_omitted(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="empty shell")

    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["categories"]["shell"] == []
    markdown = (session.directory / "outputs.md").read_text()
    assert "## Shell commands\n\n_No committed outputs yet._" in markdown


def test_stale_shell_bundles_are_pruned_but_the_category_root_remains(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="shell cleanup")
    _commit_artifact(
        session,
        execution_id="bd8e764e-b746-4b64-9a40-d208817875f8",
        tool_name="run_shell_command",
        summary="Shell command completed.",
        files=_shell_files("du -sh data"),
    )
    bundle = next((session.directory / "shell").iterdir())

    refresh_output_view(session.directory, {})

    assert not bundle.exists()
    assert (session.directory / "shell").is_dir()
    assert (session.directory / "data" / "intermediates").is_dir()


def test_repeated_refresh_reuses_shell_bundles_without_duplicating_them(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="shell idempotent")
    _commit_artifact(
        session,
        execution_id="efea735b-a19f-4bf2-8876-8f02e40d14b4",
        tool_name="run_shell_command",
        summary="Shell command completed.",
        files=_shell_files("nproc"),
    )

    first = json.loads((session.directory / "outputs.json").read_text())
    session.refresh_outputs()
    session.refresh_outputs()
    second = json.loads((session.directory / "outputs.json").read_text())

    assert first == second
    assert len(list((session.directory / "shell").iterdir())) == 1
    assert not list((session.directory / "shell").rglob("*--view-*"))


def test_refresh_removes_only_stale_generated_links(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cleanup")
    _commit_artifact(
        session,
        execution_id="99999999-1111-2222-3333-444444444444",
        tool_name="visualize_embedding",
        summary="Visualized UMAP",
        files=[("embedding", "embedding.png", "image/png", b"PNG")],
    )
    generated = next((session.directory / "figures").glob("*.png"))
    user_file = session.directory / "figures" / "README.md"
    user_file.write_text("keep\n")

    refresh_output_view(session.directory, {})

    assert not generated.exists()
    assert user_file.read_text() == "keep\n"
