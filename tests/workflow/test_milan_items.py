"""Each live countertop's architect comparison, looked up by the countertop (#1273, Milan by item).

Verification for `workflow/milan_items.py`: a countertop says what the countertop results show for
its row (review of #1279). A recorded CT-ARCH-WIDTH-001 result is said as recorded: compared, with
each vendor piece named by the piece item a person confirmed, or for the reviewer. With no result,
"not checked yet", or "not compared" with the reason its row's stored pairing gives. It reads only,
asks a fixed number of statements for any number of countertops, and no check imports it.

The countertops and pieces are made the way a reviewer makes them: Bala suggests them from the
countertop row the slot reader read (or, on the architect check's fixtures, a suggestion linked to
the row as Bala links his), and a person confirms each with one click. Every value is made up.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from app.api import visual_countertops
from app.db.session import session_factory
from app.models import (
    ArchitectPairingRecord,
    CheckRun,
    DrawingItem,
    DrawingView,
    Finding,
    ObservationCandidate,
    Package,
    PackageRevision,
    PartConfirmation,
    PartProposal,
    PartProposalRow,
)
from tests.workflow.test_architect_row_evidence import (
    ARCH_RULE,
    _architect_drawing,
    _architect_value,
    _overall,
    _pairing,
    _piece,
    _run,
    _sealed_rows,
)
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_item_plan import _confirm_bala_countertop, _unlinked_countertop
from tests.workflow.test_part_proposals_route import _upgrade
from vocabulary.part_kinds import PartKind
from workflow.architect_pairing_contract import EffectivePair, EffectivePairing, PairingSource
from workflow.bala_items import propose_items
from workflow.milan_items import (
    NO_ROW_FOR_ARCHITECT,
    NOT_CHECKED_YET,
    architect_status_for_item,
    architect_statuses_for_revision,
)
from workflow.parts import confirm_part, record_part_proposal

pytest_plugins = ("tests.app.postgres_fixture",)

PERSON = "reviewer@example.com"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _confirm_bala_piece(session: Session, anchor: UUID, slot: int, kind: PartKind) -> UUID:
    """A person confirms Bala's suggestion for piece `slot` of the row anchored at `anchor`."""
    proposal = session.scalars(
        select(PartProposal)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .where(PartProposalRow.row_candidate_id == anchor, PartProposalRow.slot_index == slot)
    ).one()
    confirmation = confirm_part(session, proposal=proposal, kind=kind, code=None, actor=PERSON)
    assert confirmation.drawing_item_id is not None
    return confirmation.drawing_item_id


def _nothing_comparable(session: Session, built: _Revision, anchor: UUID, reason: str) -> None:
    """The row's stored pairing: the architect prints nothing comparable, and why."""
    row = session.get_one(ObservationCandidate, anchor)
    session.add(
        ArchitectPairingRecord(
            package_revision_id=built.revision.id,
            page_id=row.page_id,
            row_anchor_candidate_id=anchor,
            extraction_run_id=row.extraction_run_id,
            source="none",
            status="nothing_comparable",
            pairs=[],
            details={"reasons": [reason]},
        )
    )
    session.flush()


def _one_countertop(session: Session, *, pieces: int = 3) -> tuple[_Revision, UUID, UUID]:
    built = _Revision(session)
    anchor = built.row(built.run(), page=0, pieces=pieces)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, anchor)
    return built, anchor, item


def test_a_countertop_whose_row_has_nothing_comparable_says_so_with_the_stored_reason(
    session: Session,
) -> None:
    built, anchor, item = _one_countertop(session)
    _nothing_comparable(session, built, anchor, "The architect prints only blocking on this page")

    (status,) = architect_statuses_for_revision(session, built.revision.id)

    assert status.countertop.item_id == item
    assert status.state == "not_compared"
    assert status.reason == "The architect prints only blocking on this page."
    assert status.pairs == ()
    assert status.pairing_source == "none"
    assert architect_status_for_item(session, item) == status


def test_a_countertop_whose_row_was_never_paired_says_nothing_is_paired(session: Session) -> None:
    _built, _anchor, item = _one_countertop(session)

    status = architect_status_for_item(session, item)

    assert status is not None
    assert (status.state, status.reason) == (
        "not_compared",
        "No architect dimension is paired with this row.",
    )
    assert status.pairing_source is None


