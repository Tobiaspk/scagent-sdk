---
name: batch-investigation
description: Produce concise gene-first batch evidence and optionally record a keep, integrate, or separate decision when sample effects matter to the requested analysis.
---

# Batch Investigation

This skill separates evidence from the decision without forcing either step into unrelated work.

1. **`investigate_batch`** accepts any H5AD with expression values, cluster labels, and a
   meaningful batch column, then produces evidence and records **no** decision. It derives
   portable identities from the artifact. This region-comparison method uses the supplied
   clustering and is **gene-first**:
   - finds **sample-enriched cluster regions** by enrichment over each batch's dataset-wide
     frequency (not raw purity — a region that is 42% of one sample when that sample is 9% of the
     data is caught);
   - cheaply nominates likely same-population cross-sample pairs by correlation over the most
     variable non-nuisance region-mean expression profiles;
   - lazily runs **within-sample identity DEG** only for nominated pairs, in correlation order
     (that cluster vs the rest of its OWN batch, holding batch constant), tries at most 10 pairs,
     and stops after three confirmed matches;
   - confirms identity by shared discriminating genes, then **directly compares only those three
     confirmed pairs** and reports genes higher on each side;
   - flags a **recurring** sample-associated program (higher in the same batch across ≥2 distinct
     populations).

   Composition, cluster-vs-sample agreement (**ARI + NMI**), neighborhood mixing, and per-batch QC
   are kept only as **advisory context** — they tell you *where* samples separate, never *why*, so
   they never decide anything and a high value is never proof of a tumor or any tissue. The evidence
   is written up as a plain-language `README`/report (per-file notes plus a dataset-specific
   Interpretation and a concrete suggestion, all built from the numbers). The inline result is a
   compact legacy-style reasoning summary: separation, the strongest cross-sample identity pair
   and shared genes, recurring programs, design limitation, and recommendation. It also reports
   the strongest supported population match. **When you record the decision, base the short rationale only on this
   evidence and the dataset's own metadata — never assert a disease, tissue type, or replicate/design
   structure the metadata does not state; if the design is unknown, say so.** The verdict is the
   single `gene_evidence` axis (`none`/`localized`/`recurring_sample_associated`) — `none`/
   `localized` recommend keeping the uncorrected representation, `recurring_sample_associated`
   (the same cell type split by sample across the dataset) recommends **integration (scVI)** — plus
   a **non-binding** recommendation and the standing design caveat (genes cannot settle
   technical-vs-biological). These findings stay
   in the evidence report; the user-facing decision is not required to reproduce their vocabulary.
   If there is no meaningful batch variable, pass `batch_key=null`; no second decision call is
   needed.

   The tool takes **no design or condition inputs** — only `path`, `batch_key`, and `cluster_key`.
   Pass the obvious sample/donor column as `batch_key` and the clustering you already have; there is
   nothing else to "decide" about what to pass. This is deliberate: whether a sample-linked split is
   a technical batch or the biology of interest is an experimental-design question the **user**
   answers, not a lever the agent can pull to steer the verdict. So `design_interpretation` is always
   `unknown` and the recommendation is a function of the **gene evidence alone**.

2. Present the material conclusion and design limitation concisely, and present the handling
   choice **with a recommended default, not a neutral menu** — the way the legacy checkpoint did.
   When the same sample-linked program recurs across populations and confirmed cross-sample
   identity pairs show the same population split by sample, recommend **integration (scVI)**:
   clusters separating by sample beyond the biology the user expects is what justifies
   correction, and the design caveat is stated alongside that recommendation, not instead of it.
   When the gene evidence is `none`/`localized`, recommend keeping the uncorrected
   representation.

   **Then STOP and ask the user — never decide this yourself.** Batch handling is the user's call,
   not yours — integrating rewrites the representation the whole downstream analysis continues
   from, and no gene table can settle technical-vs-biological on its own. So state the material
   conclusion, say in one line what the recommendation rests on and what the design cannot tell
   you, then present the choice as a short selector **in this exact order** and END YOUR TURN:
   (1) the recommended option (integrate with scVI when the evidence is
   `recurring_sample_associated`, otherwise keep uncorrected), (2) the other of integrate /
   keep-uncorrected, (3) analyze the samples separately, (4) **describe the experiment setup so we
   can understand it better** — always the last option. Do **not** call `decide_batch_handling`,
   and do **not** start the correction, until the user has actually chosen. Recording a decision
   the user did not make — or picking one because it seems obvious — is a bug, not a shortcut. A
   user preference always wins, and a user-stated design (comparable replicates vs distinct
   conditions) resolves the technical-vs-biological ambiguity — cite that statement in the
   decision rationale.

   The one case that does not need the pause: there is no meaningful batch variable
   (`batch_key=null`), where no decision call is needed at all.

3. **`decide_batch_handling`** consumes the current `evidence_id` and records
   `keep_uncorrected`, `integrate`, or `separate`, plus a short rationale — carrying the user's
   answer from step 2, not your own preference. The handler validates the
   evidence link directly; no predecessor floor is needed.

A matched identity plus a direct gene list does **not** prove a technical batch effect; cell-level
q-values rank separation and are not sample-level replication. Investigate batch once on the
uncorrected cells/counts. After integration, use `score_integration` to assess mixing; integration
and reclustering do not require a second batch investigation or decision. A real cell-set or count
change does make the evidence stale.

Read [references/decision-guide.md](references/decision-guide.md) for confounding and integration
cautions.
