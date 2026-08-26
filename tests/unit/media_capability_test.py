from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from PIL import Image, ImageDraw

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.session import AnalysisSession


def _media_package() -> SkillPackage:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "inspect-media"
    )


def test_image_preview_is_attached_but_base64_is_not_persisted(tmp_path: Path) -> None:
    source = tmp_path / "plot.png"
    image = Image.new("RGB", (640, 320), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 280, 280), fill="red")
    draw.rectangle((360, 40, 600, 280), fill="blue")
    image.save(source)
    package = _media_package()
    tool = next(item for item in package.manifest.tools if item.name == "inspect_image")
    session = AnalysisSession.create(tmp_path / "sessions", title="image")
    executor = CapabilityExecutor(session)

    response = asyncio.run(executor.execute(package, tool, {"path": str(source)}))

    assert response.get("is_error") is not True
    # Envelope, the pixels, then the directive that obliges the model to actually read them.
    assert [item["type"] for item in response["content"]] == ["text", "image", "text"]
    assert "Read them now, before your next tool call" in response["content"][2]["text"]
    attached = base64.b64decode(response["content"][1]["data"])
    assert attached.startswith(b"\x89PNG")
    envelope = response["structuredContent"]
    assert envelope["model_media"][0]["media_type"] == "image/png"
    execution_id = envelope["scagent_execution_id"]
    result_path = executor.pending_root / execution_id / "result.json"
    assert response["content"][1]["data"] not in result_path.read_text(encoding="utf-8")
    assert executor.commit_from_hook({"tool_response": response}) is True
    assert session.store.state.artifacts[execution_id]["model_media"][0]["name"] == "image-preview"


def test_pdf_returns_extracted_text_and_selected_page_image(tmp_path: Path) -> None:
    import fitz

    source = tmp_path / "report.pdf"
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "First page: methods")
    second = document.new_page()
    second.insert_text((72, 72), "Second page: result chart")
    second.draw_rect(fitz.Rect(72, 100, 300, 300), color=(1, 0, 0), fill=(1, 0, 0))
    document.save(source)
    document.close()
    package = _media_package()
    tool = next(item for item in package.manifest.tools if item.name == "inspect_pdf")
    session = AnalysisSession.create(tmp_path / "sessions", title="pdf")
    executor = CapabilityExecutor(session)

    response = asyncio.run(
        executor.execute(package, tool, {"path": str(source), "pages": [2]})
    )

    assert response.get("is_error") is not True
    assert response["structuredContent"]["details"]["rendered_pages"] == [2]
    assert "Second page: result chart" in response["structuredContent"]["details"][
        "extracted_text"
    ]
    assert response["content"][1]["type"] == "image"
    execution_id = response["structuredContent"]["scagent_execution_id"]
    assert (executor.pending_root / execution_id / "page-2.png").is_file()


