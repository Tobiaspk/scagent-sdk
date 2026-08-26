from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.manifest import CapabilityManifest, CapabilityReadiness
from scagent_sdk.capabilities.readiness import (
    ReadinessReport,
    probe_packages,
    render_readiness_block,
)
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.errors import CapabilityManifestError

SKILLS_ROOT = Path(__file__).parents[2] / ".claude" / "skills"

MANIFEST = """
schema_version: 1
skill:
  id: probe-skill
  version: "0.1.0"
  description: Skill with a declared readiness probe.
readiness:
  entrypoint: scripts/readiness.py:probe
  environment: demo
tools:
  - name: do_nothing
    description: Deterministic no-op used to satisfy the manifest contract.
    entrypoint: scripts/tool.py:run
    input_schema:
      type: object
      properties: {}
      additionalProperties: false
"""


def _skill(root: Path, probe_body: str) -> Path:
    package = root / "probe-skill"
    (package / "scripts").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: probe-skill\n---\n\nProbe skill.\n", encoding="utf-8"
    )
    (package / "capability.yaml").write_text(MANIFEST, encoding="utf-8")
    (package / "scripts" / "tool.py").write_text(
        "def run(arguments, context):\n    return {'summary': 'ok'}\n", encoding="utf-8"
    )
    (package / "scripts" / "readiness.py").write_text(probe_body, encoding="utf-8")
    return package


def _load_skill_probe(skill_id: str) -> Any:
    module_path = SKILLS_ROOT / skill_id / "scripts" / "readiness.py"
    sys.path.insert(0, str(module_path.parent))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(f"{skill_id}_readiness", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(module_path.parent))


def test_manifest_parses_and_round_trips_declared_readiness(tmp_path: Path) -> None:
    manifest_path = _skill(tmp_path, "def probe(environment):\n    return {}\n") / "capability.yaml"
    manifest = CapabilityManifest.from_path(manifest_path)

    assert manifest.readiness == CapabilityReadiness(
        entrypoint="scripts/readiness.py:probe", environment="demo"
    )
    assert manifest.to_dict()["readiness"]["entrypoint"] == "scripts/readiness.py:probe"


def test_manifest_readiness_is_optional(tmp_path: Path) -> None:
    package = _skill(tmp_path, "def probe(environment):\n    return {}\n")
    text = (package / "capability.yaml").read_text(encoding="utf-8")
    without = text.replace(
        "readiness:\n  entrypoint: scripts/readiness.py:probe\n  environment: demo\n", ""
    )
    (package / "capability.yaml").write_text(without, encoding="utf-8")

    assert CapabilityManifest.from_path(package / "capability.yaml").readiness is None


def test_absolute_readiness_entrypoint_is_rejected() -> None:
    with pytest.raises(CapabilityManifestError):
        CapabilityReadiness(entrypoint="/etc/readiness.py:probe")


def test_discovery_rejects_a_readiness_probe_that_cannot_load(tmp_path: Path) -> None:
    package = _skill(tmp_path, "def other_name(environment):\n    return {}\n")
    assert package.is_dir()

    with pytest.raises(CapabilityManifestError, match="entrypoint function is missing"):
        CapabilityRegistry(tmp_path).discover()


def test_probe_reports_declared_readiness_with_resolved_environment(tmp_path: Path) -> None:
    _skill(
        tmp_path,
        "def probe(environment):\n"
        "    return {\n"
        "        'status': 'partial',\n"
        "        'summary': 'one of two assets present',\n"
        "        'details': [f\"seen: {environment.get('DEMO_ASSET', 'unset')}\", '', 'kept'],\n"
        "    }\n",
    )
    packages = CapabilityRegistry(tmp_path).discover()

    class Broker:
        registry = type(
            "Registry",
            (),
            {
                "resolve": staticmethod(
                    lambda name: type(
                        "Profile",
                        (),
                        {"build_environment": staticmethod(lambda: {"DEMO_ASSET": "/models/demo"})},
                    )()
                )
            },
        )()

    reports = probe_packages(packages, broker=Broker())  # type: ignore[arg-type]

    assert reports == (
        ReadinessReport(
            skill_id="probe-skill",
            status="partial",
            summary="one of two assets present",
            details=("seen: /models/demo", "kept"),
        ),
    )


