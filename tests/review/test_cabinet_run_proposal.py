"""A cabinet run proposed from the AI's stored width readings, never guessed (cabinets e2e).

`app/review/cabinet_run_proposal.py` proposes a vendor row as a cabinet run only when every piece
was read alike by two AIs in the vendor's ink, the chain is complete, no label adds two numbers,
the stone does not run into the walls there, and a sealed printed overall equals the sum. Anything
else is listed with its reason. What a piece is comes only from a printed "Filler".

Every number and name is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from fractions import Fraction
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import DocumentKind, Package, PackageRevision, Project
from app.review.cabinet_run_proposal import (
    NOTHING_READ,
    cabinet_proposal,
    proposed_runs,
)
from tests.app.postgres_fixture import alembic_config
from tests.review.cabinet_run_fixtures import SAMPLE_RUN, Piece, add_reading

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


def _revision(session: Session) -> tuple[UUID, UUID]:
    project = Project(name="cabinet run proposal tests")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic vendor", product_type="cabinet")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state="NEEDS_INPUT")
    session.add(revision)
    session.commit()
    return package.id, revision.id


def _widths(session: Session, revision: UUID) -> list[list[str]]:
    runs, _ = proposed_runs(session, revision)
    return [[piece.width.display for piece in run.pieces] for run in runs]


def _reasons(session: Session, revision: UUID) -> list[str]:
    return [entry.why for entry in proposed_runs(session, revision)[1]]


def test_a_sealed_complete_vendor_chain_is_proposed_left_to_right(session: Session) -> None:
    package, revision = _revision(session)
    anchor = add_reading(session, package_id=package, revision_id=revision, pieces=SAMPLE_RUN)

    runs, skipped = proposed_runs(session, revision)

    assert skipped == []
    assert len(runs) == 1
    run = runs[0]
    assert run.row_id == anchor
    assert run.page == 13
    assert [piece.width.display for piece in run.pieces] == ['2"', '21"', '30"', '24"', '1 1/2"']
    assert [piece.position for piece in run.pieces] == [1, 2, 3, 4, 5]
    assert run.total.display == '78 1/2"'
    # Nothing printed names a piece, so nothing is pre-selected: never from width or place.
    assert [piece.proposed for piece in run.pieces] == [None] * 5
    assert any("two AIs" in check for check in run.checks)


def test_a_printed_filler_word_is_the_only_pre_selection(session: Session) -> None:
    package, revision = _revision(session)
    pieces = list(SAMPLE_RUN)
    pieces[0] = replace(pieces[0], flags=["kind:filler", 'kind-evidence:printed word "Filler"'])
    pieces[1] = replace(pieces[1], flags=["kind:cabinet", 'kind-evidence:cabinet tag "B21"'])
    add_reading(session, package_id=package, revision_id=revision, pieces=pieces)

    run = proposed_runs(session, revision)[0][0]

    assert [piece.proposed for piece in run.pieces] == ["filler", None, None, None, None]
    assert [piece.printed_kind for piece in run.pieces][:2] == ["filler", "cabinet"]


def test_a_sealed_overall_must_equal_the_sum(session: Session) -> None:
    package, revision = _revision(session)
    add_reading(
        session,
        package_id=package,
        revision_id=revision,
        pieces=SAMPLE_RUN,
        overall=Piece('80"', Fraction(80)),
    )

    assert _widths(session, revision) == []
    assert _reasons(session, revision) == [
        'The pieces add up to 78 1/2" but the printed overall reads 80".'
    ]


def test_a_matching_sealed_overall_is_named_as_checked(session: Session) -> None:
    package, revision = _revision(session)
    add_reading(
        session,
        package_id=package,
        revision_id=revision,
        pieces=SAMPLE_RUN,
        overall=Piece('78 1/2"', Fraction(157, 2)),
    )

    run = proposed_runs(session, revision)[0][0]

    assert 'The pieces add up to the printed overall, 78 1/2".' in run.checks


@pytest.mark.parametrize(
    ("spoil", "reason"),
    [
        (lambda p: replace(p, sealed=False), "Piece 3: the two AIs did not read the same inches."),
        (lambda p: replace(p, ink="gv"), "Piece 3: not positively the vendor's own ink."),
        (
            lambda p: replace(p, flags=["reviewer-markup"]),
            "Piece 3: not positively the vendor's own ink.",
        ),
        (
            lambda p: replace(p, text='28"+2"'),
            'Piece 3: its label adds two numbers ("28"+2"").',
        ),
        (lambda p: replace(p, inches=None), "Piece 3: the two AIs did not read the same inches."),
    ],
)
def test_one_spoiled_piece_keeps_the_whole_row_from_being_proposed(
    session: Session, spoil: object, reason: str
) -> None:
    package, revision = _revision(session)
    pieces = list(SAMPLE_RUN)
    pieces[2] = spoil(pieces[2])  # type: ignore[operator]
    add_reading(session, package_id=package, revision_id=revision, pieces=pieces)

    assert _widths(session, revision) == []
    assert _reasons(session, revision) == [reason]


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["row-ambiguous"], "The AIs were not sure this row is the run."),
        (["row-hold:counter-break"], "The AIs did not read this row (counter break)."),
        (
            ["check-hold:stone-into-walls"],
            (
                "The stone runs into the walls here, so this row may be the stone's rather than "
                "the cabinets' between the walls."
            ),
        ),
    ],
)
def test_a_doubtful_or_held_row_is_not_proposed(
    session: Session, flags: list[str], reason: str
) -> None:
    package, revision = _revision(session)
    add_reading(
        session, package_id=package, revision_id=revision, pieces=SAMPLE_RUN, row_flags=flags
    )

    assert _widths(session, revision) == []
    assert _reasons(session, revision) == [reason]


def test_the_architects_row_is_never_a_vendor_run(session: Session) -> None:
    package, revision = _revision(session)
    add_reading(
        session,
        package_id=package,
        revision_id=revision,
        pieces=SAMPLE_RUN,
        kind=DocumentKind.ARCHITECTURAL.value,
    )

    assert _widths(session, revision) == []
    assert _reasons(session, revision) == ["This row is not on the vendor's drawing."]


def test_nothing_read_says_so_with_the_site_width_kept(session: Session) -> None:
    _package, revision = _revision(session)

    proposal = cabinet_proposal(session, revision, site_width=Fraction(75))

    assert proposal.runs == []
    assert proposal.note == NOTHING_READ
    assert proposal.site_width is not None and proposal.site_width.display == '75"'
