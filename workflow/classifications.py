"""What a reviewer said each item in an ordered run is, and how it reaches a check.

`cabinet_type` is declared in a rule's `inputs:` with `source: USER_INPUT`, exactly as `field_width`
is, and for the same reason: it is not on either drawing as a value this system can read. Raj's deck
is explicit about who supplies it — slide 11 has the reviewer draw a box around a cabinet and
*categorise* it — so nothing here infers one, and a cabinet nobody classified is simply absent.

Reading is the mirror of `workflow/measurements.py`: latest wins, order preserved, and the operand
that comes out is `HUMAN_CONFIRMED` with a reference naming who supplied it.

Source: issue #684. Verification: tests/workflow/test_classifications.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import ItemClassification
from verdict.operands import EvidenceStatus, VerdictOperand

__all__ = ["classification_operands", "confirmed_classifications", "record_classifications"]

#: The same source string reviewer measurements carry, because it is the same act by the same person
#: on the same form. A finding that distinguished "typed a number" from "chose a category" would be
#: inviting a reviewer to trust one more than the other.
SOURCE = "USER_INPUT"


def record_classifications(
    session: Session,
    *,
    package_revision_id: UUID,
    rule_id: str,
    input_name: str,
    categories: Sequence[str],
    confirmed_by: str,
) -> tuple[ItemClassification, ...]:
    """Write one row per item, in the order given.

    The order is the run left to right, and the distribution adjusts positionally, so the index is
    taken from the sequence rather than from anything the caller has to remember to send.
    """
    rows = tuple(
        ItemClassification(
            package_revision_id=package_revision_id,
            rule_id=rule_id,
            input_name=input_name,
            position=position,
            category=category,
            confirmed_by=confirmed_by,
        )
        for position, category in enumerate(categories)
    )
    session.add_all(rows)
    return rows


def confirmed_classifications(
    session: Session, package_revision_id: UUID
) -> dict[str, dict[str, tuple[str, ...]]]:
    """The latest classification for every run, keyed by rule then input name.

    **Latest per position, not per run.** A reviewer who corrects one cabinet writes one row, and
    resolving the whole run to the newest *row* would drop every position they did not touch. Each
    position is resolved on its own and the run is rebuilt from them.

    A run with a gap — positions 0 and 2 confirmed, 1 never answered — is returned with the
    positions that exist and no filler for the one that does not. The operation compares the run's
    length against the cabinets and abstains (#673), which is the honest outcome: a shorter run
    means a cabinet nobody classified, and guessing one is exactly what slide 3 exists to prevent.
    """
    rows = session.execute(
        select(ItemClassification)
        .where(ItemClassification.package_revision_id == package_revision_id)
        .order_by(
            ItemClassification.rule_id,
            ItemClassification.input_name,
            ItemClassification.position,
            ItemClassification.created_at.desc(),
            ItemClassification.id.desc(),
        )
    ).scalars()

    latest: dict[tuple[str, str], dict[int, str]] = {}
    for row in rows:
        latest.setdefault((row.rule_id, row.input_name), {}).setdefault(row.position, row.category)

    confirmed: dict[str, dict[str, tuple[str, ...]]] = {}
    for (rule_id, input_name), by_position in latest.items():
        confirmed.setdefault(rule_id, {})[input_name] = tuple(
            by_position[position] for position in sorted(by_position)
        )
    return confirmed


def classification_operands(
    session: Session, package_revision_id: UUID
) -> dict[str, dict[str, VerdictOperand]]:
    """The confirmed runs as sealed operands, ready to merge with the measurement operands."""
    operands: dict[str, dict[str, VerdictOperand]] = {}
    for rule_id, inputs in confirmed_classifications(session, package_revision_id).items():
        for input_name, categories in inputs.items():
            operands.setdefault(rule_id, {})[input_name] = VerdictOperand(
                name=input_name,
                value=categories,
                status=EvidenceStatus.HUMAN_CONFIRMED,
                source=SOURCE,
                evidence_ref=f"reviewer:{package_revision_id}:{rule_id}:{input_name}",
            )
    return operands