def test_a_countertop_with_no_row_says_so(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    item = _unlinked_countertop(session, built, page=1)

    (status,) = architect_statuses_for_revision(session, built.revision.id)

    assert status.countertop.item_id == item
    assert (status.state, status.reason, status.pairs) == ("not_compared", NO_ROW_FOR_ARCHITECT, ())


def _paired(*pairs: EffectivePair, source: PairingSource = "reviewer") -> EffectivePairing:
    return EffectivePairing(
        record_id=uuid4(),
        source=source,
        status="reviewer" if source == "reviewer" else "paired",
        pairs=pairs,
        reasons=(),
    )


def test_a_pairing_with_no_check_run_yet_says_not_checked_yet_as_the_results_do(
    session: Session,
) -> None:
    """Review of #1279: a reviewer's pairing on the overall and no check run. The results say "Not
    checked yet", so the countertop does too; it never says "compared" before a check compared."""
    _built, _anchor, item = _one_countertop(session)
    pairing = _paired(EffectivePair("overall", uuid4(), ()))

    status = architect_status_for_item(session, item, pairing_lookup=lambda _s, _a: pairing)

    assert status is not None
    assert (status.state, status.reason, status.pairs) == ("not_checked_yet", NOT_CHECKED_YET, ())
    assert (status.finding_id, status.outcome, status.pairing_source) == (None, None, "reviewer")
    assert NOT_CHECKED_YET == visual_countertops.NOT_CHECKED_YET


def test_a_disagreement_before_any_check_run_says_not_checked_yet(session: Session) -> None:
    _built, _anchor, item = _one_countertop(session)
    pairing = EffectivePairing(
        record_id=uuid4(), source="none", status="ais-disagree", pairs=(), reasons=()
    )

    status = architect_status_for_item(session, item, pairing_lookup=lambda _s, _a: pairing)

    assert status is not None
    assert (status.state, status.reason, status.pairs) == ("not_checked_yet", NOT_CHECKED_YET, ())


# --- a recorded CT-ARCH-WIDTH-001 result (the architect check's own fixtures) ---------------------


def _countertop_on_row(session: Session, anchor: UUID, *, slot: int | None = None) -> UUID:
    """A person confirms a suggestion linked to the row (and to piece `slot`), as Bala links his:
    a countertop, or a cabinet for a piece. On the row's page's drawing; every value made up."""
    candidate = session.get_one(ObservationCandidate, anchor)
    view = session.scalars(
        select(DrawingView).where(DrawingView.page_id == candidate.page_id).order_by(DrawingView.id)
    ).first()
    assert view is not None
    left = Decimal("0.10") + Decimal(slot or 0) / 10
    box = [
        (left, Decimal("0.20")),
        (left + Decimal("0.05"), Decimal("0.20")),
        (left + Decimal("0.05"), Decimal("0.25")),
        (left, Decimal("0.25")),
    ]
    kind = PartKind.COUNTERTOP if slot is None else PartKind.CABINET
    proposal = record_part_proposal(
        session,
        drawing_view_id=view.id,
        kind=kind,
        extent=box,
        defining_line=(box[0], box[1]),
        code_as_printed=None,
        code_candidate_id=None,
        reason="a synthetic suggestion linked to the row",
        source="test",
        source_version="1",
    )
    session.add(
        PartProposalRow(
            part_proposal_id=proposal.id,
            row_candidate_id=anchor,
            slot_index=slot,
            extraction_run_id=candidate.extraction_run_id,
            kind_named=True,
        )
    )
    session.flush()
    confirmation = confirm_part(session, proposal=proposal, kind=kind, code=None, actor=PERSON)
    assert confirmation.drawing_item_id is not None
    return confirmation.drawing_item_id


def _revision_of(session: Session, package_id: UUID) -> UUID:
    return session.query(PackageRevision).filter_by(package_id=package_id).one().id


def test_a_recorded_pass_or_fail_is_compared_with_the_pairs_the_check_compared(
    session: Session, tmp_path: Path
) -> None:
    """Pieces 1 and 2 compared, one each against its own architect span: FAIL recorded. Piece 1's
    pair names the cabinet confirmed for that place; piece 2's names none (nobody confirmed it)."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    first = _architect_value(session, run, anchors[0], "1' - 8\"", slot=0)
    second = _architect_value(
        session, run, anchors[0], "1' - 10\"", slot=1, box=(420, 200, 520, 230)
    )
    recorded = _run(
        session,
        package_id,
        tmp_path,
        {anchors[0]: _pairing(_piece(first, 0), _piece(second, 1))},
    )[ARCH_RULE][anchors[0]]
    item = _countertop_on_row(session, anchors[0])
    cabinet = _countertop_on_row(session, anchors[0], slot=0)

    status = architect_status_for_item(session, item)

    assert status is not None
    assert (status.state, status.outcome, status.finding_id) == ("compared", "FAIL", recorded.id)
    assert status.reason == recorded.reason
    assert [(p.label, p.vendor_slot, p.piece_item_id, p.piece_item_type) for p in status.pairs] == [
        ("piece 1", 0, cabinet, "cabinet_width"),
        ("piece 2", 1, None, None),
    ]
    assert (status.pairing_source, status.pairing_judgments) == ("code+ais", "code and both AIs")


@pytest.mark.parametrize("source", ["code", "both-ais"])
def test_a_one_judgment_pairing_is_for_the_reviewer_never_compared(
    session: Session, tmp_path: Path, source: PairingSource
) -> None:
    """The check recorded REVIEW_REQUIRED (one judgment): the countertop says so, with the check's
    reason, though the pairing alone would plan a comparison."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    recorded = _run(
        session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall), source=source)}
    )[ARCH_RULE][anchors[0]]
    item = _countertop_on_row(session, anchors[0])

    status = architect_status_for_item(session, item)

    assert status is not None
    assert (status.state, status.outcome) == ("for_reviewer", "REVIEW_REQUIRED")
    assert status.reason == recorded.reason
    assert [pair.label for pair in status.pairs] == ["overall"]


