from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _handlers() -> tuple[Any, Any]:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        item
        for item in CapabilityRegistry(skills_root).discover()
        if item.manifest.skill_id == "cellbender-background-removal"
    )
    tools = {tool.name: tool for tool in package.manifest.tools}
    return (
        package.load_handler(tools["validate_cellbender_input"]),
        package.load_handler(tools["remove_ambient_background"]),
    )


def _summary(*, barcodes: int = 10000, low_count: int = 5000) -> dict[str, Any]:
    return {
        "container": "10x-h5",
        "shape": {"features": 2000, "barcodes": barcodes},
        "nnz": 100000,
        "count_dtype": "int32",
        "root_keys": ["matrix"],
        "matrix_title": None,
        "feature_types": {"Gene Expression": 2000},
        "gene_expression_features": 2000,
        "zero_count_barcodes": 100,
        "nonzero_barcodes": barcodes - 100,
        "low_count_umi_threshold": 10,
        "low_count_barcodes": low_count,
        "low_count_fraction": low_count / barcodes,
        "umi_quantiles": {
            "min": 0.0,
            "q01": 1.0,
            "q10": 2.0,
            "median": 5.0,
            "q90": 1000.0,
            "q99": 5000.0,
            "max": 10000.0,
        },
    }


def _context(handler: Any, tmp_path: Path, dataset: Path) -> Any:
    fingerprint = handler.__globals__["_dataset_fingerprint"](dataset, mode="full")
    stat = dataset.stat()
    dataset_fact = {
        "path": str(dataset.resolve()),
        "size_bytes": stat.st_size,
        "modified_time_ns": stat.st_mtime_ns,
        "fingerprint": fingerprint,
        "fingerprint_mode": "full",
    }
    staging = tmp_path / "staging"
    staging.mkdir()
    return SimpleNamespace(
        staging_dir=staging,
        session_dir=tmp_path / "session",
        execution_id="execution-1",
        artifact_relative_path="artifacts/capabilities/run-cellbender--execution-1",
        state_facts={"dataset": dataset_fact},
    )


def test_cellbender_manifest_is_strict_and_gpu_routed() -> None:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        item
        for item in CapabilityRegistry(skills_root).discover()
        if item.manifest.skill_id == "cellbender-background-removal"
    )
    tools = {tool.name: tool for tool in package.manifest.tools}

    assert set(tools) == {"validate_cellbender_input", "remove_ambient_background"}
    validate = tools["validate_cellbender_input"]
    assert validate.environment == "cellbender"
    assert validate.floors == ()
    remove = tools["remove_ambient_background"]
    assert remove.environment == "cellbender"
    assert remove.floors == ("dataset_identity", "cellbender_input_suitable")
    assert remove.input_schema["additionalProperties"] is False
    assert "use_cuda" not in remove.input_schema["properties"]


def test_suitability_accepts_raw_tail_and_rejects_filtered_or_cellbender() -> None:
    validate, _remove = _handlers()
    assess = validate.__globals__["_assess_suitability"]

    raw = assess(Path("raw_feature_bc_matrix.h5"), _summary())
    filtered = assess(
        Path("filtered_feature_bc_matrix.h5"),
        _summary(barcodes=8000, low_count=0),
    )
    post = _summary()
    post["root_keys"] = ["droplet_latents", "global_latents", "matrix", "metadata"]
    corrected = assess(Path("renamed.h5"), post)

    assert raw["status"] == "suitable"
    assert "not an expected-cell estimate" in raw["required_low_count_semantics"]
    assert filtered["status"] == "unsuitable"
    assert any("filtered" in reason for reason in filtered["reasons"])
    assert corrected["status"] == "unsuitable"
    assert any("CellBender" in reason for reason in corrected["reasons"])


def test_validate_records_suitability_bound_to_fingerprint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    validate, _remove = _handlers()
    dataset = tmp_path / "raw.h5"
    dataset.write_bytes(b"raw-droplet-test")
    context = _context(validate, tmp_path, dataset)
    context.state_facts = {}
    monkeypatch.setitem(
        validate.__globals__,
        "_read_10x_h5",
        lambda _path: (_summary(), [100, 10, 2, 1]),
    )
    monkeypatch.setitem(
        validate.__globals__,
        "_write_rank_plot",
        lambda _totals, path, **_kwargs: path.write_bytes(b"png"),
    )

    result = validate({"path": str(dataset)}, context)

    fact = result["facts_patch"]["ambient_background"]["input_validation"]
    recorded_dataset = result["facts_patch"]["dataset"]
    assert fact["status"] == "suitable"
    assert fact["dataset_fingerprint"] == recorded_dataset["fingerprint"]
    assert recorded_dataset["path"] == str(dataset.resolve())
    assert recorded_dataset["fingerprint_mode"] == "full"
    assert {item["relative_path"] for item in result["artifacts"]} == {
        "input-validation.json",
        "input-assessment.md",
        "input-umi-rank.png",
    }


