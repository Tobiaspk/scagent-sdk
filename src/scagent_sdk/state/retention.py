"""What a session's artifacts cost, and what deleting some of them would actually reclaim.

Storage reporting has to distinguish four quantities, because on a filesystem where versions may
share bytes they diverge sharply:

- **apparent** -- the sum of file sizes, which is what ``du`` reports and what a naive report would
  promise;
- **unique** -- distinct ``(device, inode)`` pairs, so a file counted once no matter how many
  versions link to it;
- **shared** -- unique bytes that a version being considered for deletion holds in common with a
  version being kept;
- **reclaimable** -- unique bytes whose last link would go away, which is the only number a prune
  may promise.

Under hard-linked snapshots those numbers can differ by everything: a branch that only added an
embedding shares essentially all of its bytes with the line it forked from, so its apparent size is
its full store while its reclaimable size is near zero. Measured on a four-version fixture, pruning
such a branch reported 6.5 MB apparent and freed 0.0 MB. Today's artifacts are independent H5AD
files where apparent and reclaimable nearly coincide, which is exactly why the distinction has to be
built in now rather than discovered when the storage format changes underneath the report.

This module only measures and proposes. Nothing here deletes anything.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scagent_sdk.state.lineage import active_head, ancestry, reachable_from_heads


def read_state_without_opening(session_dir: Path) -> dict[str, Any]:
    """Read a session's checkpoint without touching it.

    ``SessionStore.open`` is not an inspection path: it takes the session lock, replays unapplied
    events, rewrites ``state.json``, can run the v1 -> v2 migration and append an event for it, and
    refreshes the derived output view. That is right for a session about to be worked on and wrong
    for a report that claims to observe. Reporting therefore reads the checkpoint directly and
    accepts that it may lag the event log -- visible as ``last_event_sequence`` -- rather than
    mutating a session in order to describe it.
    """

    path = Path(session_dir) / "state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ByteAccounting:
    """The four quantities, plus the file count they were computed over."""

    apparent_bytes: int = 0
    unique_bytes: int = 0
    shared_bytes: int = 0
    reclaimable_bytes: int = 0
    files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "apparent_bytes": self.apparent_bytes,
            "unique_bytes": self.unique_bytes,
            "shared_bytes": self.shared_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "files": self.files,
        }


@dataclass(frozen=True)
class ArtifactUsage:
    """Storage attributable to one execution's artifact directory."""

    execution_id: str
    path: str
    exists: bool
    on_active_line: bool
    reachable: bool
    accounting: ByteAccounting
    tool_name: str = ""
    head_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "path": self.path,
            "exists": self.exists,
            "on_active_line": self.on_active_line,
            "reachable": self.reachable,
            "tool_name": self.tool_name,
            "head_path": self.head_path,
            **self.accounting.to_dict(),
        }


@dataclass(frozen=True)
class PruneProposal:
    """A dry-run prune: what would be removed, and what it would honestly free."""

    session_id: str
    candidates: tuple[ArtifactUsage, ...] = ()
    retained: tuple[str, ...] = ()
    total: ByteAccounting = ByteAccounting()
    candidate_total: ByteAccounting = ByteAccounting()
    warnings: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_total": self.total.to_dict(),
            "candidate_total": self.candidate_total.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "retained_count": len(self.retained),
            "warnings": list(self.warnings),
            "notes": self.notes,
        }


def _files(root: Path) -> list[tuple[Path, tuple[int, int], int, int]]:
    """Every regular file under ``root`` with its ``(device, inode)``, size and link count."""

    out: list[tuple[Path, tuple[int, int], int, int]] = []
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
        except OSError:
            continue
        out.append((path, (stat.st_dev, stat.st_ino), stat.st_size, stat.st_nlink))
    return out


