"""`scripts/smriti_candidates.py`: export candidates, list them, confirm some into a local key (#1232).

Synthetic decisions on the test database and invented confirmations only. No model call.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from eval.pariksha.keys import AnswerKey, read_key
from eval.smriti.confirm import CONFIRMATIONS_FORMAT
from tests.eval.test_pariksha_stored import PAGE_HASH, slot_box_of, synthetic_run
from tests.eval.test_smriti_export import build_world

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_scorecard
import smriti_candidates

pytest_plugins = ("tests.app.postgres_fixture",)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine(postgres_engine: Engine) -> Engine:
    from alembic import command
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return postgres_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    opened = session_factory(engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture(autouse=True)
def _known_reviewer(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local real-reviewer list (#1267), naming the invented reviewer of `build_world`."""
    from eval.smriti.actors import REVIEWERS_ENV, REVIEWERS_FORMAT
    from tests.eval.test_smriti_export import REVIEWER

    people = tmp_path_factory.mktemp("local") / "people.json"
    people.write_text(json.dumps({"format": REVIEWERS_FORMAT, "reviewers": [REVIEWER]}))
    monkeypatch.setenv(REVIEWERS_ENV, str(people))


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _sha(path: Path) -> str:
    return str(json.loads(path.read_text())["sha256"])


def _write_answers(path: Path, candidates: Path, *answers: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(
            {
                "format": CONFIRMATIONS_FORMAT,
                "confirmed_by": "a person",
                "candidates_sha256": _sha(candidates),
                "answers": list(answers),
            }
        )
    )
    return path


def _confirm_args(
    candidates: Path, answers: Path, out_dir: Path, library: Path, *extra: str
) -> list[str]:
    return [
        "confirm",
        str(candidates),
        "--confirmations",
        str(answers),
        "--out-dir",
        str(out_dir),
        "--version",
        "1",
        "--library",
        str(library),
        *extra,
    ]


def test_export_list_and_confirm_write_a_local_key_and_register_it(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.models.evidence import NoCountertopAnswer
    from eval.smriti.library import read_library

    world = build_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    printed = capsys.readouterr().out
    assert "rekha" in printed and "akshara" in printed
    assert "needs a person's confirmation" in printed

    assert smriti_candidates.main(["list", str(candidates), "--agent", "rekha"]) == 0
    # The counts come first, then only Rekha's candidates that can become key cases.
    listed = capsys.readouterr().out.split("can become key cases", 1)[1]
    assert f"- no_countertop_answers:{world.no_countertop_reader}" in listed
    assert "- architect_pairing_records" not in listed

    # Exporting wrote no key: a candidate is only a candidate.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["candidates.json"]

    answers = _write_answers(
        tmp_path / "confirmations.json",
        candidates,
        {"candidate": f"no_countertop_answers:{world.no_countertop_reader}", "verdict": "yes"},
    )
    out_dir = tmp_path / "keys"
    library = tmp_path / "library.json"
    assert smriti_candidates.main(_confirm_args(candidates, answers, out_dir, library)) == 0
    written = sorted(out_dir.iterdir())
    assert [path.name for path in written] == ["rekha-smriti-1.json"]
    key = read_key(written[0])
    assert isinstance(key, AnswerKey)
    assert [case.question.question_id for case in key.cases] == ["p0:row-choice"]
    # Registered in the key library, with who confirmed it.
    entries = read_library(library).entries
    assert [(entry.agent, entry.sha256, entry.confirmed_by) for entry in entries] == [
        ("rekha", key.sha256, "a person")
    ]
    # The database was only read.
    assert session.scalar(select(func.count()).select_from(NoCountertopAnswer)) == 4
    # Written once: a second confirm to the same keys is refused.
    assert (
        smriti_candidates.main(_confirm_args(candidates, answers, out_dir, library))
        == smriti_candidates.REFUSED
    )


def _piece_on_a_row_with_widths(session: Session) -> object:
    """A person's piece kind on a row the run stored with widths: piece 0 2", piece 1 12 1/2"."""
    from app.models.document import Page
    from app.models.drawing import (
        DrawingItem,
        DrawingView,
        PartConfirmation,
        PartProposal,
        PartProposalRow,
    )
    from app.models.evidence import NoCountertopAnswer, ObservationCandidate
    from tests.eval.test_smriti_export import REVIEWER, _run
    from vocabulary.part_kinds import PartKind

    page = session.scalars(select(Page)).one()
    view = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).first()
    revision = session.scalars(select(NoCountertopAnswer.package_revision_id)).first()
    assert view is not None and revision is not None
    run = _run(session, revision, "extraction.form_reader")
    spans = []
    for slot, (numerator, denominator) in enumerate(((2, 1), (25, 2))):
        span = ObservationCandidate(
            document_version_id=page.document_version_id,
            page_id=page.id,
            extraction_run_id=run.id,
            raw_text="invented",
            value_numerator=numerator,
            value_denominator=denominator,
            unit="in",
            polygon=[[0, 0], [1, 1]],
            ambiguity_flags=["slot-reader", f"slot:{slot}", "row-rank:1"],
        )
        session.add(span)
        session.flush()
        spans.append(span.id)
    proposal = PartProposal(
        drawing_view_id=view.id,
        kind="filler",
        extent={"space": "stored", "points": [["0", "0"], ["1", "1"]]},
        source="bala",
        source_version="1",
        reason="invented",
    )
    session.add(proposal)
    session.flush()
    session.add(
        PartProposalRow(
            part_proposal_id=proposal.id,
            row_candidate_id=spans[0],
            slot_index=1,
            extraction_run_id=run.id,
            kind_named=False,
        )
    )
    item = DrawingItem(
        drawing_view_id=view.id,
        item_type=PartKind.CABINET.item_type.value,
        extent={"space": "stored", "points": [["0", "0"], ["1", "1"]]},
    )
    session.add(item)
    session.flush()
    decision = PartConfirmation(
        part_proposal_id=proposal.id,
        decision="confirmed",
        kind="cabinet",
        drawing_item_id=item.id,
        confirmed_by=REVIEWER,
    )
    session.add(decision)
    session.commit()
    return decision.id


