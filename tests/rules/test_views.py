"""Routing a check to the drawing view its evidence may come from.

Source: `docs/decisions/CALL_2026_08_25_INPUTS.md` N2 — plan carries cut-out position and offsets and
the wall-to-wall dimension, elevation carries cabinet widths and fillers, section carries overhang;
the full mapping is owed by the client and until it lands a check must not be assumed readable from a
view it does not appear in.
Verification for: `rules/views.py`.

The tests that matter are the refusals: an unrouted check and an unclassified page both answer "no".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rules.schema import Rule
from rules.views import (
    CHECK_VIEWS,
    UnroutedCheckError,
    may_read_from,
    require_views,
    unrouted,
    views_for,
)
from vocabulary.page_types import PageType

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _authored_rule_ids() -> frozenset[str]:
    """Every rule in the rulebook, read from the files rather than listed here.

    A literal list would go stale the moment somebody authors a rule, and going stale silently is the
    whole problem this module addresses.
    """
    return frozenset(
        Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).id
        for path in RULEBOOK.glob("*.yaml")
    )


# ---------------------------------------------------------------------------
# The working set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_id",
    [
        "CT-SINK-CUTOUT-WIDTH-001",
        "CT-SINK-CUTOUT-DEPTH-001",
        "CT-SINK-OFFSET-FRONT-001",
        "CT-BACK-OFFSET-MIN-001",
        "CT-WIDTH-001",
    ],
)
def test_cutouts_offsets_and_wall_to_wall_read_from_the_plan(rule_id: str) -> None:
    """Raj's first grouping: cut-out position, front and back offsets, wall-to-wall."""
    assert views_for(rule_id) == frozenset({PageType.PLAN})
    assert may_read_from(rule_id, PageType.PLAN)


@pytest.mark.parametrize("rule_id", ["CAB-FILLER-001", "CAB-ARCH-VS-SHOP-001"])
def test_cabinet_widths_and_fillers_read_from_the_elevation(rule_id: str) -> None:
    """His second grouping. A cabinet width is called out face-on, not in plan."""
    assert views_for(rule_id) == frozenset({PageType.ELEVATION})
    assert may_read_from(rule_id, PageType.ELEVATION)


def test_a_plan_check_refuses_an_elevation_page() -> None:
    """§3.2's own example, as a test: a countertop width found on a cabinet elevation is a plausible
    number attached to the wrong drawing, and no arithmetic downstream catches it."""
    assert not may_read_from("CT-WIDTH-001", PageType.ELEVATION)
    assert not may_read_from("CT-WIDTH-001", PageType.SECTION)
    assert not may_read_from("CT-WIDTH-001", PageType.SCHEDULE)


# ---------------------------------------------------------------------------
# The refusals — the half that keeps the missing list visible
# ---------------------------------------------------------------------------


def test_an_unrouted_check_reads_from_nothing() -> None:
    """**The deliberately awkward direction.**

    A permissive default would let every future check read any page, silently, and nothing
    downstream could tell that routing had never been decided for it. Refusing turns a missing
    client answer into an abstention somebody sees.
    """
    assert views_for("CT-SOME-FUTURE-CHECK-001") is None
    for page_type in PageType:
        assert not may_read_from("CT-SOME-FUTURE-CHECK-001", page_type)


def test_countertop_depth_is_unrouted_rather_than_guessed() -> None:
    """`CT-DEPTH-001` is in none of the three groupings Raj gave.

    Placing it by inference — depth sounds sectional, so try section — would be a routing decision
    made by guesswork that nobody would ever see was made. It stays unrouted until the full list
    arrives, which is a visible gap rather than an invisible assumption.
    """
    assert views_for("CT-DEPTH-001") is None
    assert "CT-DEPTH-001" in unrouted(_authored_rule_ids())


def test_none_and_the_empty_set_mean_different_things() -> None:
    """`None` is "nobody decided"; an empty set would be "decided: no view". The first is a question
    for the client and the second would be a bug, and they must not be spelled the same."""
    assert views_for("CT-DEPTH-001") is None
    assert views_for("CT-DEPTH-001") != frozenset()


def test_an_unclassified_page_is_refused_by_every_check() -> None:
    """**A page `extraction/page_type.py` could not classify must not become a page anything reads.**

    That classifier answers `None` rather than rounding to the nearest plausible type, which is the
    right behaviour — and it would be undone here if an unknown page satisfied every route. The
    classifier's honesty must not be the thing that widens what gets read.
    """
    for rule_id in CHECK_VIEWS:
        assert not may_read_from(rule_id, None)


def test_require_views_names_the_missing_client_answer() -> None:
    """Whoever hits this is looking at an exception and needs to know it is an owed decision rather
    than a broken lookup."""
    with pytest.raises(UnroutedCheckError, match="CALL_2026_08_25_INPUTS N2"):
        require_views("CT-DEPTH-001")

    assert require_views("CT-WIDTH-001") == frozenset({PageType.PLAN})


# ---------------------------------------------------------------------------
# The table against the rulebook
# ---------------------------------------------------------------------------


def test_every_routed_id_is_a_rule_that_exists() -> None:
    """A typo in the table routes nothing and refuses the real rule silently — the mapping would be
    present, spelled wrong, and every run of that check would abstain for a reason nobody could see.
    """
    unknown = sorted(set(CHECK_VIEWS) - _authored_rule_ids())

    assert not unknown, f"routed ids that name no authored rule: {unknown}"


def test_the_outstanding_list_is_reportable_rather_than_discovered_one_abstention_at_a_time() -> (
    None
):
    """`unrouted` exists so the missing half of Raj's list can be printed at startup or in a report.

    Discovering it one NOT_FOUND at a time, during a review, is how a missing decision gets mistaken
    for a broken check.
    """
    outstanding = unrouted(_authored_rule_ids())

    assert outstanding == tuple(sorted(outstanding))
    assert set(outstanding).isdisjoint(CHECK_VIEWS)
