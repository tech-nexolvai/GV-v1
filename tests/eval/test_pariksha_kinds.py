"""Pariksha's piece-kind scorecard (Bala, #1250): the kind code names for each keyed piece.

Verification for: `eval/pariksha/kinds.py`. Invented pages, pieces, tags and kinds; the stored-run
tests use a synthetic run on the test database. No model call, no client value.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from eval.pariksha.keys import KeyFormatError, load_key
from eval.pariksha.kinds import (
    FACE_TAG,
    KINDS_AGENT,
    KINDS_FAMILY,
    NOT_SCORED,
    PRODUCT,
    RIGHT,
    ROW_UNVERIFIED,
    UNKNOWN,
    WRONG,
    StoredPiece,
    StoredPiecesRun,
    StoredWord,
    compare_kinds,
    dump_kinds_key,
    face_tag_kind,
    load_kinds_key,
    load_stored_pieces,
    score_kinds,
    truth_kinds,
)
from eval.pariksha.score import NOT_ASKED, Scorecard
from eval.pariksha.stored import StoredRunError
from eval.regression import IncomparableRuns

pytest_plugins = ("tests.app.postgres_fixture",)

PAGES = {name: name * 64 for name in "abcde"}


#: Each keyed row's piece widths (inches), in proportion to the stored spans below.
WIDTHS = {
    "a": ["1", "3", "3"],
    "b": ["1", "2"],
    "c": ["1", "1", "1", "1"],
    "d": ["1", "1"],
    "e": ["1", "2"],
}


def _case(page: str, index: int, pieces: int, answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "family": KINDS_FAMILY,
        "page_sha256": PAGES[page],
        "question_id": f"p{'abcde'.index(page)}:piece{index}",
        "pieces": pieces,
        "row_widths": WIDTHS[page],
        "answer": answer,
        "source": "invented",
    }


def key_document() -> dict[str, Any]:
    return {
        "format": "pariksha-key/1",
        "agent": KINDS_AGENT,
        "name": "synthetic kinds",
        "version": "1",
        "cases": [
            _case("a", 0, 3, {"kinds": ["filler"]}),
            _case("a", 1, 3, {"kinds": ["cabinet"]}),
            _case("a", 2, 3, {"kinds": ["appliance_space"]}),
            _case("b", 0, 2, {"kinds": ["panel"]}),
            _case("b", 1, 2, {"kinds": ["cabinet"]}),
            _case("c", 0, 4, {"kinds": ["filler"]}),  # the run read 2 pieces here
            _case("d", 0, 2, {"kinds": ["filler"]}),  # the run read no row here
            _case("e", 0, 2, {"not_scored": "section"}),
        ],
    }


def _piece(page: str, slot: int, x0: int, x1: int, kind: str | None) -> StoredPiece:
    return StoredPiece(
        page_sha256=PAGES[page],
        page_index="abcde".index(page),
        slot=slot,
        box=(x0, 1000, x1, 1100),
        kind=kind,
    )


def _word(text_: str, x0: float, x1: float, y: float) -> StoredWord:
    return StoredWord(text=text_, box=(x0, y - 20, x1, y + 20))


def stored_run(**words: tuple[StoredWord, ...]) -> StoredPiecesRun:
    pieces = (
        _piece("a", 0, 0, 100, "filler"),
        _piece("a", 1, 100, 400, "unknown"),
        _piece("a", 2, 400, 700, "unknown"),
        _piece("b", 0, 0, 200, "filler"),
        _piece("b", 1, 200, 600, None),
        _piece("c", 0, 0, 300, "unknown"),
        _piece("c", 1, 300, 600, "unknown"),
        _piece("e", 0, 0, 300, "unknown"),
        _piece("e", 1, 300, 900, "unknown"),
    )
    on_pages = {
        PAGES["a"]: (
            _word("B24", 150, 250, 900),  # above piece 1, inside its span
            _word("DW", 500, 560, 950),  # above piece 2
            _word("B36", 150, 250, 300),  # far above: another drawing's tag
            _word("W2430", 380, 450, 900),  # not wholly inside one piece's span
        ),
        PAGES["b"]: (
            _word("W2430", 50, 150, 850),  # a wall cabinet's tag above a panel
            _word("B15", 250, 300, 900),
            _word("DW", 400, 450, 900),  # two kinds named for piece 1
        ),
    }
    on_pages.update({PAGES[page]: found for page, found in words.items()})
    return StoredPiecesRun(
        database="synthetic",
        extraction_run_id=str(uuid4()),
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-ink-v1",
        config_hash="test",
        document_sha256="9" * 64,
        pieces=pieces,
        words=on_pages,
    )


def _score(run: StoredPiecesRun, source: str, key: dict[str, Any] | None = None) -> Scorecard:
    return score_kinds(
        load_kinds_key(key or key_document()),
        run,
        source=source,
        code_version="abc1234",
        created_at="2026-01-01T00:00:00+00:00",
    )


def _metric(card: Scorecard, name: str) -> tuple[int, int]:
    metric = card.metrics[name]
    return metric.numerator, metric.denominator


def _outcomes(card: Scorecard) -> dict[str, str]:
    return {
        case.case_id[:1] + case.case_id.rsplit("piece", 1)[-1]: case.outcome for case in card.cases
    }


# --- the truth words and the key ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "kinds"),
    [
        ("filler", ("filler",)),
        ("filler (combined)", ("filler",)),
        ("cabinet", ("cabinet",)),
        ("cabinet B99", ("cabinet",)),
        ("sink cabinet", ("cabinet",)),
        ("vanity", ("cabinet",)),
        ("cabinets (4 equal)", ("cabinet", "equal_cabinets")),
        ("end panel", ("panel",)),
        ("middle panel", ("panel",)),
        ("end piece", ("panel",)),
        ("filler/panel", ("filler", "panel")),
        # The order of the words never matters.
        ("panel/filler", ("filler", "panel")),
        ("Panel / Filler", ("filler", "panel")),
        ("end piece or filler", ("filler", "panel")),
        ("space for the dishwasher", ("appliance_space",)),
        ("equal cabinets", ("cabinet", "equal_cabinets")),
        ("into wall / end", ("filler", "panel")),
        ("dishwasher space", ("appliance_space",)),
        ("fridge space (not under countertop)", ("appliance_space",)),
        ("appliance_space", ("appliance_space",)),
    ],
)
def test_the_keys_words_become_the_kinds_a_reader_may_name(
    word: str, kinds: tuple[str, ...]
) -> None:
    truth = truth_kinds(word)
    assert truth.kinds == kinds and truth.not_scored is None


@pytest.mark.parametrize(
    "word",
    [
        "section",
        "countertop_segment",
        "unknown",
        "unknown_check_sink_or_appliance",
        "",
        # Two kinds the key cannot both mean: never guessed.
        "cabinet / filler",
        "filler or dishwasher space",
    ],
)
def test_a_word_that_names_no_kind_is_kept_but_not_scored(word: str) -> None:
    truth = truth_kinds(word)
    assert truth.kinds == () and truth.not_scored == (word or "no kind given")


def test_the_key_reads_back_in_the_same_shape() -> None:
    key = load_kinds_key(key_document())

    assert len(key.cases) == 8
    assert key.cases[0].slot == 0 and key.cases[0].pieces == 3
    assert key.cases[-1].truth.not_scored == "section"
    assert load_kinds_key(dump_kinds_key(key)).sha256 == key.sha256


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"answer": {"kinds": ["shelf"]}}, "kinds"),
        ({"answer": {"kinds": []}}, "kinds"),
        ({"answer": {"kinds": ["filler"], "not_scored": "x"}}, "answer"),
        ({"pieces": 0}, "pieces"),
        ({"question_id": "p0:piece3"}, "piece"),
        ({"question_id": "p0:slot0:0"}, "question_id"),
        ({"family": "claude-slot-span"}, "family"),
        ({"page_sha256": "A" * 64}, "page_sha256"),
        ({"text": "3"}, "unknown field"),
        ({"row_widths": ["1", "3"]}, "row_widths"),
        ({"row_widths": ["1", "3", "three"]}, "row_widths"),
        ({"row_widths": ["1", "3", "-3"]}, "row_widths"),
        ({"row_widths": "1,3,3"}, "row_widths"),
    ],
)
def test_a_bad_case_is_refused(change: dict[str, Any], message: str) -> None:
    document = key_document()
    document["cases"][0] = {**document["cases"][0], **change}
    with pytest.raises(KeyFormatError, match=message):
        load_kinds_key(document)


def test_the_label_and_row_scorer_refuses_a_kinds_key() -> None:
    with pytest.raises(KeyFormatError, match="agent"):
        load_key(key_document())


# --- the face-tag rule --------------------------------------------------------------------------


def test_a_tag_wholly_inside_the_span_just_above_the_piece_names_it() -> None:
    kind, evidence = face_tag_kind((100, 1000, 400, 1100), (_word("B24", 150, 250, 900),))
    assert kind == "cabinet" and "B24" in evidence


@pytest.mark.parametrize(
    "word",
    [
        _word("B24", 50, 250, 900),  # starts left of the piece
        _word("B24", 150, 250, 300),  # far above the row
        _word("B24", 150, 250, 1050),  # below the piece's top
        _word("b24", 150, 250, 900),  # not in capitals: not a tag
        _word("TW-101", 150, 250, 900),  # a finish code, never a cabinet
        _word("PANEL", 150, 250, 900),  # a word the rule does not read
    ],
)
def test_anything_else_names_nothing(word: StoredWord) -> None:
    kind, evidence = face_tag_kind((100, 1000, 400, 1100), (word,))
    assert kind == UNKNOWN and evidence


def test_two_kinds_named_for_one_piece_leave_it_unknown_naming_both() -> None:
    kind, evidence = face_tag_kind(
        (100, 1000, 400, 1100), (_word("B24", 150, 250, 900), _word("Filler", 260, 390, 900))
    )
    assert kind == UNKNOWN and "B24" in evidence and "Filler" in evidence


def test_the_word_filler_and_the_appliance_words() -> None:
    box = (100, 1000, 400, 1100)
    assert face_tag_kind(box, (_word("FILLER", 150, 250, 900),))[0] == "filler"
    assert face_tag_kind(box, (_word("Range", 150, 250, 900),))[0] == "appliance_space"


# --- scoring ------------------------------------------------------------------------------------


def test_the_products_stored_kinds_are_scored() -> None:
    card = _score(stored_run(), PRODUCT)

    assert card.agent == KINDS_AGENT
    assert card.extractor_versions["kind_source"] == PRODUCT
    outcomes = _outcomes(card)
    assert outcomes == {
        "a0": RIGHT,
        "a1": UNKNOWN,
        "a2": UNKNOWN,
        "b0": WRONG,  # a panel called a filler
        "b1": UNKNOWN,  # no kind recorded
        "c0": NOT_ASKED,
        "d0": NOT_ASKED,
        "e0": NOT_SCORED,
    }
    assert _metric(card, "asked") == (6, 8)
    assert _metric(card, "scored") == (5, 6)
    assert _metric(card, "right") == (1, 5)
    assert _metric(card, "wrong") == (1, 5)
    assert _metric(card, "unknown") == (3, 5)
    # Every piece the run read, keyed or not: 2 of 9 have a kind.
    assert _metric(card, "named_pieces") == (2, 9)
    assert _metric(card, "unkeyed_pieces") == (3, 9)
    assert _metric(card, "row_unverified") == (0, 8)
    assert _metric(card, "skipped_malformed") == (0, 9)


def test_the_face_tag_rule_is_scored_on_the_same_pieces() -> None:
    card = _score(stored_run(), FACE_TAG)

    outcomes = _outcomes(card)
    assert outcomes["a0"] == UNKNOWN
    assert outcomes["a1"] == RIGHT
    assert outcomes["a2"] == RIGHT
    assert outcomes["b0"] == WRONG  # a wall cabinet's tag above a panel
    assert outcomes["b1"] == UNKNOWN  # two kinds named
    assert _metric(card, "right") == (2, 5)
    assert _metric(card, "wrong") == (1, 5)
    b1 = next(c for c in card.cases if c.case_id.startswith("b") and c.case_id.endswith("piece1"))
    assert "two ways" in b1.readers[0].answer or "two ways" in b1.note


def test_a_case_the_run_did_not_read_says_why() -> None:
    card = _score(stored_run(), PRODUCT)

    notes = {c.case_id[:1]: c.note for c in card.cases if c.outcome == NOT_ASKED}
    assert "read 2 pieces" in notes["c"] and "the key has 4" in notes["c"]
    assert "no row" in notes["d"]


def test_the_source_must_be_known() -> None:
    with pytest.raises(ValueError, match="source"):
        _score(stored_run(), "guess")


def test_one_page_read_twice_in_the_run_is_refused() -> None:
    run = stored_run()
    doubled = run.pieces + (_piece("a", 0, 0, 100, "filler"),)
    with pytest.raises(StoredRunError, match="twice"):
        _score(StoredPiecesRun(**{**_fields(run), "pieces": doubled}), PRODUCT)


def _fields(run: StoredPiecesRun) -> dict[str, Any]:
    return {name: getattr(run, name) for name in run.__dataclass_fields__}


def test_a_row_whose_spans_are_not_in_proportion_to_the_key_is_unverified_never_scored() -> None:
    """Same piece count is not the same row: the spans must fit the key's widths too."""
    document = key_document()
    for case in document["cases"][:3]:
        case["row_widths"] = ["1", "3", "30"]  # the run's last piece is as wide as its second
    card = _score(stored_run(), FACE_TAG, document)

    outcomes = _outcomes(card)
    assert [outcomes[f"a{slot}"] for slot in range(3)] == [ROW_UNVERIFIED] * 3
    a1 = next(c for c in card.cases if c.case_id.endswith("p0:piece1"))
    assert "not in proportion" in a1.note and not a1.readers
    assert _metric(card, "row_unverified") == (3, 8)
    assert _metric(card, "asked") == (3, 8)  # b0, b1, e0
    assert _metric(card, "right") == (0, 2)


