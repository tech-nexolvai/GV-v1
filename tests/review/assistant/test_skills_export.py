"""The chat agent's skill folder (`app/review/assistant/skills_export.py`, #1465). No model call, $0.

What is asserted:

- the folder holds the repository's `SKILL.md` byte for byte, `rules.md` for this drawing set, and
  GV's local files (plain files only: no hidden file, no link, nothing over `SKILL.md`);
- `rules.md` says each setting's value in inches and where it comes from (the rulebook, GV's company
  standard, the project, the sink card, this drawing set), and a setting nobody gave as missing;
- only the drawing set's own product's checks are listed; every check when no product was stated;
- the same records give the same bytes, and a folder is rebuilt in place;
- a missing local folder is fine; a folder this did not write is never replaced; an unknown
  revision is refused before anything is written;
- `SKILL.md` has its front-matter, stays short, and holds no client value.

All projects, people and values here are made up.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import Package, PackageRevision, Project
from app.models.package import PackageState
from app.models.parameters import to_rows
from app.models.project_sinks import ProjectSink
from app.review.assistant.skills_export import (
    LOCAL_FOLDER_VARIABLE,
    SKILL_SOURCE,
    build_skill_folder,
    local_skills_folder,
    rules_markdown,
)
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook
from units.measurement import Unit

pytest_plugins = ("tests.app.postgres_fixture",)

WHEN = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    _publish_rulebook(opened)
    opened.commit()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """GV's local skill files: a checklist, plus what must never be copied."""
    folder = tmp_path / "local-skill"
    (folder / "checklists").mkdir(parents=True)
    (folder / "checklists" / "countertop-checks.md").write_text("# Made-up checklist\n")
    (folder / ".hidden-notes.md").write_text("never copied\n")
    (folder / "SKILL.md").write_text("---\nname: something-else\n---\nnever copied\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("a file outside the folder\n")
    (folder / "checklists" / "link.md").symlink_to(outside)
    monkeypatch.setenv(LOCAL_FOLDER_VARIABLE, str(folder))
    return folder


def _revision(session: Session, product: str | None = "countertop") -> tuple[UUID, UUID]:
    project = Project(name="Made-up job")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Made-up vendor", product_type=product)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
    )
    session.add(revision)
    session.commit()
    return project.id, revision.id


def _inches(value: str) -> Quantity:
    return Quantity(value=Fraction(value), unit=Unit.INCH)


def _layer(
    session: Session,
    layer: ParameterLayer,
    values: dict[str, tuple[str, str | None]],
    *,
    project_id: UUID | None = None,
    revision_id: UUID | None = None,
) -> UUID:
    """Store one version of one settings layer, as the settings screens do."""
    stored, rows = to_rows(
        ParameterSet(
            project_id=None if project_id is None else str(project_id),
            layer=layer,
            version=1,
            parameters={
                name: ParameterValue(
                    value=_inches(value),
                    provenance=Provenance.COMPANY_STANDARD,
                    set_by="A. Reviewer",
                    set_at=WHEN,
                    reference=reference,
                )
                for name, (value, reference) in values.items()
            },
        ),
        package_revision_id=revision_id,
    )
    session.add(stored)
    session.flush()
    for row in rows:
        row.parameter_set_id = stored.id
        session.add(row)
    session.commit()
    return stored.id


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _row(text: str, setting: str) -> str:
    rows = [
        line for line in text.splitlines() if line.startswith("| ") and f"(`{setting}`)" in line
    ]
    assert len(rows) == 1, (setting, rows)
    return rows[0]


def test_the_folder_holds_the_skill_the_rules_and_gvs_local_files(
    session: Session, local: Path, tmp_path: Path
) -> None:
    _project, revision = _revision(session)
    dest = tmp_path / "chat" / "skills" / "gv-review"

    build_skill_folder(session, revision, dest)

    files = _files(dest)
    assert set(files) == {"SKILL.md", "rules.md", "checklists/countertop-checks.md"}
    assert files["SKILL.md"] == SKILL_SOURCE.read_bytes()
    assert files["checklists/countertop-checks.md"] == b"# Made-up checklist\n"
    assert files["rules.md"].decode("utf-8") == rules_markdown(session, revision)
    # Nothing is left beside the folder from building it.
    assert [path.name for path in dest.parent.iterdir()] == ["gv-review"]