def test_an_architect_value_the_check_may_not_use_is_for_the_reviewer(
    session: Session, tmp_path: Path
) -> None:
    """The plan says compare, but the operands are not eligible (millimetres): the check recorded
    REVIEW_REQUIRED with no operand, and the countertop says so, comparing nothing."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"", unit="mm")
    pairing = _pairing(_overall(overall))
    recorded = _run(session, package_id, tmp_path, {anchors[0]: pairing})[ARCH_RULE][anchors[0]]
    item = _countertop_on_row(session, anchors[0])

    status = architect_status_for_item(session, item, pairing_lookup=lambda _s, _a: pairing)

    assert status is not None
    assert (status.state, status.outcome, status.pairs) == ("for_reviewer", "REVIEW_REQUIRED", ())
    assert status.reason == recorded.reason and "inches" in (status.reason or "")


def test_a_pairing_changed_after_the_run_shows_what_the_run_recorded(
    session: Session, tmp_path: Path
) -> None:
    """The row's pairing now says nothing is comparable, but the last run compared it: the results
    show the recorded FAIL until the checks run again, and so does the countertop."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 6\"")
    recorded = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})[
        ARCH_RULE
    ][anchors[0]]
    item = _countertop_on_row(session, anchors[0])
    now = EffectivePairing(
        record_id=uuid4(), source="none", status="nothing_comparable", pairs=(), reasons=()
    )

    status = architect_status_for_item(session, item, pairing_lookup=lambda _s, _a: now)

    assert status is not None
    assert (status.state, status.outcome, status.finding_id) == ("compared", "FAIL", recorded.id)
    assert [pair.label for pair in status.pairs] == ["overall"]


def test_a_package_whose_checks_include_no_countertop_lists_none(session: Session) -> None:
    """A cabinet package (#1205): the countertop results list no countertop, so neither does this."""
    built, _anchor, item = _one_countertop(session)
    session.get_one(Package, built.revision.package_id).product_type = "cabinet"
    session.flush()

    assert architect_statuses_for_revision(session, built.revision.id) == ()
    assert architect_status_for_item(session, item) is None


def test_not_a_live_countertop_has_no_status(session: Session) -> None:
    _built, anchor, _item = _one_countertop(session)
    piece = _confirm_bala_piece(session, anchor, 0, PartKind.CABINET)

    assert architect_status_for_item(session, piece) is None
    assert architect_status_for_item(session, uuid4()) is None


def _counts(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (Finding, CheckRun, ArchitectPairingRecord, PartConfirmation, DrawingItem)
    )


def test_it_reads_only(session: Session) -> None:
    built, anchor, _item = _one_countertop(session)
    _nothing_comparable(session, built, anchor, "Nothing comparable")
    session.commit()
    before = _counts(session)

    architect_statuses_for_revision(session, built.revision.id)

    assert not session.new and not session.dirty and not session.deleted
    assert _counts(session) == before


