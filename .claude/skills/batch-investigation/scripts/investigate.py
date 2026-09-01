"""Gene-first batch evidence, then a separate identity-bound handling decision.

`investigate_batch` (``run_evidence``) produces evidence and records no decision. It is gene-first:
it finds sample-enriched cluster regions, characterizes each with a within-sample identity DEG
(that cluster versus the rest of its OWN batch, holding batch constant), matches the same population
across batches by shared **discriminating** identity genes, compares matched regions directly, and
flags any sample-associated program that recurs across >=2 distinct populations. Composition,
cluster-vs-sample agreement (ARI + NMI), neighborhood mixing, and per-batch QC are retained only as
advisory context — they locate where samples separate, never why. The verdict is two independent
axes — gene evidence x experimental design — and a deterministic, non-binding recommendation, plus a
plain-language interpretation built entirely from the evidence (no assumed biology). The DE test is
scanpy's in-environment Wilcoxon rank test.

`decide_batch_handling` (``run_decision``) consumes a current evidence id after the user chooses and
records only the choice and a concise rationale. The scientific detail remains in the evidence
artifact instead of being duplicated into durable state.

Pure classifiers live at module scope for unit testing without Scanpy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

BATCH_EVIDENCE_SCHEMA = 1
DE_ENGINE = "scanpy_wilcoxon"

# --- versioned gene classification (shared vocabulary with cluster QC) -------
GENE_CLASS_VERSION = "batch-gene-class-v1"

_NUISANCE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^MT-",
        r"^MRP[LS]\d",
        r"^RP[LS]\d",
        r"^MALAT1$",
        r"^NEAT1$",
        r"^HB[ABDEGMQZ]$",
        r"^HBA\d$",
        r"^LINC\d",
        r"^A[CL]\d{6}\.",
        r"\.\d+$",
    )
)
# Broad/context: antigen presentation, heat-shock/ER stress, activation/IEG, housekeeping, cell
# cycle. Real programs, but shared across lineages, so they must not decide a population match.
_BROAD_PATTERNS = tuple(re.compile(p) for p in (r"^HLA-", r"^HSP", r"^HIST\d", r"^DNAJ"))
_BROAD_CONTEXT_GENES = frozenset(
    {
        "B2M",
        "ACTB",
        "ACTG1",
        "GAPDH",
        "TMSB4X",
        "TMSB10",
        "FTL",
        "FTH1",
        "FOS",
        "FOSB",
        "JUN",
        "JUNB",
        "JUND",
        "EGR1",
        "DUSP1",
        "NFKBIA",
        "IER2",
        "CD74",
        "XIST",
        "TPT1",
        "EEF1A1",
        "EEF2",
        "UBC",
        "UBB",
        "MKI67",
        "TOP2A",
        "STMN1",
        "TUBB",
        "TUBA1B",
        "PCNA",
        "CENPF",
        "HMGB1",
        "HMGB2",
        "S100A4",
        # ER/secretory stress program (the live false-match culprits: DERL3, HSP90B1, ...).
        "HSP90B1",
        "HSPA5",
        "DERL3",
        "XBP1",
        "SSR4",
        "SEC61B",
        "SDF2L1",
        "PDIA4",
        "PDIA6",
        "CALR",
        "CANX",
        "MANF",
        "DNAJB9",
        "SEL1L",
        "H13",
        "HM13",
    }
)


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)

def gene_class(gene: str) -> str:
    """Classify a gene symbol as ``nuisance``, ``broad``, or ``discriminating``."""
    symbol = str(gene).upper()
    for pattern in _NUISANCE_PATTERNS:
        if pattern.search(symbol):
            return "nuisance"
    if symbol in _BROAD_CONTEXT_GENES:
        return "broad"
    for pattern in _BROAD_PATTERNS:
        if pattern.search(symbol):
            return "broad"
    return "discriminating"


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


# --- pure classifiers --------------------------------------------------------
def region_enrichment(n_in_region: int, n_in_cluster: int, batch_global_fraction: float) -> float:
    """Enrichment of a batch within a cluster over that batch's dataset-wide frequency.

    A region that is 42% one sample when that sample is 9% of the data returns ~4.6 — caught even
    though raw purity (0.42) is modest.
    """
    if n_in_cluster <= 0 or batch_global_fraction <= 0:
        return 0.0
    return (n_in_region / n_in_cluster) / batch_global_fraction


def match_regions(
    disc_a: list[str], disc_b: list[str], *, min_shared: int, min_jaccard: float
) -> dict[str, Any]:
    """Decide whether two regions are the same population using DISCRIMINATING genes only.

    Broad/context and nuisance genes are excluded upstream, so an ER/stress or housekeeping overlap
    can no longer manufacture a match. A match needs both a minimum count of shared discriminating
    genes and a minimum Jaccard, and records an explicit rejection reason otherwise.
    """
    set_a = {str(g).upper() for g in disc_a if gene_class(g) == "discriminating"}
    set_b = {str(g).upper() for g in disc_b if gene_class(g) == "discriminating"}
    shared = sorted(set_a & set_b)
    union = set_a | set_b
    jaccard = len(shared) / len(union) if union else 0.0
    if len(shared) < min_shared:
        supported, reason = False, f"only {len(shared)} shared discriminating genes (<{min_shared})"
    elif jaccard < min_jaccard:
        supported, reason = False, f"jaccard {jaccard:.2f} below {min_jaccard:.2f}"
    else:
        supported, reason = True, "shared discriminating identity above thresholds"
    return {
        "shared": len(shared),
        "jaccard": jaccard,
        "shared_genes": shared,
        "supported": supported,
        "reason": reason,
    }


def nominate_cross_sample_pairs(
    keys: list[tuple[str, str]],
    mean_matrix: Any,
    genes: list[str],
    *,
    min_corr: float = 0.4,
    n_top_variable: int = 2000,
) -> list[dict[str, Any]]:
    """Nominate likely same-population regions cheaply, before any DEG tests.

    This is the legacy search strategy: Pearson correlation over the most variable non-nuisance
    mean-expression features. The expensive within-sample Wilcoxon tests are reserved for the
    highest-correlation cross-sample candidates and used only to confirm identity.
    """
    import numpy as np

    matrix = np.asarray(mean_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 2:
        return []
    keep = np.array([gene_class(gene) != "nuisance" for gene in genes], dtype=bool)
    if keep.any():
        matrix = matrix[:, keep]
    variance = matrix.var(axis=0)
    informative = np.flatnonzero(variance > 1e-8)
    if informative.size < 2:
        return []
    top = informative[np.argsort(variance[informative])[::-1][:n_top_variable]]
    corr = np.corrcoef(matrix[:, top])
    pairs: list[dict[str, Any]] = []
    for i, (cluster_a, batch_a) in enumerate(keys):
        for j in range(i + 1, len(keys)):
            cluster_b, batch_b = keys[j]
            if batch_a == batch_b:
                continue
            value = float(corr[i, j])
            if math.isfinite(value) and value >= min_corr:
                pairs.append(
                    {
                        "index_a": i,
                        "index_b": j,
                        "cluster_a": cluster_a,
                        "batch_a": batch_a,
                        "cluster_b": cluster_b,
                        "batch_b": batch_b,
                        "profile_correlation": value,
                    }
                )
    pairs.sort(key=lambda pair: pair["profile_correlation"], reverse=True)
    return pairs


def summarize_recurrence(
    direct_rows: list[dict[str, Any]], *, min_populations: int = 2
) -> list[dict[str, Any]]:
    """Programs where the same batch is higher for a gene across >=2 distinct populations.

    Order-invariant: the result depends only on the set of (gene, batch, population) facts.
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in direct_rows:
        slot = seen.setdefault(
            (str(row["gene"]), str(row["higher_in_batch"])),
            {"populations": set(), "effects": []},
        )
        slot["populations"].add(row["population"])
        slot["effects"].append(abs(float(row.get("logfoldchange", 0.0))))
    recurring = [
        {
            "gene": gene,
            "higher_in_batch": batch,
            "n_populations": len(slot["populations"]),
            "populations": sorted(map(str, slot["populations"])),
            "mean_abs_logfoldchange": (
                sum(slot["effects"]) / len(slot["effects"]) if slot["effects"] else 0.0
            ),
        }
        for (gene, batch), slot in seen.items()
        if len(slot["populations"]) >= min_populations
    ]
    recurring.sort(
        key=lambda r: (
            -r["n_populations"],
            -r["mean_abs_logfoldchange"],
            r["gene"],
            r["higher_in_batch"],
        )
    )
    return recurring


