"""Pariksha's walls scorecard (Rekha, #1250): the two AI wall answers and code's wall ink, per row end.

Verification for: `eval/pariksha/walls.py`. Invented pages, invented rows, invented answers; the
stored-run tests use the synthetic run on the test database. No model call, no client value.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from eval.pariksha.keys import KeyFormatError, QuestionIdentity, load_key
from eval.pariksha.score import AGREED_RIGHT, NOT_ASKED, Scorecard
from eval.pariksha.stored import StoredAttempt, StoredRunError
from eval.pariksha.walls import (
    AUTOMATIC_RIGHT,
    AUTOMATIC_WRONG,
    BOTH_UNSURE,
    DISAGREED,
    HELD_LAYOUT,
    INK_READER,
    KEY_UNSURE,
    NO_ANSWER,
    ONE_UNSURE,
    SEALED_RIGHT,
    SEALED_WRONG,
    WALLS_AGENT,
    WALLS_FAMILY,
    StoredWallQuestion,
    StoredWallRow,
    StoredWallsRun,
    compare_walls,
    dump_walls_key,
    load_stored_walls,
    load_walls_key,
    score_walls,
)
from eval.regression import IncomparableRuns
from tests.eval.test_pariksha_stored import OPUS, PAGE_HASH, SONNET, add_answer

pytest_plugins = ("tests.app.postgres_fixture",)

PAGES = {name: name * 64 for name in "abcdef"}
BOX = (100, 200, 900, 600)


def wall_reply(left: str, right: str, behind: str = "unsure", view: str = "elevation") -> str:
    return json.dumps(
        {
            "left": left,
            "right": right,
            "behind": behind,
            "left_evidence": "invented",
            "right_evidence": "invented",
            "behind_evidence": "invented",
            "view": view,
        }
    )


def _end(drawn: str, site: str) -> dict[str, str]:
    return {"drawn": drawn, "site": site}


def _row(
    page: str,
    left: dict[str, str],
    right: dict[str, str],
    *,
    layout: str | None = "back_left_right",
    box: tuple[int, int, int, int] = BOX,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "family": WALLS_FAMILY,
        "page_sha256": PAGES[page],
        "question_id": "p0:walls",
        "row_box": ",".join(str(v) for v in box),
        "left": left,
        "right": right,
        "source": "invented",
    }
    if layout is not None:
        case["site_layout"] = layout
    return case


def key_document() -> dict[str, Any]:
    return {
        "format": "pariksha-key/1",
        "agent": WALLS_AGENT,
        "name": "synthetic walls",
        "version": "1",
        "cases": [
            # a: wall to wall, drawn and on site.
            _row("a", _end("wall", "wall"), _end("wall", "wall")),
            # b: the vendor drew a wall the site does not have at the left; open at the right.
            _row("b", _end("wall", "open"), _end("open", "open"), layout="island"),
            # c: what the vendor drew at the left is not settled yet.
            _row("c", _end("key_unsure", "open"), _end("wall", "wall"), layout="back_and_right"),
            # d: the run asked about another row on this page.
            _row("d", _end("wall", "wall"), _end("wall", "wall"), box=(100, 900, 900, 1300)),
            # e: never asked.
            _row("e", _end("open", "open"), _end("open", "open"), layout="island"),
        ],
    }


def _question(
    page: str,
    answers: Mapping[str, str | None],
    *,
    ink: tuple[bool, bool] | None = (False, False),
    layout: str | None = None,
    source: str | None = None,
    box: tuple[int, int, int, int] | None = BOX,
    cost: int = 1000,
) -> StoredWallQuestion:
    return StoredWallQuestion(
        identity=QuestionIdentity(
            family=WALLS_FAMILY, page_sha256=PAGES[page], question_id="p0:walls"
        ),
        prompt_id="slot-walls-v3",
        packet={"packet_sha256": page * 64},
        attempts=tuple(
            StoredAttempt(
                model_id=model,
                outcome="ok" if raw is not None else "timeout",
                raw_response=raw,
                cost_micros=cost,
                reused=False,
            )
            for model, raw in answers.items()
        ),
        row=StoredWallRow(
            candidate_id=str(uuid4()), row_box=box, layout=layout, source=source, ink=ink
        ),
    )


def stored_run(**changes: StoredWallQuestion) -> StoredWallsRun:
    questions = {
        "a": _question(
            "a",
            {OPUS: wall_reply("yes", "yes"), SONNET: wall_reply("yes", "yes")},
            ink=(True, True),
            layout="back_left_right",
            source="code-ink-and-both-ais",
        ),
        "b": _question(
            "b",
            {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("yes", "unsure")},
            ink=(True, False),
        ),
        "c": _question(
            "c",
            {OPUS: wall_reply("unsure", "unsure"), SONNET: wall_reply("unsure", "unsure")},
            ink=None,
        ),
        "d": _question(
            "d",
            {OPUS: wall_reply("yes", "yes"), SONNET: wall_reply("yes", "yes")},
            ink=(True, True),
        ),
        "f": _question("f", {OPUS: wall_reply("no", "no"), SONNET: wall_reply("no", "no")}),
    }
    questions.update(changes)
    return StoredWallsRun(
        database="synthetic",
        extraction_run_id=str(uuid4()),
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-ink-v1",
        config_hash="test",
        questions=tuple(questions.values()),
    )


def _score(run: StoredWallsRun, key: dict[str, Any] | None = None) -> Scorecard:
    return score_walls(
        load_walls_key(key or key_document()),
        run,
        code_version="abc1234",
        created_at="2026-01-01T00:00:00+00:00",
    )


def _metric(card: Scorecard, name: str) -> tuple[int, int]:
    metric = card.metrics[name]
    return metric.numerator, metric.denominator


def _outcomes(card: Scorecard) -> dict[str, str]:
    return {
        case.case_id.split("/", 1)[0][:1] + ":" + case.case_id.rsplit(":", 1)[-1]: case.outcome
        for case in card.cases
    }


# --- the key ------------------------------------------------------------------------------------


def test_the_key_reads_back_in_the_same_shape_and_is_hashed() -> None:
    key = load_walls_key(key_document())

    assert key.name == "synthetic walls"
    assert len(key.cases) == 5
    assert key.cases[1].left.drawn == "wall" and key.cases[1].left.site == "open"
    assert load_walls_key(dump_walls_key(key)).sha256 == key.sha256
    assert len(key.sha256) == 64


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": "pariksha-key/2"}, "format"),
        ({"agent": "rekha"}, "agent"),
        ({"cases": []}, "non-empty"),
        ({"extra": 1}, "unknown field"),
    ],
)
def test_a_key_that_is_not_a_walls_key_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(KeyFormatError, match=message):
        load_walls_key({**key_document(), **change})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"left": {"drawn": "maybe", "site": "wall"}}, "drawn"),
        ({"left": {"drawn": "wall", "site": "not_shown"}}, "site"),
        ({"left": {"drawn": "wall"}}, "site"),
        ({"row_box": "1,2,3"}, "row_box"),
        ({"row_box": "5,2,3,9"}, "row_box"),
        ({"question_id": "p0:row-choice"}, "walls"),
        ({"family": "slot-row-choice"}, "family"),
        ({"site_layout": "somewhere"}, "site_layout"),
        ({"behind": "yes"}, "unknown field"),
    ],
)
def test_a_bad_row_is_refused(change: dict[str, Any], message: str) -> None:
    document = key_document()
    document["cases"][0] = {**document["cases"][0], **change}
    with pytest.raises(KeyFormatError, match=message):
        load_walls_key(document)


def test_the_same_question_twice_is_refused() -> None:
    document = key_document()
    document["cases"].append(dict(document["cases"][0]))
    with pytest.raises(KeyFormatError, match="twice"):
        load_walls_key(document)


def test_the_label_and_row_scorer_refuses_a_walls_key() -> None:
    # Same file format, another agent: the old scorer never scores walls by mistake.
    with pytest.raises(KeyFormatError, match="agent"):
        load_key(key_document())


# --- scoring ------------------------------------------------------------------------------------


def test_each_row_end_is_scored_for_the_pair_against_what_the_vendor_drew() -> None:
    card = _score(stored_run())

    assert card.agent == WALLS_AGENT
    outcomes = _outcomes(card)
    assert outcomes["a:left"] == AGREED_RIGHT
    assert outcomes["a:right"] == AGREED_RIGHT
    # Both say "yes" at a wall the vendor drew (and the site does not have): right on `drawn`.
    assert outcomes["b:left"] == AGREED_RIGHT
    assert outcomes["b:right"] == ONE_UNSURE  # "no" and "unsure"
    assert outcomes["c:left"] == KEY_UNSURE
    assert outcomes["c:right"] == BOTH_UNSURE
    for end in ("d:left", "d:right", "e:left", "e:right"):
        assert outcomes[end] == NOT_ASKED

    assert _metric(card, "asked") == (6, 10)
    assert _metric(card, "key_unsure_ends") == (1, 6)
    assert _metric(card, "pair_agreed_right") == (3, 5)
    assert _metric(card, "pair_agreed_wrong") == (0, 5)
    assert _metric(card, "pair_one_unsure") == (1, 5)
    assert _metric(card, "pair_disagreed") == (0, 5)
    assert _metric(card, "pair_no_answer") == (0, 5)
    assert _metric(card, "pair_both_unsure") == (1, 5)
    assert "pair_split" not in card.metrics
    assert _metric(card, f"reader_right:{OPUS}") == (4, 5)
    assert _metric(card, f"reader_unsure:{OPUS}") == (1, 5)
    assert _metric(card, f"reader_right:{SONNET}") == (3, 5)
    assert _metric(card, f"reader_unsure:{SONNET}") == (2, 5)
    assert _metric(card, "unkeyed_questions") == (1, 5)


def test_the_site_answer_is_reported_beside_it_never_gating() -> None:
    card = _score(stored_run())

    assert _metric(card, "pair_agreed_right@site") == (2, 6)
    assert _metric(card, "pair_agreed_wrong@site") == (1, 6)  # b left: the vendor's wall
    assert _metric(card, "pair_one_unsure@site") == (1, 6)
    assert _metric(card, "pair_both_unsure@site") == (2, 6)
    assert _metric(card, f"reader_wrong:{OPUS}@site") == (1, 6)
    b_left = next(c for c in card.cases if c.case_id.startswith("b") and c.case_id.endswith("left"))
    assert "site: agreed_wrong" in b_left.note


def test_a_run_that_asked_about_another_row_is_not_asked_never_right() -> None:
    card = _score(stored_run())

    d_left = next(c for c in card.cases if c.case_id.startswith("d") and c.case_id.endswith("left"))
    assert d_left.outcome == NOT_ASKED
    assert "another row" in d_left.note


def test_a_row_box_moved_by_a_few_pixels_is_still_the_same_row() -> None:
    nudged = _question(
        "a",
        {OPUS: wall_reply("yes", "yes"), SONNET: wall_reply("yes", "yes")},
        box=(103, 198, 905, 601),
    )
    card = _score(stored_run(a=nudged))

    assert _outcomes(card)["a:left"] == AGREED_RIGHT


def test_code_wall_ink_is_scored_separately_as_its_own_judgment() -> None:
    card = _score(stored_run())

    a_left = next(c for c in card.cases if c.case_id.startswith("a") and c.case_id.endswith("left"))
    ink = next(reader for reader in a_left.readers if reader.model_id == INK_READER)
    assert (ink.answer, ink.verdict) == ("wall ink", "right")
    # Ink is positive only: no ink says nothing, it is never "open".
    assert _metric(card, "ink_right") == (3, 5)
    assert _metric(card, "ink_wrong") == (0, 5)
    assert _metric(card, "ink_none") == (1, 5)
    assert _metric(card, "ink_not_recorded") == (1, 5)
    assert _metric(card, "ink_right@site") == (2, 6)
    assert _metric(card, "ink_wrong@site") == (1, 6)


def test_each_rows_stored_layout_is_scored_against_the_site_layout() -> None:
    sealed_wrong = _question(
        "b",
        {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("yes", "no")},
        ink=(True, False),
        layout="back_and_left",
        source="readers",
    )
    card = _score(stored_run(b=sealed_wrong))

    layouts = {c.case_id[:1]: c.outcome for c in card.cases if c.case_id.endswith(":layout")}
    assert layouts["a"] == AUTOMATIC_RIGHT
    assert layouts["b"] == SEALED_WRONG
    assert layouts["c"] == HELD_LAYOUT
    assert layouts["d"] == NOT_ASKED
    assert _metric(card, "automatic_right") == (1, 3)
    assert _metric(card, "automatic_wrong") == (0, 3)
    assert _metric(card, "sealed_right") == (0, 3)
    assert _metric(card, "sealed_wrong") == (1, 3)
    assert _metric(card, "held_rows") == (1, 3)


def test_cost_per_question_is_counted_once_per_row() -> None:
    card = _score(stored_run())

    assert _metric(card, "cost_micros_per_question") == (6000, 3)


def test_a_missing_answer_is_invalid_and_never_an_agreement() -> None:
    one_missing = _question("a", {OPUS: wall_reply("yes", "yes"), SONNET: None})
    card = _score(stored_run(a=one_missing))

    assert _outcomes(card)["a:left"] == NO_ANSWER
    assert _metric(card, "pair_no_answer") == (2, 5)
    assert _metric(card, f"reader_invalid:{SONNET}") == (2, 5)  # both ends


def test_a_yes_against_a_no_is_disagreed_never_lumped_with_one_unsure() -> None:
    contradiction = _question(
        "b",
        {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("no", "unsure")},
        ink=(True, False),
    )
    card = _score(stored_run(b=contradiction))

    outcomes = _outcomes(card)
    assert outcomes["b:left"] == DISAGREED  # "yes" against "no"
    assert outcomes["b:right"] == ONE_UNSURE  # "no" against "unsure"
    assert _metric(card, "pair_disagreed") == (1, 5)
    assert _metric(card, "pair_one_unsure") == (1, 5)
    assert _metric(card, "pair_disagreed@site") == (1, 6)


def test_an_accepted_answer_that_cannot_be_read_refuses_the_scorecard() -> None:
    broken = _question("a", {OPUS: "not json", SONNET: wall_reply("yes", "yes")})
    with pytest.raises(StoredRunError, match="cannot be read"):
        _score(stored_run(a=broken))


def test_a_run_with_more_than_two_readers_is_refused() -> None:
    three = _question(
        "a",
        {
            OPUS: wall_reply("yes", "yes"),
            SONNET: wall_reply("yes", "yes"),
            "another.model": wall_reply("yes", "yes"),
        },
    )
    with pytest.raises(StoredRunError, match="exactly two readers"):
        _score(stored_run(a=three))


def test_the_code_version_must_be_a_commit_or_unrecorded() -> None:
    from eval.runs import MissingProvenanceError

    with pytest.raises(MissingProvenanceError):
        score_walls(load_walls_key(key_document()), stored_run(), code_version="main")


# --- compare ------------------------------------------------------------------------------------


def test_better_with_no_new_wrong_is_accepted() -> None:
    before = _score(stored_run())
    fixed = _question(
        "b",
        {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("yes", "no")},
        ink=(True, False),
    )
    after = _score(stored_run(b=fixed))

    comparison = compare_walls(before, after)

    assert comparison.accepted, comparison.verdict
    assert [change.case_id[:1] for change in comparison.fixed] == ["b"]


def test_a_new_agreed_wrong_end_is_rejected() -> None:
    before = _score(stored_run())
    wrong = _question(
        "c",
        {OPUS: wall_reply("no", "no"), SONNET: wall_reply("no", "no")},
        ink=None,
    )
    after = _score(stored_run(c=wrong))

    comparison = compare_walls(before, after)

    assert not comparison.accepted
    assert "1 new wrong" in comparison.verdict


def test_new_wrong_wall_ink_is_rejected_even_when_the_readers_improve() -> None:
    before = _score(stored_run())
    inked = _question(
        "b",
        {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("yes", "no")},
        ink=(True, True),  # ink at an end the vendor drew open
    )
    after = _score(stored_run(b=inked))

    comparison = compare_walls(before, after)

    assert not comparison.accepted
    assert "new wrong wall ink" in comparison.verdict


def test_a_new_wrong_automatic_layout_is_rejected() -> None:
    before = _score(stored_run())
    automatic = _question(
        "b",
        {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("yes", "unsure")},
        ink=(True, False),
        layout="back_left_right",
        source="code-ink-and-both-ais",
    )
    after = _score(stored_run(b=automatic))

    comparison = compare_walls(before, after)

    assert not comparison.accepted
    assert "new wrong automatic layout" in comparison.verdict
    layouts = {c.case_id[:1]: c.outcome for c in after.cases if c.case_id.endswith(":layout")}
    assert layouts["b"] == AUTOMATIC_WRONG


def test_a_new_sealed_wrong_layout_from_the_readers_does_not_reject() -> None:
    """The readers' sealed layout is a proposal nobody pre-selects (decided 2026-10-13 (c)).

    b's readers now agree on what the vendor drew (a wall at the left the site does not have), so
    their sealed layout is wrong against the site; nothing new is agreed-wrong on `drawn`, and no
    check uses the layout without a person's click. Only an automatic layout rejects."""
    before = _score(stored_run())
    proposed = _question(
        "b",
        {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("yes", "no")},
        ink=(True, False),
        layout="back_and_left",
        source="readers",
    )
    after = _score(stored_run(b=proposed))

    comparison = compare_walls(before, after)

    layouts = {c.case_id[:1]: c.outcome for c in after.cases if c.case_id.endswith(":layout")}
    assert layouts["b"] == SEALED_WRONG
    assert comparison.accepted, comparison.verdict


