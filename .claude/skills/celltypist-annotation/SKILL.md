---
name: celltypist-annotation
description: Run a compatible cached CellTypist classifier on raw single-cell counts and gene symbols to produce per-cell labels and confidence, then optionally summarize those predictions over a supplied grouping.
---

# CellTypist

CellTypist is a supervised logistic-regression reference classifier trained on labeled expression
profiles. It expects gene symbols and expression normalized to 10,000 counts per cell followed by
`log1p`; this skill starts from raw counts and performs that normalization itself.

`run_celltypist_annotation` requires a count-bearing H5AD, gene symbols, and a cached model
appropriate to the organism and tissue. It produces per-cell predictions and confidence.

`summarize_celltypist_by_cluster` optionally aggregates existing per-cell calls over a supplied
grouping for group-level interpretation.

Choose the model deliberately from the cached classifiers named in the local-prerequisite
inventory in your instructions; that list is authoritative, so do not search the filesystem for
models. `Immune_All_Low.pkl` is broad immune reference evidence, not a
universal default for every tissue. Specialized models can add resolution but can also force
out-of-domain cells into known labels. Treat predictions as evidence to reconcile with genes and
context, not final truth.

Inspect the entire inventory, not only the generic models. In a comprehensive run, use broad
SCimilarity output and dataset metadata to establish likely context, then choose the closest
organism/tissue/disease CellTypist model (for example an intestinal or colorectal model for CRC)
and state why it is appropriate.

When one cached model is clearly closest to an already established organism and tissue, select it
and run it without asking the user to choose a filename. Ask only when the scientific context is
still unresolved or two plausible models encode materially different scopes that would change the
interpretation. There is no generic runtime default: the `model` argument must always make the
choice explicit in provenance.

Read [references/model-selection.md](references/model-selection.md).
