---
name: batch-investigation
description: Produce concise, gene-first batch evidence, present it once, then record the user's keep/integrate/separate choice. Use before integration and before finalization whenever samples, donors, libraries, lanes, or batches may affect the analysis.
---

# Batch Investigation

This is a two-step, evidence-before-decision capability.

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
     populations);
   - cross-tabs supplied `condition_keys` against the batch key for **confounding**.

   Composition, cluster-vs-sample agreement (**ARI + NMI**), neighborhood mixing, and per-batch QC
   are kept only as **advisory context** — they tell you *where* samples separate, never *why*, so
   they never decide anything and a high value is never proof of a tumor or any tissue. The evidence
   is written up as a plain-language `README`/report (per-file notes plus a dataset-specific
   Interpretation and a concrete suggestion, all built from the numbers). The inline result is a
   compact legacy-style reasoning summary: separation, the strongest cross-sample identity pair
   and shared genes, recurring programs, design limitation, and recommendation. It also reports
   the strongest supported population match. **When you record the decision, base the short rationale only on this
   evidence and the dataset's own metadata — never assert a disease, tissue type, or replicate/design
   structure the metadata does not state; if the design is unknown, say so.** The verdict is two
   independent axes — `gene_evidence` (`none`/`localized`/
   `recurring_sample_associated`) × `design_interpretation` (`unknown`/`confounded_with_biology`/
   `orthogonal_but_not_known_technical`) — and a **non-binding** recommendation. These findings stay
   in the evidence report; the user-facing decision is not required to reproduce their vocabulary.
   If there is no meaningful batch variable, pass `batch_key=null`; the not-applicable evidence
   satisfies finalization without a second ceremonial decision call.

2. After `investigate_batch`, **stop and present the evidence to the user**. Explain why the
   within-sample identity comparison matters, name the strongest same-population pair and genes,
   distinguish sample separation from its cause, and offer: integrate with scVI, keep the
   uncorrected representation, analyze separately, or provide missing design context. Do not call
   `decide_batch_handling` in the same autonomous run and do not choose on the user's behalf.

3. **`decide_batch_handling`** consumes the current `evidence_id` only after the user chooses and
   records `keep_uncorrected`, `integrate`, or `separate`, plus a one- or two-sentence rationale.
   The evidence link is the durable guard; do not manufacture authorization fields or duplicate the
   report in the decision.

A matched identity plus a direct gene list does **not** prove a technical batch effect; cell-level
q-values rank separation and are not sample-level replication. Investigate batch once on the
uncorrected cells/counts. After integration, use `score_integration` to assess mixing; integration
and reclustering do not require a second batch investigation or decision. A real cell-set or count
change does make the evidence stale.

Read [references/decision-guide.md](references/decision-guide.md) for confounding and integration
cautions.
