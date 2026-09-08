"""Convert ``var_names`` to gene symbols and save a provenance-bearing AnnData.

Offline-first and non-destructive: identifiers are mapped to symbols using the dataset's own
symbol column (``feature_name``/``gene_symbols``/…); original Ensembl IDs are preserved into
``var`` before ``var_names`` is overwritten; duplicate symbols are made unique rather than
dropped; and genes without a symbol keep their original identifier. ``mygene`` is an opt-in online
fallback that fails soft when unavailable. Optional species-case normalization (human UPPER /
mouse Title) is available, though aligning to a reference model's vocabulary at annotation time is
more reliable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import genes  # noqa: E402  (sibling module; path inserted above)
import identity as identity_recipes  # noqa: E402  (sibling module; path inserted above)


def _remint_after_relabel(
    adata: Any, path: Path, facts: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Re-mint count identities after a var-axis relabel, and invalidate what it stales.

    ``count_matrix_id`` hashes ``var_names`` and ``dataset_revision.id`` derives from the count
    representation, so relabelling genes after counts were materialized leaves both identities
    describing a vocabulary the file no longer uses. Returns ``(facts_patch_fragment, uns_updates)``
    or ``None`` when no count representation exists yet -- the common ordering, where conversion
    precedes ``resolve_count_matrix`` and nothing needs re-minting.
    """

    analysis = facts.get("analysis")
    if not isinstance(analysis, dict):
        return None
    representation = analysis.get("count_representation")
    revision = analysis.get("dataset_revision")
    cell_set = analysis.get("cell_set")
    if not (
        isinstance(representation, dict)
        and isinstance(revision, dict)
        and isinstance(cell_set, dict)
    ):
        return None
    cell_set_id = cell_set.get("id")
    selected_source = representation.get("count_source")
    if not isinstance(cell_set_id, str) or not isinstance(selected_source, str):
        return None

    counts = adata.layers.get("counts") if hasattr(adata, "layers") else None
    if counts is None:
        return None

    matrix_id = identity_recipes.count_matrix_identity(counts, adata.obs_names, adata.var_names)
    count_id = identity_recipes.count_representation_identity(
        matrix_id, cell_set_id, selected_source
    )
    source_path = revision.get("source_path")
    revision_id = identity_recipes.dataset_revision_identity(
        str(source_path) if isinstance(source_path, str) else str(path), cell_set_id, count_id
    )

    fragment = {
        "analysis": {
            "count_representation": {"id": count_id, "matrix_id": matrix_id},
            "dataset_revision": {"id": revision_id},
        },
        # Annotation evidence is scored against gene symbols but the annotation floor keys only on
        # clustering_id, so a relabel would otherwise leave it "current" and permit finalization
        # against the previous vocabulary. Cluster QC, cell QC, doublets, and batch already stale
        # on count_representation_id and need no explicit clearing.
        "annotation": None,
        "finalization": None,
        "reference_runs": None,
    }
    uns_updates = {
        "count_representation_id": count_id,
        "count_matrix_id": matrix_id,
        "dataset_revision_id": revision_id,
    }
    return fragment, uns_updates


def _mygene_map(names: list[str], fmt: str, organism: str) -> list[str] | None:
    """Best-effort Ensembl/Entrez -> symbol via mygene.info. Fails soft (returns None)."""

    try:
        import mygene  # type: ignore
    except Exception:
        return None
    scopes = "ensembl.gene" if fmt == "ensembl" else "entrezgene"
    species = organism if organism in {"human", "mouse"} else "all"
    query = [genes.strip_ensembl_version(name) for name in names]
    try:
        result = mygene.MyGeneInfo().querymany(
            query, scopes=scopes, fields="symbol", species=species,
            as_dataframe=False, verbose=False,
        )
    except Exception:
        return None
    best: dict[str, str] = {}
    for hit in result:
        query_id, symbol = hit.get("query"), hit.get("symbol")
        if query_id and symbol and query_id not in best:
            best[query_id] = symbol
    return [best.get(q, orig) for q, orig in zip(query, names, strict=False)]