def classify_gene_evidence(n_matched_with_diffs: int, n_recurring_populations: int) -> str:
    """gene_evidence axis from cross-sample matches and program recurrence."""
    if n_recurring_populations >= 2:
        return "recurring_sample_associated"
    if n_matched_with_diffs >= 1:
        return "localized"
    return "none"


def recommend(gene_evidence: str) -> str:
    """Return a non-binding recommendation driven ONLY by the gene evidence.

    The technical-versus-biological question is an experimental-design call the user makes, not a
    verdict the data can settle, so the recommendation no longer branches on a design axis (and the
    tool no longer accepts design assertions that let the caller steer it). Confirmed cross-sample
    split populations — the same cell type separating by sample across the dataset, established by
    matching within-sample identity signatures — are the actionable signal that integrating to
    co-embed those shared populations is worth doing. The user still authorizes it, and the design
    caveat travels alongside the recommendation as narration, never as a competing verdict.
    """
    if gene_evidence == "recurring_sample_associated":
        return "integration_recommended"
    return "do_not_integrate_based_on_current_evidence"


# --- plain-language translation (no jargon reaches the reader or the model) ---
# Every enum the verdict produces has a plain-English translation. The narrative below is built
# entirely from the evidence — no hardcoded biology, every gene name comes from the results — so
# the model reads a grounded finding instead of inventing a disease, tissue, or study design it
# was never given. Wording mirrors the legacy gene-first diagnostic.
GENE_EVIDENCE_PLAIN = {
    "none": "no sample-linked expression differences were established",
    "localized": (
        "sample-linked differences turned up in individual cell populations but did not repeat "
        "across the dataset"
    ),
    "recurring_sample_associated": (
        "the same sample-linked expression shift showed up in several different cell populations — "
        "a pattern that spans the dataset rather than one cell type"
    ),
}
# The tool takes no design inputs, so ``design_interpretation`` is always ``unknown`` — the design
# question (technical vs biological) is the user's to answer. This caveat is stated as narration
# beside the gene-driven recommendation, never as a competing verdict.
DESIGN_PLAIN = {
    "unknown": (
        "no experimental-design information was provided, so we cannot tell whether the samples "
        "are meant to be comparable replicates or are different patients/conditions"
    ),
}
VERDICT_PLAIN = {
    "integration_recommended": (
        "the same cell type is separating by sample across the dataset, so integrating (scVI) to "
        "co-embed the shared populations is recommended — unless that per-sample difference is the "
        "biology you mean to study, which only you can say"
    ),
    "do_not_integrate_based_on_current_evidence": (
        "the current gene evidence does not justify integrating the dataset"
    ),
}
SUGGESTION_PLAIN = {
    "do_not_integrate_based_on_current_evidence": (
        "Based on this dataset, we did not find clear evidence that batch integration is required. "
        "It is reasonable to proceed without integrating; revisit only if you have a specific "
        "reason to expect a technical batch effect."
    ),
    "integration_recommended": (
        "The same cell type appears split by sample across the dataset: the within-sample identity "
        "signatures of the matched clusters agree, so they are the same population separated by "
        "sample rather than different cell types. That split is exactly what integration is meant "
        "to fix — integrating with scVI (the sample as the batch covariate) co-embeds these shared "
        "populations. The one thing the genes cannot tell you is whether that per-sample difference "
        "is a technical batch effect or real biology you intend to study; that is an experimental-"
        "design question only you can answer. Recommended: integrate with scVI — unless these "
        "per-sample differences are the contrast you want to keep, in which case keep the "
        "uncorrected representation or analyze the samples separately."
    ),
}


def cluster_batch_concordance(batch_labels: Any, cluster_labels: Any) -> dict[str, Any]:
    """Advisory global agreement between the clustering and the sample labels (ARI + NMI).

    ARI and NMI compress how strongly the uncorrected clusters line up with sample identity: ~0
    means samples are blended across clusters (well mixed); high means clusters largely correspond
    to individual samples. A high value is NOT proof of a technical batch effect — donor/patient
    biology (donor-specific epithelial states, genetic background, malignant clones) also drives
    clusters to track sample. It is advisory only; the gene evidence and the design decide, and the
    tissue must never be assumed (do not assume a tumor).
    """
    from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

    ari = float(adjusted_rand_score(batch_labels, cluster_labels))
    nmi = float(normalized_mutual_info_score(batch_labels, cluster_labels))
    if ari >= 0.5 or nmi >= 0.5:
        interpretation = "clusters largely correspond to individual samples"
    elif ari >= 0.2 or nmi >= 0.2:
        interpretation = "clusters partly track sample labels"
    else:
        interpretation = "clusters are largely independent of sample (well mixed)"
    return {
        "ari": round(ari, 4),
        "nmi": round(nmi, 4),
        "tracks_sample": bool(ari >= 0.2 or nmi >= 0.2),
        "interpretation": interpretation,
    }


def _mixing_plain(concordance: dict[str, Any] | None, mixing: dict[str, Any] | None) -> str:
    """Plain paragraph on where samples separate (ARI/NMI + neighborhood mixing).

    Framed as necessary-but-not-sufficient: a technical batch and real biology push these numbers
    the same way, so they locate separation but never explain it.
    """
    if not concordance:
        return (
            "First, how the samples sit in the data: the mixing metrics were not available "
            "for this run."
        )
    interp = str(concordance.get("interpretation", "")).capitalize()
    sent = (
        f"First, how the samples sit together in the data. {interp} (cluster-vs-sample agreement "
        f"ARI = {concordance.get('ari')}, NMI = {concordance.get('nmi')}; 0 means samples are "
        "fully blended across clusters, and higher values mean clusters increasingly correspond "
        "to individual samples)."
    )
    if mixing and mixing.get("status") == "complete":
        same = mixing.get("mean_same_batch_neighbor_fraction")
        rand = mixing.get("random_composition_same_batch_fraction")
        if same is not None:
            mixed_word = (
                "tending to sit next to cells from their own sample"
                if rand is None or float(same) > 1.5 * float(rand)
                else "fairly well mixed across samples"
            )
            sent += (
                f" Looking cell by cell, a typical cell's nearest neighbours were {mixed_word} "
                f"({round(float(same) * 100)}% of neighbours came from the same sample, versus "
                f"{round(float(rand) * 100)}% expected if samples were blended)."
                if rand is not None
                else f" Looking cell by cell, a typical cell's neighbours were {mixed_word}."
            )
    sent += (
        " These numbers only tell us WHERE samples separate, never WHY: a genuine biological "
        "difference between samples and a technical batch effect push them in exactly the same "
        "direction, so on their own they cannot decide whether to correct anything. That is why "
        "the rest of the check looks directly at genes."
    )
    return sent