def test_rules_md_says_each_value_in_inches_and_where_it_comes_from(
    session: Session, local: Path
) -> None:
    project, revision = _revision(session)
    _layer(session, ParameterLayer.GLOBAL, {"field_cut": ("3/2", None)})
    project_set = _layer(
        session,
        ParameterLayer.PROJECT,
        {
            "countertop_overhang": ("1", "Made-up section 2"),
            "sink_interior_width": ("31/2", None),
        },
        project_id=project,
    )
    _layer(
        session,
        ParameterLayer.RUN,
        {"sink_interior_depth": ("16", None)},
        project_id=project,
        revision_id=revision,
    )
    session.add(
        ProjectSink(
            project_id=project,
            model_name="Made-up sink 100",
            source_note="spec sheet",
            parameter_set_id=project_set,
            set_by="A. Reviewer",
        )
    )
    session.commit()

    text = rules_markdown(session, revision)

    assert text.startswith("# GV's rules for this drawing set\n")
    assert "Product: countertop. Only the countertop checks run on this set." in text
    field_cut = _row(text, "field_cut")
    assert "| 1 1/2\" | GV's company standard (set by A. Reviewer; 2026-01-05" in field_cut
    assert "CT-WIDTH-001" in field_cut
    overhang = _row(text, "countertop_overhang")
    assert "| 1\" | this project's value (" in overhang and "Made-up section 2" in overhang
    assert "| 15 1/2\" | this project's sink card" in _row(text, "sink_interior_width")
    assert '| 16" | typed for this drawing set' in _row(text, "sink_interior_depth")
    assert "| 2 1/2\" | the rulebook's default |" in _row(text, "back_offset_minimum")
    assert "| 1/4\" | the rulebook's default |" in _row(text, "sink_cutout_clearance")
    cabinet_depth = _row(text, "cabinet_depth")
    assert "| not set | missing:" in cabinet_depth and "NOT FOUND" in cabinet_depth
    assert "- Sink on this project's sink card: Made-up sink 100 (size from: spec sheet)." in text
    # The rulebook's note on a default is printed only while that default is in force.
    assert "Note on Field cut per wall end" not in text

    width = text[text.index("### CT-WIDTH-001") :].split("\n### ")[0]
    assert "- Compares: the vendor's drawing against itself" in width
    assert (
        "- Reads `countertop_width`: countertop overall width, from the vendor's drawing" in width
    )
    assert '- Setting `field_cut` (Field cut per wall end): 1 1/2".' in width
    assert "- Works out `field_cut_total` = `field_cut` x `field_cut_count`." in width
    assert "- Works out `expected_width` = `row_width` + `field_cut_total`." in width
    assert "- Decides: `countertop_width` must equal `expected_width` exactly." in width
    assert "  - `back_left_right` (back wall and both ends): `field_cut_count` = 2." in width
    assert "- A value it needs is missing: NOT FOUND." in width
    assert "- Decides: `back_offset` must be at least `back_offset_minimum`." in text
    assert "must match its paired value in `vendor_widths` exactly" in text
    assert "CAB-FILLER-001" not in text and "CAB-ARCH-VS-SHOP-001" not in text


def test_the_sink_card_is_named_only_while_its_sizes_are_in_force(
    session: Session, local: Path
) -> None:
    project, revision = _revision(session)
    project_set = _layer(
        session,
        ParameterLayer.PROJECT,
        {"sink_interior_width": ("30", None), "sink_interior_depth": ("16", None)},
        project_id=project,
    )
    session.add(
        ProjectSink(
            project_id=project,
            model_name="Made-up sink 200",
            source_note=None,
            parameter_set_id=project_set,
            set_by="A. Reviewer",
        )
    )
    session.commit()
    assert "- Sink on this project's sink card: Made-up sink 200." in rules_markdown(
        session, revision
    )

    _layer(
        session,
        ParameterLayer.RUN,
        {"sink_interior_width": ("33", None), "sink_interior_depth": ("19", None)},
        project_id=project,
        revision_id=revision,
    )

    text = rules_markdown(session, revision)
    assert "Made-up sink 200" not in text
    assert '| 33" | typed for this drawing set' in _row(text, "sink_interior_width")


