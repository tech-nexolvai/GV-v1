"""`scripts/smriti_review_page.py`: the local Yes/No page a person confirms Smriti cases on (#1271).

Invented candidates, invented pictures and a synthetic test database only. No model call.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eval.smriti.candidates import read_candidates
from eval.smriti.confirm import CONFIRMATIONS_FORMAT, ready_candidates
from tests.eval.test_smriti_confirm import (
    _batch,
    _confirmed_reading,
    _correction,
    _countertop,
    _export,
    _milan,
    _no_countertop,
    _piece,
    _walls,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import smriti_candidates
import smriti_review_page

pytest_plugins = ("tests.app.postgres_fixture",)

REPO = Path(__file__).resolve().parents[2]
#: A tiny invented PNG (1x1), the picture "the AI saw".
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_SHA = hashlib.sha256(PNG).hexdigest()
FILE_SHA = "f" * 64


def _cards(page: str) -> list[str]:
    return re.findall(r'<section class="card[^"]*" data-id="([^"]+)"', page)


def _picture(name: str = "full_view") -> smriti_review_page.Picture:
    return smriti_review_page.Picture(name=name, sha256=PNG_SHA, png=PNG, problem=None)


# --- the page itself (no database) ----------------------------------------------------------------


def test_the_page_lists_exactly_the_ready_candidates() -> None:
    stranger = replace(_no_countertop("no_countertop_answers:20"), actor_status="unknown")
    superseded = replace(_countertop("no_countertop_answers:21"), kind="superseded")
    export = _export(
        _no_countertop(),
        _countertop(),
        _batch(),
        _correction(),
        _confirmed_reading(),
        _walls(),
        _piece(),
        _milan(),
        stranger,
        superseded,
    )
    page = smriti_review_page.build_page(
        export, candidates_sha256=FILE_SHA, candidates_path="/local/candidates.json", pictures={}
    )
    assert _cards(page) == [candidate.candidate_id for candidate in ready_candidates(export)]
    assert "architect_pairing_records:8" not in page
    assert "no_countertop_answers:20" not in page
    assert "no_countertop_answers:21" not in page


def test_each_card_shows_the_picture_the_proposed_answer_its_source_and_the_decision() -> None:
    reading = _confirmed_reading()
    page = smriti_review_page.build_page(
        _export(reading),
        candidates_sha256=FILE_SHA,
        candidates_path="/local/candidates.json",
        pictures={reading.candidate_id: [_picture("full_view"), _picture("close_up")]},
    )
    card = page.split('data-id="review_actions:5"', 1)[1]
    picture_at = card.index("data:image/png;base64," + base64.b64encode(PNG).decode())
    answer_at = card.index("12-1/2")
    source_at = card.index("pdf-text")
    decision_at = card.index("The reviewer confirmed")
    buttons_at = card.index('data-v="yes"')
    # In this order: the picture, the proposed answer and where it came from, the decision, Yes/No.
    assert picture_at < answer_at < source_at < decision_at < buttons_at
    assert 'data-v="no"' in card and 'data-v="unsure"' in card


def test_a_card_whose_picture_is_missing_says_so_and_cannot_be_answered() -> None:
    reading = _confirmed_reading()
    page = smriti_review_page.build_page(
        _export(reading),
        candidates_sha256=FILE_SHA,
        candidates_path="/local/candidates.json",
        pictures={
            reading.candidate_id: [
                smriti_review_page.Picture(
                    name="full_view", sha256=PNG_SHA, png=None, problem="not in the store"
                )
            ]
        },
    )
    card = page.split('data-id="review_actions:5"', 1)[1]
    assert "not in the store" in card
    assert 'data-v="yes"' not in card.split("</section>", 1)[0]


def test_a_card_with_no_picture_has_no_buttons() -> None:
    page = smriti_review_page.build_page(
        _export(_no_countertop()),
        candidates_sha256=FILE_SHA,
        candidates_path="/local/candidates.json",
        pictures={},
    )
    assert _cards(page) == ["no_countertop_answers:1"]
    assert 'data-v="yes"' not in page and "cannot be answered here" in page


def test_each_question_type_gets_its_own_buttons() -> None:
    export = _export(_countertop(), _batch(), _correction(), _walls(), _piece())
    page = smriti_review_page.build_page(
        export,
        candidates_sha256=FILE_SHA,
        candidates_path="/local/candidates.json",
        pictures={c.candidate_id: [_picture()] for c in ready_candidates(export)},
        boxes={"no_countertop_answers:2": 3},
    )

    def card(candidate_id: str) -> str:
        return page.split(f'data-id="{candidate_id}"', 1)[1].split("</section>", 1)[0]

    countertop = card("no_countertop_answers:2")
    assert [f'data-box="{n}"' in countertop for n in (1, 2, 3, 4)] == [True, True, True, False]
    assert 'data-box="not_among_boxes"' in countertop
    assert 'data-own="no_countertop"' in card("no_countertop_answers:3")
    correction = card("correction_ledger:4")
    assert 'data-mistake="ai_misread"' in correction
    assert 'data-mistake="vendor_drew_it"' in correction
    assert "AI misread" in correction and "vendor drew it wrong" in correction
    walls = card("slot_row_review_decisions:6:walls")
    for end in ("left", "right"):
        for drawn in ("wall", "open", "not_shown", "key_unsure"):
            assert f'data-end="{end}" data-drawn="{drawn}"' in walls
    assert "back wall and left end" in walls
    assert 'data-v="yes"' in card("part_confirmations:7")


def test_the_page_is_self_contained_and_copies_the_confirmations_json() -> None:
    page = smriti_review_page.build_page(
        _export(_no_countertop()),
        candidates_sha256=FILE_SHA,
        candidates_path="/local/candidates.json",
        pictures={},
    )
    assert "<script src" not in page and "<link" not in page
    assert "http://" not in page and "https://" not in page
    assert CONFIRMATIONS_FORMAT in page and FILE_SHA in page
    assert "Copy answers" in page


def test_an_empty_export_gives_a_page_that_says_so() -> None:
    page = smriti_review_page.build_page(
        _export(_milan()),
        candidates_sha256=FILE_SHA,
        candidates_path="/local/candidates.json",
        pictures={},
    )
    assert _cards(page) == []
    assert "Nothing to confirm" in page


def test_a_page_inside_a_git_working_tree_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = REPO / "smriti-page-should-never-exist.html"
    candidates = tmp_path / "candidates.json"
    from eval.smriti.candidates import write_candidates

    write_candidates(_export(_no_countertop()), candidates)
    arguments = [
        str(candidates),
        "--db",
        "postgresql+psycopg://nobody@localhost:1/none",
        "--store",
        str(tmp_path),
        "--out",
        str(inside),
    ]
    assert smriti_review_page.main(arguments) == smriti_review_page.REFUSED
    assert "git working tree" in capsys.readouterr().err
    assert not inside.exists()


def test_the_default_page_lands_under_gv_local_smriti_and_the_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = smriti_review_page.default_out("synthetic", "2026-10-15")
    assert out == tmp_path / "gv-local" / "smriti" / "2026-10-15" / "review-synthetic.html"


# --- the page from a database and a store ($0: stored pictures only) -----------------------------


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
    from eval.smriti.actors import REVIEWERS_ENV, REVIEWERS_FORMAT
    from tests.eval.test_smriti_export import REVIEWER

    people = tmp_path_factory.mktemp("local") / "people.json"
    people.write_text(json.dumps({"format": REVIEWERS_FORMAT, "reviewers": [REVIEWER]}))
    monkeypatch.setenv(REVIEWERS_ENV, str(people))


def store_pictures(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Every question the synthetic world asks shows one invented PNG, kept in a local store.

    The stored packets are append-only, so the world is built with them from the start."""
    import tests.eval.test_smriti_export as world_module
    from storage.local import LocalStore

    key = f"reader-questions/invented/{PNG_SHA}.png"
    LocalStore(root=root).put(key, io.BytesIO(PNG), content_type="image/png")
    original = world_module._packet

    def with_picture(*args: object, **kwargs: object) -> dict[str, object]:
        packet: dict[str, object] = original(*args[:-1], PNG_SHA)  # type: ignore[arg-type,call-arg]
        packet["images"] = {"view": {"sha256": PNG_SHA, "storage_key": key}}
        return packet

    monkeypatch.setattr(world_module, "_packet", with_picture)