def build_plain_interpretation(
    *,
    batch_key: str,
    n_regions: int,
    pairs: list[dict[str, Any]],
    recurring_by_sample: dict[str, list[str]],
    gene_evidence: str,
    design_interpretation: str,
    recommendation: str,
    concordance: dict[str, Any] | None = None,
    mixing: dict[str, Any] | None = None,
) -> str:
    """Readable plain-language walkthrough of the results, built only from the evidence.

    No hardcoded biology: every gene, cluster, and sample name comes from ``pairs`` /
    ``recurring_by_sample``. It walks the reader through what was done in order (where samples
    separate, then the gene work), names the populations and genes involved, points at the file
    that holds each piece of evidence, and ends with a plain bottom line and a concrete suggestion.
    Internal enum values are never shown — only their plain-language translations.
    """
    paras: list[str] = [
        "This check asks one question: do the differences between samples look like a technical "
        "batch effect that should be corrected, or like real biology that should be left alone? "
        "It answers in two stages — a quick look at how the samples sit in the data, then a "
        "gene-level investigation.",
        _mixing_plain(concordance, mixing),
        (
            "Next, the genes. Looking at each cell type in each sample, the check found "
            f"{n_regions} cluster-and-sample regions that held far more of one sample than its "
            "size would predict (`sample-enriched-regions.csv`) and then examined "
            f"{len(pairs)} cross-sample pair(s) that looked like the same cell type "
            "(`population-matches.csv`). For each pair it identified the cell type from within a "
            f"single sample — comparing the cluster against the rest of that same sample so "
            f"{batch_key} is held constant (`within-sample-identity-degs.csv`) — and then compared "
            "the two samples' versions of it head to head (`direct-matched-region-degs.csv`)."
        ),
    ]
    for p in pairs:
        shared = ", ".join(p.get("shared_genes", [])[:8]) or "shared identity genes"
        sent = (
            f"Cluster {p['cluster_a']} in {p['batch_a']} and cluster {p['cluster_b']} in "
            f"{p['batch_b']} share {len(p.get('shared_genes', []))} of their top identity genes "
            f"({shared}), so they look like the same cell type present in both samples."
        )
        hi_a = ", ".join(p.get("higher_in_a", [])[:6])
        hi_b = ", ".join(p.get("higher_in_b", [])[:6])
        if hi_a or hi_b:
            sent += (
                f" Comparing that cell type across the two samples directly, {p['batch_a']} is "
                f"higher for {hi_a or 'n/a'}, while {p['batch_b']} is higher for {hi_b or 'n/a'}. "
                "This describes how the two versions differ; it does not, by itself, show the "
                "difference is technical rather than real."
            )
        paras.append(sent)
    if recurring_by_sample:
        bits = [
            f"in {sample} ({', '.join(genes[:8])})"
            for sample, genes in recurring_by_sample.items()
        ]
        paras.append(
            "Crucially, the same sample-linked shift shows up in more than one cell type — the "
            "same genes are consistently higher " + "; ".join(bits) + ". A shift that repeats "
            "across several cell types is a dataset-wide, sample-linked pattern rather than a "
            "one-off. But dataset-wide still is not the same as technical: a real, systemic "
            "biological difference between samples (a different patient, treatment, or tissue "
            "state) would produce exactly this picture too."
        )
    elif pairs:
        paras.append(
            "This sample-linked difference did not repeat across other cell types, so it looks "
            "localized to a few populations rather than being a dataset-wide pattern."
        )
    paras.append(
        f"On the study design, {DESIGN_PLAIN.get(design_interpretation, design_interpretation)}. "
        f"In short: {GENE_EVIDENCE_PLAIN.get(gene_evidence, gene_evidence)}; and "
        f"{VERDICT_PLAIN.get(recommendation, recommendation)}."
    )
    paras.append(
        "**What we suggest.** "
        + SUGGESTION_PLAIN.get(
            recommendation,
            "Weigh the gene evidence and the study design before deciding whether to integrate.",
        )
    )
    paras.append(
        "One caveat on reading the tables: the q-values rank how cleanly cells separate, not how "
        "reproducible a difference is across samples — cells are not independent replicates. Weigh "
        "the expression effect, the percent of cells expressing each gene, whether the pattern "
        "recurs, and the study design, rather than the q-values alone. Do not assume a disease, "
        "tissue, or replicate structure that the metadata does not state."
    )
    return "\n\n".join(paras)


def build_terminal_summary(
    *,
    batch_key: str,
    n_regions: int,
    supported_pairs: list[dict[str, Any]],
    recurring: list[dict[str, Any]],
    gene_evidence: str,
    design_interpretation: str,
    recommendation: str,
    concordance: dict[str, Any],
    mixing: dict[str, Any] | None,
) -> str:
    """Compact legacy-shaped reasoning surface for the next model step.

    Full tables and the long explanation remain artifacts. The inline result names the decisive
    observations and their logic so the model can reason from evidence without rereading a large
    deterministic essay into every reconstructed context.
    """

    lines = [
        f"Batch investigation complete for `{batch_key}`.",
        "",
        f"Verdict: {GENE_EVIDENCE_PLAIN.get(gene_evidence, gene_evidence)}; "
        f"{DESIGN_PLAIN.get(design_interpretation, design_interpretation)}.",
        "",
        "Evidence:",
        f"- {n_regions} sample-enriched cluster regions; {len(supported_pairs)} cross-sample "
        "pairs share a within-sample discriminating identity signature.",
    ]
    if supported_pairs:
        top = max(
            supported_pairs,
            key=lambda pair: (
                float(pair.get("profile_correlation", 0.0)),
                float(pair.get("signature_similarity", 0.0)),
                len(pair.get("shared_genes", [])),
            ),
        )
        shared = ", ".join(top.get("shared_genes", [])[:8]) or "none listed"
        lines.append(
            f"- Strongest identity pair: cluster {top['cluster_a']} in {top['batch_a']} vs "
            f"cluster {top['cluster_b']} in {top['batch_b']}; signature similarity "
            f"{float(top.get('signature_similarity', 0.0)):.2f} after profile correlation "
            f"{float(top.get('profile_correlation', 0.0)):.2f}; shared genes: {shared}. "
            "Because each signature was derived against the rest of its own sample, this is "
            "evidence that sample-associated clusters may represent the same population split "
            "across samples."
        )
    if recurring:
        top_programs = ", ".join(
            f"{row['gene']} higher in {row['higher_in_batch']} across {row['n_populations']} "
            "populations"
            for row in recurring[:5]
        )
        lines.append(f"- Recurring sample-associated programs: {top_programs}.")
    else:
        lines.append("- No gene shift recurred across two or more matched populations.")
    lines.append(
        f"- Cluster/sample agreement: ARI {concordance.get('ari')}, NMI "
        f"{concordance.get('nmi')} (separation location only, not cause)."
    )
    if isinstance(mixing, dict) and mixing.get("status") == "complete":
        lines.append(
            "- Mean same-sample neighbor fraction "
            f"{float(mixing.get('mean_same_batch_neighbor_fraction', 0.0)):.1%} vs "
            f"{float(mixing.get('random_composition_same_batch_fraction', 0.0)):.1%} expected "
            "from composition (advisory only)."
        )
    # Decision framing mirrors the legacy post-investigation checkpoint, but the recommendation is
    # fixed by the gene evidence, not by anything the caller passed. When the same cell type is
    # split by sample across the dataset, integration is the recommended default; otherwise
    # keep-uncorrected is. Either way the tool NEVER records the decision — the user does. The pause
    # is deliberately emphatic because a prior run auto-recorded a decision the user never made.
    if gene_evidence == "recurring_sample_associated":
        checkpoint = (
            "Decision checkpoint — STOP HERE, DO NOT DECIDE YOURSELF. Explain to the user: the "
            "strongest cross-sample identity pair, the recurring-program evidence, and the one "
            "thing the genes cannot settle — whether this per-sample difference is a technical "
            "batch effect or the biology they want to study. The same populations appear split by "
            "sample across the dataset, so present **integrate with scVI as the recommended "
            "default**. Then offer these choices, in this order, and WAIT for the user's answer: "
            "(1) integrate with scVI (recommended), (2) keep the uncorrected representation, "
            "(3) analyze the samples separately, (4) describe the experiment setup so we can "
            "understand it better. Do NOT call decide_batch_handling, and do NOT start "
            "integration, until the user has chosen — recording a decision the user did not make "
            "is a bug. END YOUR TURN now."
        )
    else:
        checkpoint = (
            "Decision checkpoint — STOP HERE, DO NOT DECIDE YOURSELF. The gene evidence does not "
            "support a dataset-wide correction, so present **keep the uncorrected representation "
            "as the recommended default**. Then offer, in this order, and WAIT for the user's "
            "answer: (1) keep the uncorrected representation (recommended), (2) integrate with "
            "scVI, (3) analyze the samples separately, (4) describe the experiment setup so we can "
            "understand it better. Do NOT call decide_batch_handling until the user has chosen. "
            "END YOUR TURN now."
        )
    lines.extend(
        [
            "",
            f"Recommendation: {SUGGESTION_PLAIN.get(recommendation, recommendation)}",
            "",
            checkpoint,
        ]
    )
    return "\n".join(lines)


LEGEND_CARDINALITY_LIMIT = 12


