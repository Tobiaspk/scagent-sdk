"""Pure lineage contracts: identity signatures, fact scope, and forest topology.

These pin the decisions that the executor half will rely on, including the two that a previous
design round got wrong: parentage must come from the resolved input rather than the active head,
and an ancestor input must be distinguishable from a sibling.
"""

from __future__ import annotations

import pytest

from scagent_sdk.state.lineage import (
    FACT_ROOT_SCOPES,
    IDENTITY_AXES_V1,
    LineageContractError,
    LineageNode,
    active_head,
    ancestry,
    checkout,
    classify_input,
    empty_forest,
    fact_scope,
    head_path,
    identity_signature,
    merge_diff,
    node_for_path,
    node_scoped_roots,
    partition_facts_patch,
    place_node,
    reachable_from_heads,
)
from scagent_sdk.state.store import apply_merge_patch


def _node(
    execution_id: str,
    *,
    parent: str | None,
    path: str | None = None,
    signature: str = "identity:v1:sha256:aa",
    branch: bool = False,
) -> LineageNode:
    return LineageNode(
        execution_id=execution_id,
        parent_execution_id=parent,
        head_path=path or f"artifacts/capabilities/{execution_id}/matrix.h5ad",
        identity_signature=signature,
        requested_input=None,
        resolved_input_execution_id=parent,
        branch_intent=branch,
        skill_id="test-skill",
        tool_name="test_tool",
    )


# --- identity signature --------------------------------------------------------------------


def test_signature_covers_every_axis_present_in_a_real_session() -> None:
    """The eight axes the reference session actually carries must all be registered.

    A shorter allowlist combined with fail-closed behaviour would raise on the reference session
    the first time it was opened.
    """

    facts = {
        "analysis": {
            axis: {"id": f"{axis}:sha256:{index}"}
            for index, axis in enumerate(IDENTITY_AXES_V1)
        }
    }
    assert identity_signature(facts).startswith("identity:v1:sha256:")


def test_signature_changes_when_any_axis_changes() -> None:
    base = {"analysis": {"cell_set": {"id": "cells:1"}, "clustering": {"id": "clust:1"}}}
    changed = {"analysis": {"cell_set": {"id": "cells:1"}, "clustering": {"id": "clust:2"}}}
    assert identity_signature(base) != identity_signature(changed)


def test_absent_axis_is_not_the_same_as_a_cleared_one() -> None:
    """Omitted axes are dropped, not encoded as null, so 'not yet clustered' is its own state."""

    absent = {"analysis": {"cell_set": {"id": "cells:1"}}}
    cleared = {"analysis": {"cell_set": {"id": "cells:1"}, "clustering": {}}}
    assert identity_signature(absent) == identity_signature(cleared)


def test_signature_is_stable_under_key_order_and_extra_fields() -> None:
    a = {"analysis": {"cell_set": {"id": "cells:1", "n_cells": 10}, "hvg": {"id": "hvg:1"}}}
    b = {"analysis": {"hvg": {"id": "hvg:1", "flavor": "seurat"}, "cell_set": {"id": "cells:1"}}}
    assert identity_signature(a) == identity_signature(b)


def test_unregistered_identity_axis_fails_closed() -> None:
    facts = {"analysis": {"cell_set": {"id": "cells:1"}, "feature_schema": {"id": "feat:1"}}}
    with pytest.raises(LineageContractError, match="unregistered identity axis"):
        identity_signature(facts)


def test_signature_tolerates_missing_analysis() -> None:
    assert identity_signature({}) == identity_signature({"analysis": {}})


# --- fact scope ----------------------------------------------------------------------------


def test_reference_runs_is_session_scoped_despite_nesting_a_cell_set_id() -> None:
    """Scope is semantic. A shape rule would classify this node-scoped and duplicate the cache."""

    assert fact_scope("reference_runs") == "session"
    assert fact_scope("annotation") == "node"
    assert fact_scope("group_gene_ranking") == "node"


def test_every_registered_root_has_a_valid_scope() -> None:
    assert set(FACT_ROOT_SCOPES.values()) == {"node", "session"}
    assert node_scoped_roots() == {
        root for root, scope in FACT_ROOT_SCOPES.items() if scope == "node"
    }


def test_unregistered_fact_root_fails_closed() -> None:
    with pytest.raises(LineageContractError, match="unregistered fact root"):
        fact_scope("pathways")


def test_merge_diff_replaces_rather_than_merges() -> None:
    """A merge patch only names keys, so switching lines of descent needs an explicit diff.

    ``{"cluster_qc": new}`` merges recursively and leaves stale nested keys behind; the whole point
    is that the result equals the target exactly.
    """

    current = {"cluster_qc": {"axes": {"metric": 1, "deg": 2}}, "batch": {"decision": "keep"}}
    target = {"cluster_qc": {"axes": {"metric": 9}}}
    patch = merge_diff(current, target)

    assert patch == {"cluster_qc": {"axes": {"deg": None, "metric": 9}}, "batch": None}
    assert apply_merge_patch(current, patch) == target


def test_merge_diff_of_identical_values_is_empty() -> None:
    value = {"annotation": {"evidence": {"markers": "complete"}}}
    assert merge_diff(value, value) == {}


