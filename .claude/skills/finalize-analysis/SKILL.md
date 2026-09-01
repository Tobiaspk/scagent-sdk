---
name: finalize-analysis
description: Publish a complete cluster-to-cell-type mapping as a final annotated AnnData, table, figures, and provenance report.
---

# Finalize Analysis

Call `finalize_analysis` with a complete cluster-to-label mapping. Rationales, confidence, DEG
hypotheses, and evidence summaries are optional: include them when they make the result more useful,
not to satisfy a ceremony. Use broad or explicitly uncertain labels when the evidence is ambiguous;
`unknown` is a valid reviewed outcome.

The capability enforces only intrinsic publication safety: labels must cover the current clusters,
the clustering must match the current artifact, and a new annotation column must not overwrite a
user column. QC, batch investigation, marker review, and reference methods are recommended when
scientifically useful, but finalization does not require them.

The report is reconstructed from durable state and committed capability provenance. It includes
the workflow/parameters, QC and cluster-review decisions, batch evidence, annotation agreement,
per-cluster labels, automatically surfaced caveats, and artifact guidance. It also writes an exact
ordered capability-call recipe into the session `code/` view.

Finalization emits two surfaces over the same provenance: `reports/final-analysis-report.md` is the
narrative findings and caveats, and `code/analysis-recipe.py` is the machine replay list.

It deliberately does **not** emit a notebook. A readable step-by-step walkthrough is the separate
`analysis-notebook` capability, requestable at any point and rebuildable as the analysis grows —
finalization is not necessarily the end of the work, and a user may want a walkthrough before it or
long after. Offer it here rather than assuming it, and point a user who asks *what was done* at that
notebook rather than at the recipe.

When evidence conflicts, lower confidence, generalize upward, or use an unknown/ambiguous label
instead of inventing precision. Do not finalize a cluster as "doublet" on a Scrublet call, and do
not call a `GZMB`-high cluster "plasma" without immunoglobulin/secretory markers.

Read [references/adjudication.md](references/adjudication.md) for disagreement and uncertainty handling.