def test_compare_walls_refuses_a_card_of_another_agent() -> None:
    walls = _score(stored_run())
    other = replace(walls, agent="bala-kinds")

    for before, after in ((other, other), (walls, other), (other, walls)):
        with pytest.raises(IncomparableRuns, match="rekha-walls"):
            compare_walls(before, after)


def test_a_sealed_right_layout_is_counted() -> None:
    sealed = _question(
        "a",
        {OPUS: wall_reply("yes", "yes"), SONNET: wall_reply("yes", "yes")},
        layout="back_left_right",
        source="readers",
    )
    card = _score(stored_run(a=sealed))

    layouts = {c.case_id[:1]: c.outcome for c in card.cases if c.case_id.endswith(":layout")}
    assert layouts["a"] == SEALED_RIGHT


def test_malformed_stored_rows_are_counted_never_silent() -> None:
    clean = _score(stored_run())
    assert _metric(clean, "skipped_malformed") == (0, 5)

    run = replace(stored_run(), malformed=2)
    card = _score(run)

    assert _metric(card, "skipped_malformed") == (2, 5)


def test_scorecards_of_different_keys_are_not_compared() -> None:
    before = _score(stored_run())
    other = key_document()
    other["version"] = "2"
    after = _score(stored_run(), other)

    with pytest.raises(IncomparableRuns, match="different answer keys"):
        compare_walls(before, after)


