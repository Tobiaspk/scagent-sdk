---
name: expression-preprocessing
description: Normalize a single-cell count matrix or select highly variable genes as independent, composable operations. Use when a downstream method needs log-normalized expression or an HVG mask.
---

# Expression Preprocessing

`normalize_single_cell_expression` reads a validated count matrix, writes total-count-normalized
log1p expression to `X`, and preserves the original counts layer.

`counts_layer` defaults to `auto`: it uses `layers["counts"]` when present and otherwise validates
`X`, matching `calculate_single_cell_qc`. A dataset whose counts live in `X` therefore normalizes
directly, with no `materialize_count_matrix` round-trip -- which is what previously forced the
lineage back to the original input file and dropped any annotations added since. Name a layer
explicitly only when it is known to exist; a named layer is never silently substituted. Pass
`null` to force `X`.

`select_highly_variable_genes` computes an HVG Boolean mask on the current expression matrix (or
on a named layer) without subsetting genes. It can be run after normalization for `seurat`, or
directly from raw counts with a count-aware flavor such as `seurat_v3` when the installed Scanpy
stack supports it.

Each transformation is invoked explicitly when its output is required by the selected downstream
method.

See [references/matrix-semantics.md](references/matrix-semantics.md).