def test_model_media_must_also_be_a_declared_artifact(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "bad-media"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: bad-media\ndescription: test invalid media\n---\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(
        """schema_version: 1
skill: {id: bad-media, version: "1", description: test}
tools:
  - name: bad_media
    description: return undeclared media
    entrypoint: scripts/run.py:run
    input_schema: {type: object}
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        "def run(arguments, context):\n"
        "    (context.staging_dir / 'x.png').write_bytes(b'x')\n"
        "    return {'summary': 'bad', 'model_media': [{'name': 'x', "
        "'relative_path': 'x.png', 'media_type': 'image/png'}]}\n",
        encoding="utf-8",
    )
    package = CapabilityRegistry(skill.parent).discover()[0]
    session = AnalysisSession.create(tmp_path / "sessions", title="bad media")

    response = asyncio.run(
        CapabilityExecutor(session).execute(package, package.manifest.tools[0], {})
    )

    assert response["is_error"] is True
    assert "must also be declared" in response["content"][0]["text"]
    assert "eA==" not in json.dumps(session.summary())


def test_dense_figure_preview_is_downscaled_and_reencoded_when_png_is_wasteful(
    tmp_path: Path,
) -> None:
    """A noisy wide figure must not come back larger than it went in.

    Re-encoding an antialiased scientific figure as PNG can inflate it; several such previews in
    one turn is what overflowed the runtime's stdout frame and killed a whole turn.
    """

    import random

    source = tmp_path / "dense.png"
    image = Image.new("RGB", (2400, 900), "white")
    draw = ImageDraw.Draw(image)
    generator = random.Random(0)
    for _ in range(60_000):
        x = generator.randrange(2400)
        y = generator.randrange(900)
        draw.point((x, y), fill=(generator.randrange(256), generator.randrange(256), 40))
    image.save(source)
    package = _media_package()
    tool = next(item for item in package.manifest.tools if item.name == "inspect_image")
    session = AnalysisSession.create(tmp_path / "sessions", title="dense")

    executor = CapabilityExecutor(session)
    response = asyncio.run(executor.execute(package, tool, {"path": str(source)}))

    envelope = response["structuredContent"]
    preview = envelope["model_media"][0]
    attached = base64.b64decode(response["content"][1]["data"])
    # JPEG is chosen only because it measured smaller than the PNG encoding of the same preview.
    assert preview["media_type"] == "image/jpeg"
    with Image.open(source) as original:
        assert max(original.size) > 1568
    preview_path = tmp_path / "preview-check.jpg"
    preview_path.write_bytes(attached)
    with Image.open(preview_path) as normalized:
        assert max(normalized.size) == 1568
    assert len(attached) < 2 * 1024 * 1024


# --- figure directive ---------------------------------------------------------


def test_directive_names_every_attached_figure_and_forbids_deferring_the_reading() -> None:
    from scagent_sdk.capabilities.executor import CapabilityExecutor as Executor
    from scagent_sdk.capabilities.results import CapabilityResult, ModelMedia

    result = CapabilityResult(
        summary="two figures",
        model_media=(
            ModelMedia(name="qc-violins", relative_path="qc-violins.png", media_type="image/png"),
            ModelMedia(name="qc-scatter", relative_path="qc-scatter.png", media_type="image/png"),
        ),
    )

    directive = Executor._figure_directive(result)

    assert "2 figures are attached" in directive
    assert "qc-violins" in directive and "qc-scatter" in directive
    # The failure this exists to prevent: seeing a figure, moving on, and attesting to it later
    # from recall when a review floor blocks the turn.
    assert "before your next tool call" in directive
    assert "do not defer it to a later review call" in directive
    # Legibility failures must be reported, not worked around by reading the table instead.
    assert "treat the visual review as incomplete" in directive


def test_directive_is_singular_for_one_figure() -> None:
    from scagent_sdk.capabilities.executor import CapabilityExecutor as Executor
    from scagent_sdk.capabilities.results import CapabilityResult, ModelMedia

    result = CapabilityResult(
        summary="one figure",
        model_media=(
            ModelMedia(name="embedding", relative_path="embedding.png", media_type="image/png"),
        ),
    )

    assert "1 figure is attached" in Executor._figure_directive(result)


def test_no_directive_is_emitted_when_a_result_carries_no_pixels(tmp_path: Path) -> None:
    from scagent_sdk.capabilities.executor import CapabilityExecutor as Executor
    from scagent_sdk.capabilities.results import CapabilityContext, CapabilityResult

    context = CapabilityContext(
        scientific_session_id="s",
        session_dir=tmp_path,
        staging_dir=tmp_path,
        skill_id="k",
        tool_name="t",
        execution_id="e",
        state_revision=0,
        state_facts={},
    )

    assert Executor._model_content(context, CapabilityResult(summary="no figures")) == []


def test_media_ceiling_bounds_model_context_even_when_artifacts_are_complete() -> None:
    from scagent_sdk.capabilities.results import MODEL_MEDIA_LIMIT

    # Complete heatmap sets stay registered on disk; only a compact decision surface belongs in
    # a tool result that will be replayed through the model context.
    assert MODEL_MEDIA_LIMIT == 8