def test_metric_directions_follow_the_walls_meaning() -> None:
    before = _score(stored_run())
    fixed = _question(
        "b",
        {OPUS: wall_reply("yes", "no"), SONNET: wall_reply("yes", "no")},
        ink=(True, False),
    )
    comparison = compare_walls(before, _score(stored_run(b=fixed)))

    deltas = {delta.metric: delta for delta in comparison.metrics}
    assert deltas["pair_agreed_right"].improved
    assert deltas["pair_one_unsure"].improved
    assert deltas["pair_disagreed"].direction is not None
    assert deltas["ink_right"].direction is not None
    assert deltas["key_unsure_ends"].direction is None


def test_the_written_scorecard_reads_back_identically(tmp_path: Any) -> None:
    from eval.pariksha.scorecard import read_scorecard, write_scorecard

    card = _score(stored_run())
    write_scorecard(card, tmp_path / "card.json")

    assert read_scorecard(tmp_path / "card.json") == card


# --- reading a stored run -----------------------------------------------------------------------


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


def add_wall_row(
    session: Session, version: Any, page: Any, run: Any, flags: list[str], raw: str
) -> str:
    from app.models.evidence import ObservationCandidate

    x0, y0, x1, y1 = BOX
    candidate = ObservationCandidate(
        document_version_id=version.id,
        page_id=page.id,
        extraction_run_id=run.id,
        raw_text=raw,
        polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        coordinate_space="image",
        ambiguity_flags=flags,
    )
    session.add(candidate)
    session.flush()
    return str(candidate.id)


