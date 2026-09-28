"""Render every skill's instructions into the model's standing context.

Progressive disclosure — letting the model decide when to load a skill — is built for an assistant
with hundreds of mostly irrelevant skills. This project has one domain, ~20 skills, and a very
large context window, so deferring the read buys little and costs the failure it was meant to
prevent: a model that never reaches for the guidance and improvises instead.

Instructions are therefore always present. The deeper `references/` material is NOT inlined; its
links are relative inside `SKILL.md` and only resolve if the skill's own directory is known, so
each section states its absolute base directory and the model reads those files from disk. That
path works in every harness — a `Skill` tool is not relied on here, because not every host exposes
one (the opencode bridge disables it outright to keep a same-named global skill from being loaded).
"""

from __future__ import annotations

from pathlib import Path

from scagent_sdk.capabilities.registry import DiscoveredSkill

# A soft ceiling so a pathological skill cannot silently consume the context window. Sized against
# the window it protects rather than as a round number: 192 KB is roughly 48k tokens, under a fifth
# of the 262k context the local Qwen deployment serves and about a quarter of a 200k one.
#
# Keep the headroom honest when adding skills. The corpus has grown 44 KB -> ~97 KB across 27
# skills (2026-09), so the previous 128 KB ceiling was within ~5 skills of truncating. Overflow is
# reported with the absolute path of each omitted SKILL.md rather than trimmed invisibly.
INSTRUCTION_BUDGET_BYTES = 192 * 1024


def strip_frontmatter(text: str) -> str:
    """Return the instruction body without its YAML frontmatter block."""

    if not text.startswith("---\n"):
        return text.strip()
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) == 3 else text.strip()


def render_skill_instructions(
    skills: tuple[DiscoveredSkill, ...],
    *,
    budget_bytes: int = INSTRUCTION_BUDGET_BYTES,
) -> str:
    """Render one section per skill, in a stable order, bounded by a total byte budget."""

    if not skills:
        return ""
    lines = [
        "## Skill instructions",
        "",
        "The scientific contract for every capability follows: when it applies, what its inputs "
        "mean, how to read its output, and where it misleads. This is standing context — you do "
        "not need to load it, and a tool schema alone never replaces it. Each section names the "
        "skill's absolute directory; resolve its relative `references/` links against that "
        "directory when you need the deeper method detail.",
        "",
    ]
    used = sum(len(line) + 1 for line in lines)
    # The two ways a skill can fall out are reported separately: they need different fixes, and
    # calling an unreadable file "over budget" would send the reader after the wrong problem.
    unreadable: list[str] = []
    over_budget: list[tuple[str, Path]] = []
    for skill in sorted(skills, key=lambda item: item.name):
        instructions = skill.root / "SKILL.md"
        try:
            body = strip_frontmatter(instructions.read_text(encoding="utf-8"))
        except OSError:
            unreadable.append(skill.name)
            continue
        section = f"### {skill.name}\n\nDirectory: `{skill.root}`\n\n{body}\n"
        if used + len(section) > budget_bytes:
            over_budget.append((skill.name, instructions))
            continue
        lines.append(section)
        used += len(section)
    if over_budget:
        listed = ", ".join(f"{name} (`{path}`)" for name, path in over_budget)
        lines.append(
            "The instructions for these skills exceeded the inline budget and are NOT included "
            f"above. Read the named file before using one: {listed}."
        )
    if unreadable:
        lines.append(
            "The instructions for these skills could not be read and are NOT included above: "
            f"{', '.join(sorted(unreadable))}."
        )
    return "\n".join(lines).rstrip()


def instruction_sources(skills: tuple[DiscoveredSkill, ...]) -> list[dict[str, str]]:
    """Provenance for what was injected, recorded alongside capability assembly."""

    return [
        {
            "skill": skill.name,
            "path": str(skill.root / "SKILL.md"),
            "fingerprint": skill.fingerprint,
        }
        for skill in sorted(skills, key=lambda item: item.name)
        if (Path(skill.root) / "SKILL.md").is_file()
    ]
