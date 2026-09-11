"""How the chat endpoint projects a stored finding into the facts a model may see.

These are the pure helpers in `app/api/reviewer_chat.py`. They had no tests, which is how the page
convention drifted away from the rest of the product without anything noticing.
"""

from __future__ import annotations

import json

from app.api.reviewer_chat import _evidence_page

DOCUMENT = "11111111-1111-4111-8111-111111111111"


def _reference(**overrides: object) -> str:
    payload: dict[str, object] = {"page": 0, "document_version_id": DOCUMENT}
    payload.update(overrides)
    return json.dumps(payload)


def test_a_page_is_counted_the_way_a_reviewer_counts_sheets() -> None:
    """**Input: stored index 0. Outcome: "1".**

    `pages.index` is zero-based, the way the reader addresses a document. A reviewer counts sheets
    from one, and so does every other surface in the product — `EnterValuesPage` and
    `ConfirmReadingsPage` both render `page_index + 1`.

    This endpoint was the exception, and it showed: asked which sheet had the failure, the AI
    answered *"Sheet 0 has the failure"* — a sheet that exists on no drawing. The number reaching a
    reviewer has to be the number printed on the paper in front of them.
    """
    assert _evidence_page(_reference(page=0)) == "1"
    assert _evidence_page(_reference(page=12)) == "13"


def test_a_reference_that_is_not_structurally_complete_is_not_labelled() -> None:
    """Outcome: `None`, so nothing downstream prints a page it cannot stand behind.

    A partial reference is not a page 1. Returning `None` keeps the evidence line off the finding
    entirely, which is honest; inventing a default would put a reviewer on the wrong sheet.
    """
    assert _evidence_page(json.dumps({"page": 0})) is None
    assert _evidence_page(json.dumps({"document_version_id": DOCUMENT})) is None
    assert _evidence_page(_reference(document_version_id="  ")) is None
    assert _evidence_page("not json") is None
    assert _evidence_page("") is None
    assert _evidence_page(None) is None


def test_a_page_that_is_not_a_whole_count_is_refused() -> None:
    """Outcome: `None` for a negative, a float, or a bool.

    `True` is an `int` in Python and would otherwise label evidence as sheet 2. The bool check is
    why this reads `isinstance(page, bool)` before the integer test rather than after it.
    """
    assert _evidence_page(_reference(page=-1)) is None
    assert _evidence_page(_reference(page=1.5)) is None
    assert _evidence_page(_reference(page=True)) is None
