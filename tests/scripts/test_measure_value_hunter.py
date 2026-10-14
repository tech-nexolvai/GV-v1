"""`scripts/measure_value_hunter.py` measures Khoj on a stored package, read-only, at no cost (#1231).

The issue's three tests: the script on a synthetic package; no writes to the measured database; no
model calls. The package is the synthetic sheet `tests/workflow/test_value_hunter.py` reads through
the real extraction stage: its left half is the architect's drawing and its right half the vendor's.
Synthetic values only.
"""

from __future__ import annotations

import ast
import json
import tempfile
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import scripts.measure_value_hunter as measure
from app.db.session import session_factory
from app.models import ParameterProposal, ViewRole
from app.models.package_text import TextPhrase
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_parameter_proposals import LEFT, RIGHT, _drawings, _read, _sheet
from tests.workflow.test_value_hunter import RULEBOOK, _publish, _rule
from workflow import parameter_proposals
from workflow.value_hunter import Proposal, hunt_values

pytest_plugins = ("tests.app.postgres_fixture",)

SCRIPT = Path(measure.__file__)

#: The architect's half states the overhang and names the backsplash with no number; the vendor's
#: half states a side panel. The settings the published rules leave outstanding are the four with
#: search words and the eight without.
NOTES = (
    (LEFT, 80, 'OVERHANG 1 1/2"'),
    (LEFT, 50, "BACKSPLASH TYP."),
    (RIGHT, 80, 'SIDE PANEL 5/8"'),
)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _package(session: Session, store: LocalStore) -> UUID:
    """The synthetic sheet read, its halves confirmed, and the whole rulebook published."""
    sheet = _sheet(*NOTES)
    revision = _read(session, store, sheet, kind="shop")
    _drawings(session, revision, sheet, ViewRole.ARCH, ViewRole.SHOP)
    _publish(session, *(_rule(path.name) for path in sorted(RULEBOOK.glob("*.yaml"))))
    session.commit()
    return revision.id


def _counts(session: Session) -> dict[str, int]:
    """Rows in every table of the schema, to see that nothing at all was written."""
    session.expire_all()
    tables = session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    ).scalars()
    return {
        table: session.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
        for table in sorted(tables)
    }