def test_merge_diff_handles_type_changes_and_additions() -> None:
    current = {"a": {"nested": 1}, "keep": 1}
    target = {"a": "scalar", "b": [1, 2], "keep": 1}
    patch = merge_diff(current, target)

    assert apply_merge_patch(current, patch) == target


def test_patch_is_split_by_scope() -> None:
    node, session = partition_facts_patch(
        {"annotation": {"evidence": 1}, "gene_conversion": {"changed": True}, "cluster_qc": None}
    )
    assert node == {"annotation": {"evidence": 1}, "cluster_qc": None}
    assert session == {"gene_conversion": {"changed": True}}


def test_partition_rejects_an_unregistered_root() -> None:
    with pytest.raises(LineageContractError):
        partition_facts_patch({"annotation": {}, "unknown_root": {}})


# --- forest topology -----------------------------------------------------------------------


def test_first_node_becomes_the_active_head() -> None:
    forest = place_node(empty_forest(), _node("a", parent=None))
    assert active_head(forest) == "a"
    assert head_path(forest) == "artifacts/capabilities/a/matrix.h5ad"


def test_sequential_nodes_extend_one_line_of_descent() -> None:
    forest = place_node(empty_forest(), _node("a", parent=None))
    forest = place_node(forest, _node("b", parent="a"))
    forest = place_node(forest, _node("c", parent="b"))
    assert active_head(forest) == "c"
    assert ancestry(forest, "c") == ["c", "b", "a"]


def test_three_runs_from_one_parent_are_siblings_not_a_chain() -> None:
    """Acceptance case 1. Parentage comes from the resolved input, not the mutating active head.

    Deriving a parent from ``active`` recorded the reference session's three clusterings -- all of
    which read one UMAP -- as a three-link chain, fabricating ancestry.
    """

    forest = place_node(empty_forest(), _node("umap", parent=None))
    for name in ("clust1", "clust2", "clust3"):
        forest = place_node(forest, _node(name, parent="umap", branch=True))

    for name in ("clust1", "clust2", "clust3"):
        assert ancestry(forest, name) == [name, "umap"]
    # None of them is an ancestor of another.
    assert "clust1" not in ancestry(forest, "clust3")


def test_branch_commit_leaves_the_active_head_alone() -> None:
    """Acceptance case 4/9: a branch is reachable without becoming the working head."""

    forest = place_node(empty_forest(), _node("a", parent=None))
    forest = place_node(forest, _node("branch", parent="a", branch=True))

    assert active_head(forest) == "a"
    assert set(forest["nodes"]) == {"a", "branch"}
    assert classify_input(forest, "branch") == "sibling"


def test_ancestor_input_is_distinguished_from_sibling() -> None:
    """Acceptance case 3. Rejecting only siblings is what let two annotators diverge.

    Once the first annotator advanced the head, the artifact both were handed became an ancestor.
    """

    forest = place_node(empty_forest(), _node("clustered", parent=None))
    forest = place_node(forest, _node("scimilarity", parent="clustered"))

    assert classify_input(forest, "scimilarity") == "head"
    assert classify_input(forest, "clustered") == "ancestor"
    assert classify_input(forest, "never-seen") == "untracked"
    assert classify_input(forest, None) == "untracked"


def test_checkout_moves_the_head_without_touching_topology() -> None:
    forest = place_node(empty_forest(), _node("a", parent=None))
    forest = place_node(forest, _node("branch", parent="a", branch=True))
    switched = checkout(forest, "branch")

    assert active_head(switched) == "branch"
    assert switched["nodes"] == forest["nodes"]
    assert classify_input(switched, "a") == "ancestor"


def test_checkout_of_an_unknown_node_fails() -> None:
    with pytest.raises(LineageContractError, match="unknown lineage node"):
        checkout(empty_forest(), "nope")


def test_input_path_resolves_to_its_producing_execution() -> None:
    forest = place_node(empty_forest(), _node("a", parent=None))
    relative = "artifacts/capabilities/a/matrix.h5ad"

    assert node_for_path(forest, relative) == "a"
    assert node_for_path(forest, f"/home/user/sessions/run_1/{relative}") == "a"
    assert node_for_path(forest, "artifacts/capabilities/zzz/matrix.h5ad") is None
    assert node_for_path(forest, "") is None


def test_reachability_excludes_abandoned_branches() -> None:
    """The prerequisite for pruning: two dead sweep branches are not reachable from live work."""

    forest = place_node(empty_forest(), _node("umap", parent=None))
    for name in ("chosen", "dead1", "dead2"):
        forest = place_node(forest, _node(name, parent="umap", branch=True))
    forest = place_node(forest, _node("annotated", parent="chosen"))

    keep = reachable_from_heads(forest, ["annotated"])
    assert keep == {"annotated", "chosen", "umap"}
    assert {"dead1", "dead2"}.isdisjoint(keep)


def test_ancestry_terminates_on_a_corrupted_cycle() -> None:
    forest = {
        "active_execution_id": "a",
        "nodes": {
            "a": {"parent_execution_id": "b", "head_path": "x"},
            "b": {"parent_execution_id": "a", "head_path": "y"},
        },
    }
    assert ancestry(forest, "a") == ["a", "b"]


def test_empty_forest_has_no_head() -> None:
    assert active_head(empty_forest()) is None
    assert head_path(empty_forest()) is None
    assert reachable_from_heads(empty_forest(), []) == set()