def test_a_key_without_row_widths_leaves_the_row_unverified() -> None:
    document = key_document()
    for case in document["cases"][3:5]:
        del case["row_widths"]
    card = _score(stored_run(), PRODUCT, document)

    outcomes = _outcomes(card)
    assert (outcomes["b0"], outcomes["b1"]) == (ROW_UNVERIFIED, ROW_UNVERIFIED)
    assert "no widths" in next(c.note for c in card.cases if c.case_id.endswith("p1:piece0"))


def test_spans_a_few_pixels_off_still_fit_the_row() -> None:
    run = stored_run()
    nudged = tuple(
        (
            StoredPiece(**{**_piece_fields(piece), "box": (piece.box[0] + 3, *piece.box[1:])})
            if piece.page_sha256 == PAGES["a"] and piece.slot == 1
            else piece
        )
        for piece in run.pieces
    )
    card = _score(StoredPiecesRun(**{**_fields(run), "pieces": nudged}), FACE_TAG)

    assert _outcomes(card)["a1"] == RIGHT


def test_a_row_of_one_known_width_cannot_be_verified() -> None:
    document = key_document()
    for case in document["cases"][3:5]:
        case["row_widths"] = ["1", None]
    card = _score(stored_run(), PRODUCT, document)

    assert _outcomes(card)["b0"] == ROW_UNVERIFIED


