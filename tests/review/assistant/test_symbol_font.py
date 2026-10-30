"""The symbol font (`symbol_font`): a drawing whose words are stored as private-use characters
(U+F020 to U+F07E) is read as the letters the page shows, wherever code reads a PDF's words (the
page reviewer's words, the set pack's page texts, the value check). Made-up pages only, $0."""

from __future__ import annotations

from pathlib import Path

from app.review.assistant import set_pack, under_markup
from app.review.assistant import verify_value as vv
from app.review.assistant.symbol_font import decoded
from tests.review.assistant.test_verify_value import _pdf, _text, _write


def _symbol(text: str) -> str:
    """`text` as the symbol font stores it."""
    return "".join(chr(0xF000 + ord(ch)) for ch in text)


def test_the_symbol_font_reads_as_the_letters_it_draws() -> None:
    assert decoded(_symbol("STAFF LOUNGE")) == "STAFF LOUNGE"
    assert decoded(_symbol("4'-3\"") + " ABC-101") == "4'-3\" ABC-101"
    # Only U+F020 to U+F07E: any other private-use character, and ordinary text, stay as they are.
    for kept in ("\uf000", "\uf01f", "\uf07f", "\ue000", "STAFF LOUNGE", ""):
        assert decoded(kept) == kept
    # The value check reads through the same decoder.
    assert vv._plain(_symbol("3'-6\"")) == "3'-6\""


def test_a_pages_words_in_the_symbol_font_are_read(tmp_path: Path) -> None:
    """A room name printed in the symbol font on the page and inside a pasted drawing: the page
    reviewer's words and the set pack's page texts read it. The font's own space is no gap
    between words to the PDF reader: the name is one word, as the page shows it."""
    pdf = _write(
        tmp_path,
        _pdf(
            page=_text(40, 300, "STAFF LOUNGE", font=b"/F2") + _text(40, 280, "ABC-101"),
            stamp=_text(20, 100, "BREAK ROOM", font=b"/F2"),
        ),
    )
    words = {w["text"] for w in under_markup.page_words(pdf, 1)}
    assert {"STAFF LOUNGE", "ABC-101", "BREAK ROOM"} <= words
    assert not any(0xF020 <= ord(ch) <= 0xF07E for word in words for ch in word)
    texts, own = set_pack._texts(pdf)
    assert "STAFF LOUNGE" in texts[0] and "BREAK ROOM" in texts[0]
    assert "STAFF LOUNGE" in own[0] and "BREAK ROOM" not in own[0]
