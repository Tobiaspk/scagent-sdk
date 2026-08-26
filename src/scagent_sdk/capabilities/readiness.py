"""Startup inventory of host prerequisites a skill cannot obtain for itself.

Some capabilities depend on assets this project deliberately never downloads implicitly:
cached CellTypist classifiers, local SCimilarity reference models. Whether those assets exist
decides whether the model can answer "can you run this on my data?" at all, so the answer is
probed once at assembly time and rendered into the model's own instructions. Discovering it by
searching the filesystem mid-conversation is slow, unreliable, and unnecessary.

Probes are declarative skill code, run in the control plane with only the declared capability
environment's variables. A probe that fails or hangs degrades to a reported unknown; it never
blocks session assembly.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

from scagent_sdk.capabilities.registry import SkillPackage
from scagent_sdk.errors import ScagentSDKError
from scagent_sdk.execution.broker import EnvironmentBroker

READINESS_STATUSES = ("ready", "partial", "unavailable", "unknown")
DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0
GPU_QUERY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ReadinessReport:
    """One skill's local prerequisite state as of session assembly."""

    skill_id: str
    status: str
    summary: str
    details: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "status": self.status,
            "summary": self.summary,
            "details": list(self.details),
        }


def _coerce(skill_id: str, value: Any) -> ReadinessReport:
    if not isinstance(value, dict):
        return ReadinessReport(skill_id, "unknown", "readiness probe returned a non-mapping result")
    status = str(value.get("status", "unknown"))
    if status not in READINESS_STATUSES:
        return ReadinessReport(skill_id, "unknown", f"readiness probe reported {status!r}")
    summary = str(value.get("summary", "")).strip() or "no readiness summary reported"
    raw_details = value.get("details") or ()
    details = tuple(str(item).strip() for item in raw_details if str(item).strip())
    return ReadinessReport(skill_id, status, summary, details)


def _environment(package: SkillPackage, broker: EnvironmentBroker | None) -> dict[str, str]:
    readiness = package.manifest.readiness
    if readiness is None or readiness.environment is None or broker is None:
        return {}
    try:
        return broker.registry.resolve(readiness.environment).build_environment()
    except ScagentSDKError:
        return {}