def test_broken_probe_degrades_to_unknown_without_blocking_assembly(tmp_path: Path) -> None:
    _skill(tmp_path, "def probe(environment):\n    raise RuntimeError('cache is corrupt')\n")
    packages = CapabilityRegistry(tmp_path).discover()

    reports = probe_packages(packages)

    assert reports[0].status == "unknown"
    assert "cache is corrupt" in reports[0].summary


def test_slow_probe_is_bounded_by_the_probe_timeout(tmp_path: Path) -> None:
    _skill(tmp_path, "import time\n\n\ndef probe(environment):\n    time.sleep(5)\n    return {}\n")
    packages = CapabilityRegistry(tmp_path).discover()

    reports = probe_packages(packages, timeout_seconds=0.2)

    assert reports[0].status == "unknown"
    assert "did not finish" in reports[0].summary


def test_unrecognized_probe_status_is_not_trusted(tmp_path: Path) -> None:
    _skill(tmp_path, "def probe(environment):\n    return {'status': 'great', 'summary': 'x'}\n")
    packages = CapabilityRegistry(tmp_path).discover()

    assert probe_packages(packages)[0].status == "unknown"


def test_rendered_block_is_empty_without_reports() -> None:
    assert render_readiness_block(()) == ""


def test_rendered_block_states_status_and_forbids_filesystem_discovery() -> None:
    block = render_readiness_block(
        (
            ReadinessReport("celltypist-annotation", "ready", "2 cached classifiers", ("a", "b")),
            ReadinessReport("scimilarity-annotation", "unavailable", "no local model"),
        )
    )

    assert "**celltypist-annotation** — READY: 2 cached classifiers" in block
    assert "**scimilarity-annotation** — UNAVAILABLE: no local model" in block
    assert "never search the filesystem" in block
    assert "  - a" in block


def test_celltypist_probe_lists_cached_models(tmp_path: Path) -> None:
    module = _load_skill_probe("celltypist-annotation")
    models = tmp_path / ".celltypist" / "data" / "models"
    models.mkdir(parents=True)
    (models / "Human_Colorectal_Cancer.pkl").write_bytes(b"0")
    (models / "Immune_All_Low.pkl").write_bytes(b"0")
    (models / "notes.txt").write_text("ignored", encoding="utf-8")

    result = module.probe({"HOME": str(tmp_path)})

    assert result["status"] == "ready"
    assert result["summary"] == "2 cached classifiers available"
    assert "Human_Colorectal_Cancer.pkl, Immune_All_Low.pkl" in result["details"][1]


def test_celltypist_probe_honours_celltypist_folder_and_reports_empty_cache(tmp_path: Path) -> None:
    module = _load_skill_probe("celltypist-annotation")
    folder = tmp_path / "shared"
    (folder / "data" / "models").mkdir(parents=True)

    assert module.models_directory({"CELLTYPIST_FOLDER": str(folder)}) == folder / "data" / "models"
    assert module.probe({"CELLTYPIST_FOLDER": str(folder)})["status"] == "unavailable"
    assert module.probe({"HOME": str(tmp_path / "empty")})["status"] == "unavailable"


