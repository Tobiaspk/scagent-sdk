from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from scagent_sdk.capabilities.assembly import CapabilityAssembler
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.session import AnalysisSession


class HookMatcher:
    def __init__(self, *, matcher, hooks):
        self.matcher = matcher
        self.hooks = hooks


def tool(name, description, input_schema):
    def decorate(handler):
        return SimpleNamespace(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )

    return decorate


def create_sdk_mcp_server(name, *, version, tools):
    return {"name": name, "version": version, "tools": tools}


def test_assembler_builds_allowlisted_tool_and_committing_post_hook(tmp_path: Path) -> None:
    sdk = SimpleNamespace(
        tool=tool,
        HookMatcher=HookMatcher,
        create_sdk_mcp_server=create_sdk_mcp_server,
    )
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    session = AnalysisSession.create(tmp_path / "sessions", title="assembly")
    assembler = CapabilityAssembler(CapabilityRegistry(skills_root), session, sdk_module=sdk)
    extensions = assembler.assemble()
    dataset = tmp_path / "matrix.mtx"
    dataset.write_text("%%MatrixMarket matrix coordinate integer general\n", encoding="utf-8")

    assert "mcp__inspect_dataset__inspect_dataset" in extensions.allowed_tools
    assert "orchestrate-single-cell" in extensions.skills
    transforming = next(
        candidate
        for server in extensions.mcp_servers.values()
        for candidate in server["tools"]
        if candidate.name == "convert_gene_ids"
    )
    assert "adopt_untracked" in transforming.input_schema["properties"]
    registered = extensions.mcp_servers["inspect_dataset"]["tools"][0]
    response = asyncio.run(registered.handler({"path": str(dataset)}))
    hook = extensions.hooks["PostToolUse"][0].hooks[0]
    asyncio.run(hook({"tool_response": response}, "use-1", {}))

    assert session.store.state.facts["dataset"]["format"]["byte_signature"] == "matrix-market"


def test_assembler_denies_floor_bound_tool_until_state_satisfies_floor(tmp_path: Path) -> None:
    sdk = SimpleNamespace(
        tool=tool,
        HookMatcher=HookMatcher,
        create_sdk_mcp_server=create_sdk_mcp_server,
    )
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    session = AnalysisSession.create(tmp_path / "sessions", title="floors")
    extensions = CapabilityAssembler(
        CapabilityRegistry(skills_root), session, sdk_module=sdk
    ).assemble()

    matcher = next(
        item
        for item in extensions.hooks["PreToolUse"]
        if item.matcher
        == "mcp__cellbender_background_removal__remove_ambient_background"
    )
    result = asyncio.run(matcher.hooks[0]({}, "use-floor", {}))

    specific = result["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "dataset_identity" in specific["permissionDecisionReason"]
    assert session.store.events()[-1].kind == "floor.denied"


def test_assembler_gives_the_model_local_prerequisites_and_records_the_probe(
    tmp_path: Path,
) -> None:
    sdk = SimpleNamespace(
        tool=tool,
        HookMatcher=HookMatcher,
        create_sdk_mcp_server=create_sdk_mcp_server,
    )
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    session = AnalysisSession.create(tmp_path / "sessions", title="readiness")

    extensions = CapabilityAssembler(
        CapabilityRegistry(skills_root), session, sdk_module=sdk
    ).assemble()

    suffix = extensions.system_prompt_suffix
    assert "## Local prerequisites on this host" in suffix
    assert "celltypist-annotation" in suffix
    assert "scimilarity-annotation" in suffix
    # Skill instructions ride in the same suffix, so the readiness block must survive alongside.
    assert "### celltypist-annotation" in suffix
    assert "not a universal default" in suffix.replace("\n", " ")
    injected = next(
        event
        for event in session.store.events()
        if event.kind == "capability.instructions_injected"
    )
    assert len(injected.payload["skills"]) >= 21
    assert injected.payload["bytes"] > 20_000
    probed = next(
        event for event in session.store.events() if event.kind == "capability.readiness_probed"
    )
    reported = {report["skill_id"] for report in probed.payload["reports"]}
    assert reported == {"celltypist-annotation", "research-web", "scimilarity-annotation"}
    # Every environment a skill routes to is covered, not only those with asset probes.
    environments = {report["name"] for report in probed.payload["environments"]}
    assert {"current", "gpu-singlecell", "celltypist", "scimilarity"} <= environments
    assert "### Compute environments" in suffix


def test_assembler_grants_only_the_skill_builtin_and_publishes_skills_as_a_plugin(
    tmp_path: Path,
) -> None:
    """Skill instructions must reach the model without importing project settings.

    `setting_sources: ["project"]` would also load this repository's CLAUDE.md/AGENTS.md — coding
    instructions — into a scientific session, so skills are published as a local plugin instead.
    """

    sdk = SimpleNamespace(
        tool=tool,
        HookMatcher=HookMatcher,
        create_sdk_mcp_server=create_sdk_mcp_server,
    )
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    session = AnalysisSession.create(tmp_path / "sessions", title="skill plugin")

    extensions = CapabilityAssembler(
        CapabilityRegistry(skills_root), session, sdk_module=sdk
    ).assemble()

    assert extensions.tools == ("Skill",)
    assert len(extensions.plugins) == 1
    plugin = extensions.plugins[0]
    assert plugin["type"] == "local"
    published = Path(plugin["path"]) / "skills"
    assert published.resolve() == skills_root.resolve()
    assert (Path(plugin["path"]) / ".claude-plugin" / "plugin.json").is_file()
    # The skill allowlist still gates which skills may be loaded.
    assert "celltypist-annotation" in (extensions.skills or ())
