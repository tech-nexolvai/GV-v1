"""A reading belongs to its drawing's one countertop, or to none, and that only ever withholds (#1245).

Verification for `workflow/evidence_operands.py` (`_assembly`, the per-item assembly of a reading)
and `workflow/item_plan.py` (`countertops_by_view`).

**The point** is `test_two_countertops_on_one_drawing_withhold_the_summed_cabinets`: a drawing showing
two countertops (a kitchenette's two runs, say) used to have every labelled cabinet on it summed into
one check. Once a person has confirmed both countertops, a reading on that drawing belongs to neither,
so the check withholds the run and says why, instead of judging one countertop against both runs.

**And it may only withhold.** `test_items_only_ever_withhold_never_add` checks, on the real rulebook,
that no input is given with items that was not given without them, with the very same value; and
`test_the_per_item_assembly_only_refines_the_drawing` checks the property behind it on many made-up
layouts: the assembly only ever splits a drawing's readings apart, never joins two drawings.

Every value here is made up.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    DrawingView,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    PartProposal,
    ViewRole,
)
from app.models.verdicts import Finding
from rules.schema import Rule
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_identifier_pairing import _finding, _rules
from tests.workflow.test_one_drawing_per_check import _drawing, _revision
from tests.workflow.test_reading_parts import REGION, _confirmed
from tests.workflow.test_stages import _publish_rulebook
from verdict.outcomes import Outcome
from vocabulary.part_kinds import PartKind
from workflow.evidence_operands import (
    COUNTERTOPS_SHARE_A_DRAWING,
    EvidenceOperands,
    _assembly,
    _spanning,
    evidence_operands,
    position_sensitive_inputs,
)
from workflow.item_plan import countertops_by_view
from workflow.parts import withdraw_part
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

RULE = "CT-WIDTH-001"
WIDTH_INPUTS = {"countertop_width", "cabinet_widths", "filler_widths"}


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


class _Sheet:
    """One vendor drawing on one page: a 45" countertop over 18" and 20" cabinets with 2" and 3"
    fillers, every reading confirmed by a person (made up). With a 1" field cut at each end the
    widths add up exactly: 2 + 18 + 20 + 3 + 1 + 1 = 45."""

    def __init__(self, session: Session, store: LocalStore) -> None:
        self.session = session
        self.revision: PackageRevision = _revision(session, store, _drawing("synthetic"))
        version = session.scalars(
            select(PackageRevisionDocument).where(
                PackageRevisionDocument.package_revision_id == self.revision.id
            )
        ).one()
        self.page = Page(
            document_version_id=version.document_version_id,
            index=0,
            content_hash="c" * 64,
            width_pt=400,
            height_pt=100,
            rotation=0,
            has_vector_text=True,
            render_failed=False,
        )
        session.add(self.page)
        session.flush()
        self.view = DrawingView(page_id=self.page.id, tag="SYNTHETIC", region=REGION)
        session.add(self.view)
        session.flush()
        confirm_view_role(session, view=self.view, role=ViewRole.SHOP, actor="reviewer")
        for kind, left, right, value in (
            (PartKind.COUNTERTOP, "0.20", "0.80", 45),
            (PartKind.FILLER, "0.20", "0.25", 2),
            (PartKind.CABINET, "0.25", "0.45", 18),
            (PartKind.CABINET, "0.45", "0.75", 20),
            (PartKind.FILLER, "0.75", "0.80", 3),
        ):
            session.add(
                CanonicalObservation(
                    document_version_id=self.page.document_version_id,
                    page_id=self.page.id,
                    document_role="SHOP",
                    coordinate_space="stored",
                    polygon=[[left, "0.30"], [right, "0.30"], [right, "0.31"], [left, "0.31"]],
                    semantic_type=kind.item_type.value,
                    value_numerator=Fraction(value).numerator,
                    value_denominator=Fraction(value).denominator,
                    unit="in",
                    status="HUMAN_CONFIRMED",
                    authority="AUTHORITATIVE",
                )
            )
        session.flush()

    def countertop(self, left: str, right: str) -> tuple[UUID, UUID]:
        """A countertop a person confirmed on this drawing: its suggestion and its item."""
        return _confirmed(self.session, self.view, PartKind.COUNTERTOP, left, right)

    def evidence(self) -> EvidenceOperands:
        return evidence_operands(self.session, self.revision.id, _rules())

    def check(self, store: LocalStore) -> Finding:
        _publish_rulebook(self.session)
        DatabaseStages(store, discriminators={"wall_config": "back_left_right"}).run_checks(
            self.session, self.revision.id
        )
        return _finding(self.session, self.revision, RULE)


def _assert_only_withheld(before: EvidenceOperands, after: EvidenceOperands) -> None:
    """Every input given after was given before, with the very same value; nothing was added."""
    for rule_id, operands in after.operands.items():
        for name, operand in operands.items():
            assert name in before.operands.get(rule_id, {}), (rule_id, name)
            assert operand == before.operands[rule_id][name], (rule_id, name)
    assert after.owned == before.owned
    assert after.missing == before.missing


def test_no_items_keeps_today_s_operands(session: Session, store: LocalStore) -> None:
    """**The control.** No countertop is confirmed: the drawing is the assembly, as before #1245.
    The five readings are given to the width check and it passes, exactly."""
    sheet = _Sheet(session, store)

    evidence = sheet.evidence()

    assert countertops_by_view(session, sheet.revision.id) == {}
    assert WIDTH_INPUTS <= evidence.operands[RULE].keys()
    cabinets = evidence.operands[RULE]["cabinet_widths"].value
    assert isinstance(cabinets, tuple)
    assert [getattr(member, "exact", None) for member in cabinets] == [18, 20]
    assert RULE not in evidence.ambiguous
    assert sheet.check(store).outcome == Outcome.PASS.value


def test_one_countertop_on_the_drawing_keeps_today_s_operands_exactly(
    session: Session, store: LocalStore
) -> None:
    """One confirmed countertop on the drawing: its readings belong to it, and every rule gets
    exactly what it got before the countertop was confirmed."""
    sheet = _Sheet(session, store)
    before = sheet.evidence()

    _proposal, item = sheet.countertop("0.20", "0.80")
    after = sheet.evidence()

    assert countertops_by_view(session, sheet.revision.id) == {sheet.view.id: (item,)}
    assert after == before
    assert sheet.check(store).outcome == Outcome.PASS.value


def test_two_countertops_on_one_drawing_withhold_the_summed_cabinets(
    session: Session, store: LocalStore
) -> None:
    """**The point.** Two confirmed countertops on one drawing: no reading on it says which one it
    belongs to, so the width check is given none of that side's widths, says why, and asks the
    reviewer, never judging one countertop against both runs' cabinets."""
    sheet = _Sheet(session, store)
    before = sheet.evidence()

    sheet.countertop("0.20", "0.50")
    sheet.countertop("0.50", "0.80")
    after = sheet.evidence()
    finding = sheet.check(store)

    assert not WIDTH_INPUTS & set(after.operands.get(RULE, {}))
    assert WIDTH_INPUTS <= set(after.ambiguous[RULE])
    assert set(after.ambiguous[RULE].values()) == {COUNTERTOPS_SHARE_A_DRAWING}
    _assert_only_withheld(before, after)
    assert finding.outcome == Outcome.REVIEW_REQUIRED.value, finding.reason
    assert finding.reason is not None and COUNTERTOPS_SHARE_A_DRAWING in finding.reason