def _infer_organism(names: list[str]) -> str:
    """Light auto-detection: 'human' | 'mouse' | 'unknown' from identifier shape."""

    sample = [str(n) for n in names[:1000]]
    if sum(1 for n in sample if n.upper().startswith("ENSMUSG")) > 0:
        return "mouse"
    if sum(1 for n in sample if n.upper().startswith("ENSG")) > 0:
        return "human"
    upper = [n for n in sample if n.isupper()]
    title = [n for n in sample if n[:1].isupper() and n[1:].islower()]
    if len(title) > len(upper) and title:
        return "mouse"
    if upper:
        return "human"
    return "unknown"


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import json

    import anndata as ad
    import pandas as pd

    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path must be a non-empty string")
    path = Path(raw_path).expanduser().resolve()
    is_zarr = path.suffix.lower() == ".zarr"
    if not (path.is_dir() if is_zarr else path.is_file()):
        raise FileNotFoundError(f"dataset not found: {path}")
    if not is_zarr and path.suffix.lower() != ".h5ad":
        raise ValueError("convert_gene_ids supports .h5ad files and .zarr stores")

    use_mygene = bool(arguments.get("use_mygene", False))
    normalize_case = bool(arguments.get("normalize_case", False))
    organism = str(arguments.get("organism", "auto")).strip().lower()

    # Conversion rewrites every var name and saves a new object, so the input is read whole;
    # a lazy open would buy nothing here.
    adata = ad.read_zarr(path) if is_zarr else ad.read_h5ad(path)
    original = [str(name) for name in adata.var_names]
    if organism in {"", "auto"}:
        organism = _infer_organism(original)

    from_format = genes.infer_id_format(original)
    names, prefix = genes.strip_genome_prefix(original)
    names = [genes.strip_ensembl_version(name) for name in names]
    fmt_after_prefix = genes.infer_id_format(names)

    report: dict[str, Any] = {
        "from_format": from_format,
        "organism": organism,
        "genome_prefix_stripped": prefix,
        "n_genes": int(adata.n_vars),
        "original_ids_saved_to": None,
        "symbol_column": None,
        "source": "none",
        "sample_before": original[:10],
    }

    mapped: list[str] | None = None
    if fmt_after_prefix == "symbol":
        mapped = names
        report["source"] = "already_symbols" if not prefix else f"genome_prefix:{prefix}"
    else:
        if from_format == "ensembl":
            save_col = "ensembl_id" if "ensembl_id" not in adata.var.columns else "ensembl_id_orig"
            adata.var[save_col] = original
            report["original_ids_saved_to"] = save_col
        mapped, symbol_col = genes.map_names_to_symbols(names, adata.var)
        if mapped is not None:
            report["source"] = f"column:{symbol_col}"
            report["symbol_column"] = symbol_col
        elif use_mygene and fmt_after_prefix in {"ensembl", "entrez"}:
            mapped = _mygene_map(names, fmt_after_prefix, organism)
            if mapped is not None:
                report["source"] = "mygene"
        if mapped is None:
            mapped = names
            report["source"] = "unmapped"

    if normalize_case and organism in {"human", "mouse"} and report["source"] != "unmapped":
        mapped = [genes.to_species_case(name, organism) for name in mapped]

    n_mapped = sum(
        1
        for orig, new in zip(original, mapped, strict=False)
        if str(new) != str(orig) and genes.classify_gene_id(str(new)) == "symbol"
    )
    index = pd.Index([str(name) for name in mapped])
    n_duplicates = int(index.duplicated().sum())
    adata.var_names = index
    if not adata.var_names.is_unique:
        adata.var_names_make_unique()

    report["n_mapped"] = n_mapped
    report["n_unmapped"] = int(adata.n_vars) - n_mapped
    report["n_duplicates_made_unique"] = n_duplicates
    report["to_format"] = genes.infer_id_format(list(adata.var_names))
    report["sample_after"] = [str(name) for name in adata.var_names[:10]]
    report["changed"] = report["source"] not in {"already_symbols", "unmapped"} or bool(prefix)

    facts_patch: dict[str, Any] = {"gene_conversion": report}
    reminted: dict[str, Any] | None = None
    if report["changed"]:
        facts = getattr(context, "state_facts", None)
        outcome = _remint_after_relabel(adata, path, facts if isinstance(facts, dict) else {})
        if outcome is not None:
            fragment, uns_updates = outcome
            facts_patch.update(fragment)
            metadata = dict(adata.uns.get("scagent_sdk", {}))
            metadata.update(uns_updates)
            adata.uns["scagent_sdk"] = metadata
            reminted = uns_updates
            report["reminted_identities"] = uns_updates
            report["invalidated_facts"] = ["annotation", "finalization", "reference_runs"]

    output_relative = "gene-symbols.h5ad"
    output_path = context.staging_dir / output_relative
    adata.write_h5ad(output_path, compression="gzip")
    (context.staging_dir / "gene-conversion.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )

    if report["source"] == "unmapped":
        summary = (
            f"No gene-symbol source for {path.name}: var_names left as {from_format} "
            f"({adata.n_vars:,} genes). Reference annotation may not overlap."
        )
    else:
        summary = (
            f"Converted {path.name} var_names to symbols via {report['source']}: "
            f"{n_mapped:,}/{adata.n_vars:,} mapped"
            + (f", {n_duplicates} duplicate(s) made unique" if n_duplicates else "")
            + "."
        )
    if reminted is not None:
        summary += (
            " Count representation and dataset revision were re-minted for the new gene "
            "vocabulary; annotation and finalization evidence must be regenerated."
        )

    return {
        "schema_version": 1,
        "summary": summary,
        "details": report,
        "facts_patch": facts_patch,
        "artifacts": [
            {
                "name": "gene-symbols",
                "relative_path": output_relative,
                "media_type": "application/x-hdf5",
            },
            {
                "name": "gene-conversion-report",
                "relative_path": "gene-conversion.json",
                "media_type": "application/json",
            },
        ],
    }