def test_two_cases_of_one_row_with_different_widths_are_refused() -> None:
    document = key_document()
    document["cases"][1]["row_widths"] = ["1", "3", "4"]
    with pytest.raises(KeyFormatError, match="row_widths"):
        load_kinds_key(document)


def test_malformed_pieces_are_counted_never_silent() -> None:
    card = _score(StoredPiecesRun(**{**_fields(stored_run()), "malformed": 2}), PRODUCT)

    assert _metric(card, "skipped_malformed") == (2, 11)


def _piece_fields(piece: StoredPiece) -> dict[str, Any]:
    return {name: getattr(piece, name) for name in piece.__dataclass_fields__}


# --- compare ------------------------------------------------------------------------------------


def test_two_sources_compare_case_by_case() -> None:
    comparison = compare_kinds(_score(stored_run(), PRODUCT), _score(stored_run(), FACE_TAG))

    # Face tags name more (2 right vs 1) and call b0 a cabinet: b0 was already wrong as a filler,
    # so nothing new is wrong; a0 is no longer named.
    assert comparison.accepted, comparison.verdict
    assert [change.case_id[:1] + change.case_id[-1] for change in comparison.fixed] == ["a1", "a2"]
    assert [change.case_id[:1] + change.case_id[-1] for change in comparison.broke] == ["a0"]


