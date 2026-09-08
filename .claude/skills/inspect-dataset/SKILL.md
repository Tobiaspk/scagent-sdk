---
name: inspect-dataset
description: Inspect a candidate single-cell dataset before analysis — establish its durable file identity, detect the container format from bytes and extension, and report its full contents (H5AD or .zarr) (shape, matrix/layer/raw value facts, obs/var columns, embeddings, gene-identifier signals). Use when beginning or resuming an analysis, or whenever the user asks what is in a dataset, before choosing preprocessing or annotation methods.
---

# Inspect Dataset

This skill has three tools. Prefer them over writing custom inspection or gene-mapping code — they
are deterministic, handle backed/sparse/GPU matrices safely, and never load a large matrix whole.

## `inspect_dataset` — file identity

Call `inspect_dataset` with the user-provided path to establish byte-level identity before
scientific interpretation. It accepts a file or a `.zarr` store. Use `hash_mode=full` when
immutable identity matters; keep the default metadata/sample fingerprint for very large inputs —
for a store, `sampled` hashes the complete member manifest and distributes a fixed byte budget
across metadata and representative members from the common AnnData namespaces, so it stays cheap
without trusting one large chunk. It remains a change detector, not a cryptographic commitment to
every unsampled byte; use `full` when immutable identity matters. Treat path, size, format evidence,
and fingerprint as facts; report uncertainty when extension and byte signature disagree.

## `describe_dataset` — dataset contents

When the user asks **what is in a dataset**, or you need its structure before preprocessing or
annotation, call `describe_dataset` on the `.h5ad` path **or on any `.zarr` artifact produced
earlier in this analysis**. It reports facts only — shape; `X`, layer, and `raw` value
characteristics (dtype, sampled min/max, `fraction_integer_valued`, `has_negative_sample`) sampled
safely from a bounded row block; per-column `obs`/`var` summaries (dtype, cardinality, missingness,
value-counts or numeric stats); embedding keys **with their shapes** (`obsm_shapes`, so you can see
whether `X_scVI` is 30- or 50-dimensional); `uns` keys; and gene-identifier signals. Neither format
loads the matrix whole — `read_mode` distinguishes backed H5AD from lazy Zarr, while `backed`
retains AnnData's literal `isbacked` meaning. Do **not** write custom code to
open the AnnData for this: checking an artifact's layers, obs columns, or embeddings is exactly
what this tool is for.

Interpretation stays with you: the tool never decides roles, species, or whether a matrix is raw
counts. Read the value facts — e.g. `all_integer_sample` with no negatives suggests counts even when
the dtype is float32; decimals or `log1p_in_uns` suggest normalized data — and the per-column facts
(a near-`1.0` `unique_fraction` is an identifier, not a label) to reach those conclusions yourself.

## `convert_gene_ids` — normalize var_names to gene symbols

Reference tools (SCimilarity, CellTypist), marker overlap, and gene plotting align to a gene-**symbol**
space; handed Ensembl/Entrez IDs they overlap almost nothing. When `describe_dataset` reports
`gene_namespace` as ensembl/entrez/mixed (var_names are not symbols), call `convert_gene_ids` on the
`.h5ad` path or `.zarr` artifact before those steps. It maps var_names to symbols **offline** using the dataset's own
symbol column (`gene_symbols.symbol_column`, e.g. `feature_name`), preserves the original IDs in
`var['ensembl_id']`, makes duplicate symbols unique without dropping genes, and saves a new
provenance-bearing AnnData (`gene-symbols.h5ad`). Continue the analysis from that output.

Options: `use_mygene=true` attempts an online Ensembl/Entrez→symbol lookup only when there is no
in-file symbol column (fails soft offline); `organism` (auto/human/mouse) and `normalize_case=true`
apply best-effort species casing. Case correction for a specific reference model is handled
automatically inside `run_scimilarity`/`run_celltypist` (they realign to the model's own vocabulary),
so you do not need `normalize_case` before annotation — only namespace conversion when var_names are
Ensembl.

Read [references/dataset-identity.md](references/dataset-identity.md) when deciding between sampled
and full hashing or interpreting format evidence.