def test_scimilarity_probe_separates_complete_and_incomplete_models(tmp_path: Path) -> None:
    module = _load_skill_probe("scimilarity-annotation")
    human = tmp_path / "human_v2"
    human.mkdir()
    (human / "encoder.ckpt").write_bytes(b"0")
    (human / "gene_order.tsv").write_text("A1BG\nCD3D\n\n", encoding="utf-8")
    mouse = tmp_path / "mouse_v1"
    mouse.mkdir()
    (mouse / "gene_order.tsv").write_text("Cd3d\n", encoding="utf-8")

    result = module.probe(
        {
            "SCIMILARITY_MODEL_PATH": str(human),
            "SCIMILARITY_MODEL_PATH_MOUSE": str(mouse),
        }
    )

    assert result["status"] == "partial"
    assert result["summary"] == (
        "human model usable; mouse unavailable; no atlas query index on this host"
    )
    assert "2-gene reference vocabulary" in result["details"][0]
    assert "query_reference_cells is unavailable" in result["details"][0]
    assert "missing encoder.ckpt" in result["details"][1]


def test_scimilarity_probe_reports_the_atlas_query_index_and_its_size(tmp_path: Path) -> None:
    """Index presence and load cost are host facts the model cannot discover for itself."""

    module = _load_skill_probe("scimilarity-annotation")
    human = tmp_path / "human_v2"
    (human / "cellsearch" / "cell_metadata").mkdir(parents=True)
    (human / "encoder.ckpt").write_bytes(b"0")
    (human / "gene_order.tsv").write_text("CD3D\n", encoding="utf-8")
    (human / "cellsearch" / "full_kNN.bin").write_bytes(b"x" * (3 * 1024**3))

    result = module.probe({"SCIMILARITY_MODEL_PATH": str(human)})

    assert "atlas query available for human" in result["summary"]
    assert "cell-search index present (3.0 GiB" in result["details"][0]
    assert "one call" in result["details"][0]


def test_scimilarity_probe_reports_unconfigured_and_absent_paths(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_skill_probe("scimilarity-annotation")
    monkeypatch.delenv("SCIMILARITY_MODEL_PATH", raising=False)
    monkeypatch.delenv("SCIMILARITY_MODEL_PATH_MOUSE", raising=False)

    result = module.probe({"SCIMILARITY_MODEL_PATH": str(tmp_path / "absent")})

    assert result["status"] == "unavailable"
    assert "configured path is absent" in result["details"][0]
    assert "SCIMILARITY_MODEL_PATH_MOUSE is not configured" in result["details"][1]


# --- environment-level readiness ---------------------------------------------


def _environment_broker(profiles: dict[str, Any]) -> Any:
    class Registry:
        @staticmethod
        def resolve(name: str) -> Any:
            if name not in profiles:
                raise CapabilityManifestError(f"unknown environment: {name}")
            return profiles[name]

    return type("Broker", (), {"registry": Registry()})()


def _profile(
    *, python: Path, gpu_required: bool = False, min_gpu_memory_mb: int = 0, modules: tuple = ()
) -> Any:
    return type(
        "Profile",
        (),
        {
            "python": python,
            "gpu_required": gpu_required,
            "min_gpu_memory_mb": min_gpu_memory_mb,
            "required_modules": modules,
        },
    )()


def test_every_environment_skills_route_to_is_reported_with_its_users() -> None:
    from scagent_sdk.capabilities.readiness import probe_environments

    packages = CapabilityRegistry(SKILLS_ROOT).discover()

    reports = probe_environments(packages, broker=None)

    names = {report.name for report in reports}
    routed = {tool.environment for package in packages for tool in package.manifest.tools}
    assert names == routed
    current = next(report for report in reports if report.name == "current")
    assert current.status == "ready"
    assert "inspect-media" in current.skills
    # Without a broker nothing can be claimed about a configured compute environment.
    assert all(
        report.status == "unknown" for report in reports if report.name not in {"current"}
    )


def test_missing_interpreter_and_absent_gpu_are_reported_as_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    from scagent_sdk.capabilities import readiness as readiness_module

    _skill(tmp_path, "def probe(environment):\n    return {}\n")
    packages = CapabilityRegistry(tmp_path).discover()
    monkeypatch.setattr(readiness_module, "_gpu_snapshot", lambda: (0, None))
    missing = _profile(python=tmp_path / "absent" / "python")
    reports = readiness_module.probe_environments(
        packages, broker=_environment_broker({"current": missing})
    )
    assert reports[0].name == "current" and reports[0].status == "ready"

    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    gpu_profile = _profile(python=interpreter, gpu_required=True, min_gpu_memory_mb=4096)

    class Package:
        manifest = type(
            "Manifest",
            (),
            {
                "skill_id": "demo",
                "tools": (type("Tool", (), {"environment": "gpu-env"})(),),
                "readiness": None,
            },
        )()

    reports = readiness_module.probe_environments(
        (Package(),), broker=_environment_broker({"gpu-env": gpu_profile})
    )
    assert reports[0].status == "unavailable"
    assert "none is visible" in reports[0].summary

    monkeypatch.setattr(readiness_module, "_gpu_snapshot", lambda: (1, 2048))
    reports = readiness_module.probe_environments(
        (Package(),), broker=_environment_broker({"gpu-env": gpu_profile})
    )
    assert reports[0].status == "partial"
    assert "4,096 MiB free" in reports[0].summary

    monkeypatch.setattr(readiness_module, "_gpu_snapshot", lambda: (2, 81000))
    reports = readiness_module.probe_environments(
        (Package(),), broker=_environment_broker({"gpu-env": gpu_profile})
    )
    assert reports[0].status == "ready"
    assert "2 GPU(s)" in reports[0].summary


def test_gpu_snapshot_counts_unified_memory_devices_reporting_na_free(monkeypatch) -> None:
    # The GB10 (DGX Spark) enumerates as index 0 but reports memory.free as "[N/A]".
    # Presence must be recovered from the index row; free memory degrades to unknown.
    from scagent_sdk.capabilities import readiness as readiness_module

    def fake_run(cmd: list[str], **_: Any) -> SimpleNamespace:
        assert "--query-gpu=index,memory.free" in cmd
        return SimpleNamespace(returncode=0, stdout="0, [N/A]\n")

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)
    assert readiness_module._gpu_snapshot() == (1, None)