def _figure_layout(n_batches: int, n_clusters: int) -> dict[str, Any]:
    """Choose a readable composition figure for the batch cardinality (advisory only)."""
    if n_batches > LEGEND_CARDINALITY_LIMIT:
        width = float(max(6.0, min(0.35 * n_batches + 3.0, 26.0)))
        height = float(max(4.0, min(0.30 * n_clusters + 2.0, 22.0)))
        return {
            "mode": "heatmap",
            "figsize": (width, height),
            "legend": "colorbar",
            "annotate": n_batches <= 30 and n_clusters <= 30,
            "tick_fontsize": 7 if max(n_batches, n_clusters) > 20 else 8,
        }
    width = float(max(7.0, min(0.5 * n_clusters + 2.0, 26.0)))
    return {
        "mode": "bar",
        "figsize": (width, 5.0),
        "legend": "outside",
        "legend_ncol": 1 if n_batches <= 8 else 2,
        "rotate_xticks": n_clusters > 12,
    }


def _cramers_v(table: Any) -> float:
    import numpy as np
    from scipy.stats import chi2_contingency

    values = np.asarray(table, dtype=float)
    n = values.sum()
    if n <= 0 or min(values.shape) <= 1:
        return 0.0
    chi2 = chi2_contingency(values, correction=False)[0]
    phi2 = chi2 / n
    rows, cols = values.shape
    corrected = max(0.0, phi2 - ((cols - 1) * (rows - 1)) / max(n - 1, 1))
    rows_corrected = rows - ((rows - 1) ** 2) / max(n - 1, 1)
    cols_corrected = cols - ((cols - 1) ** 2) / max(n - 1, 1)
    denominator = min(cols_corrected - 1, rows_corrected - 1)
    return float(math.sqrt(corrected / denominator)) if denominator > 0 else 0.0


def _resolve_input_identities(
    provenance: Any, adata: Any, cluster_key: str
) -> dict[str, str]:
    """Resolve portable identities from the artifact without consulting session state."""

    provenance = provenance if isinstance(provenance, dict) else {}
    cell_set_id = provenance.get("cell_set_id")
    if not isinstance(cell_set_id, str):
        cell_set_id = _identity("cells", sorted(map(str, adata.obs_names)))
    count_representation_id = provenance.get("count_representation_id")
    if not isinstance(count_representation_id, str):
        count_representation_id = _identity(
            "count-representation",
            {
                "cell_set_id": cell_set_id,
                "count_matrix_id": provenance.get("count_matrix_id"),
                "genes": list(map(str, adata.var_names)),
            },
        )
    representation_id = provenance.get("representation_id")
    if not isinstance(representation_id, str):
        representation_id = _identity(
            "representation",
            {
                "cell_set_id": cell_set_id,
                "shape": [int(adata.n_obs), int(adata.n_vars)],
                "source": "X",
            },
        )
    clustering_id = provenance.get("clustering_id")
    if not isinstance(clustering_id, str):
        clustering_id = _identity(
            "clustering",
            {
                "cell_set_id": cell_set_id,
                "key": cluster_key,
                "assignments": list(
                    zip(
                        map(str, adata.obs_names),
                        map(str, adata.obs[cluster_key]),
                        strict=True,
                    )
                ),
            },
        )
    return {
        "cell_set_id": cell_set_id,
        "count_representation_id": count_representation_id,
        "representation_id": representation_id,
        "clustering_id": clustering_id,
    }


def _analysis_patch(
    identities: dict[str, str], adata: Any, cluster_key: str
) -> dict[str, Any]:
    """Re-assert the identities this evidence is bound to.

    Deliberately does not touch ``dataset_revision.prepared_path``. Batch investigation reads a
    matrix and writes none, so asserting a head would let a read-only tool redirect the analysis to
    whatever file it happened to be handed -- including a stale one.
    """

    return {
        "cell_set": {"id": identities["cell_set_id"], "n_cells": int(adata.n_obs)},
        "count_representation": {"id": identities["count_representation_id"]},
        "representation": {"id": identities["representation_id"]},
        "clustering": {
            "id": identities["clustering_id"],
            "key": cluster_key,
            "n_clusters": int(adata.obs[cluster_key].astype(str).nunique()),
        },
    }


def _region_markers(
    sub: Any,
    target_mask: Any,
    *,
    n_top: int,
    min_lfc: float,
    max_padj: float,
    min_frac_diff: float,
    sc: Any,
    np: Any,
) -> dict[str, Any]:
    """One-vs-rest positive Wilcoxon markers for a region within its own batch subset.

    Returns full per-gene records (effect, q-value, target/reference fractions) plus the
    discriminating-only identity gene list used for cross-sample matching.
    """
    n_target = int(target_mask.sum())
    n_rest = int((~target_mask).sum())
    if n_target < 3 or n_rest < 3:
        return {"discriminating_genes": [], "records": [], "n_target": n_target, "n_rest": n_rest}
    sub = sub.copy()
    # Identity DEG is about what the cell type IS: genes seen in only a handful of cells cannot be
    # identity markers and only slow the Wilcoxon test. Dropping them mirrors legacy (which filtered
    # low-detection genes before DE) and typically halves the gene count on sparse data. The
    # direct cross-sample comparison deliberately keeps ALL genes and is untouched by this.
    sc.pp.filter_genes(sub, min_cells=3)
    sub.obs["_grp"] = np.where(target_mask, "target", "rest")
    sc.tl.rank_genes_groups(
        sub, "_grp", groups=["target"], reference="rest", method="wilcoxon", pts=True
    )
    frame = sc.get.rank_genes_groups_df(sub, group="target")
    has_pts = "pct_nz_group" in frame.columns and "pct_nz_reference" in frame.columns
    positive = frame[(frame["logfoldchanges"] >= min_lfc) & (frame["pvals_adj"] <= max_padj)]
    positive = positive.sort_values("scores", ascending=False)
    records: list[dict[str, Any]] = []
    discriminating: list[str] = []
    for _, row in positive.head(max(n_top * 3, 60)).iterrows():
        gene = str(row["names"])
        frac_diff = float(row["pct_nz_group"] - row["pct_nz_reference"]) if has_pts else None
        record = {
            "gene": gene,
            "gene_class": gene_class(gene),
            "logfoldchange": float(row["logfoldchanges"]),
            "score": float(row["scores"]),
            "pvals_adj": float(row["pvals_adj"]),
            "pct_target": float(row["pct_nz_group"]) if has_pts else None,
            "pct_reference": float(row["pct_nz_reference"]) if has_pts else None,
        }
        records.append(record)
        if record["gene_class"] == "discriminating" and (
            frac_diff is None or frac_diff >= min_frac_diff
        ):
            discriminating.append(gene)
    return {
        "discriminating_genes": discriminating[:n_top],
        "records": records[: n_top * 2],
        "n_target": n_target,
        "n_rest": n_rest,
    }


def _mean_vector(matrix: Any) -> Any:
    import numpy as np

    value = matrix.mean(axis=0)
    return np.asarray(value.A1 if hasattr(value, "A1") else value).ravel()


def _region_mean_profiles(
    adata: Any,
    regions: list[dict[str, Any]],
    batch: Any,
    cluster: Any,
    np: Any,
) -> tuple[list[tuple[str, str]], Any]:
    """Return one cheap whole-transcriptome mean profile per enriched region."""
    keys: list[tuple[str, str]] = []
    profiles: list[Any] = []
    batch_values = batch.to_numpy()
    cluster_values = cluster.to_numpy()
    for region in regions:
        mask = (batch_values == region["batch"]) & (cluster_values == region["cluster"])
        if not bool(mask.any()):
            continue
        keys.append((region["cluster"], region["batch"]))
        profiles.append(_mean_vector(adata.X[mask]))
    matrix = np.vstack(profiles) if profiles else np.zeros((0, adata.n_vars), dtype=float)
    return keys, matrix


