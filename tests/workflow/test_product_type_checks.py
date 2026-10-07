"""A check run evaluates only the rules for the product the drawing set is for (#994).

The reviewer chooses the product at upload. This narrows **which** published rules run, and nothing
else: a rule that runs gets exactly the inputs, parameters and arithmetic it got before, so its
outcome cannot change, and in particular nothing that would have run can become a PASS. A package
from before #994 has no product (NULL) and still runs every published rule, one finding each.
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import Package, PackageRevision
from tests.workflow.test_stages import (
    _depth_operands,
    _findings_by_rule,
    _project_depth_parameters,
    _publish_rulebook,
    _revision,
    _upgrade,
)
from vocabulary.semantic_types import ProductType
from workflow.stages import DatabaseStages, _revision_product

pytest_plugins = ("tests.app.postgres_fixture",)

COUNTERTOP_RULES = frozenset(
    {
        "CT-BACK-OFFSET-MIN-001",
        "CT-DEPTH-001",
        "CT-SINK-CABINET-WIDTH-001",
        "CT-SINK-CUTOUT-DEPTH-001",
        "CT-SINK-CUTOUT-WIDTH-001",
        "CT-SINK-OFFSET-FRONT-001",
        "CT-WIDTH-001",
    }
)
CABINET_RULES = frozenset({"CAB-ARCH-VS-SHOP-001", "CAB-FILLER-001"})


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _for(session: Session, product: str | None) -> PackageRevision:
    revision = _revision(session)
    package = session.get(Package, revision.package_id)
    assert package is not None
    package.product_type = product
    session.flush()
    return revision


def test_the_rulebook_ids_this_file_names_are_the_published_ones(session: Session) -> None:
    """Guards the constants below: a renamed or added rule must be classified here, not missed."""
    revision = _for(session, None)
    _publish_rulebook(session)

    DatabaseStages().run_checks(session, revision.id)

    assert set(_findings_by_rule(session, revision.id)) == COUNTERTOP_RULES | CABINET_RULES


def test_a_countertop_set_gets_countertop_findings_only(session: Session) -> None:
    revision = _for(session, "countertop")
    _publish_rulebook(session)

    result = DatabaseStages().run_checks(session, revision.id)

    by_rule = _findings_by_rule(session, revision.id)
    assert set(by_rule) == COUNTERTOP_RULES
    assert not set(by_rule) & CABINET_RULES, "a cabinet check ran on a countertop set"
    assert result["findings"] == len(COUNTERTOP_RULES)
    assert result["product_type"] == "countertop"
    assert result["rules_not_in_scope"] == sorted(CABINET_RULES)


def test_a_cabinet_set_gets_cabinet_findings_only(session: Session) -> None:
    """The per-countertop width block is a countertop rule too, so it stays out of a cabinet set."""
    revision = _for(session, "cabinet")
    _publish_rulebook(session)

    result = DatabaseStages().run_checks(session, revision.id)

    assert set(_findings_by_rule(session, revision.id)) == CABINET_RULES
    assert result["product_type"] == "cabinet"
    assert result["rules_not_in_scope"] == sorted(COUNTERTOP_RULES)


def test_a_set_with_no_product_still_runs_every_published_rule(session: Session) -> None:
    """A package from before #994: exactly the old behaviour, one finding per published rule."""
    revision = _for(session, None)
    published = _publish_rulebook(session)

    result = DatabaseStages().run_checks(session, revision.id)

    assert result["findings"] == published
    assert len(_findings_by_rule(session, revision.id)) == published
    assert result["product_type"] is None
    assert result["rules_not_in_scope"] == []


@pytest.mark.parametrize("shop_depth", [Fraction(51, 2), Fraction(101, 4)], ids=["pass", "fail"])
def test_choosing_the_product_changes_no_outcome_of_a_rule_that_runs(
    session: Session, shop_depth: Fraction
) -> None:
    """**The safety property.** The same inputs, with and without a product: every countertop rule
    reaches the same outcome for the same reason. Choosing a product only removes rules; it can never
    turn a check into a PASS — a FAIL stays a FAIL and an abstention stays an abstention."""
    _publish_rulebook(session)
    outcomes: list[dict[str, tuple[str, str]]] = []
    for product in (None, "countertop"):
        revision = _for(session, product)
        _project_depth_parameters(session, revision)
        DatabaseStages(operands=_depth_operands(shop_depth)).run_checks(session, revision.id)
        outcomes.append(
            {
                rule_id: (str(finding.outcome), str(finding.reason))
                for rule_id, finding in _findings_by_rule(session, revision.id).items()
                if rule_id in COUNTERTOP_RULES
            }
        )

    every_product, countertop_only = outcomes
    assert countertop_only == every_product
    expected = "PASS" if shop_depth == Fraction(51, 2) else "FAIL"
    assert countertop_only["CT-DEPTH-001"][0] == expected


def test_the_readers_are_given_the_product_the_package_states(session: Session) -> None:
    """What the extraction stage tells both readers: the package's own product, or nothing."""
    assert _revision_product(session, _for(session, "countertop").id) is ProductType.COUNTERTOP
    assert _revision_product(session, _for(session, None).id) is None
    assert _revision_product(session, uuid4()) is None
