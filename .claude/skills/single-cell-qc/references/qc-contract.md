# QC evidence and filtering contract

The standard cell metrics are total counts, detected genes, and mitochondrial-count percentage.
They describe technical or physiological signals; none alone proves a cell is unusable.

Human mitochondrial symbols commonly begin `MT-`. Mouse symbols commonly begin `mt-` or `Mt-`.
When `var_names` are Ensembl identifiers, a symbol column such as `feature_name`,
`gene_symbol`, or `mgi_symbol` is used if present.

`calculate_single_cell_qc` writes Boolean flags for the requested thresholds and reports exact
flag counts. It does not subset the object.

`filter_single_cells` and `filter_single_cell_genes` are mutation operations. Each requires an
explicit confirmation argument, saves a new H5AD, records before/after counts, refreshes affected
identities, and invalidates downstream state. They never overwrite the source.

`filter_single_cells` has no threshold defaults and refuses a call that names none. The thresholds
on `calculate_single_cell_qc` exist to draw flags and figures; carrying them into the mutating tool
would turn a bare call into a silent global mitochondrial cut. State the cut you intend, with the
count it removes.

Ordering matters as much as authorization. `review_single_cell_qc` is the record of a decision, so
it must precede the mutation it authorizes: a review recorded after the cells are already gone
describes an outcome instead of choosing one, and it is evaluated against an artifact whose flagged
tail has been deleted. For cells, the default adjudication venue is `cluster-qc` on a clustered
artifact, where metric severity, DEG identity, and covariance structure are available together;
pre-clustering cell filtering is the exception and is justified in the review rationale.
