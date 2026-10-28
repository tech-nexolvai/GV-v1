"""Pages not checked: one "Confirm these N pages" action, and blank pages need no click (#1195).

Anant (2026-10-12, "yes build the confirm-all button"): every page listed under "Pages not checked"
needed its own click before sign-off (#1191). Now:

1. a page code **proved** blank from the PDF (a title or divider page: nothing drawn) is still
   listed, with its proof, but never blocks sign-off; any doubt about the proof keeps the click;
2. one reviewer action confirms the pages the screen named, each recorded exactly like a single
   answer (who, when, the optional note), with one audit event per page plus one for the batch; a
   stale list is refused with 409, a page that is not on the list with 422; unnamed pages wait.

Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    ObservationCandidate,
)
from tests.app.postgres_fixture import alembic_config
from workflow.slot_row_scope import unchosen_page

pytest_plugins = ("tests.app.postgres_fixture",)


# --- helpers --------------------------------------------------------------------------------------


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


# --- blank pages need no click --------------------------------------------------------------------


# --- "Confirm these N pages" ----------------------------------------------------------------------


# --- carried to the next revision like a single answer --------------------------------------------


# --- review fixes (#1195 review) ------------------------------------------------------------------

#: A complete, clean proof under rule 1, as the extraction stage stores it.
PROOF_FLAGS = (
    "no-row:blank-page",
    "blank-proof:rule=1",
    "blank-proof:stamps=0",
    "blank-proof:other-annotations=0",
    "blank-proof:images=0",
    "blank-proof:drawn-paths=0",
    "blank-proof:frames=1",
    "blank-proof:white-paint=1",
    "blank-proof:typed-texts=2",
    "blank-proof:typed-characters=24",
)


def _record(*flags: str, reason: str = "synthetic reason") -> ObservationCandidate:
    """A row-choice record, never stored: enough for the page's own rules."""
    return ObservationCandidate(
        ambiguity_flags=["slot-reader-row-choice", *flags], review_reason=reason, raw_text=""
    )


@pytest.mark.parametrize(
    ("flags", "doubtful"),
    [
        (("no-row:no-row-found",), True),  # a vendor drawing code found no row on
        (("no-row:unreadable",), True),  # a page code could not read
        (("row-choice:0", "row-pick:opus-5-5:0", "row-pick:sonnet-5-5:0"), True),  # older
        (("no-row:no-drawing", "no-row-ink-found"), True),  # no pasted drawing, but ink
        (("no-row:no-vendor-drawing", "no-row-unnamed-drawing"), True),  # an unknown label
        (("no-row:no-drawing",), False),
        (("no-row:no-vendor-drawing",), False),  # only the architect's or a brand prototype
        (("no-row:reference-only",), False),
        (
            (
                "row-choice:0",
                "row-pick:opus-5-5:0",
                "row-pick:sonnet-5-5:0",
                "row-kind:opus-5-5:no_countertop",
                "row-kind:sonnet-5-5:no_countertop",
            ),
            False,
        ),  # both AIs: no countertop on the sheet
    ],
    ids=[
        "no-row-found",
        "unreadable",
        "older-reading",
        "ink-found",
        "unnamed-drawing",
        "no-drawing",
        "no-vendor-drawing",
        "reference-only",
        "ais-no-countertop",
    ],
)
def test_a_page_with_a_reason_to_doubt_starts_unticked_and_still_needs_an_answer(
    flags: tuple[str, ...], doubtful: bool
) -> None:
    """The tick "Confirm these N pages" starts with is `not doubtful` (the screen's rule)."""
    page = unchosen_page(4, _record(*flags))

    assert page.listed
    assert page.doubtful is doubtful
    assert page.needs_answer


@pytest.mark.parametrize(
    "doubt",
    [("no-row-unnamed-drawing",), ("no-row-ink-found",)],
    ids=["unnamed-drawing", "ink-found"],
)
def test_doubt_wins_over_a_blank_proof(doubt: tuple[str, ...]) -> None:
    proven = unchosen_page(4, _record(*PROOF_FLAGS))
    doubted = unchosen_page(4, _record(*PROOF_FLAGS, *doubt))
    older = replace(proven, kind="older")

    assert (proven.needs_answer, proven.doubtful) == (False, False)
    assert (doubted.needs_answer, doubted.doubtful) == (True, True)
    assert (older.needs_answer, older.doubtful) == (True, True)