def synthetic_walls_run(session: Session, *, ink: bool = True) -> tuple[Any, Any]:
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, page, run = _scaffold(session)
    flags = ["wall-reader", "walls-sealed:back_left_right"]
    if ink:
        flags.append("wall-ink:left=True,right=False")
    flags.append("wall-source:readers")
    candidate = add_wall_row(session, version, page, run, flags, "walls: back_left_right")
    for model in (OPUS, SONNET):
        add_answer(
            session,
            run.id,
            version.id,
            question_id="p0:walls",
            prompt_id="slot-walls-v3",
            model_id=model,
            raw=wall_reply("yes", "yes"),
            candidate_ids=[candidate],
        )
    session.commit()
    return version, run


def test_a_stored_run_reads_each_wall_question_with_its_row(session: Session) -> None:
    _version, run = synthetic_walls_run(session)

    stored = load_stored_walls(session, database="synthetic")

    assert stored.extraction_run_id == str(run.id)
    (question,) = stored.questions
    assert question.identity == QuestionIdentity(WALLS_FAMILY, PAGE_HASH, "p0:walls")
    assert sorted(a.model_id for a in question.attempts) == [OPUS, SONNET]
    assert question.row is not None
    assert question.row.row_box == BOX
    assert question.row.ink == (True, False)
    assert question.row.layout == "back_left_right"
    assert question.row.source == "readers"


