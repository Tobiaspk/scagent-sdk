---
name: export-dataset
description: Publish an existing AnnData analysis artifact as a portable, registered H5AD without changing its scientific contents or advancing the active analysis version.
---

# Export Dataset

Capability-produced Zarr stores are efficient internal working artifacts. Use `export_anndata`
when the user asks for an H5AD or when concluding a request whose result is an AnnData object and a
portable user-facing file is appropriate. Do not export every intermediate merely because it
exists.

Choose the source that semantically answers the request: the current enriched artifact for the
current analysis, or a specific earlier artifact when the user refers to that result. Export is a
format-only derivative. It preserves the complete AnnData payload, verifies the H5AD round trip,
registers the file as a data artifact, and does not advance the active scientific lineage head.
