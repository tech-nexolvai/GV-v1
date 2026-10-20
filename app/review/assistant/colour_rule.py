"""The colour rule in the words every prompt uses (decided 2026-10-19, Anant: vendors may draw in
any colour; GV's marks are recognised by what they are, never by their colour alone).

Code knows a file's kind (`extraction.gv_marks.gv_combined`, `verify_value.combined_sheet`, the
simple check's `schema.Drawing.sheet`); a prompt that is told the kind names it per drawing
(`simple_check.prompts.kinds_sentence`). A prompt that is not told it uses `COLOUR_RULE`, which
states both kinds, so no prompt says "anything coloured is GV's markup" for every file.
"""

from __future__ import annotations

from typing import Final

__all__ = ["COLOUR_RULE"]

#: Both kinds of file in one sentence, for prompts that are not told a drawing's kind.
COLOUR_RULE: Final = (
    "What coloured ink means depends on the file: on GV's combined review sheets (drawings "
    "pasted as snapshots under GV's headings) coloured notes, clouds and numbers may be GV's "
    "reviewers' markup, never the vendor's or the architect's value; on an ordinary upload every "
    "colour is the drawing's own ink, and only PDF comments and GV Review's own signed marks are "
    "markup."
)