def test_gpu_snapshot_parses_multiple_devices_with_free_memory(monkeypatch) -> None:
    from scagent_sdk.capabilities import readiness as readiness_module

    def fake_run(cmd: list[str], **_: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="0, 81000\n1, 40000\n")

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)
    assert readiness_module._gpu_snapshot() == (2, 81000)


def test_present_gpu_with_unknown_free_memory_is_reported_ready(
    tmp_path: Path, monkeypatch
) -> None:
    # A device that is present but does not expose free memory must not be blocked; the
    # min-memory gate is only meaningful when free memory is actually reported.
    from scagent_sdk.capabilities import readiness as readiness_module

    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    gpu_profile = _profile(python=interpreter, gpu_required=True, min_gpu_memory_mb=4096)

    class Package:
        manifest = type(
            "Manifest",
            (),
            {
                "skill_id": "demo",
                "tools": (type("Tool", (), {"environment": "gpu-env"})(),),
                "readiness": None,
            },
        )()

    monkeypatch.setattr(readiness_module, "_gpu_snapshot", lambda: (1, None))
    reports = readiness_module.probe_environments(
        (Package(),), broker=_environment_broker({"gpu-env": gpu_profile})
    )
    assert reports[0].status == "ready"
    assert "free memory unknown" in reports[0].summary


def test_rendered_block_covers_environments_and_assets_in_separate_sections() -> None:
    from scagent_sdk.capabilities.readiness import EnvironmentReadiness

    block = render_readiness_block(
        (ReadinessReport("research-web", "partial", "TAVILY_API_KEY is not set"),),
        (EnvironmentReadiness("gpu-singlecell", "ready", "2 GPU(s)", ("cluster-qc",)),),
    )

    assert "### Compute environments" in block
    assert "**gpu-singlecell** — READY: 2 GPU(s) (used by cluster-qc)" in block
    assert "### Skill assets" in block
    assert "**research-web** — PARTIAL" in block
