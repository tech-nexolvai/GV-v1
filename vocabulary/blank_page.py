"""The blank-page rule's shared numbers (#1195), kept apart from the PDF reader that applies them.

`extraction/blank_page.py` proves a page has nothing drawn on it and needs `pikepdf`; the API reads
stored proofs back (`workflow/slot_row_scope.blank_proof`) and must not import the PDF reader
(backend §4.1, `tests/api/test_no_heavy_work.py`). Both take the rule from here.
"""

from __future__ import annotations

from typing import Final

__all__ = ["BLANK_PAGE_RULE", "MAX_TITLE_CHARACTERS"]

#: The rule's version, stored with every proof so a later change of rule can tell old proofs apart.
BLANK_PAGE_RULE: Final = 1

#: The most typed characters a title or divider page holds: a title, not a page of notes.
MAX_TITLE_CHARACTERS: Final = 200