def test_better_with_no_new_wrong_is_accepted_and_a_new_wrong_rejected() -> None:
    before = _score(stored_run(b=()), FACE_TAG)
    after = _score(stored_run(), FACE_TAG)
    assert not compare_kinds(before, after).accepted
    assert "1 new wrong" in compare_kinds(before, after).verdict

    quiet_b = (_word("B15", 250, 300, 900),)
    accepted = compare_kinds(before, _score(stored_run(b=quiet_b), FACE_TAG))
    assert accepted.accepted, accepted.verdict
    assert {change.after for change in accepted.fixed} == {RIGHT}


def test_a_case_that_becomes_unverified_is_rejected_as_no_longer_asked() -> None:
    before = _score(stored_run(), PRODUCT)
    run = stored_run()
    # The run's last piece on page a is now far wider than the key's: another row, same count.
    moved = tuple(
        (
            StoredPiece(**{**_piece_fields(piece), "box": (piece.box[0], 1000, 3000, 1100)})
            if piece.page_sha256 == PAGES["a"] and piece.slot == 2
            else piece
        )
        for piece in run.pieces
    )
    after = _score(StoredPiecesRun(**{**_fields(run), "pieces": moved}), PRODUCT)

    comparison = compare_kinds(before, after)

    assert not comparison.accepted
    assert "no longer asked" in comparison.verdict
    assert {change.after for change in comparison.dropped} == {ROW_UNVERIFIED}


