---
name: single-cell-counts
description: Resolve and materialize a validated raw-count matrix from H5AD or 10x input. Use whenever a method needs raw counts or when an input has several possible count representations.
---

# Single-Cell Count Matrix

`materialize_count_matrix` chooses a raw-count source, validates it, and saves a non-overwriting
H5AD with that matrix in `layers["counts"]`, preserving the input observations and variables.

The input can be an H5AD, a 10x H5 file, or a 10x Matrix Market directory. For H5AD, the tool
inspects `X`, every layer, and an alignable `.raw`. `auto` uses count-like `X`, otherwise the sole
count-like alternative, and refuses ambiguity. A valid count matrix is finite, nonnegative, and
integer-valued. Explicit `X`, `raw`, or `layer` selection is available when the scientist knows
which representation is authoritative.

When the active analyzed artifact has lost its count layer but an identity-matched raw artifact is
available, keep the active artifact as `path` and pass the raw artifact as `counts_from`. The tool
requires exactly the same cells and genes, aligns their order, and adds `layers["counts"]` without
replacing normalized `X`, embeddings, graphs, annotations, or their lineage facts. Do not adopt the
raw file as a new analysis root merely to satisfy a downstream count requirement.

The output records count, cell-set, and dataset-revision identities derived from the actual
matrix and names. Those identities describe the artifact; they are not proof that another tool
ran first.

In ordinary materialization, the selected counts are written to both `X` and `layers["counts"]`.
Because that duplicates them, the artifact drops what it just copied: an `.raw`, and the source
layer when a layer was selected. In `counts_from` attachment mode, only `layers["counts"]` is added
and no target payload is dropped. Inputs remain immutable, and `details.dropped_payload` names
exactly what was omitted — report it rather than describing the artifact as a faithful copy.

Read [references/count-contract.md](references/count-contract.md) for source-selection and
lineage details.