def account(
    targets: Iterable[Path], *, retained: Iterable[Path] = ()
) -> ByteAccounting:
    """Measure ``targets``, counting as reclaimable only bytes whose last link would go away.

    Measures by ``(device, inode)`` rather than by summing file sizes: a hard-linked file occupies
    its bytes once however many directories reference it, so summing apparent sizes overstates both
    the cost and the saving.

    Reclaimability is decided by comparing the links observed inside ``targets`` against the file's
    own ``st_nlink``. Checking only whether the inode also appears under ``retained`` is not enough:
    a link held anywhere the scan does not cover -- another session, a backup tree, an export --
    keeps the bytes alive after the candidate is deleted, and would otherwise be reported as freed.
    ``st_nlink`` is authoritative about that because it counts every link on the filesystem, not
    just the ones this function was pointed at.
    """

    kept_inodes = {key for path in retained for _, key, _, _ in _files(Path(path))}
    apparent = 0
    count = 0
    sizes: dict[tuple[int, int], int] = {}
    nlinks: dict[tuple[int, int], int] = {}
    seen_here: dict[tuple[int, int], int] = {}
    for path in targets:
        for _, key, size, nlink in _files(Path(path)):
            apparent += size
            count += 1
            sizes[key] = size
            nlinks[key] = nlink
            seen_here[key] = seen_here.get(key, 0) + 1

    unique = shared = reclaimable = 0
    for key, size in sizes.items():
        unique += size
        # Links this scan can see: the ones inside the candidate set, plus any under retained.
        outside = nlinks[key] > seen_here[key]
        if key in kept_inodes or outside:
            shared += size
        else:
            reclaimable += size
    return ByteAccounting(
        apparent_bytes=apparent,
        unique_bytes=unique,
        shared_bytes=shared,
        reclaimable_bytes=reclaimable,
        files=count,
    )


def topology_defects(lineage: Mapping[str, Any]) -> tuple[str, ...]:
    """Reasons the recorded forest cannot be trusted to identify abandoned work.

    Reachability is only meaningful over a forest that actually explains how the work connects.
    Counting parentless versions is not sufficient: a node whose parent id is not in the forest has
    a parent recorded and still sits on no line of descent, so it would pass a count-only check
    while making "unreachable" mean nothing. Each condition below independently breaks the
    reachability argument, so any of them disqualifies the whole session from pruning.
    """

    nodes = lineage.get("nodes")
    nodes = nodes if isinstance(nodes, Mapping) else {}
    if not nodes:
        return ()

    defects: list[str] = []
    head = active_head(lineage)
    if head is None:
        defects.append(f"no active version is recorded for {len(nodes)} version(s)")
    elif head not in nodes:
        defects.append(f"the active version {head[:8]} is not among the recorded versions")

    roots: list[str] = []
    dangling: list[str] = []
    for execution_id, node in sorted(nodes.items()):
        if not isinstance(node, Mapping):
            defects.append(f"version {str(execution_id)[:8]} is not a record")
            continue
        parent = node.get("parent_execution_id")
        if not isinstance(parent, str):
            roots.append(str(execution_id))
        elif parent not in nodes:
            dangling.append(str(execution_id))
    if dangling:
        defects.append(
            f"{len(dangling)} version(s) name a parent that is not recorded "
            f"({', '.join(item[:8] for item in sorted(dangling)[:4])})"
        )
    if len(roots) > 1:
        defects.append(f"{len(roots)} of {len(nodes)} versions have no recorded parent")

    # A cycle makes ancestry non-terminating in principle and unreachable in practice; ``ancestry``
    # guards against hanging, so detect it here rather than inferring it from a truncated walk.
    cyclic: list[str] = []
    for execution_id in sorted(nodes):
        seen: set[str] = set()
        current: Any = execution_id
        while isinstance(current, str) and current in nodes and current not in seen:
            seen.add(current)
            node = nodes[current]
            current = node.get("parent_execution_id") if isinstance(node, Mapping) else None
        if isinstance(current, str) and current in seen:
            cyclic.append(str(execution_id))
    if cyclic:
        defects.append(f"{len(cyclic)} version(s) sit on a parent cycle")

    # Every version must be on some line of descent from a root, or reachability is partial.
    if head is not None and head in nodes and not defects:
        connected = set()
        for execution_id in nodes:
            connected.update(ancestry(lineage, execution_id))
        orphans = sorted(set(nodes) - connected)
        if orphans:
            defects.append(f"{len(orphans)} version(s) sit on no line of descent")
    return tuple(defects)