def _statements(session: Session, pages: int) -> int:
    built = _Revision(session, pages=pages)
    run = built.run()
    anchors = [built.row(run, page=page) for page in range(pages)]
    propose_items(session, built.revision.id)
    for anchor in anchors:
        _confirm_bala_countertop(session, anchor)
        _confirm_bala_piece(session, anchor, 0, PartKind.CABINET)
        _nothing_comparable(session, built, anchor, "Nothing comparable")
    session.commit()
    session.expire_all()
    seen: list[object] = []

    def count(*_args: object) -> None:
        seen.append(None)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        statuses = architect_statuses_for_revision(session, built.revision.id)
    finally:
        event.remove(bind, "before_cursor_execute", count)
    assert [status.state for status in statuses] == ["not_compared"] * pages
    return len(seen)


def test_the_statement_count_does_not_grow_with_the_countertops(session: Session) -> None:
    assert _statements(session, 1) == _statements(session, 4)


def _count_statements(session: Session, revision: UUID) -> tuple[int, list[str]]:
    session.commit()
    session.expire_all()
    seen: list[object] = []

    def count(*_args: object) -> None:
        seen.append(None)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        statuses = architect_statuses_for_revision(session, revision)
    finally:
        event.remove(bind, "before_cursor_execute", count)
    return len(seen), [status.state for status in statuses]


def test_the_statement_count_does_not_grow_on_the_compared_path(
    session: Session, tmp_path: Path
) -> None:
    """Both rows have a recorded comparison; one countertop, then two, each with a piece."""
    package_id, anchors = _sealed_rows(session)
    pairings = {}
    for anchor in (anchors[0], anchors[1]):
        run = _architect_drawing(session, anchor)
        pairings[anchor] = _pairing(_overall(_architect_value(session, run, anchor, "3' - 7\"")))
    _run(session, package_id, tmp_path, pairings)
    revision = _revision_of(session, package_id)
    _countertop_on_row(session, anchors[0])
    _countertop_on_row(session, anchors[0], slot=0)
    one = _count_statements(session, revision)
    _countertop_on_row(session, anchors[1])
    _countertop_on_row(session, anchors[1], slot=0)
    two = _count_statements(session, revision)

    assert one[1] == ["compared"] and two[1] == ["compared", "compared"]
    assert one[0] == two[0]


#: The only module outside the tests that may import the lookup: the read endpoint.
_ALLOWED_IMPORTERS = {"app/api/item_architect.py"}
_TARGET = "workflow.milan_items"


def _imports_the_lookup(relative: str, source: str) -> bool:
    """Whether the module at `relative` (a repo path) imports `workflow.milan_items` in any form:
    `import workflow.milan_items`, `from workflow.milan_items import …`, `from workflow import
    milan_items`, a relative `from .milan_items import …` / `from . import milan_items`, or the
    dotted name as a string (`importlib.import_module`)."""
    package = relative.removesuffix(".py").split("/")[:-1]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(
                alias.name == _TARGET or alias.name.startswith(_TARGET + ".")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            if module == _TARGET or module.startswith(_TARGET + "."):
                return True
            if any(f"{module}.{alias.name}" == _TARGET for alias in node.names):
                return True
        elif isinstance(node, ast.Constant) and node.value == _TARGET:
            return True
    return False


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("app/x.py", "import workflow.milan_items"),
        ("app/x.py", "import workflow.milan_items as m"),
        ("app/x.py", "from workflow.milan_items import architect_statuses_for_revision"),
        ("app/x.py", "from workflow import milan_items"),
        ("app/x.py", "from workflow import parts, milan_items as m"),
        ("workflow/x.py", "from .milan_items import architect_statuses_for_revision"),
        ("workflow/x.py", "from . import milan_items"),
        ("workflow/sub/x.py", "from ..milan_items import ItemPair"),
        ("app/x.py", "import importlib\nimportlib.import_module('workflow.milan_items')"),
    ],
)
def test_the_import_guard_sees_every_form_of_import(relative: str, source: str) -> None:
    assert _imports_the_lookup(relative, source)


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("app/x.py", "from workflow import parts"),
        ("app/x.py", "from .milan_items import x"),
        ("workflow/x.py", "from . import parts"),
    ],
)
def test_the_import_guard_ignores_other_imports(relative: str, source: str) -> None:
    assert not _imports_the_lookup(relative, source)


def test_no_check_rule_or_evidence_step_reads_it() -> None:
    """Read-only by construction: nothing but the read endpoint imports `workflow.milan_items`, so
    no check, rule, evidence step or stage can take an operand from it (#1273)."""
    importers: set[str] = set()
    for path in REPO.rglob("*.py"):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith(("tests/", ".venv/", "frontend/", "node_modules/")):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "milan_items" in source and _imports_the_lookup(relative, source):
            importers.add(relative)
    assert importers == _ALLOWED_IMPORTERS
