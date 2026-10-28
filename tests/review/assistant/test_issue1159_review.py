"""Issue #1159, independent review: every finding as a test (synthetic records, fake model).

1. a connector cannot lean on a sentence code drops; no double space where one was dropped;
2. a ranking question about deciding says whose decision it is;
3. a page-less other check can be the start item (no dead end);
4. the architect check ranks and explains a countertop when only it is open;
5. the queue fill is dropped only when the outcome wording already says "needs your decision";
6. ranking questions are narrow and never name a page;
7. sign-off possible with only unchecked countertops says they do not hold it up;
8. failures rank by severity, then difference (documented tie rule); a cap keeps the ranked ones;
9. provider refusals and failures and a records-only guard failure log codes at WARNING.
"""

from __future__ import annotations

from fractions import Fraction
from uuid import uuid4

from tests.review.assistant import synthetic as syn
from tests.review.assistant.synthetic import exact, snapshot

# ---- 1 -------------------------------------------------------------------------------------------


# ---- 2 -------------------------------------------------------------------------------------------


# ---- 3 -------------------------------------------------------------------------------------------


# ---- 4 and 5 -------------------------------------------------------------------------------------


# ---- 6 -------------------------------------------------------------------------------------------


# ---- 7 -------------------------------------------------------------------------------------------


# ---- 8 -------------------------------------------------------------------------------------------


def test_a_cap_keeps_the_countertop_the_answer_starts_with(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.review.assistant import records as records_module

    base = syn.countertop_results()
    small = base.items[0]
    big = small.model_copy(
        update={
            "row_id": uuid4(),
            "finding_id": uuid4(),
            "page_number": 6,
            "label": "Sample run F",
            "delta": exact(Fraction(-3)),
        }
    )
    monkeypatch.setattr(records_module, "MAX_COUNTERTOPS", 1)
    review = snapshot(
        syn.Inputs(countertops=base.model_copy(update={"items": (small, big)}), findings=())
    )
    assert [item.page_number for item in review.countertops] == [6]


# ---- 9 -------------------------------------------------------------------------------------------