def test_a_row_stored_before_code_judged_wall_ink_has_no_ink(session: Session) -> None:
    synthetic_walls_run(session, ink=False)

    (question,) = load_stored_walls(session, database="synthetic").questions

    assert question.row is not None and question.row.ink is None


def test_a_held_row_has_no_layout(session: Session) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, page, run = _scaffold(session)
    candidate = add_wall_row(
        session,
        version,
        page,
        run,
        ["wall-reader", "walls-held:walls-not-agreed", "wall-ink:left=False,right=False"],
        "walls: for the person",
    )
    add_answer(
        session,
        run.id,
        version.id,
        question_id="p0:walls",
        prompt_id="slot-walls-v2",
        model_id=OPUS,
        raw=wall_reply("yes", "unsure"),
        candidate_ids=[candidate],
    )
    session.commit()

    (question,) = load_stored_walls(session, database="synthetic").questions

    assert question.row is not None and question.row.layout is None


def test_walls_are_read_in_a_read_only_transaction(session: Session) -> None:
    synthetic_walls_run(session)

    load_stored_walls(session, database="synthetic")

    with pytest.raises(DBAPIError, match="read-only"):
        session.execute(text("CREATE TABLE pariksha_walls_must_not_exist (id int)"))
    session.rollback()


def test_a_database_without_wall_questions_is_refused(session: Session) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    _scaffold(session)
    session.commit()

    with pytest.raises(StoredRunError, match="no stored"):
        load_stored_walls(session, database="synthetic")