def _region_recurrence_rows(
    adata: Any,
    regions: list[dict[str, Any]],
    batch: Any,
    cluster: Any,
    *,
    min_cells: int,
    min_effect: float,
    top_n: int,
    np: Any,
) -> list[dict[str, Any]]:
    """Cheap deterministic recurrence scan over co-clustered sample-enriched regions.

    Holding cluster constant, rank genes by the mean-expression shift for one sample versus all
    other samples. This preserves legacy's second recurrence geometry without launching one full
    Scanpy Wilcoxon job per enriched region. Direct Wilcoxon tests remain the inferential support
    for the three confirmed split-population pairs.
    """
    rows: list[dict[str, Any]] = []
    batch_values = batch.to_numpy()
    cluster_values = cluster.to_numpy()
    genes = np.asarray(adata.var_names.astype(str))
    for region in sorted(regions, key=lambda r: (r["cluster"], r["batch"])):
        in_cluster = cluster_values == region["cluster"]
        target = in_cluster & (batch_values == region["batch"])
        reference = in_cluster & (batch_values != region["batch"])
        if int(target.sum()) < min_cells or int(reference.sum()) < min_cells:
            continue
        effect = _mean_vector(adata.X[target]) - _mean_vector(adata.X[reference])
        eligible = np.flatnonzero(effect >= min_effect)
        if eligible.size == 0:
            continue
        ranked = eligible[np.argsort(effect[eligible])[::-1][:top_n]]
        for position in ranked:
            gene = str(genes[position])
            rows.append(
                {
                    "population": f"cluster:{region['cluster']}",
                    "gene": gene,
                    "gene_class": gene_class(gene),
                    "higher_in_batch": region["batch"],
                    "logfoldchange": float(effect[position]),
                    "source": "within_cluster_mean_shift",
                }
            )
    return rows


