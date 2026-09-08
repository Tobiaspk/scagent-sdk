"""Read-only, standard-library dataset identity inspection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SAMPLE_BYTES = 1024 * 1024
STORE_SAMPLE_BUDGET = 2 * SAMPLE_BYTES
STORE_METADATA_LIMIT = 32
STORE_DATA_LIMIT = 16
STORE_DATA_NAMESPACES = ("X", "layers", "obs", "var", "obsm", "obsp", "varm", "raw")


def _store_members(path: Path) -> list[Path]:
    """Every file in a directory store, ordered deterministically by relative POSIX path."""

    return sorted(
        (member for member in path.rglob("*") if member.is_file()),
        key=lambda member: member.relative_to(path).as_posix(),
    )


def _is_store_metadata(member: Path) -> bool:
    """True for zarr's own metadata members (v2 dotfiles, or the v3 ``zarr.json``)."""

    return member.name.startswith(".z") or member.name == "zarr.json"


def _evenly_spaced(members: list[Path], limit: int) -> list[Path]:
    """Select deterministic coverage across an ordered member list."""

    if len(members) <= limit:
        return members
    return [members[index * (len(members) - 1) // (limit - 1)] for index in range(limit)]


def _sampled_store_members(path: Path, members: list[Path]) -> list[Path]:
    """Choose bounded, distributed store members instead of trusting one large chunk.

    The manifest always covers every member path and size. Content sampling then spans metadata,
    the ordered data-member range, the largest chunk, and each common AnnData namespace. This is
    still a sample -- callers that need immutable byte identity must request ``hash_mode=full``.
    """

    metadata = [member for member in members if _is_store_metadata(member)]
    data = [member for member in members if not _is_store_metadata(member)]
    selected = set(_evenly_spaced(metadata, STORE_METADATA_LIMIT))
    selected.update(
        member for member in metadata if "/" not in member.relative_to(path).as_posix()
    )
    selected.update(_evenly_spaced(data, STORE_DATA_LIMIT))
    if data:
        selected.add(max(data, key=lambda member: (member.stat().st_size, str(member))))
    for namespace in STORE_DATA_NAMESPACES:
        candidates = [
            member
            for member in data
            if member.relative_to(path).parts
            and member.relative_to(path).parts[0] == namespace
        ]
        if candidates:
            selected.add(max(candidates, key=lambda member: (member.stat().st_size, str(member))))
    return sorted(selected, key=lambda member: member.relative_to(path).as_posix())


def _update_with_member_sample(
    digest: Any, path: Path, member: Path, *, byte_budget: int
) -> None:
    """Hash at most ``byte_budget`` bytes, split across a member's head and tail."""

    relative = member.relative_to(path).as_posix()
    size = member.stat().st_size
    digest.update(f"sample\0{relative}\0{size}\0".encode())
    with member.open("rb") as handle:
        if size <= byte_budget:
            digest.update(handle.read())
            return
        head_bytes = (byte_budget + 1) // 2
        tail_bytes = byte_budget // 2
        digest.update(handle.read(head_bytes))
        if tail_bytes:
            handle.seek(size - tail_bytes)
            digest.update(handle.read(tail_bytes))


def _fingerprint_store(path: Path, *, mode: str, members: list[Path], size: int) -> str:
    """Fingerprint a directory store under the same sampled/full contract as a single file.

    A store has no one byte stream, so identity starts from its member manifest -- every relative
    path and size, which already moves whenever a chunk is rewritten to a different length.
    ``sampled`` then distributes a fixed byte budget across metadata and representative data
    members from the common AnnData namespaces, so the default stays cheap without reducing a
    multi-array store to one arbitrarily large chunk. ``full`` reads every byte.
    """

    digest = hashlib.sha256()
    # Version the algorithm because sampled membership changed from one largest chunk to a
    # distributed selection. Old and new fingerprints must never look directly comparable.
    digest.update(f"scagent-store-v2\0{size}\0{len(members)}\0".encode())
    for member in members:
        relative = member.relative_to(path).as_posix()
        digest.update(f"{relative}\0{member.stat().st_size}\0".encode())
    if mode == "full":
        for member in members:
            with member.open("rb") as handle:
                for chunk in iter(lambda: handle.read(SAMPLE_BYTES), b""):
                    digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    sampled = _sampled_store_members(path, members)
    if sampled:
        per_member_budget = max(1, STORE_SAMPLE_BUDGET // len(sampled))
        for member in sampled:
            _update_with_member_sample(
                digest, path, member, byte_budget=per_member_budget
            )
    return f"sha256:{digest.hexdigest()}"


def _format_evidence(path: Path, head: bytes) -> dict[str, Any]:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    signature = "unknown"
    if path.is_dir():
        # `head` is the store's own metadata member when one was found, empty otherwise.
        signature = "zarr" if head else "directory"
    elif head.startswith(b"\x89HDF\r\n\x1a\n"):
        signature = "hdf5"
    elif head.startswith(b"%%MatrixMarket"):
        signature = "matrix-market"
    elif head.startswith(b"\x1f\x8b"):
        signature = "gzip"
    elif head.startswith(b"PK\x03\x04"):
        signature = "zip"
    extension = suffixes[-1].lstrip(".") if suffixes else "none"
    expected = {
        "h5ad": "hdf5",
        "h5": "hdf5",
        "hdf5": "hdf5",
        "mtx": "matrix-market",
        "gz": "gzip",
        "zip": "zip",
        "zarr": "zarr",
    }.get(extension)
    return {
        "extension": extension,
        "suffixes": suffixes,
        "byte_signature": signature,
        "extension_signature_consistent": expected is None or expected == signature,
    }


def _fingerprint(path: Path, *, mode: str, size: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"scagent-dataset-v1\0{size}\0".encode())
    with path.open("rb") as handle:
        if mode == "full":
            for chunk in iter(lambda: handle.read(SAMPLE_BYTES), b""):
                digest.update(chunk)
        else:
            digest.update(handle.read(SAMPLE_BYTES))
            if size > SAMPLE_BYTES:
                handle.seek(max(0, size - SAMPLE_BYTES))
                digest.update(handle.read(SAMPLE_BYTES))
    return f"sha256:{digest.hexdigest()}"


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path must be a non-empty string")
    path = Path(raw_path).expanduser().resolve()
    is_directory = path.is_dir()
    # A .zarr artifact is a directory (ADR 0011); identity must cover it, not recursively hash an
    # arbitrary directory that merely happened to be supplied as a dataset path.
    is_store = is_directory and path.suffix.lower() == ".zarr"
    if is_directory and not is_store:
        raise ValueError(
            "directory inputs must be .zarr stores; materialize a 10x Matrix Market directory "
            "with materialize_count_matrix before inspecting its AnnData contents"
        )
    if not (is_store or path.is_file()):
        raise FileNotFoundError(f"dataset not found: {path}")
    mode = arguments.get("hash_mode", "sampled")
    if mode not in {"sampled", "full"}:
        raise ValueError("hash_mode must be sampled or full")
    if is_store:
        members = _store_members(path)
        root_names = {
            member.relative_to(path).as_posix()
            for member in members
            if "/" not in member.relative_to(path).as_posix()
        }
        if not root_names.intersection({".zgroup", ".zmetadata", "zarr.json"}):
            raise ValueError(
                f"directory has a .zarr suffix but no root Zarr metadata marker: {path}"
            )
        size = sum(member.stat().st_size for member in members)
        modified_time_ns = max(
            (member.stat().st_mtime_ns for member in members),
            default=path.stat().st_mtime_ns,
        )
        marker = next((member for member in members if _is_store_metadata(member)), None)
        head = marker.read_bytes()[:64] if marker is not None else b""
        fingerprint = _fingerprint_store(path, mode=mode, members=members, size=size)
    else:
        stat = path.stat()
        size = stat.st_size
        modified_time_ns = stat.st_mtime_ns
        with path.open("rb") as handle:
            head = handle.read(64)
        fingerprint = _fingerprint(path, mode=mode, size=size)
    identity = {
        "path": str(path),
        "size_bytes": size,
        "modified_time_ns": modified_time_ns,
        "fingerprint": fingerprint,
        "fingerprint_mode": mode,
        "format": _format_evidence(path, head),
    }
    artifact_path = context.staging_dir / "inspection.json"
    artifact_path.write_text(
        json.dumps(identity, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "summary": (
            f"Inspected {path.name}: {size} bytes, "
            f"signature={identity['format']['byte_signature']}, hash_mode={mode}."
        ),
        "details": identity,
        "facts_patch": {"dataset": identity},
        "artifacts": [
            {
                "name": "dataset-inspection",
                "relative_path": "inspection.json",
                "media_type": "application/json",
            }
        ],
    }
