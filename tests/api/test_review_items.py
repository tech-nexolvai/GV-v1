"""The simple review list: every blocking result once, real guesses only, read-only (#1338)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _candidate(flags: list[str], value: tuple[int, int] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        ambiguity_flags=flags,
        value_numerator=None if value is None else value[0],
        value_denominator=None if value is None else value[1],
        unit=None if value is None else "in",
    )


def test_best_guess_is_exactly_the_stored_value_with_every_reading_beside_it() -> None:
    """#1358: the guess is the value a Confirm records (the stored one), never the PDF text or one
    reader's text when they differ; every reading is listed so the disagreement is in sight."""
    from app.review.review_items import _readers, best_guess

    held = _candidate(
        [
            "ink:vendor",
            "reader:pdf-text-layer:21",
            "reader:model-a:21",
            "reader:model-b:20",
            "readers-differ",
        ],
        (20, 1),
    )
    guess = best_guess(held)
    assert guess is not None and guess.text == '20"' and guess.source == "suggestion"
    assert guess.value is not None and guess.value.display == '20"'
    assert [(reader.reader, reader.text) for reader in _readers(held)] == [
        ("pdf-text-layer", "21"),
        ("model-a", "21"),
        ("model-b", "20"),
    ]

    # Who wrote the stored value: the PDF's own text, else both readers identically, else neither.
    both = ["ink:vendor", "reader:model-a:12 1/4", "reader:model-b:12 1/4"]
    pdf = best_guess(_candidate(["reader:pdf-text-layer:12 1/4", *both], (49, 4)))
    assert pdf is not None and pdf.source == "pdf_text" and pdf.text == '12 1/4"'
    agreed = best_guess(_candidate([*both, "odd-fraction"], (49, 4)))
    assert agreed is not None and agreed.source == "agreed_text" and agreed.text == '12 1/4"'
    other_pdf = best_guess(_candidate(["reader:pdf-text-layer:12 3/4", *both], (49, 4)))
    assert other_pdf is not None and other_pdf.text == '12 1/4"'
    assert other_pdf.source == "agreed_text"

    # Nothing stored, or not in inches: no guess (checked by hand), whatever the texts say.
    assert best_guess(_candidate([*both, "odd-fraction"])) is None
    in_mm = _candidate(both, (311, 1))
    in_mm.unit = "mm"
    assert best_guess(in_mm) is None


def test_gv_markup_is_never_a_guess_or_a_reading() -> None:
    from app.review.review_items import _readers, best_guess, plain_why

    for markup in (["ink:vendor", "reviewer-markup"], ["ink:vendor", "gv-tick"], ["ink:reviewer"]):
        held = _candidate(
            [*markup, "reader:pdf-text-layer:30", "reader:model-a:30", "reader:model-b:30"],
            (30, 1),
        )
        assert best_guess(held) is None
        assert _readers(held) == []
    # The hold's "reviewer wrote X" is GV's own writing: never shown.
    why = plain_why(
        "Covered by reviewer markup; reviewer wrote 31 1/2", "Check the vendor's own number."
    )
    assert "31" not in why and "wrote" not in why


def test_a_second_label_or_unchecked_ink_is_never_a_guess_or_a_reading() -> None:
    """A piece with two labels keeps every label's reader texts on one record with no ink flag:
    typically the vendor's number and GV's correction beside it (made-up values). Only a record
    positively checked as the vendor's one label gives a guess or reader texts."""
    from app.review.review_items import _readers, best_guess

    texts = ["reader:model-a:30", "reader:model-b:30", "reader:model-a:31 1/2"]
    for flags in (
        ["two-labels", *texts],  # two labels, one of them GV's ink: no ink flag at all
        ["two-labels", "ink:vendor", *texts],
        ["ink-unchecked", "reader:model-a:30", "reader:model-b:30"],
        ["reader:model-a:30", "reader:model-b:30"],  # ink never recorded
        ["ink:vendor", "ink:reviewer", "reader:model-a:30", "reader:model-b:30"],
    ):
        held = _candidate([*flags, "reader:pdf-text-layer:30"], (30, 1))
        assert best_guess(held) is None, flags
        assert _readers(held) == [], flags


def test_reviewer_wrote_is_removed_to_the_end_of_its_clause() -> None:
    from app.review.review_items import plain_why

    fallback = "Check the vendor's own number."
    assert plain_why("Covered by reviewer markup; reviewer wrote 42.5", fallback) == (
        "Covered by reviewer markup"
    )
    kept = plain_why("Covered by reviewer markup; reviewer wrote 3 1/2. Box 2; edge", fallback)
    assert "3 1/2" not in kept and "Box" not in kept and kept.endswith("edge")
    assert plain_why("reviewer wrote 42.5", fallback) == fallback


def test_the_accounting_names_a_result_or_page_left_out_or_listed_twice() -> None:
    from app.review.review_items import (
        FailureOut,
        NotCheckedOut,
        ReviewItemOut,
        account,
    )

    kept, dropped, doubled = uuid4(), uuid4(), uuid4()

    def item(finding_id: UUID | None, *, kind: str = "number", page: int = 1) -> ReviewItemOut:
        return ReviewItemOut(
            key=str(uuid4()),
            kind=kind,  # type: ignore[arg-type]
            page=page,
            what="made-up",
            why="made-up",
            actions=[],
            status="open",
            finding_id=finding_id,
            row_id=uuid4(),
        )

    failure = FailureOut(
        key="f", finding_id=doubled, rule_id="R", why="made-up", actions=[], status="open"
    )
    waiting = NotCheckedOut(
        key="n", finding_id=doubled, rule_id="R", check="made-up", why="made-up", status="open"
    )
    found = account(
        blocking_findings=[kept, dropped, doubled],
        blocking_pages=[4, 5],
        # Two questions on one result are still one group.
        items=[item(kept), item(kept), item(None, kind="no_countertop_page", page=4)],
        failures=[failure],
        not_checked=[waiting],
    )
    by_id = {entry.finding_id: entry.why for entry in found if entry.kind == "finding"}
    assert by_id == {
        dropped: "in no group",
        doubled: "listed more than once: failures, not_checked",
    }
    assert [(entry.page, entry.why) for entry in found if entry.kind == "page"] == [
        (5, "in no item")
    ]


# --- walls: a countertop whose only open question is its walls (#1358) ----------------------------


def test_walls_the_readers_did_not_agree_on_propose_nothing() -> None:
    """`walls-held:` (the readers differ), or a person's earlier answer asked again: every layout
    is offered with nothing pre-selected, as the wall review shows it."""
    from app.review.review_items import _readers_proposal

    def result(config: str | None, chosen: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            wall_layout=SimpleNamespace(config=config, chosen_by_reviewer=chosen)
        )

    def row(flags: list[str], wall_hold: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            wall_candidate=SimpleNamespace(ambiguity_flags=flags), wall_hold=wall_hold
        )

    sealed = ["walls-sealed:back_left_right", "wall-source:readers"]
    assert _readers_proposal(result("back_left_right"), row(sealed)) == "back_left_right"
    held = row([*sealed, "walls-held:readers differ"])
    assert _readers_proposal(result("back_left_right"), held) is None
    asked_again = row(sealed, wall_hold="an earlier answer could not be carried")
    assert _readers_proposal(result("back_left_right"), asked_again) is None
    assert _readers_proposal(result("back_and_left", chosen=True), row(sealed)) is None