def test_every_key_type_is_confirmed_registered_and_read_by_pariksha(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from eval.pariksha.kinds import KindsKey
    from eval.pariksha.walls import WallsKey
    from eval.smriti.library import read_any_key, read_library
    from tests.eval.test_smriti_export import build_v2_world

    world = build_v2_world(session)
    piece = _piece_on_a_row_with_widths(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    capsys.readouterr()
    answers = _write_answers(
        tmp_path / "confirmations.json",
        candidates,
        {
            "candidate": f"no_countertop_answers:{world.countertop_answer}",
            "verdict": "yes",
            "box": 2,
        },
        {"candidate": f"review_actions:{world.confirm_action}", "verdict": "yes"},
        {"candidate": f"part_confirmations:{piece}", "verdict": "yes"},
        {
            "candidate": f"slot_row_review_decisions:{world.walls_decision}:walls",
            "verdict": "yes",
            "left": "wall",
            "right": "open",
        },
    )
    out_dir = tmp_path / "keys"
    library = tmp_path / "library.json"
    # A piece kind needs the stored row's widths, so it needs the database.
    assert (
        smriti_candidates.main(_confirm_args(candidates, answers, out_dir, library))
        == smriti_candidates.REFUSED
    )
    assert "row_widths" in capsys.readouterr().err
    assert not out_dir.exists() and not library.exists()
    assert (
        smriti_candidates.main(
            _confirm_args(candidates, answers, out_dir, library, "--db", _url(engine))
        )
        == 0
    )
    keys = {path.name: read_any_key(path) for path in out_dir.iterdir()}
    assert sorted(keys) == [
        "akshara-smriti-1.json",
        "bala-kinds-smriti-1.json",
        "rekha-smriti-1.json",
        "rekha-walls-smriti-1.json",
    ]
    kinds = keys["bala-kinds-smriti-1.json"]
    assert isinstance(kinds, KindsKey)
    assert [str(width) for width in kinds.cases[0].row_widths or ()] == ["2", "25/2"]
    walls = keys["rekha-walls-smriti-1.json"]
    assert isinstance(walls, WallsKey)
    assert walls.cases[0].site_layout == "back_and_left"
    rows = keys["rekha-smriti-1.json"]
    assert isinstance(rows, AnswerKey) and len(rows.cases) == 1
    registered = read_library(library).entries
    assert sorted(entry.agent for entry in registered) == [
        "akshara",
        "bala-kinds",
        "rekha",
        "rekha-walls",
    ]
    assert {entry.sha256 for entry in registered} == {key.sha256 for key in keys.values()}


def test_an_unlisted_actor_is_never_written(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.eval.test_smriti_export import build_v2_world

    world = build_v2_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    answers = _write_answers(
        tmp_path / "confirmations.json",
        candidates,
        {"candidate": f"part_confirmations:{world.stranger_piece}", "verdict": "yes"},
    )
    out_dir = tmp_path / "keys"
    arguments = _confirm_args(
        candidates, answers, out_dir, tmp_path / "library.json", "--db", _url(engine)
    )
    assert smriti_candidates.main(arguments) == smriti_candidates.REFUSED
    assert "list of real reviewers" in capsys.readouterr().err
    assert not out_dir.exists()


def test_a_conflict_with_a_registered_key_is_refused_and_nothing_is_written(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.eval.test_smriti_export import build_v2_world

    world = build_v2_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    library = tmp_path / "library.json"
    pick = f"no_countertop_answers:{world.countertop_answer}"
    first = _write_answers(
        tmp_path / "first.json", candidates, {"candidate": pick, "verdict": "yes", "box": 1}
    )
    assert smriti_candidates.main(_confirm_args(candidates, first, tmp_path / "a", library)) == 0
    second = _write_answers(
        tmp_path / "second.json", candidates, {"candidate": pick, "verdict": "yes", "box": 2}
    )
    arguments = _confirm_args(candidates, second, tmp_path / "b", library)
    arguments[arguments.index("--version") + 1] = "2"
    capsys.readouterr()
    assert smriti_candidates.main(arguments) == smriti_candidates.REFUSED
    assert "different answer" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize("command", ["export", "confirm"])
def test_an_output_inside_a_git_working_tree_is_refused(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = REPO / "smriti-should-never-exist"
    arguments = (
        ["export", "--db", "postgresql+psycopg://nobody@localhost:1/none", "--out", str(inside)]
        if command == "export"
        else _confirm_args(
            tmp_path / "candidates.json",
            tmp_path / "confirmations.json",
            inside,
            tmp_path / "library.json",
        )
    )
    assert smriti_candidates.main(arguments) == smriti_candidates.REFUSED
    assert "git working tree" in capsys.readouterr().err
    assert not inside.exists()


def test_a_confirmation_without_a_person_refuses_and_writes_nothing(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    answers = _write_answers(tmp_path / "confirmations.json", candidates)
    out_dir = tmp_path / "keys"
    assert (
        smriti_candidates.main(
            _confirm_args(candidates, answers, out_dir, tmp_path / "library.json")
        )
        == smriti_candidates.REFUSED
    )
    assert "nothing" in capsys.readouterr().err
    assert not out_dir.exists()


def test_the_pariksha_report_shows_the_agents_reviewer_corrections(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    capsys.readouterr()

    _version, run = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    key = tmp_path / "akshara-key.json"
    key.write_text(
        json.dumps(
            {
                "format": "pariksha-key/1",
                "agent": "akshara",
                "name": "invented",
                "version": "1",
                "cases": [
                    {
                        "family": "claude-slot-span",
                        "page_sha256": PAGE_HASH,
                        "question_id": "p0:slot0:0",
                        "slot_box": slot_box_of("p0:slot0:0"),
                        "answer": {"texts": ['3/4"']},
                    }
                ],
            }
        )
    )
    assert (
        pariksha_scorecard.main(
            [
                "score",
                "--agent",
                "akshara",
                "--db",
                _url(engine),
                "--key",
                str(key),
                "--run",
                str(run.id),
                "--code-version",
                "a1b2c3d",
                "--out",
                str(tmp_path / "card.json"),
                "--smriti",
                str(candidates),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "reviewer decisions on akshara: 3" in printed


@pytest.mark.parametrize("where", ["missing", "inside-git"])
def test_a_reviewer_list_that_cannot_be_used_is_refused_cleanly(
    where: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from eval.smriti.actors import REVIEWERS_ENV

    path = (
        tmp_path / "missing.json"
        if where == "missing"
        else REPO / "smriti-reviewers-should-never-exist.json"
    )
    monkeypatch.setenv(REVIEWERS_ENV, str(path))
    arguments = [
        "export",
        "--db",
        "postgresql+psycopg://nobody@localhost:1/none",
        "--out",
        str(tmp_path / "candidates.json"),
    ]
    assert smriti_candidates.main(arguments) == smriti_candidates.REFUSED
    err = capsys.readouterr().err
    assert err.startswith("smriti: ") and "Traceback" not in err
    assert ("does not exist" if where == "missing" else "git working tree") in err
    assert not (tmp_path / "candidates.json").exists()