def test_a_set_with_no_product_stated_lists_every_check(session: Session, local: Path) -> None:
    _project, revision = _revision(session, product=None)

    text = rules_markdown(session, revision)

    assert "Product: not stated for this drawing set, so every published check runs" in text
    assert "### CAB-FILLER-001" in text and "### CT-WIDTH-001" in text
    assert "equipment cabinets (sink, appliances) never change size" in text
    assert "| Filler width, minimum (`filler_min`) | 1\" | the rulebook's default |" in text
    assert "- Note on Filler width, maximum:" in text


def test_the_same_records_give_the_same_bytes_and_a_folder_is_rebuilt_in_place(
    session: Session, local: Path, tmp_path: Path
) -> None:
    _project, revision = _revision(session)
    first, second = tmp_path / "one" / "gv-review", tmp_path / "two" / "gv-review"

    build_skill_folder(session, revision, first)
    build_skill_folder(session, revision, second)
    assert _files(first) == _files(second)

    (local / "checklists" / "countertop-checks.md").unlink()
    build_skill_folder(session, revision, first)
    assert set(_files(first)) == {"SKILL.md", "rules.md"}
    assert [path.name for path in first.parent.iterdir()] == ["gv-review"]


def test_a_missing_local_folder_is_fine(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOCAL_FOLDER_VARIABLE, str(tmp_path / "not-here"))
    _project, revision = _revision(session)
    dest = tmp_path / "gv-review"

    build_skill_folder(session, revision, dest)

    assert set(_files(dest)) == {"SKILL.md", "rules.md"}


def test_a_folder_it_did_not_write_is_never_replaced(session: Session, tmp_path: Path) -> None:
    _project, revision = _revision(session)
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / "notes.md").write_text("the chat's own work\n")

    with pytest.raises(FileExistsError):
        build_skill_folder(session, revision, dest)

    assert _files(dest) == {"notes.md": b"the chat's own work\n"}
    assert [path.name for path in tmp_path.iterdir()] == ["home"]


def test_an_unknown_revision_is_refused_before_anything_is_written(
    session: Session, tmp_path: Path
) -> None:
    dest = tmp_path / "gv-review"

    with pytest.raises(LookupError):
        build_skill_folder(session, uuid4(), dest)

    assert not dest.exists()


def test_the_local_folder_comes_from_the_environment_or_the_default() -> None:
    assert local_skills_folder({LOCAL_FOLDER_VARIABLE: "/srv/gv-skills"}) == Path("/srv/gv-skills")
    assert local_skills_folder({}) == Path("~/gv-local/skills/gv-review").expanduser()


# ----------------------------------------------------------------------------------------------
# SKILL.md itself
# ----------------------------------------------------------------------------------------------

SKILL_TEXT = SKILL_SOURCE.read_text(encoding="utf-8")


def test_skill_md_has_the_front_matter_a_skill_needs() -> None:
    match = re.match(r"---\n(.*?)\n---\n", SKILL_TEXT, re.DOTALL)
    assert match is not None, "SKILL.md starts with a front-matter block"
    fields = dict(line.split(": ", 1) for line in match.group(1).splitlines())
    assert fields["name"] == "gv-review"
    assert 50 < len(fields["description"]) <= 1024


def test_skill_md_stays_short_and_says_the_rules_that_matter() -> None:
    assert len(SKILL_TEXT.splitlines()) <= 260
    for words in (
        "rules.md",
        "in inches",
        "GV's markup is GV's reviewer comments",
        "-hide-annotations",
        "Verify every lead yourself",
        "AI markup: draft, not GV's signed review",
        "/mnt/user-data/outputs",
        "present_files",
        "Never invent a number",
    ):
        assert words in SKILL_TEXT, words


#: Shapes of client values that must never reach a file in the repository: a drawing or sheet
#: number (`A-501`, `ID-203`, `A3.01`), the name of a client drawing set file, a job number.
CLIENT_VALUE_SHAPES = (
    re.compile(r"\b[A-Z]{1,3}-?\d{3}\b"),
    re.compile(r"\b[A-Z]{1,2}\d\.\d{2}\b"),
    re.compile(r"\bAI[ _]Set\b", re.IGNORECASE),
    re.compile(r"\bSet \d\b"),
    re.compile(r"\b(?:job|project|PO) ?(?:no\.?|number|#) ?\d", re.IGNORECASE),
)


@pytest.mark.parametrize("shape", CLIENT_VALUE_SHAPES, ids=lambda shape: shape.pattern)
def test_skill_md_holds_no_client_value(shape: re.Pattern[str]) -> None:
    assert not shape.search(SKILL_TEXT), shape.search(SKILL_TEXT)
