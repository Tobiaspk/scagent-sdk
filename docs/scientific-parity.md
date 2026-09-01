# Scientific capability parity audit

Status: implementation guide for the scientific-capability migration phase

## Scope and evidence reviewed

This audit maps scientific responsibilities, not source code. The new project remains independent
of legacy `scagent`; paths below are read-only references and are not runtime dependencies.

Reviewed sources:

- all rules in legacy `scagent/agent/prompts.py`;
- the complete legacy tool schema plus the implementation areas selected by the schema inventory,
  including the existing `run_cellbender` path in `scagent/agent/tools.py`;
- public functions and contracts under legacy `core/`, `analysis/`, `annotation/`, and `batch/`;
- `scagent/config/defaults.py`;
- the complete legacy test inventory, with targeted inspection of QC, raw-count, loading,
  clustering, batch, annotation, DEG, environment, artifact, and recovery tests;
- the current `scagent-sdk` skill manifests, scripts, floors, state reducer, executor, environment
  broker, deterministic tests, and the PBMC first-slice evidence in `docs/handoff.md`;
- BioNeMo contributed RAPIDS-singlecell, SCimilarity, single-cell workflow, and preprocessing
  skills and their package structure;
- the locked CellBender 0.3.2 CLI/API on Iris and a prior real CellBender output group for output
  naming, metrics, logs, checkpoint, posterior, and report behavior.

The BioNeMo skills are useful packaging and communication references. Their automatic CPU fallback,
early global cell filtering, and scripted umbrella workflow are not adopted because they conflict
with this project's fail-closed environment routing, evidence-driven filtering, and model-selected
branching.

## Classification scheme

| Class | Meaning in `scagent-sdk` |
|---|---|
| Orchestration guidance | Model-facing choice, sequencing advice, narration, or interpretation in the concise orchestration skill/reference. |
| Focused-skill guidance | Method-specific assumptions, parameters, caveats, and interpretation in one skill package. |
| Deterministic validation | Identity, schema, input, computation, output, provenance, or artifact checks implemented in code. |
| State floor | A consequential action denied by an identity-aware state predicate. Floors do not select a workflow. |
| Evaluation | A model-behavior or scientific-quality expectation tested separately from deterministic code. |

## Responsibility and coverage matrix