def _truth(path: Path, revision: UUID) -> Path:
    path.write_text(
        json.dumps(
            {
                str(revision): {
                    "countertop_overhang": {"stated": True, "value": "3/2"},
                    "backsplash_thickness": {"stated": True, "value": "5/8"},
                    "cabinet_side_thickness": {"stated": False},
                    "cabinet_depth": {"stated": False},
                }
            }
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The script on a synthetic package
# ---------------------------------------------------------------------------


def test_the_script_measures_and_grades_every_outstanding_setting(
    session: Session,
    store: LocalStore,
    postgres_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Outcome: the overhang is proposed on the architect's page and graded right; the backsplash is
    found but has no number, so not found, and graded a miss because the truth says it is stated;
    the side panel is on the vendor's half, refused by the guard, and graded right not to be found;
    the cabinet depth has no passage; the eight settings without words are not searched. The JSON
    holds no passage's words and no number."""
    revision = _package(session, store)
    out = tmp_path / "measure.json"

    code = measure.main(
        [
            "--database-url",
            _url(postgres_engine),
            "--truth",
            str(_truth(tmp_path / "truth.json", revision)),
            "--json",
            str(out),
        ]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert f"Package revision {revision}" in printed
    assert "countertop_overhang: PROPOSED, page 1" in printed
    saved = json.loads(out.read_text(encoding="utf-8"))
    (measured,) = saved["revisions"]
    by_setting = {entry["setting"]: entry for entry in measured["settings"]}
    assert list(by_setting) == sorted(by_setting)
    assert len(by_setting) == 12

    overhang = by_setting["countertop_overhang"]
    assert (overhang["ending"], overhang["grade"]) == ("proposed", measure.RIGHT)
    assert overhang["pointer"]["sides"] == ["ARCH"]
    assert overhang["pointer"]["guard"] == "citable"

    backsplash = by_setting["backsplash_thickness"]
    assert (backsplash["ending"], backsplash["grade"]) == ("not_found", measure.MISSED)
    assert [passage["guard"] for passage in backsplash["passages"]] == ["no_number"]

    side_panel = by_setting["cabinet_side_thickness"]
    assert (side_panel["ending"], side_panel["grade"]) == ("not_found", measure.RIGHT_NOT_FOUND)
    (passage,) = side_panel["passages"]
    assert passage["sides"] == ["SHOP"]
    assert passage["guard"] == "wrong_side"
    assert passage["number_readable"] is True

    depth = by_setting["cabinet_depth"]
    assert (depth["ending"], depth["passages"]) == ("not_found", [])
    assert depth["grade"] == measure.RIGHT_NOT_FOUND

    assert {
        name for name, entry in by_setting.items() if entry["grade"] == measure.NOT_SEARCHED
    } == {
        "double_door_cab_width_max",
        "double_door_cab_width_min",
        "drawer_cab_width_max",
        "drawer_cab_width_min",
        "single_door_cab_width_max",
        "single_door_cab_width_min",
        "sink_interior_depth",
        "sink_interior_width",
    }
    assert saved["totals"] == {
        "revisions": 1,
        "outstanding": 12,
        "not_searched": 8,
        "searched": 4,
        "proposed": 1,
        "not_found": 3,
        "not_found_no_passage": 1,
        "not_found_passages_refused": 2,
        "passages_refused_by_reason": {"no_number": 1, "wrong_side": 1},
        "grades": {
            measure.MISSED: 1,
            measure.NOT_SEARCHED: 8,
            measure.RIGHT: 1,
            measure.RIGHT_NOT_FOUND: 2,
        },
        "held_back_pointers": 1,
    }
    # Nothing from the drawing leaves without `--with-text`.
    dumped = out.read_text(encoding="utf-8")
    for said in ("OVERHANG", "1 1/2", "3/2", "SIDE PANEL", "TYP"):
        assert said not in dumped


def test_with_text_shows_the_passages_words_and_number(
    session: Session, store: LocalStore, postgres_engine: Engine, tmp_path: Path
) -> None:
    """For the local report only. Outcome: each passage's words and its number are included."""
    revision = _package(session, store)
    out = tmp_path / "measure.json"

    assert (
        measure.main(["--database-url", _url(postgres_engine), "--json", str(out), "--with-text"])
        == 0
    )

    (measured,) = json.loads(out.read_text(encoding="utf-8"))["revisions"]
    assert measured["package_revision_id"] == str(revision)
    by_setting = {entry["setting"]: entry for entry in measured["settings"]}
    pointer = by_setting["countertop_overhang"]["pointer"]
    assert (pointer["text"], pointer["number"]) == ('OVERHANG 1 1/2"', "3/2")
    # Without a truth file nothing is graded, and nothing searched is graded as if it were known.
    assert by_setting["countertop_overhang"]["grade"] == measure.UNGRADED
    assert by_setting["backsplash_thickness"]["grade"] == measure.UNGRADED


def test_a_pointer_whose_number_is_not_the_truth_is_wrong(
    session: Session, store: LocalStore, postgres_engine: Engine, tmp_path: Path
) -> None:
    """Outcome: the same pointer, against a truth with another value or none stated, is wrong."""
    revision = _package(session, store)
    for entry in ({"stated": True, "value": "1"}, {"stated": False}):
        truth = tmp_path / "truth.json"
        truth.write_text(
            json.dumps({str(revision): {"countertop_overhang": entry}}), encoding="utf-8"
        )
        out = tmp_path / "measure.json"
        assert (
            measure.main(
                ["--database-url", _url(postgres_engine), "--truth", str(truth), "--json", str(out)]
            )
            == 0
        )
        (measured,) = json.loads(out.read_text(encoding="utf-8"))["revisions"]
        (overhang,) = (e for e in measured["settings"] if e["setting"] == "countertop_overhang")
        assert overhang["grade"] == measure.WRONG


def test_the_endings_are_the_hunters_own(
    session: Session, store: LocalStore, postgres_engine: Engine
) -> None:
    """The script runs the production planner, not a copy of it. Outcome: its endings are exactly
    what `hunt_values` returns on the same package."""
    revision = _package(session, store)

    with measure.read_only_session(_url(postgres_engine)) as (reader, held):
        measured = measure.measure_revision(reader, revision, held=held)

    hunt = hunt_values(session, revision)
    session.rollback()
    assert [(setting.setting, setting.ending) for setting in measured.settings] == [
        (ending.setting, "proposed" if isinstance(ending, Proposal) else "not_found")
        for ending in hunt.endings
    ]
    assert {setting.setting: setting.reason for setting in measured.settings if setting.reason} == (
        hunt.summary()["not_found"]
    )
    (proposal,) = (ending for ending in hunt.endings if isinstance(ending, Proposal))
    (pointed,) = (setting.pointer for setting in measured.settings if setting.pointer is not None)
    assert pointed.phrase_id == str(proposal.phrase_id)


def test_an_unknown_revision_is_refused(
    session: Session, postgres_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = measure.main(
        [
            "--database-url",
            _url(postgres_engine),
            "--revision",
            "00000000-0000-4000-8000-000000000000",
        ]
    )
    assert code == 2
    assert "no package revision" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# No writes to the measured database
# ---------------------------------------------------------------------------


def test_the_measured_database_is_not_written(
    session: Session, store: LocalStore, postgres_engine: Engine, tmp_path: Path
) -> None:
    """The hunter would file a pointer to the overhang. Outcome: after the measurement every table
    holds exactly the rows it held before, no pointer among them, and the stand-in is put back."""
    revision = _package(session, store)
    before = _counts(session)
    original = parameter_proposals._record

    assert measure.main(["--database-url", _url(postgres_engine), "--revision", str(revision)]) == 0

    assert parameter_proposals._record is original
    after = _counts(session)
    assert after == before
    assert session.execute(select(func.count()).select_from(ParameterProposal)).scalar_one() == 0


def test_the_transaction_refuses_any_write(session: Session, postgres_engine: Engine) -> None:
    """Outcome: inside the measurement the database itself refuses a write, so a write the stand-in
    missed would fail the measurement rather than change the database."""
    with (
        measure.read_only_session(_url(postgres_engine)) as (reader, _held),
        pytest.raises(DBAPIError, match="read-only transaction"),
    ):
        reader.execute(text("CREATE TABLE measured_by_mistake (id integer)"))
    session.expire_all()
    assert session.execute(select(func.count()).select_from(TextPhrase)).scalar_one() == 0


def test_the_stand_in_keeps_the_pointer_in_memory() -> None:
    """Outcome: inside, a pointer is kept in the list and not written; outside, the original is back,
    even when the measurement fails part-way."""
    original = parameter_proposals._record
    with pytest.raises(RuntimeError), measure.pointers_held_back() as held:
        assert parameter_proposals._record is not original
        row = parameter_proposals._record(None, None, "a citation")  # type: ignore[arg-type]
        assert row.id is None and held == ["a citation"]
        raise RuntimeError("a measurement that fails")
    assert parameter_proposals._record is original


# ---------------------------------------------------------------------------
# No model calls
# ---------------------------------------------------------------------------


def test_the_script_reaches_no_model() -> None:
    """Outcome: the script imports no model client and nothing that calls one; the hunter's planner
    is the code-owned table."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    for name in imported:
        assert name is not None
        assert not any(
            word in name for word in ("anthropic", "boto", "bedrock", "openai", "model_client")
        ), name
    assert "table_planner" in SCRIPT.read_text(encoding="utf-8")


def test_a_truth_file_is_read_exactly(tmp_path: Path) -> None:
    path = tmp_path / "truth.json"
    revision = "00000000-0000-4000-8000-000000000001"
    path.write_text(
        json.dumps({revision: {"countertop_overhang": {"stated": True, "value": "5/8"}}}),
        encoding="utf-8",
    )
    truth = measure.load_truth(path)
    assert truth == {
        UUID(revision): {"countertop_overhang": measure.SettingTruth(True, Fraction(5, 8))}
    }
    path.write_text(json.dumps({revision: {"cabinet_depth": {"stated": "yes"}}}), encoding="utf-8")
    with pytest.raises(TypeError, match="true or false"):
        measure.load_truth(path)