def propose_prune(
    session_dir: Path,
    *,
    session_id: str,
    lineage: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> PruneProposal:
    """Describe what an unreachable-artifact prune would remove and reclaim.

    Candidates are matrix versions the active head does not descend from. That is a *reachability*
    answer, not a liveness one: a version off the active line may still be an alternative the user
    is comparing, so this proposes and never deletes. Deciding which branches are genuinely spent
    needs an explicit disposition vocabulary (retained / pinned / rejected) that does not exist yet,
    which is recorded here as a warning rather than assumed away.
    """

    nodes = lineage.get("nodes")
    nodes = nodes if isinstance(nodes, Mapping) else {}
    head = active_head(lineage)
    live = reachable_from_heads(lineage, [head] if head else [])
    root = Path(session_dir)

    def artifact_directory(execution_id: str) -> Path:
        # Committed artifact directories are named ``<action>--<execution_id>``, so the ID alone no
        # longer reconstructs the path. Trust the directory recorded on the artifact record; fall
        # back to the legacy bare name for sessions written before descriptive naming.
        record = artifacts.get(execution_id)
        recorded = record.get("path") if isinstance(record, Mapping) else None
        if isinstance(recorded, str) and recorded:
            return root / recorded
        return root / "artifacts" / "capabilities" / execution_id

    defects = topology_defects(lineage)
    topology_reliable = not defects

    warnings: list[str] = []
    if not nodes:
        warnings.append(
            "no lineage versions recorded, so nothing can be judged unreachable; "
            "open the session once to reconstruct the forest before pruning"
        )
    if not topology_reliable:
        warnings.append(
            "this session's recorded topology does not explain how its work connects, so "
            "reachability cannot identify abandoned work; treat every candidate below as "
            "unverified and prune nothing here — " + "; ".join(defects)
        )

    candidates: list[ArtifactUsage] = []
    retained_paths: list[Path] = []
    retained_ids: list[str] = []
    for execution_id in sorted(nodes):
        node = nodes[execution_id]
        if not isinstance(node, Mapping):
            continue
        directory = artifact_directory(execution_id)
        if execution_id in live:
            retained_paths.append(directory)
            retained_ids.append(execution_id)

    for execution_id in sorted(nodes):
        node = nodes[execution_id]
        if not isinstance(node, Mapping) or execution_id in live:
            continue
        directory = artifact_directory(execution_id)
        created = node.get("created_by")
        tool = created.get("tool_name") if isinstance(created, Mapping) else None
        candidates.append(
            ArtifactUsage(
                execution_id=execution_id,
                path=str(directory.relative_to(root)),
                exists=directory.is_dir(),
                on_active_line=False,
                reachable=False,
                accounting=account([directory], retained=retained_paths),
                tool_name=str(tool) if isinstance(tool, str) else "",
                head_path=(
                    str(node["head_path"]) if isinstance(node.get("head_path"), str) else None
                ),
            )
        )

    if candidates and topology_reliable:
        warnings.append(
            "these versions are merely unreachable from the active head, which is not the same as "
            "spent; an explicit branch disposition (retained / pinned / rejected) is required "
            "before any of them may be deleted"
        )

    every_artifact = [
        artifact_directory(execution_id)
        for execution_id in sorted(artifacts)
        if isinstance(artifacts.get(execution_id), Mapping)
    ]
    candidate_total = account(
        [artifact_directory(item.execution_id) for item in candidates],
        retained=retained_paths,
    )
    if candidate_total.apparent_bytes and not candidate_total.reclaimable_bytes:
        warnings.append(
            "candidates hold no independent bytes: everything they contain is shared with a "
            "retained version, so deleting them would reclaim nothing"
        )

    return PruneProposal(
        session_id=session_id,
        candidates=tuple(candidates),
        retained=tuple(retained_ids),
        total=account(every_artifact),
        candidate_total=candidate_total,
        warnings=tuple(warnings),
        notes={
            "active_head": head,
            "versions": len(nodes),
            "versions_on_active_line": len(live),
            "active_line_depth": len(ancestry(lineage, head)) if head else 0,
            "committed_executions": len(artifacts),
            # A prune must consult this before acting on ``candidates`` at all.
            "topology_reliable": topology_reliable,
            "topology_defects": list(defects),
        },
    )