def test_a_page_from_a_database_shows_the_stored_pictures_of_exactly_the_ready_candidates(
    engine: Engine, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.eval.test_smriti_export import build_v2_world

    store_pictures(monkeypatch, tmp_path / "store")
    build_v2_world(session)
    url = engine.url.render_as_string(hide_password=False)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", url, "--out", str(candidates)]) == 0
    out = tmp_path / "local" / "review.html"
    assert (
        smriti_review_page.main(
            [str(candidates), "--db", url, "--store", str(tmp_path / "store"), "--out", str(out)]
        )
        == 0
    )
    page = out.read_text()
    ready = ready_candidates(read_candidates(candidates))
    assert ready, "the synthetic world has ready candidates"
    assert _cards(page) == [candidate.candidate_id for candidate in ready]
    encoded = "data:image/png;base64," + base64.b64encode(PNG).decode()
    for candidate_id in _cards(page):
        card = page.split(f'data-id="{candidate_id}"', 1)[1].split("</section>", 1)[0]
        assert encoded in card, f"{candidate_id} shows no stored picture"
    # Written once: a second page to the same file is refused.
    assert (
        smriti_review_page.main(
            [str(candidates), "--db", url, "--store", str(tmp_path / "store"), "--out", str(out)]
        )
        == smriti_review_page.REFUSED
    )


def test_a_missing_store_is_refused(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from eval.smriti.candidates import write_candidates

    candidates = tmp_path / "candidates.json"
    write_candidates(_export(_no_countertop()), candidates)
    url = engine.url.render_as_string(hide_password=False)
    out = tmp_path / "review.html"
    arguments = [str(candidates), "--db", url, "--store", str(tmp_path / "nope"), "--out", str(out)]
    assert smriti_review_page.main(arguments) == smriti_review_page.REFUSED
    assert "store" in capsys.readouterr().err
    assert not out.exists() and not (tmp_path / "nope").exists()


# --- Anant's look at the proof page (2026-10-15) --------------------------------------------------

#: The cases where a person accepts or rejects a proposed answer, and those where they state it.
ACCEPTS = ("no_countertop_answers:1", "review_actions:5", "part_confirmations:7")
STATES = (
    "no_countertop_answers:2",
    "no_countertop_answers:3",
    "correction_ledger:4",
    "slot_row_review_decisions:6:walls",
)


def _every_type_page() -> str:
    export = _export(
        _no_countertop(),
        _countertop(),
        _batch(),
        _correction(),
        _confirmed_reading(),
        _walls(),
        _piece(),
    )
    return smriti_review_page.build_page(
        export,
        candidates_sha256=FILE_SHA,
        candidates_path="/local/candidates.json",
        pictures={c.candidate_id: [_picture()] for c in ready_candidates(export)},
    )


def _card_of(page: str, candidate_id: str) -> str:
    return page.split(f'data-id="{candidate_id}"', 1)[1].split("</section>", 1)[0]


def test_a_case_with_no_proposed_answer_shows_one_instruction_and_no_proposed_answer_line() -> None:
    page = _every_type_page()
    for candidate_id in STATES:
        card = _card_of(page, candidate_id)
        assert "Proposed answer" not in card, candidate_id
        assert 'class="instruction"' in card, candidate_id
    assert "Tell us what the vendor drew at each end of the row." in _card_of(
        page, "slot_row_review_decisions:6:walls"
    )
    for candidate_id in ACCEPTS:
        card = _card_of(page, candidate_id)
        assert "Proposed answer" in card and 'class="instruction"' not in card, candidate_id


def test_where_the_person_states_the_answer_the_choices_are_the_answer() -> None:
    page = _every_type_page()
    for candidate_id in STATES:
        card = _card_of(page, candidate_id)
        assert 'data-v="yes"' not in card and 'data-v="no"' not in card, candidate_id
        assert card.count('data-v="unsure"') == 1, candidate_id
        assert ">Skip / unsure</button>" in card, candidate_id
        assert 'data-mode="state"' in page.split(f'data-id="{candidate_id}"', 1)[1][:200]
    for candidate_id in ACCEPTS:
        card = _card_of(page, candidate_id)
        for verdict in ("yes", "no", "unsure"):
            assert card.count(f'data-v="{verdict}"') == 1, (candidate_id, verdict)


def test_a_real_page_never_says_proof() -> None:
    assert "proof" not in _every_type_page().lower()