| Legacy responsibility and evidence | Current coverage | Missing science, diagnostics, floors, or tests | Proposed focused skill | Priority / dependencies |
|---|---|---|---|---|
| File discovery, loading, H5/MTX/H5AD handling, non-overwrite, primary-input protection (`core/io.py`, `core/inspector.py` `dataset_facts`, loading/concat tests) | `inspect-dataset`, `single-cell-counts`, workspace boundaries, artifact staging | Multi-input join decision and non-H5AD content description | strengthen `inspect-dataset`; multi-input contracts | H5AD plus 10x count loading covered |
| Raw-count detection and resolution (`core/inspector.py`, `core/normalization.py`, raw-count tests) | `single-cell-counts` independently inspects `X`/layers/`.raw`, selects a finite nonnegative integer matrix, refuses ambiguity, writes `layers['counts']`, and records identities without any inspection floor | Multi-input joins and stronger semantic validation beyond integer-valuedness | strengthen `single-cell-counts` | Live CRC execution from blank state |
| Gene identifiers, feature metadata, species signals (`core/genes.py`, gene conversion and MT/ribo tests) | Marker/reference skills validate some symbol overlap; preparation accepts organism | Deterministic genome/species evidence, mixed-reference refusal, reversible Ensembl-to-symbol mapping, feature-type handling | `gene-identity` | P2; before broad tissue/organism evaluations |
| Ambient background removal (`run_cellbender`, prompt CellBender section) | Implemented `cellbender-background-removal`: fingerprint-bound raw suitability, filtered/post-output refusal, GPU-only execution, provenance, artifacts, lineage/invalidation, and caught-failure recovery; live raw training verified | Full completed-run biological comparison remains dataset-specific; add repeated completed runs only when their scientific output is needed | `cellbender-background-removal` implemented | Completed focused slice; no corrected-matrix biology claim |
| Cell-level QC, MT/ribo metrics, flag-only philosophy (`core/qc.py`, QC tests) | `single-cell-qc` 0.2.0 auto-resolves counts/X, calculates MT+ribo metrics, emits six standard QC figures, and separates evidence-bound visual review from confirmed filtering | Broader cell-vs-nucleus policy evals and per-library threshold models | strengthen `single-cell-qc` | Comprehensive parity pass implemented 2026-07-27 |
| Doublet detection and review (`core/qc.py`, `doublet_detection_test.py`) | Implemented `doublet-evidence`: verified raw-count/cell identities, per-library GPU Scrublet, exact predicted-call semantics, rate warnings, score/call artifacts, explicit review, confirmed predicted-call filtering, new lineage/invalidation, floor and recovery tests; live PBMC run | Model eval overstated certainty; strengthen uncertainty/pDC-vs-plasma behavior and broaden designs. H5AD source resolution remains an upstream P0 dependency | `doublet-evidence` implemented | Completed focused slice; P0 behavior/upstream corrections remain |
| Normalization, low-detection genes, HVG selection (`core/normalization.py`, low-detection tests) | `expression-preprocessing` exposes normalization and HVG selection separately and never subsets on HVG selection | Ribosomal choice, source-defined exclusions, broader flavor tests | strengthen focused skill | Decoupled |
| PCA, neighbors, UMAP, alternative layouts (`core/dimred.py`, PCA default tests) | `dimensionality-reduction` exposes PCA, graph, and UMAP as three tools consuming explicit artifacts/representations | Variance-based PC evidence, force-directed layout, GPU/CPU numerical comparison | strengthen focused skill | Decoupled |
| Leiden/Louvain/PhenoGraph and resolution comparison (`core/clustering.py`, resolution tests) | Preparation/scVI issue clustering identity; QC staleness works | Resolution comparison/stability, alternative algorithms, no-overwrite cluster keys, source-defined settings | `cluster-single-cell`, `compare-clusterings` | P2; representation identity |
| Cell and cluster QC synthesis (`analysis/cluster_confidence.py`, cluster-QC tests) | `cluster-qc` 0.6.0 runs metric, DEG-identity, covariance/coherence, and Moran evidence; saves every heatmap but attaches a compact metric/UMAP overview, records shown paths automatically, requires flagged-cluster adjudication, and supports bounded cleanup with fresh identities | Model-behavior evals and multi-dataset threshold sweep | strengthen `cluster-qc` | Comprehensive parity pass implemented 2026-07-27; context-bounded review mechanics 2026-08-24 |
| Multi-sample detection and batch decision (`analysis/batch_diagnostic.py`, `batch_gene_investigation.py`, decision tests) | **Restoration 2 + legacy reasoning mechanics (2026-08-24):** `batch-investigation` 0.6.0 is gene-first and split into portable evidence + an identity-bound decision. It finds enriched regions, derives identity DEGs within each sample, matches cross-sample populations, explicitly reports legacy strong identity-signature similarity (Jaccard >= 0.30), direct differences, recurring programs, and design/confounding. The inline output is concise and names the strongest pair/genes plus advisory ARI/NMI/mixing; the long interpretation stays an artifact. It stops for the user's integrate/keep/separate/design-context choice instead of deciding autonomously. **Biology policy settled (2026-08-31, user directive):** `batch-investigation` 0.9.0 restores legacy's full design axis — `technical_batch_documented` (caller-asserted from a *stated* design, never inferred) → `documented_technical_batch` → `integration_supported` — and mirrors legacy's post-investigation checkpoint framing: with recurring gene evidence and confirmed cross-sample identity pairs, integrate-with-scVI is presented as the recommended default (the legacy checkpoint's `options[0]`/`recommendation`), with the design caveat alongside rather than a neutral can't-say menu. The user's choice still records the decision. **Consistency fix (2026-09-01, user directive — after two runs on the same dataset diverged):** the caller-supplied design levers are **removed** entirely. `investigate_batch` now takes only `path`/`batch_key`/`cluster_key` — no `condition_keys`, no `technical_batch_documented` — because the agent was steering the verdict with them (one run passed `condition_keys=["sample"]` where `sample`≡the batch key, a circular confound that flipped the narration to "biological" and led it to keep-uncorrected without asking). The recommendation is now a function of **gene evidence alone**: `recurring_sample_associated` → `integration_recommended` (was the hedgy `cannot_determine_technical_vs_biological`); `none`/`localized` → keep. `design_interpretation` is a fixed `unknown` and the technical-vs-biological caveat travels as narration, never as a competing verdict the agent can flip. The post-investigation pause is hardened in `SKILL.md` (prose only, by user's choice): STOP, present a selector whose **last** option is always "describe the experiment setup", and never call `decide_batch_handling` until the user chooses. Rationale: the "conclusive" within-sample identity-DEG test the user wanted already exists; the fix is to let it drive the recommendation instead of a design axis the agent games. | Live model-behavior eval on the comparison dataset; a true cross-runtime diffxpy NB-Wald bridge; sample-aware pseudobulk DEG skill | strengthen `batch-investigation`; `evaluate-integration` | Mechanics and recommendation policy aligned; caller-steerable design axis removed |
| scVI integration (`batch/scvi.py`, workers and performance tests) | `train_scvi_latent` saves model/history/`X_scVI` only; graph, UMAP, clustering, evaluation, and adoption are separate | Cell-count epoch heuristic, loss/convergence diagnostics, HVG training option, multi-GPU semantics, biological-conservation comparison | strengthen `scvi-integration`; `evaluate-integration` | Representation training decoupled |
| Harmony, Scanorama, BBKNN (`batch/*.py`) | Not implemented | Focused assumptions, output identities, comparative evidence, method-specific failure modes | one skill per method | P3; batch decision + common integration evaluation |
| scIB/entropy integration evaluation (`batch/scib.py`, `batch/entropy.py`) | Batch skill has limited metrics | Like-for-like latent comparison, biology conservation, label-free vs label-aware metrics, before/after artifacts | `evaluate-integration` | P2; at least two integration methods |
| Cluster marker evidence (`core/clustering.py`, annotation tests) | `marker-annotation` produces current-cell/current-clustering positive DEG summaries, thresholds, expression-aware marker-program overlap, ambiguity weighting, candidates, warnings, and artifacts; now includes a pDC program with shared-GZMB down-weighting and a deterministic pDC-vs-plasma scoring test | **P0 discrimination resolved** structurally (pDC program, shared GZMB, references, evals). Remaining: full DEG validity diagnostics, nuisance/context programs, tiny-cluster/matrix-source warnings, and running the model conservatism evals | strengthen marker skill; share DEG contract | Completed P0 structural/eval correction, then P1 DEG foundation |
| CellTypist (`annotation/celltypist.py`, model-selection tests) | Standalone per-cell `run_celltypist_annotation` plus separate optional group summary | Deterministic tissue/model suitability decision artifact and species breadth | strengthen existing skill | Computational DAG removed |
| SCimilarity (`annotation/scimilarity.py`, BioNeMo skill) | Standalone raw-count/gene/model inference with embedding, kNN labels, minimum-distance novelty, annotated H5AD, and separate optional group summary; live CRC blank-state acceptance succeeded. **2026-07-26:** `query_reference_cells` restores and exceeds legacy `query_cells` — centroid queries over any obs grouping or explicit cell set with per-query reference cell-type/tissue/disease/study composition, distance distribution, k-relative coherence, bounded artifacts, and content-addressed query identity. Legacy's centroid mode was dead code (`adata_sci` unbound) and its summaries read a `cell_type` column the release does not have, so both modes are new working capability. Organism is now required, verified against organism-specific vocabulary, and refused on contradiction before any load | Calibrated novelty review, broader organism/model compatibility, per-cell (rather than centroid) atlas search, metadata-filtered and exhaustive `max_dist` search modes | strengthen existing skill | Core session defect resolved; cell query restored 2026-07-26 |
| Cytopus/curated markers and annotation adjudication (`annotation/cytopus_markers.py`, extensive annotation tests) | Broad marker programs augmented from Cytopus 1.3.4 when available, plus finalization requiring complete DEG-primary labels, reviewed references, evidence summaries, confidence, rationales, and override justifications | **Core parity restored:** Cytopus availability/program coverage is explicit and annotation review is evidence-bound. Still missing ontology generalization, evidence-tied confidence ceilings, a dedicated PanglaoDB contract, and running the model evals | strengthen `marker-annotation` and `finalize-analysis` | P1 annotation datasets |
| Differential expression (`analysis/deg.py`, `batch/diffxpy.py`, DEG tests) | Cluster markers only; locked diffxpy runtime unused | Contrast/design contract, counts vs log matrix validation, sample-aware pseudobulk, covariates, imbalance/confounding diagnostics, complete result tables | `differential-expression` | P1; sample/condition identity |
| GSEA/pathways (`analysis/interpretation.py`, `literature.py`, defaults) | Web evidence only | Ranked-input contract, gene universe, database/version, FDR, leading edge, upstream DEG caveat propagation, literature evidence report | `pathway-enrichment` | P2; validated DEG |
| Gene signatures/SPECTRA and custom biological analyses (legacy tool inventory) | Restricted custom Python cannot satisfy scientific floors | Validated promotion of custom evidence; reproducible gene-set provenance; dedicated factor/signature outputs | `gene-signature`, `spectra`, evidence-promotion contract | P3; artifact promotion design |
| Figures and visual review (legacy plotting/tool rules, figure tests) | Media contract; six visualization tools including QC-on-embedding; QC/PCA/cluster/finalization emit standard figures and review contracts | Publication export presets | `visualize-single-cell` | Comprehensive standard suite implemented 2026-07-27 |
| Reports and artifact documentation (`core/artifact_docs.py`, report tests) | Finalization report and registered artifacts | Dataset-specific interpretation status, complete column dictionaries, downstream report composition from all evidence | `report-single-cell` | P2; stable evidence contracts |
| Web/literature evidence (`analysis/literature.py`, legacy `search_papers`/Europe PMC MCP) | `research-web` (Tavily search, Tavily-extract page fetch) plus `research-literature` (`search_literature`, `fetch_article_fulltext`) over Europe PMC: structured PMID/PMCID/DOI records, review/preprint/open-access flags, and open-access full text | **Search and retrieval restored and exceeded** (JSON backend replaces legacy regex-over-XML; preprints and full text added; failures raise instead of returning empty). Still missing the biological claim/source schema and lineage/context relevance matching from `literature.py` | strengthen `research-literature` | P3; pathway/annotation reporting |
| Sandboxed custom code, terminal, tracing, resumability (`agent/*`) | Independent session/events, restricted code/shell, media, terminal, transcript mirror, recovery | Promotion of custom results, scheduler/resource queues, scientific eval harness | platform work, not a biology monolith | Cross-cutting |

## Legacy prompt-rule disposition

The table below accounts for the complete rule families in `scagent/agent/prompts.py`. Closely
coupled sentences are grouped when they require the same owner; no group should be copied into a
new giant system prompt.

| Legacy rule family | Classification | Destination or disposition |
|---|---|---|
| Act on the request, report actual findings, continue autonomously, and preserve scientist agency inside required checkpoints | Orchestration guidance + evaluation | Keep concise in `orchestrate-single-cell`; evaluate tool selection, replanning, and evidence-grounded reporting. |
| Treat unmet obligations as non-negotiable but do not turn the workflow into a pipeline | State floor + orchestration guidance | Keep only consequential identity-aware predicates as floors; never port `world_state.unmet_obligations` as a DAG. |
| Complete tools within a turn; pause only for unavailable context, surprising/high-impact removal, or consequential forks | Orchestration guidance + evaluation | Preserve decision principles; runtime/user interaction remains replaceable. |
| Narrate what/why before actions and meaningful custom-code choices; explain errors and retries | Orchestration guidance + evaluation | Terminal behavior evaluation, not deterministic biology. |
| Treat heuristics as starting points; expose tool limitations instead of silently dropping requested parameters | Focused-skill guidance + evaluation | Every method reference names defaults/alternatives; schemas reject unsupported options. |
| Keep tools modular and do not recompute steps the user excluded | Orchestration guidance + evaluation | Focused capabilities remain independently callable. |
| Use descriptive, phase-aware, non-overwriting artifact paths and cite exact artifacts | Deterministic validation + focused-skill guidance | Staging prevents overwrite; each skill owns descriptive filenames and artifact metadata. |
| Integration figures require comparable pre/post batch views plus post-integration clusters | Focused-skill guidance + deterministic validation | `evaluate-integration` should require like-for-like representations and emit standard comparisons. |
| Publication/source parameters override generic defaults; inspect executable workflow files; do not omit unexposed parameters | Orchestration guidance + evaluation | Source-replication eval cases; use focused schema or auditable custom code. Validation still applies. |
| Normalization retries must resolve a raw-count source and report resets; source-defined feature exclusions are not universal defaults | Focused-skill guidance + deterministic validation | Future normalization skill. |
| Scrublet must name/confirm the per-library key; use its predicted call rather than an invented threshold | Focused-skill guidance + deterministic validation | `doublet-evidence`. |
| Batch correction is opt-in, not metadata-triggered; investigate before correction and require explicit authorization | State floor + orchestration guidance | Existing `batch_decision` and `integration_authorized` floors; strengthen diagnostic science. |
| Multiple inputs are not concatenated automatically; join choice and sample IDs are explicit | State floor + deterministic validation | Future multi-input decision and lineage contract. |
| Respect requests not to hard-filter MT; do not volunteer arbitrary gene-class removal | Focused-skill guidance + evaluation | Cell-QC/preparation guidance and behavior evals. |
| Early MT/library/gene thresholds are flags; global cell filtering is not the default | Focused-skill guidance + deterministic validation | Future `cell-qc`; current preparation is a known parity gap. |
| Preserve one primary dataset, never silently replace it, save/identify before switching, and keep secondary data separate | Deterministic validation + state floor | Dataset lineage plus exact active-input identity; artifacts never overwrite user data. |
| “From scratch” means ignore prior annotations for inference, not erase provenance | Orchestration guidance + deterministic validation | Input/lineage skill and behavior eval. |
| Cell/gene filtering requires evidence and high-impact removal review; reprocess after cell-set change | State floor + deterministic validation | Future preview/authorize filtering contract; identity changes stale downstream evidence. |
| User/paper construction parameters are authoritative but do not waive independent QC/annotation validation; matching counts is not annotation validation | Orchestration guidance + evaluation | Source-replication eval suite; floors remain active. |
| Do not ask permission for routine required validation | Orchestration guidance + evaluation | Keep concise; consequential user authorization remains explicit. |
| CellBender is opt-in, raw/unfiltered only, before downstream analysis; automatic priors first; GPU only when verified; inspect report/log/metrics; downstream QC still required | Focused-skill guidance + deterministic validation + evaluation | **Selected `cellbender-background-removal` slice.** Dataset identity and validated-suitability floors gate execution. |
| Early QC is instrumentation, not surgery; cell/nucleus MT starting points; keep doublets for review | Focused-skill guidance | Future `cell-qc` and `doublet-evidence`. |
| Low-detection gene filtering is count-based, scaled by dataset size, capped to protect rare populations, and explicitly disableable | Focused-skill guidance + deterministic validation | Future normalization/preparation revision. |
| Remove ribosomal genes only by an explicit project/source choice and report the effect | Focused-skill guidance + deterministic validation | Future normalization/preparation revision. |
| Paint per-cell metrics on UMAP and interpret spatial localization | Focused-skill guidance + evaluation | Each evidence skill emits overlays; model-review evals verify interpretation. |
| Cluster QC is current-clustering evidence, must re-fire after reclustering, and combines metric, marker-identity, covariance, heatmap, and technical-localization evidence | State floor + focused-skill guidance + deterministic validation | Existing current-QC floor; strengthen `cluster-qc`. |
| Visual cluster-QC review is independent evidence and flags review rather than automatic removal | Focused-skill guidance + evaluation | Model-media eval and explicit review evidence contract. |
| Auto-remove only convergent low-quality evidence below a high-impact limit; preserve conflicts and re-cluster after removal | Deterministic validation + state floor | Future filtering authorization. Legacy prompt contains both auto-removal and manual-removal formulations; the new contract must choose one explicit state transition. |
| High-MT clusters require identity-gene review; technical Moran’s I is not cell-type evidence; heatmap claims require a saved artifact | Focused-skill guidance + evaluation | Strengthen cluster-QC reference/evals. |
| Report every cleanup iteration with exact counts and evidence | Deterministic validation + evaluation | Filtering artifact schema and report evaluation. |
| HVG/PCA/neighbors/UMAP defaults and PCA variance rationale | Focused-skill guidance | Preparation/representation references, not global prompt law. |
| The legacy 2.0→1.5→1.0 resolution ladder | Focused-skill guidance + evaluation | Do **not** port as a universal state floor. Resolution is a scientific parameter; compare stability and honor source/user choices. |
| Preserve raw counts; do not scale active log-normalized X; DEG uses the verified log-normalized matrix | Deterministic validation + focused-skill guidance | Preparation and future DEG contracts. |
| Gene-first batch diagnosis, design/confounding axes, recurrence, cell-level q-value caveat, and like-for-like integration scoring | Focused-skill guidance + deterministic validation + evaluation | Strengthen batch investigation and add integration evaluation. |
| scVI epoch/device/convergence semantics and loss-curve review | Focused-skill guidance + deterministic validation | Strengthen scVI skill. |
| Every produced figure must be inspected; figure claims cite saved artifacts | Orchestration guidance + evaluation | Existing model-media path; add figure-review evals. |
| Global threshold filtering requires a preview and exact counts before mutation | State floor + deterministic validation | Future `filter-cell-set` preview/authorization contract. |
| Species comes from metadata/reference/IDs, not gene-symbol case; mixed/ambiguous species pauses annotation | Deterministic validation + state floor | Future gene/species identity plus annotation input floor. |
| Reference annotation requires gene symbols; conversion is reversible and preserves original IDs | Deterministic validation + focused-skill guidance | Future gene-identity skill; strengthen reference skills. |
| Cluster DEGs are the annotation decision basis; reference tools and curated markers corroborate; external marker lookup is optional | State floor + focused-skill guidance + evaluation | Existing marker+independent-reference floor; strengthen finalization evidence. |
| Run compatible references, preserve concrete blockers, and treat labels as hypotheses | Focused-skill guidance + deterministic validation | Existing reference skills plus evals. |
| Require an independently written DEG-derived label, explicit override rationale, marker cross-check, and conservative generalization | Deterministic validation + evaluation | Strengthen finalization schema; biology remains model judgment. |
| Land on the intended current clustering before DEG/reference evidence and bind every evidence item to that identity | State floor + deterministic validation | Existing identity freshness; no universal resolution requirement. |
| Select CellTypist model from organism/tissue context rather than defaulting to immune; distinguish raw per-cell predictions from majority voting | Focused-skill guidance + deterministic validation + evaluation | Strengthen CellTypist skill. |
| Stage compact per-cluster evidence, validate before finalization, preserve existing labels, and allow an explicitly marked unvalidated save after honest failures | Deterministic validation + state floor | Strengthen finalization; preserve no-overwrite. |
| Never hand-map final labels or score marker dictionaries through unrestricted code to bypass evidence | State floor + evaluation | Existing finalization floor; custom code cannot promote itself to final evidence. |
| Marker dictionaries require length-normalized coverage, ambiguity/shared-marker handling, and DEG adjudication | Focused-skill guidance + deterministic validation | Marker skill. |
| Anti-pattern list for premature filtering, reference domination, skipped normalization after filtering, stale UMAPs, and silent ribosomal defaults | Evaluation | Convert into focused behavior eval cases rather than system-prompt bulk. |
| Read and act on runtime warnings; safe uniqueness fixes are explicit; unexpected representation values are investigated | Orchestration guidance + deterministic validation | Standard result warnings and input validators. |
| Restricted custom code is a fallback with fresh local scope; durable results require state/artifact registration | Orchestration guidance + deterministic validation | Existing analysis-workspace; promotion remains a gap. |
| Artifact groups require structural documentation plus dataset-specific interpretation | Deterministic validation + evaluation | Reporting/artifact contracts. |
| Available-package lists and exact third-party APIs must be inspected live, not assumed | Orchestration guidance + evaluation | Environment doctor and focused runtime probes; never copy a drifting package list into the base prompt. |
| Multi-sample concatenation defaults, explicit join reporting, and validated helpers | Focused-skill guidance + deterministic validation | Future multi-input skill. The new system must not hard-code outer join as universally correct. |
| Use web for APIs and literature sources for biological claims | Orchestration guidance + evaluation | Existing web skill; future literature skill. |
| Manual researcher mappings are inputs but final labels still require evidence and non-overwrite | Deterministic validation + state floor | The older prompt section allowing direct `run_code` mapping is superseded by the later gated annotation contract. |
| Reports include context, methods, actual findings, caveats, evidence synthesis, and artifact contribution | Focused-skill guidance + evaluation | Finalization/reporting skill and evals. |
| Never overwrite outputs; publish final/checkpoint artifacts intentionally | Deterministic validation | Capability staging and atomic commit. The legacy “never save intermediate H5AD” rule is not ported because durable resumability requires registered checkpoints. |
| Plotting layout, sizing, skewed scales, and categorical legends | Focused-skill guidance + deterministic tests | Dedicated visualization/reference guidance. |
| Unknown paths require discovery before load; no blind format retries | Deterministic validation + orchestration guidance | Future multi-input inspector. |
| Initial inspection reports representation facts and metadata previews before analysis; semantic roles are model judgments grounded in facts | Orchestration guidance + evaluation | Strengthen inspection capability and evals. |
| Explain scientific terms in plain language and translate internal identifiers | Evaluation | Terminal/model behavior eval. |
| Runtime/backend claims come from live probes; never guess or silently fall back | Deterministic validation + evaluation | Existing environment broker and doctor. |

## Completed focused slice: CellBender ambient-background removal

### Why this slice

The runtime is locked and healthy, the legacy prompt and tool expose clear requirements, and Iris
contains a genuine raw/filtered acceptance pair. The raw candidate has 396,297 barcodes, median 2
UMIs, and 381,807 nonzero barcodes; the Cell Ranger filtered counterpart has 8,201 barcodes and a
minimum 247 UMIs. This supports a real empty-droplet suitability check and makes rejection testing
scientifically meaningful.

The legacy `run_cellbender` is not sufficient parity: it validates existence, parameters, process
return code, and output loading, but accepts a filtered matrix, allows ambient executable state,
does not bind validation to dataset identity, does not issue a corrected-count identity, and does
not invalidate downstream evidence.

### State and lineage contract

1. `validate_cellbender_input` requires the active dataset identity, re-computes the recorded
   fingerprint mode, inspects the 10x H5 schema and UMI-rank distribution, and records suitability
   tied to the exact dataset fingerprint.
2. `remove_ambient_background` requires both `dataset_identity` and a new
   `cellbender_input_suitable` floor. It revalidates the same facts defensively and runs only in the
   GPU-required `cellbender` logical environment.
3. The source bytes are never edited or copied over. A lineage manifest registers their path,
   fingerprint, size, and role.
4. The selected CellBender output becomes a new active dataset revision with a full output hash,
   parent fingerprint, corrected-count representation identity, and explicit selected barcode-set
   identity. Both full and filtered outputs remain registered.
5. A corrected count representation invalidates prior cell QC, representation, clustering, batch,
   annotation, and finalization facts. Old artifacts remain immutable historical evidence but no
   longer satisfy current floors.
6. On a caught process failure or timeout, logs and any checkpoint are committed as a failed run
   without changing the active dataset. A later run may copy a registered checkpoint into its new
   staging directory and resume without mutating the checkpoint source.

### Acceptance tests

- manifest schema, required fields, enums, numeric bounds, and unknown-argument rejection;
- filtered/post-CellBender/unsupported input rejection and raw empty-droplet acceptance;
- exact dataset-path/fingerprint binding and stale validation-floor re-fire;
- fail-closed `cellbender` environment routing and no CPU option;
- deterministic CellBender 0.3.2 seed capture (`1234`), command/config capture, non-overwrite, and
  bounded result summaries;
- success and failure artifact contracts, checkpoint recovery input, and two-phase commit recovery;
- corrected-count, dataset-revision, barcode-set, and downstream invalidation patches;
- real Iris run on the raw candidate, plus inspection of H5 shapes, metrics, report/plot, logs,
  hashes, environment provenance, session facts, and artifact registration;
- model-behavior cases: select only for raw/ambient-removal requests, refuse filtered input, explain
  why it precedes QC, inspect warnings/metrics, and avoid claiming CellBender replaces downstream
  QC.

### Verified implementation and live evidence

The focused package now includes both strict tools, method/state guidance, model eval cases, and a
GPU-only manifest. Deterministic tests cover schema/routing, raw-versus-filtered and post-output
assessment, fingerprint attestation, command/seed construction, successful lineage/invalidation,
failed-run artifacts, checkpoint input, and the current-suitability floor. Generic executor tests
continue to cover two-phase crash recovery.

Live Iris evidence used:

- raw input: BC002 `sample_raw_feature_bc_matrix.h5`, 39,226 features by 396,297 barcodes,
  323,534 barcodes at no more than 10 UMIs, full fingerprint
  `sha256:66149079f049652560f46e4d748c67c2b1c886da00e1d2335792024373dab79a`;
- refusal control: paired `sample_filtered_feature_bc_matrix.h5`, 18,099 features by 8,201
  barcodes, zero barcodes at no more than 10 UMIs;
- real execution: session `2c96ab8a-77fa-4737-8edc-d16aa54e3b2a` ran CellBender 0.3.2 on GPU,
  detected 7,762 probable cells, 20,733 additional barcodes, and 57,390 empty droplets; training
  proceeded normally through epoch 46 before the user-requested terminal interrupt, leaving only
  uncommitted pending command/lineage/log evidence rather than a fabricated result;
- controlled recovery: session `307a6a52-6147-44f9-9dde-50036d0cd533` caught a 60-second inner
  timeout and committed lineage, exact command/config, logs, and interpretation while preserving
  the raw active dataset and all downstream state;
- model behavior: two runs selected inspect then validate and respected a no-run request. The first
  revealed confusion between the one-percent empty-tail gate and cell-count estimation; the skill,
  reference, validator report, and eval suite now state explicitly that the gate is not an
  `expected_cells` estimate, and the repeated run interpreted it correctly.

No completed corrected matrix from this acceptance run is claimed or promoted. Success-output
schema, hashing, barcode/count identities, artifact requirements, and invalidation are covered by
deterministic contract tests; a future full run should inspect the PDF, metrics, posterior, counts
removed, and gene-level effects only when the corrected data are actually needed scientifically.

## Latest completed focused slice: doublet evidence and review

### Implemented contract

The `doublet-evidence` package owns the method guidance, strict schema, RAPIDS-singlecell script,
state transitions, reference contract, and model eval cases. It:

- validates the current prepared artifact, cell-set identity, count-representation identity, and
  finite nonnegative integer raw counts;
- requires a biological capture/library key, or explicit confirmation that unstratified execution
  is appropriate for a single library;
- runs GPU Scrublet independently by library, preserves its `predicted_doublet` calls, and records
  actual parameters, rates, warnings, plots, tables, annotated H5AD, and environment provenance;
- keeps evidence separate from mutation: review decisions normally retain cells, while
  `remove_predicted` requires explicit confirmation and removes only the current Boolean predicted
  calls;
- issues new dataset/cell-set/count identities and invalidates downstream preparation,
  representation, clustering, QC, batch, annotation, and finalization evidence after removal;
- fails rather than silently filling undersized/failed libraries with singlet calls; and
- uses the `current_doublet_evidence` floor to prevent stale review/filtering.

Deterministic tests cover strict manifests/parameters, identity/path/count contracts, evidence
patches, conservative review, confirmed filtering lineage/invalidation, floor staleness, and
executor recovery.

### Live and model evidence

On the representative PBMC analysis, Scrublet predicted 565 of 11,043 cells (5.12%). Clusters 4,
11, 16, and 17 showed enrichment and were retained for cluster/marker/QC review; the run did not
automatically remove cells. Cluster QC consumes that evidence without treating enrichment as proof
of an artifact.

A separate 26-sample analysis strengthened batch evidence with per-sample QC, neighborhood mixing,
and normalized entropy. Cramér's V was 0.539 and mean same-sample neighbors were 0.595 versus 0.205
from sample composition. Because technical and biological design were not resolved by those
statistics, the model recorded `request_guidance` rather than authorizing integration.

The model-behavior run also exposed two shortcomings: it described Scrublet calls too certainly,
and it labeled a pDC-like cluster as plasma despite conflicting gene-level evidence. Artifact
review exposed unreadable legends for the 26-sample plots. These are recorded as P0 corrections,
not hidden behind the successful deterministic baseline.

### Immediate correction acceptance criteria (met 2026-07-22)

- H5AD counts are selected from an inspected, validated source rather than assumed from `X` —
  **done** (selection tests plus a live refusal and a live layer auto-selection);
- batch-decision and integration floors become stale when current cell/count/representation or
  clustering identities change — **done** (allow/stale/re-fire tests plus a live staleness check);
- high-cardinality batch artifacts are readable and covered by plotting tests — **done** (layout
  tests plus a live bar-path render; heatmap-path live render still pending a >12-batch integer
  dataset);
- focused marker/finalization guidance and evals distinguish pDC and plasma programs, require
  uncertainty when evidence conflicts, and never treat Scrublet as a definitive identity label —
  **done structurally** (pDC program, shared GZMB, references, evals, scoring test); the model
  behavior evals themselves still need to run;
- direct finalization tests cover complete maps, overrides, staleness, non-overwrite, and emitted
  evidence/state/artifacts — **done**.

Deterministic-contract and live-compute levels are met; the remaining work for this pass is the
P0 #4 model-behavior evaluation and a heatmap-path live render. The next broad scientific slice can
begin in parallel with those.