def test_command_uses_cuda_fixed_seed_contract_and_explicit_parameters() -> None:
    _validate, remove = _handlers()
    parameters = remove.__globals__["_parameters"](
        {
            "expected_cells": 1000,
            "total_droplets_included": 5000,
            "fpr": 0.01,
            "epochs": 20,
            "exclude_feature_types": ["Antibody Capture"],
        },
        _summary(),
    )
    command = remove.__globals__["_build_command"](
        "/runtime/bin/cellbender",
        input_path=Path("/data/raw.h5"),
        output_path=Path("/staging/output.h5"),
        parameters=parameters,
        checkpoint_path=None,
    )

    assert "--cuda" in command
    assert command[command.index("--expected-cells") + 1] == "1000"
    assert command[command.index("--total-droplets-included") + 1] == "5000"
    assert parameters["random_seed"] == 1234


def test_success_issues_corrected_lineage_and_invalidates_downstream(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _validate, remove = _handlers()
    dataset = tmp_path / "raw.h5"
    dataset.write_bytes(b"raw-droplet-test")
    context = _context(remove, tmp_path, dataset)
    dataset_fact = context.state_facts["dataset"]
    context.state_facts["ambient_background"] = {
        "input_validation": {
            "status": "suitable",
            "input_path": str(dataset.resolve()),
            "dataset_fingerprint": dataset_fact["fingerprint"],
        }
    }
    monkeypatch.setitem(
        remove.__globals__,
        "_read_10x_h5",
        lambda _path: (_summary(), [100, 10, 2, 1]),
    )
    monkeypatch.setattr(remove.__globals__["shutil"], "which", lambda _name: "/bin/cellbender")
    monkeypatch.setitem(
        remove.__globals__,
        "_barcode_identity",
        lambda _path: ("barcodes:sha256:test", 4321),
    )
    monkeypatch.setitem(
        remove.__globals__,
        "_write_comparison_plot",
        lambda _raw, _corrected, path: path.write_bytes(b"png"),
    )

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        output = Path(command[command.index("--output") + 1])
        output.write_bytes(b"full-output")
        output.with_name(f"{output.stem}_filtered.h5").write_bytes(b"filtered-output")
        output.with_name(f"{output.stem}_metrics.csv").write_text(
            "fraction_counts_removed,0.05\nfound_cells,4321\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(remove.__globals__["subprocess"], "run", fake_run)

    result = remove({"path": str(dataset), "selected_output": "filtered"}, context)

    facts = result["facts_patch"]
    assert facts["dataset"]["lineage"]["parent_fingerprint"] == dataset_fact["fingerprint"]
    assert facts["analysis"]["cell_set"]["id"] == "barcodes:sha256:test"
    assert facts["analysis"]["count_representation"]["method"] == (
        "cellbender-remove-background"
    )
    assert facts["analysis"]["representation"] is None
    assert facts["analysis"]["clustering"] is None
    assert facts["cluster_qc"] is None
    assert facts["annotation"] is None
    assert result["decisions_patch"]["final_labels"] is None
    assert result["model_media"][0]["relative_path"] == "cellbender-count-comparison.png"


def test_failure_commits_checkpoint_without_changing_active_dataset(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _validate, remove = _handlers()
    dataset = tmp_path / "raw.h5"
    dataset.write_bytes(b"raw-droplet-test")
    context = _context(remove, tmp_path, dataset)
    dataset_fact = context.state_facts["dataset"]
    context.state_facts["ambient_background"] = {
        "input_validation": {
            "status": "suitable",
            "input_path": str(dataset.resolve()),
            "dataset_fingerprint": dataset_fact["fingerprint"],
        }
    }
    monkeypatch.setitem(
        remove.__globals__,
        "_read_10x_h5",
        lambda _path: (_summary(), [100, 10, 2, 1]),
    )
    monkeypatch.setattr(remove.__globals__["shutil"], "which", lambda _name: "/bin/cellbender")
    source_checkpoint = tmp_path / "source-ckpt.tar.gz"
    source_checkpoint.write_bytes(b"prior-checkpoint")

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        copied_checkpoint = Path(command[command.index("--checkpoint") + 1])
        assert copied_checkpoint != source_checkpoint
        assert copied_checkpoint.read_bytes() == b"prior-checkpoint"
        Path(kwargs["cwd"], "ckpt.tar.gz").write_bytes(b"updated-checkpoint")
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(remove.__globals__["subprocess"], "run", fake_run)

    result = remove(
        {"path": str(dataset), "checkpoint_path": str(source_checkpoint)},
        context,
    )

    assert "dataset" not in result["facts_patch"]
    assert source_checkpoint.read_bytes() == b"prior-checkpoint"
    last_run = result["facts_patch"]["ambient_background"]["last_run"]
    assert last_run["status"] == "failed"
    assert last_run["checkpoint_path"].endswith("/ckpt.tar.gz")
    assert any(item["name"] == "cellbender-checkpoint" for item in result["artifacts"])