def test_a_database_holding_several_runs_needs_the_run_named(session: Session) -> None:
    from tests.workflow.test_reader_reuse import _new_run

    version, run = synthetic_walls_run(session)
    revision_id = session.execute(
        text(
            "SELECT wr.package_revision_id FROM extraction_runs er "
            "JOIN task_runs tr ON tr.id = er.task_run_id "
            "JOIN workflow_runs wr ON wr.id = tr.workflow_run_id WHERE er.id = :run"
        ),
        {"run": run.id},
    ).scalar_one()
    rerun = _new_run(session, revision_id)
    add_answer(
        session,
        rerun.id,
        version.id,
        question_id="p0:walls",
        prompt_id="slot-walls-v3",
        model_id=OPUS,
        raw=wall_reply("yes", "yes"),
        cost_micros=0,
        reused_from=str(uuid4()),
    )
    session.commit()

    with pytest.raises(StoredRunError, match="2 extraction runs"):
        load_stored_walls(session, database="synthetic")
    session.rollback()

    chosen = load_stored_walls(session, database="synthetic", extraction_run_id=rerun.id)
    (question,) = chosen.questions
    assert [attempt.reused for attempt in question.attempts] == [True]
    # The packet named no stored wall row: the row is unknown, so the key's row is never matched.
    assert question.row is None