def probe_packages(
    packages: tuple[SkillPackage, ...],
    *,
    broker: EnvironmentBroker | None = None,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> tuple[ReadinessReport, ...]:
    """Run every declared readiness probe, degrading failures to a reported unknown."""

    declared = [package for package in packages if package.manifest.readiness is not None]
    if not declared:
        return ()
    reports: list[ReadinessReport] = []
    with ThreadPoolExecutor(max_workers=min(4, len(declared))) as pool:
        pending = []
        for package in declared:
            skill_id = package.manifest.skill_id
            try:
                probe = package.load_readiness_probe()
            except ScagentSDKError as exc:
                reports.append(
                    ReadinessReport(skill_id, "unknown", f"readiness probe is unloadable: {exc}")
                )
                continue
            assert probe is not None
            pending.append((skill_id, pool.submit(probe, _environment(package, broker))))
        for skill_id, future in pending:
            try:
                reports.append(_coerce(skill_id, future.result(timeout=timeout_seconds)))
            except FutureTimeoutError:
                future.cancel()
                reports.append(
                    ReadinessReport(
                        skill_id,
                        "unknown",
                        f"readiness probe did not finish within {timeout_seconds:g}s",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - a broken probe must not block assembly
                reports.append(
                    ReadinessReport(skill_id, "unknown", f"readiness probe failed: {exc}")
                )
    return tuple(sorted(reports, key=lambda report: report.skill_id))


@dataclass(frozen=True)
class EnvironmentReadiness:
    """Structural availability of one logical compute environment and who routes to it."""

    name: str
    status: str
    summary: str
    skills: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "skills": list(self.skills),
        }


def _gpu_snapshot() -> tuple[int, int | None]:
    """Visible GPU count and largest free memory in MiB, or (0, None) when unavailable.

    Presence is counted from device index rows, independently of memory reporting. Unified-memory
    parts such as the GB10 report ``memory.free`` as ``[N/A]`` even though the device is present and
    usable, so inferring presence from parseable free-memory would wrongly count zero GPUs. Free
    memory is parsed opportunistically per row and left as ``None`` when the device does not expose
    it, which downstream renders as "free memory unknown" rather than blocking the environment.
    """

    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=GPU_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, None
    if completed.returncode != 0:
        return 0, None
    count = 0
    free: list[int] = []
    for line in completed.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if not fields or not fields[0].isdigit():
            continue
        count += 1
        if len(fields) > 1 and fields[1].isdigit():
            free.append(int(fields[1]))
    return count, max(free) if free else None


def probe_environments(
    packages: tuple[SkillPackage, ...],
    *,
    broker: EnvironmentBroker | None = None,
) -> tuple[EnvironmentReadiness, ...]:
    """Report whether each environment skills route to can run at all.

    This is a structural check — configured, interpreter present, GPU visible — not the full
    import probe behind `scagent-sdk doctor environment`, because session start must stay fast.
    It exists so the model knows which capabilities are live on this host without testing them
    by running one.
    """

    users: dict[str, set[str]] = {}
    for package in packages:
        for tool in package.manifest.tools:
            users.setdefault(tool.environment, set()).add(package.manifest.skill_id)
    if not users:
        return ()
    gpu_count, gpu_free_mb = _gpu_snapshot()
    reports: list[EnvironmentReadiness] = []
    for name in sorted(users):
        skills = tuple(sorted(users[name]))
        if name == "current":
            reports.append(
                EnvironmentReadiness(
                    name, "ready", "in-process control plane; always available", skills
                )
            )
            continue
        if broker is None:
            reports.append(
                EnvironmentReadiness(
                    name, "unknown", "no environment profile is configured", skills
                )
            )
            continue
        try:
            profile = broker.registry.resolve(name)
        except ScagentSDKError as exc:
            reports.append(EnvironmentReadiness(name, "unavailable", str(exc), skills))
            continue
        if not profile.python.is_file():
            reports.append(
                EnvironmentReadiness(
                    name, "unavailable", f"interpreter is missing: {profile.python}", skills
                )
            )
            continue
        modules = ", ".join(profile.required_modules) or "no declared modules"
        if not profile.gpu_required:
            reports.append(EnvironmentReadiness(name, "ready", f"CPU runtime; {modules}", skills))
            continue
        if gpu_count == 0:
            reports.append(
                EnvironmentReadiness(
                    name, "unavailable", "requires a GPU and none is visible", skills
                )
            )
            continue
        if gpu_free_mb is not None and gpu_free_mb < profile.min_gpu_memory_mb:
            reports.append(
                EnvironmentReadiness(
                    name,
                    "partial",
                    f"needs {profile.min_gpu_memory_mb:,} MiB free but the largest free GPU has "
                    f"{gpu_free_mb:,} MiB",
                    skills,
                )
            )
            continue
        free = f"{gpu_free_mb:,} MiB free" if gpu_free_mb is not None else "free memory unknown"
        reports.append(
            EnvironmentReadiness(
                name, "ready", f"{gpu_count} GPU(s), {free}; {modules}", skills
            )
        )
    return tuple(reports)


def render_readiness_block(
    reports: tuple[ReadinessReport, ...],
    environments: tuple[EnvironmentReadiness, ...] = (),
) -> str:
    """Render readiness as the authoritative prerequisite section of the system prompt."""

    if not reports and not environments:
        return ""
    lines = [
        "## Local prerequisites on this host",
        "",
        "Probed at session start. This is the authoritative inventory of what can actually run "
        "here — compute environments and the reference models, credentials, and other assets "
        "that are never obtained implicitly. Answer availability questions from it directly; "
        "never search the filesystem, list packages, or run a tool just to find out whether "
        "something is installed. Treat it as a startup snapshot: if a tool later reports an "
        "asset missing or newly present, the tool result wins.",
        "",
    ]
    if environments:
        lines.append("### Compute environments")
        lines.append("")
        for environment in environments:
            used_by = ", ".join(environment.skills)
            lines.append(
                f"- **{environment.name}** — {environment.status.upper()}: "
                f"{environment.summary} (used by {used_by})"
            )
        lines.append("")
        lines.append(
            "Environment status is structural (configured, interpreter present, GPU visible); "
            "module imports are verified when a tool actually runs."
        )
        lines.append("")
    if reports:
        lines.append("### Skill assets")
        lines.append("")
        for report in reports:
            lines.append(f"- **{report.skill_id}** — {report.status.upper()}: {report.summary}")
            lines.extend(f"  - {detail}" for detail in report.details)
    return "\n".join(lines).rstrip()