def test_a_withdrawn_countertop_no_longer_counts(session: Session, store: LocalStore) -> None:
    """Only live countertops count: when a person takes one of the two back, the drawing holds one
    again and its readings are given exactly as before."""
    sheet = _Sheet(session, store)
    before = sheet.evidence()
    sheet.countertop("0.20", "0.50")
    proposal, _item = sheet.countertop("0.50", "0.80")

    withdraw_part(session, proposal=session.get_one(PartProposal, proposal), actor="reviewer")

    assert sheet.evidence() == before


def test_items_only_ever_withhold_never_add(session: Session, store: LocalStore) -> None:
    """On the whole rulebook, for no items, one item and two items: an input given with items was
    given without them, with the same value. Items can take an operand away, never give one."""
    sheet = _Sheet(session, store)
    before = sheet.evidence()
    sheet.countertop("0.20", "0.50")
    one = sheet.evidence()
    sheet.countertop("0.50", "0.80")
    two = sheet.evidence()

    _assert_only_withheld(before, one)
    _assert_only_withheld(before, two)
    _assert_only_withheld(one, two)


def _drawing_key(rng: random.Random, views: list[UUID], pages: list[UUID]) -> tuple[str, UUID]:
    choice = rng.random()
    if choice < 0.6:
        return ("view", rng.choice(views))
    if choice < 0.85:
        return ("page", rng.choice(pages))
    return ("unplaced", uuid4())


@pytest.mark.parametrize("seed", range(200))
def test_the_per_item_assembly_only_refines_the_drawing(seed: int) -> None:
    """**The property behind "only withholds".** On many made-up layouts (readings on views with
    0, 1 or 2+ countertops, on pages with no views, and on no one view), two readings that share an
    assembly always shared a drawing, so a side spans at least as many assemblies as it spanned
    drawings, and `_spanning` withholds at least what it withheld before, for every rule."""
    rng = random.Random(seed)
    views = [uuid4() for _ in range(3)]
    pages = [uuid4() for _ in range(2)]
    held = {view: tuple(uuid4() for _ in range(rng.choice((0, 1, 2, 3)))) for view in views}
    readings = [(uuid4(), _drawing_key(rng, views, pages)) for _ in range(rng.randint(0, 6))]

    assemblies = {reading: _assembly(drawing, reading, held) for reading, drawing in readings}
    for first, first_drawing in readings:
        for second, second_drawing in readings:
            if assemblies[first] == assemblies[second]:
                assert first_drawing == second_drawing

    rules: list[Rule] = _rules()
    semantic = {
        (source, kind): [
            (None, reading, drawing) for reading, drawing in readings if rng.random() < 0.7
        ]
        for source, kind in {
            (
                getattr(selector.source, "value", str(selector.source)),
                getattr(selector.semantic_type, "value", str(selector.semantic_type)),
            )
            for rule in rules
            for selector in (rule.inputs or {}).values()
        }
    }
    by_drawing = {need: list(entries) for need, entries in semantic.items()}
    by_assembly = {
        need: [(o, reading, assemblies[reading]) for o, reading, _drawing in entries]
        for need, entries in semantic.items()
    }
    for rule in rules:
        ordered = position_sensitive_inputs(rule)
        before = _spanning(rule, ordered, by_drawing)  # type: ignore[arg-type]
        after = _spanning(rule, ordered, by_assembly)  # type: ignore[arg-type]
        assert set(before) <= set(after), rule.id