def test_the_scorecard_of_a_stored_run(session: Session) -> None:
    synthetic_walls_run(session)
    stored = load_stored_walls(session, database="synthetic")
    session.rollback()
    document = {
        "format": "pariksha-key/1",
        "agent": WALLS_AGENT,
        "name": "synthetic",
        "version": "1",
        "cases": [
            {
                "family": WALLS_FAMILY,
                "page_sha256": PAGE_HASH,
                "question_id": "p0:walls",
                "row_box": ",".join(str(v) for v in BOX),
                "left": _end("wall", "wall"),
                "right": _end("open", "open"),
                "site_layout": "back_and_left",
            }
        ],
    }

    card = score_walls(load_walls_key(document), stored, code_version="unrecorded")

    assert _metric(card, "pair_agreed_right") == (1, 2)
    assert _metric(card, "pair_agreed_wrong") == (1, 2)
    assert _metric(card, "ink_right") == (1, 2)
    assert _metric(card, "ink_none") == (1, 2)
    assert _metric(card, "sealed_wrong") == (1, 1)
    assert card.extractor_versions["code_version_note"].startswith("the run's code version")
    assert replace(card, created_at="x").source["database"] == "synthetic"


def test_a_malformed_row_id_or_wall_ink_flag_is_counted(session: Session) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, page, run = _scaffold(session)
    candidate = add_wall_row(
        session,
        version,
        page,
        run,
        ["wall-reader", "walls-held:walls-not-agreed", "wall-ink:left=maybe,right=False"],
        "walls: for the person",
    )
    for model in (OPUS, SONNET):
        add_answer(
            session,
            run.id,
            version.id,
            question_id="p0:walls",
            prompt_id="slot-walls-v3",
            model_id=model,
            raw=wall_reply("yes", "yes"),
            candidate_ids=[candidate],
        )
        add_answer(
            session,
            run.id,
            version.id,
            question_id="p1:walls",
            prompt_id="slot-walls-v3",
            model_id=model,
            raw=wall_reply("yes", "yes"),
            candidate_ids=["not-a-row-id"],
        )
    session.commit()

    stored = load_stored_walls(session, database="synthetic")

    # One wall-ink flag that cannot be read, one row id that is no id: both counted.
    assert stored.malformed == 2
    rows = {question.identity.question_id: question.row for question in stored.questions}
    assert rows["p1:walls"] is None
    first = rows["p0:walls"]
    assert first is not None and first.ink is None and first.malformed
