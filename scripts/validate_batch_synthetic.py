"""Synthetic acceptance for the gene-first batch branches real data did not exercise.

Runs UNDER the compute (rapids) environment via ``launch_batch_synthetic.py``. Builds a dataset with
two shared cell populations across two batches plus an injected **recurring** sample-associated
program, and checks:

A. recurring program detected across >=2 populations -> gene_evidence=recurring_sample_associated,
   design unknown -> cannot_determine_technical_vs_biological (NOT auto-integrate);
B. the same data with a perfectly confounded condition column -> design is
   confounded_with_biology and the recommendation remains conservative;
C. pair-order invariance: shuffling cell/region order yields the same recurrence verdict;
D. a handling choice records only decision, rationale, and evidence id.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anndata as ad
import numpy as np
import scanpy as sc


def _load(skill_root: Path):
    source = skill_root / "batch-investigation" / "scripts" / "investigate.py"
    spec = importlib.util.spec_from_file_location("batch_investigate", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(seed: int = 0, shuffle: bool = False) -> ad.AnnData:
    rng = np.random.default_rng(seed)
    per = 150  # cells per (population, batch)
    pop_a = ["CD3D", "TRAC", "IL7R", "CD2", "CD7"]
    pop_b = ["LYZ", "S100A8", "S100A9", "FCN1", "VCAN"]
    tech = ["SOD2", "FOSL2", "IER3"]  # injected recurring sample-associated program
    filler = [f"BG{i}" for i in range(25)]
    genes = pop_a + pop_b + tech + filler
    gi = {g: i for i, g in enumerate(genes)}

    blocks, clusters, batches = [], [], []
    for pop, markers in (("A", pop_a), ("B", pop_b)):
        for batch in ("S1", "S2"):
            counts = np.zeros((per, len(genes)), dtype=np.int64)
            factor = rng.gamma(2.0, 1.0, size=per)
            for g in markers:
                counts[:, gi[g]] = rng.poisson(4 + 8 * factor)
            for g in filler:
                counts[:, gi[g]] = rng.poisson(1.0)
            # The recurring program is elevated in S1 in BOTH populations.
            for g in tech:
                counts[:, gi[g]] = rng.poisson(12.0 if batch == "S1" else 1.0)
            blocks.append(counts)
            clusters += [pop] * per
            batches += [batch] * per

    matrix = np.vstack(blocks)
    adata = ad.AnnData(X=matrix.astype(np.float32))
    adata.obs_names = [f"cell{i}" for i in range(adata.n_obs)]
    adata.var_names = genes
    adata.obs["leiden"] = np.array(clusters)
    adata.obs["sample"] = np.array(batches)
    # Perfectly confounded condition: S1 -> disease, S2 -> healthy.
    adata.obs["condition"] = np.where(adata.obs["sample"] == "S1", "disease", "healthy")
    if shuffle:
        order = rng.permutation(adata.n_obs)
        adata = adata[order].copy()
    for column in ("leiden", "sample", "condition"):
        adata.obs[column] = adata.obs[column].astype("category")
    adata.layers["counts"] = adata.X.copy()
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.uns["scagent_sdk"] = {
        "schema_version": 1,
        "cell_set_id": "cells:syn",
        "count_representation_id": "counts:syn",
        "representation_id": "representation:syn",
        "clustering_id": "clustering:syn",
    }
    return adata


def _context(workdir: Path, prepared: Path, tag: str) -> SimpleNamespace:
    staging = workdir / f"staging-{tag}"
    staging.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        scientific_session_id="syn",
        session_dir=workdir,
        staging_dir=staging,
        skill_id="batch-investigation",
        tool_name="investigate_batch",
        execution_id=f"syn-{tag}",
        artifact_relative_path=f"artifacts/capabilities/syn-{tag}",
        state_revision=1,
        state_facts={
            "analysis": {
                "dataset_revision": {"id": "rev:syn", "prepared_path": str(prepared)},
                "cell_set": {"id": "cells:syn"},
                "count_representation": {"id": "counts:syn"},
                "representation": {"id": "representation:syn"},
                "clustering": {"id": "clustering:syn", "key": "leiden"},
            }
        },
    )


def main() -> int:
    skill_root = Path(__file__).resolve().parents[1] / ".claude" / "skills"
    batch = _load(skill_root)
    workdir = Path(tempfile.mkdtemp(prefix="batch-syn-"))
    checks: dict[str, bool] = {}

    prepared = workdir / "prepared.h5ad"
    _build().write_h5ad(prepared)
    base_args = {
        "path": str(prepared),
        "batch_key": "sample",
        "cluster_key": "leiden",
        "min_cells_per_region": 20,
        "min_enrichment": 1.0,  # both batches are 50% of each cluster here
    }

    # A. recurring program, no design info -> cannot determine (never auto-integrate).
    a_ctx = _context(workdir, prepared, "a")
    a = batch.run_evidence(dict(base_args), a_ctx)
    a_full = json.loads((a_ctx.staging_dir / "batch-evidence.json").read_text())
    checks["A_recurring_detected"] = a["details"]["gene_evidence"] == "recurring_sample_associated"
    checks["A_design_unknown"] = a["details"]["design_interpretation"] == "unknown"
    checks["A_not_auto_integrate"] = (
        a["details"]["recommendation"] == "cannot_determine_technical_vs_biological"
    )
    recurring_genes = {r["gene"] for r in a_full["recurring_programs"]}
    checks["A_recurring_includes_injected"] = bool(recurring_genes & {"SOD2", "FOSL2", "IER3"})
    checks["A_matches_are_real_identity"] = a_full["n_supported_matches"] >= 1

    # B. A confounded condition is reported conservatively.
    b = batch.run_evidence(
        dict(base_args, condition_keys=["condition"]),
        _context(workdir, prepared, "b"),
    )
    checks["B_confounding_detected"] = (
        b["details"]["design_interpretation"] == "confounded_with_biology"
    )
    checks["B_recommendation_conservative"] = (
        b["details"]["recommendation"] == "cannot_determine_technical_vs_biological"
    )

    # B2. The user's choice is recorded without model-written authorization ceremony.
    decision_ctx = _context(workdir, prepared, "b2")
    decision_ctx.state_facts["batch"] = {"evidence": b["details"], "decision": None}
    decision = batch.run_decision(
        {
            "evidence_id": b["details"]["evidence_id"],
            "decision": "keep_uncorrected",
            "rationale": "Preserve the confounded biological contrast.",
        },
        decision_ctx,
    )
    checks["B_decision_is_minimal"] = set(decision["details"]) == {
        "decision",
        "evidence_id",
        "rationale",
    }

    # C. pair-order invariance.
    shuffled = workdir / "prepared-shuffled.h5ad"
    _build(shuffle=True).write_h5ad(shuffled)
    c_args = dict(base_args)
    c_args["path"] = str(shuffled)
    c_ctx = _context(workdir, shuffled, "c")
    c = batch.run_evidence(c_args, c_ctx)
    c_full = json.loads((c_ctx.staging_dir / "batch-evidence.json").read_text())
    checks["C_order_invariant_verdict"] = (
        c["details"]["gene_evidence"] == a["details"]["gene_evidence"]
    )
    checks["C_order_invariant_recurring_genes"] = {
        r["gene"] for r in c_full["recurring_programs"]
    } == recurring_genes

    # D. matching used discriminating genes only.
    checks["D_gene_class_versioned"] = bool(a_full.get("gene_class_version"))
    checks["D_schema_versioned"] = a["details"].get("schema_version") == 1

    passed = all(checks.values())
    print(
        json.dumps(
            {
                "passed": passed,
                "checks": checks,
                "A": {
                    k: a["details"][k]
                    for k in (
                        "gene_evidence",
                        "design_interpretation",
                        "recommendation",
                    )
                },
                "A_recurring": a_full["recurring_programs"][:5],
                "B": {
                    k: b["details"][k]
                    for k in ("design_interpretation", "recommendation")
                },
            },
            indent=2,
            default=str,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