def compact_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Keep only the decision-ready evidence pointer in durable state."""

    keys = (
        "schema_version",
        "status",
        "evidence_id",
        "batch_key",
        "cluster_key",
        "gene_evidence",
        "design_interpretation",
        "recommendation",
        "artifact_path",
        "cell_set_id",
        "count_representation_id",
    )
    return {key: evidence[key] for key in keys if key in evidence}


def run_evidence(arguments: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: C901
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    batch_key = arguments.get("batch_key")
    cluster_key = str(arguments.get("cluster_key", "leiden"))
    min_cells_region = int(arguments.get("min_cells_per_region", 30))
    min_enrichment = float(arguments.get("min_enrichment", 2.0))
    n_identity_genes = int(arguments.get("n_identity_genes", 50))
    max_regions = int(arguments.get("max_regions", 40))
    max_candidate_pairs = int(arguments.get("max_candidate_pairs", 3))
    max_match_attempts = int(arguments.get("max_match_attempts", 10))
    min_profile_correlation = float(arguments.get("min_profile_correlation", 0.4))
    n_profile_genes = int(arguments.get("n_profile_genes", 2000))
    min_shared = int(arguments.get("min_shared_identity_genes", 5))
    min_jaccard = float(arguments.get("min_match_jaccard", 0.0))
    n_neighbors = int(arguments.get("n_neighbors_for_mixing", 30))
    max_cells = int(arguments.get("max_cells_for_mixing", 20000))
    min_lfc = float(arguments.get("min_logfoldchange", 0.5))
    max_padj = float(arguments.get("max_adjusted_pvalue", 0.05))
    min_frac_diff = float(arguments.get("min_fraction_difference", 0.1))
    seed = int(arguments.get("random_seed", 0))
    effective_parameters = {
        "cluster_key": cluster_key,
        "min_cells_per_region": min_cells_region,
        "min_enrichment": min_enrichment,
        "n_identity_genes": n_identity_genes,
        "max_regions": max_regions,
        "max_candidate_pairs": max_candidate_pairs,
        "max_match_attempts": max_match_attempts,
        "min_profile_correlation": min_profile_correlation,
        "n_profile_genes": n_profile_genes,
        "min_shared_identity_genes": min_shared,
        "min_match_jaccard": min_jaccard,
        "min_logfoldchange": min_lfc,
        "max_adjusted_pvalue": max_padj,
        "min_fraction_difference": min_frac_diff,
        "random_seed": seed,
    }

    if not path.exists():
        raise FileNotFoundError(path)
    adata = _read_matrix(path)
    if cluster_key not in adata.obs:
        raise ValueError(f"observation column {cluster_key!r} is absent")
    ident = _resolve_input_identities(adata.uns.get("scagent_sdk", {}), adata, cluster_key)
    analysis_patch = _analysis_patch(ident, adata, cluster_key)

    if batch_key is None:
        evidence = {
            "schema_version": BATCH_EVIDENCE_SCHEMA,
            "status": "not_applicable",
            "batch_key": None,
            "gene_evidence": "none",
            "design_interpretation": "unknown",
            "recommendation": "not_applicable",
            "de_engine": DE_ENGINE,
            "effective_parameters": effective_parameters,
            **ident,
        }
        evidence["evidence_id"] = _identity("batch-evidence", evidence)
        evidence_fact = compact_evidence(evidence)
        (context.staging_dir / "batch-investigation.md").write_text(
            "# Batch investigation\n\nNo meaningful batch key was selected; recorded "
            "`not_applicable` bound to the current analysis identities.\n",
            encoding="utf-8",
        )
        return {
            "summary": "Recorded not-applicable batch evidence for the input artifact identities.",
            "details": evidence_fact,
            "facts_patch": {
                "analysis": analysis_patch,
                "batch": {"evidence": evidence_fact, "decision": None},
            },
            "artifacts": [
                {
                    "name": "batch-report",
                    "relative_path": "batch-investigation.md",
                    "media_type": "text/markdown",
                }
            ],
        }

    batch_key = str(batch_key)
    if batch_key not in adata.obs:
        raise ValueError(f"observation column {batch_key!r} is absent")
    if bool(adata.obs[batch_key].isna().any()):
        raise ValueError(f"batch column {batch_key!r} contains missing values")
    batch = adata.obs[batch_key].astype(str)
    cluster = adata.obs[cluster_key].astype(str)
    if batch.nunique() < 2:
        raise ValueError("batch investigation requires at least two observed batch levels")

    # --- advisory context ---------------------------------------------------
    table = pd.crosstab(cluster, batch)
    proportions = table.div(table.sum(axis=1), axis=0)
    association = _cramers_v(table)
    table.to_csv(context.staging_dir / "batch-cluster-counts.csv")
    proportions.to_csv(context.staging_dir / "batch-cluster-proportions.csv")
    n_batches = int(proportions.shape[1])
    layout = _figure_layout(n_batches, int(proportions.shape[0]))
    if layout["mode"] == "heatmap":
        fig, ax = plt.subplots(figsize=layout["figsize"])
        image = ax.imshow(proportions.to_numpy(), aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0)
        ax.set_xticks(range(n_batches))
        ax.set_xticklabels(
            list(map(str, proportions.columns)), rotation=90, fontsize=layout["tick_fontsize"]
        )
        ax.set_yticks(range(int(proportions.shape[0])))
        ax.set_yticklabels(list(map(str, proportions.index)), fontsize=layout["tick_fontsize"])
        ax.set_title(f"{batch_key} fraction within each {cluster_key} ({n_batches} batches)")
        fig.colorbar(image, ax=ax, label="fraction within cluster")
    else:
        ax = proportions.plot(kind="bar", stacked=True, figsize=layout["figsize"])
        ax.set_ylabel("fraction within cluster")
        ax.set_title(f"{batch_key} composition by {cluster_key}")
        ax.legend(
            title=str(batch_key),
            bbox_to_anchor=(1.02, 1.0),
            loc="upper left",
            ncol=layout["legend_ncol"],
            fontsize=8,
            frameon=False,
        )
        if layout["rotate_xticks"]:
            plt.setp(ax.get_xticklabels(), rotation=90, fontsize=8)
    plt.tight_layout()
    plt.savefig(context.staging_dir / "batch-composition.png", dpi=160, bbox_inches="tight")
    plt.close("all")

    mixing: dict[str, Any] = {"status": "unavailable", "reason": "X_pca is absent"}
    if "X_pca" in adata.obsm:
        from sklearn.neighbors import NearestNeighbors

        rng = np.random.default_rng(seed)
        indices = np.arange(adata.n_obs)
        if indices.size > max_cells:
            indices = np.sort(rng.choice(indices, size=max_cells, replace=False))
        embedding = np.asarray(adata.obsm["X_pca"])[indices]
        sampled_batch = batch.iloc[indices].to_numpy()
        used = min(n_neighbors, len(indices) - 1)
        nn = NearestNeighbors(n_neighbors=used + 1).fit(embedding)
        neighbor_idx = nn.kneighbors(return_distance=False)[:, 1:]
        neighbor_batches = sampled_batch[neighbor_idx]
        same = (neighbor_batches == sampled_batch[:, None]).mean(axis=1)
        # Vectorized normalized neighborhood entropy: map labels to codes once, count per row with
        # a small loop over batch levels (not a pandas Series per cell), then Shannon-normalize.
        levels = sorted(map(str, batch.unique()))
        code = {lv: i for i, lv in enumerate(levels)}
        sampled_codes = np.array([code[str(b)] for b in sampled_batch])
        nb_codes = sampled_codes[neighbor_idx]
        counts = np.stack([(nb_codes == c).sum(axis=1) for c in range(len(levels))], axis=1)
        probs = counts / counts.sum(axis=1, keepdims=True)
        with np.errstate(divide="ignore", invalid="ignore"):
            terms = np.where(probs > 0, probs * np.log(probs), 0.0)
        norm = math.log(len(levels)) if len(levels) > 1 else 1.0
        entropies = -terms.sum(axis=1) / norm
        freq = batch.value_counts(normalize=True)
        mixing = {
            "status": "complete",
            "representation": "X_pca",
            "mean_same_batch_neighbor_fraction": float(same.mean()),
            "random_composition_same_batch_fraction": float((freq**2).sum()),
            "mean_normalized_batch_entropy": float(np.mean(entropies)),
            "caution": "Descriptive mixing; not an integration objective.",
        }

    # Advisory global concordance (ARI + NMI) — legacy-style "where do samples separate" signal.
    # Necessary-but-not-sufficient: never drives the verdict, only frames the plain-language report.
    concordance = cluster_batch_concordance(
        batch.astype(str).to_numpy(), cluster.astype(str).to_numpy()
    )

    # --- stage 1: sample-enriched regions -----------------------------------
    global_freq = batch.value_counts(normalize=True).to_dict()
    cluster_sizes = cluster.value_counts().to_dict()
    regions: list[dict[str, Any]] = []
    for cl in table.index:
        for bt in table.columns:
            n_region = int(table.loc[cl, bt])
            if n_region < min_cells_region:
                continue
            enr = region_enrichment(
                n_region, int(cluster_sizes[str(cl)]), float(global_freq.get(str(bt), 0.0))
            )
            if enr >= min_enrichment:
                regions.append(
                    {"cluster": str(cl), "batch": str(bt), "n_cells": n_region, "enrichment": enr}
                )
    regions.sort(key=lambda r: r["enrichment"] * r["n_cells"], reverse=True)
    regions = regions[:max_regions]
    pd.DataFrame(
        [
            {
                "cluster": r["cluster"],
                "batch": r["batch"],
                "n_cells_in_region": r["n_cells"],
                "n_cells_in_cluster": int(cluster_sizes[r["cluster"]]),
                "batch_global_fraction": float(global_freq.get(r["batch"], 0.0)),
                "fraction_within_cluster": r["n_cells"] / int(cluster_sizes[r["cluster"]]),
                "enrichment_over_baseline": r["enrichment"],
            }
            for r in regions
        ]
    ).to_csv(context.staging_dir / "sample-enriched-regions.csv", index=False)

    # --- stage 2: cheap candidate nomination, then lazy identity DEGs --------
    # Legacy's key performance property is that DEG is confirmation, not search. Whole-expression
    # region profiles nominate likely same-population pairs in milliseconds; expensive Wilcoxon
    # runs are then bounded by ``max_match_attempts`` and stop after enough confirmed pairs.
    region_keys, profiles = _region_mean_profiles(adata, regions, batch, cluster, np)
    region_by_key = {(region["cluster"], region["batch"]): region for region in regions}
    candidates = nominate_cross_sample_pairs(
        region_keys,
        profiles,
        list(map(str, adata.var_names)),
        min_corr=min_profile_correlation,
        n_top_variable=n_profile_genes,
    )
    candidate_correlations = {
        (int(candidate["index_a"]), int(candidate["index_b"])): float(
            candidate["profile_correlation"]
        )
        for candidate in candidates
    }
    within_rows: list[dict[str, Any]] = []
    marker_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def markers_for(key: tuple[str, str]) -> dict[str, Any]:
        if key in marker_cache:
            return marker_cache[key]
        region = region_by_key[key]
        sub = adata[batch.to_numpy() == region["batch"]]
        target = cluster[batch == region["batch"]].to_numpy() == region["cluster"]
        markers = _region_markers(
            sub,
            target,
            n_top=n_identity_genes,
            min_lfc=min_lfc,
            max_padj=max_padj,
            min_frac_diff=min_frac_diff,
            sc=sc,
            np=np,
        )
        region["discriminating_genes"] = markers["discriminating_genes"]
        for record in markers["records"]:
            within_rows.append({"cluster": region["cluster"], "batch": region["batch"], **record})
        marker_cache[key] = markers
        return markers

    match_rows: list[dict[str, Any]] = []
    supported_edges: list[tuple[int, int, int, float]] = []
    selected_regions: set[tuple[str, str]] = set()
    attempts = 0
    for candidate in candidates:
        if len(supported_edges) >= max_candidate_pairs or attempts >= max_match_attempts:
            break
        i, j = int(candidate["index_a"]), int(candidate["index_b"])
        key_a, key_b = region_keys[i], region_keys[j]
        if key_a in selected_regions and key_b in selected_regions:
            continue
        attempts += 1
        markers_a = markers_for(key_a)
        markers_b = markers_for(key_b)
        match = match_regions(
            markers_a["discriminating_genes"],
            markers_b["discriminating_genes"],
            min_shared=min_shared,
            min_jaccard=min_jaccard,
        )
        match_rows.append(
            {
                "cluster_a": key_a[0],
                "batch_a": key_a[1],
                "cluster_b": key_b[0],
                "batch_b": key_b[1],
                "profile_correlation": round(candidate["profile_correlation"], 3),
                "shared_discriminating": match["shared"],
                "jaccard": round(match["jaccard"], 3),
                "signature_similarity": round(match["jaccard"], 3),
                "shared_genes": ";".join(match["shared_genes"][:15]),
                "identity_match_supported": match["supported"],
                "rejection_reason": "" if match["supported"] else match["reason"],
            }
        )
        if match["supported"]:
            supported_edges.append((i, j, match["shared"], match["jaccard"]))
            selected_regions.update((key_a, key_b))

    pd.DataFrame(within_rows).to_csv(
        context.staging_dir / "within-sample-identity-degs.csv", index=False
    )
    pd.DataFrame(match_rows).to_csv(context.staging_dir / "population-matches.csv", index=False)
    de_edges = [(i, j) for (i, j, _shared, _similarity) in supported_edges]

    # --- stage 4 + 5: direct matched comparison and recurrence --------------
    direct_rows: list[dict[str, Any]] = []
    for i, j in de_edges:
        ri, rj = region_by_key[region_keys[i]], region_by_key[region_keys[j]]
        mask = ((cluster == ri["cluster"]) & (batch == ri["batch"])) | (
            (cluster == rj["cluster"]) & (batch == rj["batch"])
        )
        pair = adata[mask.to_numpy()].copy()
        is_a = (cluster[mask] == ri["cluster"]).to_numpy() & (batch[mask] == ri["batch"]).to_numpy()
        pair.obs["_pair"] = np.where(is_a, "A", "B")
        if int(is_a.sum()) < 3 or int((~is_a).sum()) < 3:
            continue
        sc.tl.rank_genes_groups(
            pair, "_pair", groups=["A"], reference="B", method="wilcoxon", pts=True
        )
        frame = sc.get.rank_genes_groups_df(pair, group="A")
        # Compute detection fractions directly: scanpy omits ``pct_nz_reference`` when the
        # reference is a named group, and these fractions are the noise gate below.
        matrix = pair.X
        matrix = matrix.toarray() if hasattr(matrix, "toarray") else np.asarray(matrix)
        pct_a_all = (matrix[is_a] > 0).mean(axis=0)
        pct_b_all = (matrix[~is_a] > 0).mean(axis=0)
        gene_pos = {str(name): idx for idx, name in enumerate(pair.var_names)}
        sig = frame[frame["pvals_adj"] <= max_padj]
        for _, row in sig.iterrows():
            lfc = float(row["logfoldchanges"])
            if lfc >= min_lfc:
                higher = ri["batch"]
                population = f"cluster:{ri['cluster']}"
            elif lfc <= -min_lfc:
                higher = rj["batch"]
                population = f"cluster:{rj['cluster']}"
            else:
                continue
            # Detection fractions are recorded so the model can judge effect magnitude. NOTE: a
            # naive detection-fraction gate over-filters real programs on this data, so recurrence
            # is currently NOT noise-gated -- cell-level q-values rank separation and are not
            # sample-level replication, so low-expression genes can recur by chance. Weigh
            # pct_a/pct_b and effect size when reading recurring-programs.csv.
            position = gene_pos.get(str(row["names"]))
            if position is None:
                continue
            pct_a = float(pct_a_all[position])
            pct_b = float(pct_b_all[position])
            direct_rows.append(
                {
                    # Count an actual cell population, not a synthetic connected component or
                    # pair id. This also deduplicates a direct comparison against the same cluster
                    # observed by the co-clustered recurrence scan.
                    "population": population,
                    "gene": str(row["names"]),
                    "gene_class": gene_class(str(row["names"])),
                    "higher_in_batch": higher,
                    "logfoldchange": lfc,
                    "score": float(row["scores"]),
                    "pvals_adj": float(row["pvals_adj"]),
                    "pct_a": pct_a,
                    "pct_b": pct_b,
                    "cluster_a": ri["cluster"],
                    "batch_a": ri["batch"],
                    "cluster_b": rj["cluster"],
                    "batch_b": rj["batch"],
                }
            )
    pd.DataFrame(direct_rows).to_csv(
        context.staging_dir / "direct-matched-region-degs.csv", index=False
    )
    recurrence_rows = _region_recurrence_rows(
        adata,
        regions,
        batch,
        cluster,
        min_cells=min_cells_region,
        min_effect=min_lfc,
        top_n=25,
        np=np,
    )
    recurring = summarize_recurrence(recurrence_rows + direct_rows)
    pd.DataFrame(recurring).to_csv(context.staging_dir / "recurring-programs.csv", index=False)

    # Grounded matched-pair detail for the plain-language narrative (top few actually compared).
    direct_by_pair: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for r in direct_rows:
        direct_by_pair.setdefault(
            (r["cluster_a"], r["batch_a"], r["cluster_b"], r["batch_b"]), []
        ).append(r)

    def _side_genes(rows: list[dict[str, Any]], side_batch: str) -> list[str]:
        picked = [x for x in rows if x["higher_in_batch"] == side_batch]
        # Discriminating genes first, then by effect magnitude, so the narrative names real markers.
        picked.sort(key=lambda x: (x["gene_class"] != "discriminating", -abs(x["logfoldchange"])))
        return [x["gene"] for x in picked]

    pairs_detail: list[dict[str, Any]] = []
    for i, j in de_edges[:5]:
        ri, rj = regions[i], regions[j]
        shared = sorted(
            {str(g).upper() for g in ri["discriminating_genes"]}
            & {str(g).upper() for g in rj["discriminating_genes"]}
        )
        rows = direct_by_pair.get((ri["cluster"], ri["batch"], rj["cluster"], rj["batch"]), [])
        pairs_detail.append(
            {
                "cluster_a": ri["cluster"],
                "batch_a": ri["batch"],
                "cluster_b": rj["cluster"],
                "batch_b": rj["batch"],
                "shared_genes": shared,
                "signature_similarity": (
                    len(shared)
                    / len(
                        {str(g).upper() for g in ri["discriminating_genes"]}
                        | {str(g).upper() for g in rj["discriminating_genes"]}
                    )
                    if ri["discriminating_genes"] or rj["discriminating_genes"]
                    else 0.0
                ),
                "profile_correlation": candidate_correlations.get((i, j)),
                "higher_in_a": _side_genes(rows, ri["batch"]),
                "higher_in_b": _side_genes(rows, rj["batch"]),
            }
        )
    recurring_by_sample: dict[str, list[str]] = {}
    for r in recurring:
        recurring_by_sample.setdefault(str(r["higher_in_batch"]), []).append(str(r["gene"]))

    # --- stage 6: verdict ---------------------------------------------------
    # The recommendation is driven ONLY by the gene evidence. The tool deliberately takes no design
    # or condition inputs from the caller: whether a sample-linked split is a technical batch or the
    # biology of interest is an experimental-design question the user answers, not a lever the agent
    # can pull to steer the verdict. ``design_interpretation`` stays a fixed ``unknown`` so the
    # design caveat is always stated honestly beside the recommendation.
    n_matched_with_diffs = len({r["population"] for r in direct_rows})
    n_recurring_populations = max((r["n_populations"] for r in recurring), default=0)
    gene_evidence = classify_gene_evidence(n_matched_with_diffs, n_recurring_populations)
    design_interpretation = "unknown"
    recommendation = recommend(gene_evidence)

    plain_interpretation = build_plain_interpretation(
        batch_key=batch_key,
        n_regions=len(regions),
        pairs=pairs_detail,
        recurring_by_sample=recurring_by_sample,
        gene_evidence=gene_evidence,
        design_interpretation=design_interpretation,
        recommendation=recommendation,
        concordance=concordance,
        mixing=mixing,
    )

    supported_pair_details: list[dict[str, Any]] = []
    for i, j, _shared_count, similarity in supported_edges:
        ri, rj = regions[i], regions[j]
        shared_genes = sorted(
            {str(g).upper() for g in ri["discriminating_genes"]}
            & {str(g).upper() for g in rj["discriminating_genes"]}
        )
        supported_pair_details.append(
            {
                "cluster_a": ri["cluster"],
                "batch_a": ri["batch"],
                "cluster_b": rj["cluster"],
                "batch_b": rj["batch"],
                "signature_similarity": similarity,
                "profile_correlation": candidate_correlations.get((i, j)),
                "shared_genes": shared_genes,
            }
        )

    terminal_summary = build_terminal_summary(
        batch_key=str(batch_key),
        n_regions=len(regions),
        supported_pairs=supported_pair_details,
        recurring=recurring,
        gene_evidence=gene_evidence,
        design_interpretation=design_interpretation,
        recommendation=recommendation,
        concordance=concordance,
        mixing=mixing,
    )

    match_summary = [
        {
            "a": list(region_keys[i]),
            "b": list(region_keys[j]),
            "shared": shared,
            "signature_similarity": similarity,
            "profile_correlation": candidate_correlations.get((i, j)),
        }
        for (i, j, shared, similarity) in supported_edges
    ]
    evidence = {
        "schema_version": BATCH_EVIDENCE_SCHEMA,
        "status": "complete",
        "batch_key": batch_key,
        "cluster_key": cluster_key,
        "n_batches": int(batch.nunique()),
        "n_clusters": int(cluster.nunique()),
        "gene_evidence": gene_evidence,
        "design_interpretation": design_interpretation,
        "recommendation": recommendation,
        "n_enriched_regions": len(regions),
        "n_profile_candidates": len(candidates),
        "n_match_attempts": attempts,
        "n_supported_matches": len(supported_edges),
        "supported_identity_pairs": supported_pair_details[:20],
        "n_direct_compared_pairs": len(de_edges),
        "n_recurring_programs": len(recurring),
        "recurring_programs": recurring[:20],
        "de_engine": DE_ENGINE,
        "gene_class_version": GENE_CLASS_VERSION,
        "plain_interpretation": plain_interpretation,
        "terminal_summary": terminal_summary,
        "advisory": {"concordance": concordance, "cramers_v": association, "mixing": mixing},
        "figure_mode": layout["mode"],
        "effective_parameters": effective_parameters,
        "artifact_path": f"{context.artifact_relative_path}/batch-evidence.json",
        **ident,
    }
    evidence["evidence_id"] = _identity(
        "batch-evidence",
        {
            "schema": BATCH_EVIDENCE_SCHEMA,
            "gene_evidence": gene_evidence,
            "design_interpretation": design_interpretation,
            "recommendation": recommendation,
            "batch_key": batch_key,
            "identities": {k: ident[k] for k in ident},
            "effective_parameters": effective_parameters,
            "regions": region_keys,
            "matches": match_summary,
            "recurring": [(r["gene"], r["higher_in_batch"], r["n_populations"]) for r in recurring],
            "gene_class_version": GENE_CLASS_VERSION,
        },
    )
    (context.staging_dir / "batch-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    _write_evidence_report(context.staging_dir / "batch-investigation.md", evidence)
    evidence_fact = compact_evidence(evidence)

    artifacts = [
        {
            "name": "batch-report",
            "relative_path": "batch-investigation.md",
            "media_type": "text/markdown",
        },
        {
            "name": "batch-evidence",
            "relative_path": "batch-evidence.json",
            "media_type": "application/json",
        },
        {
            "name": "batch-cluster-counts",
            "relative_path": "batch-cluster-counts.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-cluster-proportions",
            "relative_path": "batch-cluster-proportions.csv",
            "media_type": "text/csv",
        },
        {
            "name": "sample-enriched-regions",
            "relative_path": "sample-enriched-regions.csv",
            "media_type": "text/csv",
        },
        {
            "name": "within-sample-identity-degs",
            "relative_path": "within-sample-identity-degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "population-matches",
            "relative_path": "population-matches.csv",
            "media_type": "text/csv",
        },
        {
            "name": "direct-matched-region-degs",
            "relative_path": "direct-matched-region-degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "recurring-programs",
            "relative_path": "recurring-programs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-composition",
            "relative_path": "batch-composition.png",
            "media_type": "image/png",
        },
    ]
    return {
        "summary": terminal_summary,
        "details": evidence_fact,
        "facts_patch": {
            "analysis": analysis_patch,
            "batch": {"evidence": evidence_fact, "decision": None},
        },
        "artifacts": artifacts,
        "model_media": [a for a in artifacts if a["media_type"].startswith("image/")],
    }


def _write_evidence_report(path: Path, evidence: dict[str, Any]) -> None:
    """Plain-language README that reads on its own — no jargon, no assumed biology.

    Structure mirrors the legacy gene-first diagnostic: a philosophy preamble, the parameters used,
    a "What it is / How it was computed / How to read it" note per file, then a dataset-specific
    Interpretation (built deterministically from the evidence) that ends in a concrete suggestion.
    """
    p = evidence.get("effective_parameters", {})
    lines = [
        "# Batch check — are the differences between samples technical or real biology?",
        "",
        "These files investigate one question: when a dataset is made of several samples, are the "
        "differences between those samples a technical batch effect worth correcting, or real "
        "biology that should be kept? The approach is gene-first. Rather than trusting that "
        "samples separating in a plot means a batch effect (real biological differences separate "
        "too), it finds the same cell type in more than one sample and reads the actual genes to "
        "see how — and whether — that cell type differs from sample to sample. The strongest "
        "evidence is the within-sample gene lists (they describe each cell type cleanly, with the "
        "sample held constant); the direct cross-sample comparison and the recurrence check are "
        "supporting detail. One limit to keep in mind: no gene table can prove a difference is "
        "technical rather than real per-sample biology — only the experimental design can settle "
        "that. The **Interpretation** section at the end walks through what was found here.",
        "",
        "## Parameters used",
        "",
        f"- batch column `{evidence['batch_key']}`, cluster column `{evidence['cluster_key']}`",
        "- DE test: scanpy Wilcoxon (in-environment)",
        f"- a region is kept when it has at least {p.get('min_cells_per_region', 30)} cells and is "
        f"at least {p.get('min_enrichment', 2.0)}× more concentrated than the sample's own size",
        f"- a cross-sample match needs at least {p.get('min_shared_identity_genes', 3)} shared "
        f"discriminating genes and Jaccard ≥ {p.get('min_match_jaccard', 0.15)}",
        f"- at most {p.get('max_candidate_pairs', '—')} matched pairs receive a direct comparison "
        "(the strongest matches first)",
        "",
        "## Files",
        "",
        "### `sample-enriched-regions.csv` — where to look",
        "Each row is a (cluster, sample) region holding far more of one sample's cells than its "
        "overall size would predict — measured as concentration relative to expected, not raw "
        "purity, so a region that is 42% of a sample that is only 9% of the data is caught. "
        "`enrichment` = fraction-of-cluster ÷ sample's-fraction-of-dataset (2 = twice expected).",
        "",
        "### `within-sample-identity-degs.csv` — the main evidence",
        "For each region above, the genes that set that cell type apart — compared **inside one "
        "sample** (this cluster vs. the rest of that same sample). Holding the sample constant "
        "means these genes describe what the cell type IS (its identity), with no sample "
        "differences mixed in. Read the effect and percent-expressed columns; the q-value is only "
        "a ranking aid.",
        "",
        "### `population-matches.csv` — the same cell type across samples",
        "Pairs a region in one sample with a region in another that share enough "
        "**discriminating** identity genes to be treated as the same cell type. "
        "Broad/stress/housekeeping genes are "
        "ignored so two different cell types can't 'match' just because both are, say, stressed. A "
        "supported match means comparing them is meaningful — not that they are identical.",
        "",
        "### `direct-matched-region-degs.csv` — supporting detail",
        "For each matched pair, exactly how the same cell type differs between the two samples, "
        "gene by gene. ALL genes are kept here (stress, mitochondrial, ribosomal and ambient genes "
        "are "
        "often the clearest sign of a sample-linked program). It supports the picture but does not "
        "outrank the within-sample evidence, and on its own does not prove the difference is "
        "technical.",
        "",
        "### `recurring-programs.csv` — does the shift repeat?",
        "Genes that are consistently higher in the same sample across two or more different cell "
        "populations. A shift that repeats is dataset-wide rather than a one-off — but "
        "dataset-wide is still not the same as technical. This is an advisory signal from "
        "cell-level tests, not "
        "biological replication; weigh `pct_a`/`pct_b` and effect size before trusting a program.",
        "",
        "### `batch-composition.png` + `batch-cluster-*.csv` — advisory context",
        "How each sample is distributed across clusters, and the ARI/NMI agreement between "
        "clusters and samples. These tell you WHERE samples separate, never WHY (biology and a "
        "technical "
        "batch push them the same way), so they never decide anything on their own — and a high "
        "value must never be read as proof of a tumor or any particular tissue.",
        "",
        "## Interpretation (this dataset)",
        "",
        str(evidence.get("plain_interpretation", "")),
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_decision(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    evidence_id = str(arguments["evidence_id"])
    decision = str(arguments["decision"])
    rationale = str(arguments["rationale"]).strip()
    if not rationale:
        raise ValueError("rationale must not be empty")

    batch = context.state_facts.get("batch")
    evidence = batch.get("evidence") if isinstance(batch, dict) else None
    if not isinstance(evidence, dict) or evidence.get("status") not in (
        "complete",
        "not_applicable",
    ):
        raise ValueError("no current batch evidence is available; run investigate_batch first")
    if evidence.get("evidence_id") != evidence_id:
        raise ValueError("evidence_id does not match the current batch evidence")
    if evidence.get("status") == "not_applicable":
        raise ValueError("batch handling is already not applicable; no decision is needed")

    decision_fact = {
        "decision": decision,
        "rationale": rationale,
        "evidence_id": evidence_id,
    }
    (context.staging_dir / "batch-decision.md").write_text(
        f"# Batch handling decision\n\n- Decision: **{decision}**\n"
        f"- Evidence: `{evidence_id}`\n\n## Rationale\n\n{rationale}\n",
        encoding="utf-8",
    )
    return {
        "summary": f"Recorded batch decision {decision!r} against the current evidence.",
        "details": decision_fact,
        "facts_patch": {"batch": {"decision": decision_fact}},
        "decisions_patch": {"batch_handling": {"decision": decision, "rationale": rationale}},
        "artifacts": [
            {
                "name": "batch-decision",
                "relative_path": "batch-decision.md",
                "media_type": "text/markdown",
            }
        ],
    }