def test_compare_kinds_refuses_a_card_of_another_agent() -> None:
    kinds = _score(stored_run(), PRODUCT)
    other = replace(kinds, agent="rekha-walls")

    for before, after in ((other, other), (kinds, other), (other, kinds)):
        with pytest.raises(IncomparableRuns, match="bala-kinds"):
            compare_kinds(before, after)


def test_scorecards_of_different_keys_are_not_compared() -> None:
    other = key_document()
    other["version"] = "2"
    with pytest.raises(IncomparableRuns, match="different answer keys"):
        compare_kinds(_score(stored_run(), PRODUCT), _score(stored_run(), PRODUCT, other))


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


def _extractor_run(session: Session, run: Any, extractor: str) -> Any:
    from app.models.runs import ExtractionRun, TaskRun

    reader_task = session.get(TaskRun, run.task_run_id)
    assert reader_task is not None
    task = TaskRun(
        workflow_run_id=reader_task.workflow_run_id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    other = ExtractionRun(
        task_run_id=task.id, extractor=extractor, extractor_version="1", config_hash="test"
    )
    session.add(other)
    session.flush()
    return other


def _candidate(
    session: Session,
    version: Any,
    page: Any,
    run: Any,
    raw: str,
    box: tuple[int, ...],
    flags: list[str],
) -> None:
    from app.models.evidence import ObservationCandidate

    x0, y0, x1, y1 = box
    session.add(
        ObservationCandidate(
            document_version_id=version.id,
            page_id=page.id,
            extraction_run_id=run.id,
            raw_text=raw,
            polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            coordinate_space="image",
            ambiguity_flags=flags,
        )
    )
    session.flush()


def synthetic_pieces_run(session: Session) -> tuple[Any, Any]:
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, page, run = _scaffold(session)
    for slot, (x0, x1, kind) in enumerate(((0, 100, "filler"), (100, 400, "unknown"))):
        _candidate(
            session,
            version,
            page,
            run,
            "invented",
            (x0, 1000, x1, 1100),
            ["slot-reader", f"slot:{slot}", f"slot-box:{x0},1000,{x1},1100", f"kind:{kind}"],
        )
    _candidate(
        session,
        version,
        page,
        run,
        "invented",
        (0, 1000, 400, 1100),
        ["slot-reader", "slot:overall"],
    )
    words = _extractor_run(session, run, "extraction.stamp_text")
    _candidate(session, version, page, words, "B24", (150, 880, 250, 920), [])
    markup = _extractor_run(session, run, "extraction.annotations")
    _candidate(session, version, page, markup, "DW", (10, 880, 90, 920), [])
    session.commit()
    return version, run


def test_a_stored_run_reads_each_piece_and_the_printed_words(session: Session) -> None:
    _version, run = synthetic_pieces_run(session)

    stored = load_stored_pieces(session, database="synthetic")

    assert stored.extraction_run_id == str(run.id)
    assert stored.document_sha256 == "1" * 64
    assert [(p.page_index, p.slot, p.box, p.kind) for p in stored.pieces] == [
        (0, 0, (0, 1000, 100, 1100), "filler"),
        (0, 1, (100, 1000, 400, 1100), "unknown"),
    ]
    # The vendor's text layers only: the reviewer's markup is the answer key, never a source.
    assert [word.text for word in stored.words["1" * 64]] == ["B24"]


def test_pieces_are_read_in_a_read_only_transaction(session: Session) -> None:
    synthetic_pieces_run(session)

    load_stored_pieces(session, database="synthetic")

    with pytest.raises(DBAPIError, match="read-only"):
        session.execute(text("CREATE TABLE pariksha_kinds_must_not_exist (id int)"))
    session.rollback()


def test_a_database_without_pieces_is_refused(session: Session) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    _scaffold(session)
    session.commit()

    with pytest.raises(StoredRunError, match="no stored"):
        load_stored_pieces(session, database="synthetic")


def test_the_scorecard_of_a_stored_run(session: Session) -> None:
    synthetic_pieces_run(session)
    stored = load_stored_pieces(session, database="synthetic")
    session.rollback()
    document = {
        "format": "pariksha-key/1",
        "agent": KINDS_AGENT,
        "name": "synthetic",
        "version": "1",
        "cases": [
            {
                "family": KINDS_FAMILY,
                "page_sha256": "1" * 64,
                "question_id": f"p0:piece{slot}",
                "pieces": 2,
                "row_widths": ["1", "3"],
                "answer": {"kinds": [kind]},
            }
            for slot, kind in ((0, "filler"), (1, "cabinet"))
        ],
    }
    key = load_kinds_key(document)

    product = score_kinds(key, stored, source=PRODUCT, code_version="unrecorded")
    tags = score_kinds(key, stored, source=FACE_TAG, code_version="unrecorded")

    assert _metric(product, "right") == (1, 2)
    assert _metric(tags, "right") == (1, 2)
    assert _metric(product, "named_pieces") == (1, 2)
    assert product.source["document_sha256"] == "1" * 64


def test_malformed_piece_flags_are_counted_and_the_overall_is_not_a_piece(
    session: Session,
) -> None:
    version, run = synthetic_pieces_run(session)
    page = session.execute(
        text("SELECT page_id FROM observation_candidates WHERE extraction_run_id = :run LIMIT 1"),
        {"run": run.id},
    ).scalar_one()
    from app.models.document import Page

    on_page = session.get(Page, page)
    for flags in (
        ["slot-reader", "slot:2"],  # no span
        ["slot-reader", "slot:3", "slot-box:1,2,3"],  # a span of three numbers
        ["slot-reader", "slot:three", "slot-box:400,1000,500,1100"],  # no position
        ["slot-reader"],  # neither
    ):
        _candidate(session, version, on_page, run, "invented", (0, 0, 10, 10), flags)
    session.commit()

    stored = load_stored_pieces(session, database="synthetic")

    # `slot:overall` is the row's overall, not a piece: never counted as malformed.
    assert stored.malformed == 4
    assert len(stored.pieces) == 2
