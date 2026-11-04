"""A drawn size with a note, its line, and the read-number fallback (2026-11-04).

A vendor wrote sizes with an equal-split note (`30"(2EQ)`: this run, split into two equal parts) and
the drawn route dropped every one, so the strings they belong to were never seen; a size printed
between an object's edge and its own dimension line took the edge; a level `2"` written a letter
height above its line took a nearby upright. Synthetic stroke lettering (`stroke_text`) and
hand-made ink; the OCR engines are the tiny `StrokeReader` or scripted readers. No OCR engine
installed, no model, no client value.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import ink_labels, label_origin
from app.review.assistant import read_number as rn
from tests.review.assistant.stroke_text import stroke_ops
from tests.review.assistant.test_dim_labels import HALVES, _dimension, _line, _pdf
from tests.review.assistant.test_dim_labels_drawn import _engines, _ink, _label, _shown

# ---- what a note says ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("note", "kind"),
    [
        ("(EQ)", "span"),
        ("EQ", "span"),
        ("(2EQ)", "span"),
        ("(4 EQ)", "span"),
        ("8EQ", "span"),
        ("EQ. SP.", "span"),
        ("TYP.", "span"),
        ("(REF)", "span"),
        ("CLR", "span"),
        ("O.A.", "span"),
        ("(FIELD TRIM)", "words"),
        ("MIN", "changes"),
        ("M1N", "changes"),  # an engine's 1 for an I
        ("MlN", "changes"),  # and its l
        ("MAX.", "changes"),
        ("±", "changes"),
        ("+/-", "changes"),
        ("(V.I.F.)", "changes"),
        ("HOLD", "changes"),
        ("N.T.S.", "changes"),
        ('TO 42"', "changes"),  # another size: a range
        ('+2" FILLER', "changes"),  # a sum
        ("(2) PLCS", "other"),
        ("(8E)", "other"),
    ],
)
def test_a_note_is_known_by_what_it_does_to_the_number(note: str, kind: str) -> None:
    assert dl._note_kind(note) == kind


class _Scripted:
    """An engine that reads the given lines in turn, whatever the picture."""

    def __init__(self, name: str, lines: Sequence[str]) -> None:
        self.name = name
        self.lines = list(lines)
        self.calls = 0

    def __call__(self, picture: Any, *, single_line: bool) -> list[str]:
        line = self.lines[self.calls % len(self.lines)]
        self.calls += 1
        return [line] if line else []


def _noted() -> dl._Part:
    letters = [
        _shown((((x, 300), (x + 2.4, 302), (x, 304), (x + 2.4, 306)),)) for x in (112.0, 115.6)
    ]
    (part,) = dl._parts(_label(_ink('24"', 100, 300) + letters))
    return part


@pytest.mark.parametrize(
    ("reads", "said"),
    [
        (["(4E@)", "(4EQ)"], ("span", "(4EQ)")),  # a Q read as @ once
        (["(FIELD TRIM)", "(FIELD TRIM)"], ("words", None)),
        (["(2EQ)", "MIN"], ("changes", None)),  # any reading of a bound: never exact
        (["(2) PLCS", "(2) PLCS"], ("unknown", None)),
        (["(8E)", "(8EQ)"], ("unknown", None)),
    ],
)
def test_every_reading_of_a_note_must_agree_on_what_it_does(
    reads: list[str], said: tuple[str, str | None]
) -> None:
    assert dl._note_reading(_noted(), [_Scripted("RapidOCR", reads)]) == said


# ---- a value and its note, one row or two ---------------------------------------------------------


def _note_ink(x: float, y: float) -> list[Any]:
    """Four zigzag letters after a value: a note an engine reads as words."""
    return [
        _shown((((x + 3.6 * n, y), (x + 3.6 * n + 2.4, y + 2), (x + 3.6 * n, y + 4)),))
        for n in range(4)
    ]


def _one(paths: Sequence[Any]) -> ink_labels.Label:
    """Ink gathered as one label (as a cluster gathers a size and the ink beside it): every label
    `ink_labels` splits it into, and a lone stroke as a glyph of its own."""
    xs = [v for path in paths for v in (path.box[0], path.box[2])]
    ys = [v for path in paths for v in (path.box[1], path.box[3])]
    found = ink_labels.labels(paths, (min(xs), min(ys), max(xs), max(ys)))
    glyphs = [g for label in found for g in label.glyphs]
    held = {id(path) for g in glyphs for path in g.paths}
    glyphs += [ink_labels.Glyph((path,), path.box) for path in paths if id(path) not in held]
    return ink_labels.Label(tuple(glyphs), ink_labels._union([g.box for g in glyphs]))


def test_an_overall_stacked_over_a_part_with_its_note_is_two_sizes() -> None:
    # `150"` over `102 3/8"(6EQ)`: the cluster gathers both rows; each is a size of its own.
    label = _one(_ink('97"', 104, 312) + _ink('43{1/8}"', 100, 300) + _note_ink(124, 301))
    assert dl._inch_marked(label)  # read whole before, its note's words lost the overall too
    parts = dl._parts(label)
    assert len(parts) == 2
    assert [bool(part.note) for part in sorted(parts, key=lambda p: p.label.box[1])] == [
        False,
        True,
    ]


def test_a_size_gathered_with_a_stray_mark_is_left_to_the_fallback() -> None:
    # `24"(EQ)` with a centre line's dash under it: not every row is a size, so code's own parts
    # are none, and the size's row goes to the read-number fallback.
    stray = _shown((((110.0, 286.0), (110.0, 289.0)),))
    label = _one([*_ink('24"', 100, 300), *_note_ink(112, 301), stray])
    assert len(ink_labels.rows(label, 0)) == 2
    assert dl._parts(label) == []
    ((row, others),) = dl._value_rows(label)
    assert row.note and len(row.label.glyphs) == 4  # 2, 4 and the inch mark's two ticks
    assert len(others) == 1  # the dash's row, read for a word that would change the size


# ---- the line: the way the text reads, the length the scale says -------------------------------


def _drawn(path: Path, body: bytes) -> list[dict[str, Any]]:
    path.write_bytes(_pdf([body]))
    found = dl.page_labels(path, 1, HALVES, drawn_budget_s=60, readers=_engines())
    return [label for label in found.labels if label["route"] == "drawn"]


def _scale_labels() -> bytes:
    """Three sizes drawn at 4 pt an inch, so the vendor's drawing has a scale."""
    body = b""
    for x, y, inches in ((300, 360, 10), (300, 330, 12), (420, 360, 15)):
        body += _dimension(x, x + 4 * inches, y, reach=12)
        body += stroke_ops(f'{inches}"', x + 2 * inches - 6, y + 2.5)
    return body


def test_the_line_drawn_to_the_value_wins_over_a_nearer_edge(tmp_path: Path) -> None:
    body = _scale_labels()
    # A 20" size (80 pt) printed between an object's edge (nearer, 60" long) and its own line.
    body += _dimension(300, 380, 200, reach=20)
    body += _line(280, 210.5, 520, 210.5) + _line(280, 210.5, 280, 250)
    body += _line(520, 210.5, 520, 250)
    body += stroke_ops('20"', 333, 203)
    (twenty,) = [label for label in _drawn(tmp_path / "edge.pdf", body) if label["text"] == '20"']
    assert twenty["orientation"] == "h"
    assert twenty["span_pt"] == pytest.approx(80, abs=1)


def test_a_level_size_takes_the_level_line_below_it_not_an_upright_beside_it(
    tmp_path: Path,
) -> None:
    body = _scale_labels()
    # A 2" (8 pt) written a letter height above its line, ticked at both ends, and an upright
    # with its own ends just right of the text.
    body += _line(398, 100, 410, 100) + _line(400, 85, 400, 101) + _line(408, 85, 408, 101)
    body += _line(398.5, 98.5, 401.5, 101.5) + _line(406.5, 98.5, 409.5, 101.5)
    body += _line(412.5, 60, 412.5, 130) + _line(405, 130, 420, 130) + _line(405, 60, 420, 60)
    body += stroke_ops('2"', 401, 108)
    (two,) = [label for label in _drawn(tmp_path / "level.pdf", body) if label["text"] == '2"']
    assert two["orientation"] == "h"
    assert two["span_pt"] == pytest.approx(8, abs=0.6)


def test_a_line_stub_at_a_labels_end_is_its_line_not_the_label() -> None:
    # A `3"` written in the gap of its line at a chain's end: the stub of the line left of it
    # (and the line resuming right of it) is line work.
    stub = _shown((((96.0, 299.0), (99.5, 299.0)),))
    label = _one([*_ink('3"', 100, 300), stub])
    lines = dl._line_ends(
        [((96.0, 101.0), (99.5, 101.0)), ((107.5, 101.0), (200.0, 101.0))]
    )  # shown points: the stub and the line after the label
    bare = dl._without_line_work(label, lines)
    assert bare is not None and len(bare.glyphs) == len(label.glyphs) - 1


# ---- the read-number fallback ---------------------------------------------------------------------


def _decided(value: str | None, code: str = "ocr_shape") -> rn.Decision:
    agreed = ("ocr", "shape") if value is not None else ()
    return rn.Decision(None if value is None else Decimal(value), agreed, code, "")


def _rejected(note: bool = False) -> dl._Rejected:
    part = _noted() if note else dl._Part(_label(_ink('24"', 100, 300)))
    line = {
        "dim_line": [[90.0, 297.0], [190.0, 297.0]],
        "extension": [None, None],
        "span_pt": 100.0,
    }
    return dl._Rejected(part, line, "h")


def test_the_fallback_keeps_a_size_two_readers_agree_on_with_its_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rn, "read_ink", lambda label, engines, ruler: (_decided("24"), None, None))
    monkeypatch.setattr(dl, "_note_reading", lambda part, engines: ("span", "(2EQ)"))
    found, held = dl._read_fallback(None, [_rejected(note=True)], [], [], ("set.pdf", 3))  # type: ignore[arg-type]
    ((label, _part),) = found
    assert label["inches"] == "24" and label["route"] == "drawn" and not held
    assert label["origin"] == label_origin.READ_NUMBER and label["read_by"] == ["ocr", "shape"]
    assert label["dim_line"] == [[90.0, 297.0], [190.0, 297.0]] and label["note"] == "(2EQ)"
    assert not label_origin.ocr_read(label)  # it has its own line: the geometry checks use it


def test_the_fallback_never_reads_a_size_whose_note_changes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*_args: Any) -> Any:
        raise AssertionError("a bound is never read as an exact size")

    monkeypatch.setattr(rn, "read_ink", never)
    monkeypatch.setattr(dl, "_note_reading", lambda part, engines: ("changes", None))
    assert dl._read_fallback(None, [_rejected(note=True)], [], [], ("set.pdf", 3)) == ([], [])  # type: ignore[arg-type]


def test_the_fallback_never_reads_a_size_gathered_with_a_word_that_changes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `36"` with `MIN` written under it, gathered as one label: the other row is read first.
    def never(*_args: Any) -> Any:
        raise AssertionError("a bound is never read as an exact size")

    monkeypatch.setattr(rn, "read_ink", never)
    monkeypatch.setattr(
        dl, "_note_reading", lambda part, engines: ("changes", None) if part.note else ("", None)
    )
    rejected = _rejected()
    under = dl._Rejected(rejected.part, rejected.line, "h", others=((object(),),))  # type: ignore[arg-type]
    assert dl._read_fallback(None, [under], [], [], ("set.pdf", 3)) == ([], [])  # type: ignore[arg-type]


def test_one_reader_alone_is_held_for_the_alphabet_with_its_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rn,
        "read_ink",
        lambda label, engines, ruler: (_decided(None, "one_route"), Decimal(24), None),
    )
    found, held = dl._read_fallback(None, [_rejected()], [], [], ("set.pdf", 3))  # type: ignore[arg-type]
    assert found == []
    ((item),) = held
    assert item.ocr == Decimal(24) and item.line is not None and item.orientation == "h"


def _routes(monkeypatch: pytest.MonkeyPatch, ocr: str | None, shape: str | None) -> None:
    monkeypatch.setattr(rn, "_ocr", lambda label, engines: (ocr and Decimal(ocr), ""))
    monkeypatch.setattr(rn, "_shape", lambda label: (shape and Decimal(shape), ""))


def test_read_ink_needs_two_readers_and_a_ruler_that_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _routes(monkeypatch, "24", "24")
    assert rn.read_ink(None, [], 24.1)[0].value == Decimal(24)
    assert rn.read_ink(None, [], None)[0].value == Decimal(24)  # no scale: nothing to block
    # Two readers agree, but the line measures 30": not this line's size.
    assert rn.read_ink(None, [], 30.0)[0].code == "ruler_blocks"
    # One reader and a ruler that agrees: `decide` reads it, the fallback does not.
    _routes(monkeypatch, "24", None)
    assert rn.read_ink(None, [], 24.0)[0].value is None


def test_a_held_spot_with_a_line_is_seconded_only_when_its_ruler_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Alphabet:
        def read(self, ink: Any, turn: int) -> Decimal:
            return Decimal(24)

    def held(ruler: float) -> dl.SweepHeld:
        line = {"dim_line": [[0, 0], [100, 0]], "extension": [None, None], "span_pt": 100.0}
        return dl.SweepHeld("set.pdf", 3, (0, 0, 9, 6), Decimal(24), None, ruler, None, (0,), line)

    seconded = dl.seconded([held(24.0), held(31.0)], _Alphabet())  # type: ignore[arg-type]
    assert [(item.ruler, value) for item, value in seconded] == [(24.0, Decimal(24))]


def test_a_rejected_unknown_note_reaches_the_fallback_and_a_bound_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    part = _noted()
    found = (
        {"dim_line": [[90.0, 297.0], [190.0, 297.0]], "extension": [None, None], "span_pt": 100.0},
        "h",
    )
    monkeypatch.setattr(dl, "_crowded", lambda *args: False)
    monkeypatch.setattr(dl, "_line_of", lambda parsed, part, taken=(): found)
    monkeypatch.setattr(dl, "_drawn_value", lambda reads, ones=None: Decimal(24))
    monkeypatch.setattr(dl, "_label_reads", lambda label, engines, turn=None: [])
    parsed: Any = type("P", (), {"page": None})()
    for kind, kept, sent in (
        ("span", True, False),
        ("unknown", False, True),
        ("changes", False, False),
    ):
        monkeypatch.setattr(dl, "_note_reading", lambda part, engines, k=kind: (k, None))
        rejected: list[dl._Rejected] = []
        label = dl._drawn_label(parsed, part, [], "vendor", rejected)
        assert (label is not None, bool(rejected)) == (kept, sent), kind


def test_a_label_that_loses_its_line_goes_back_to_the_ocr_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before, a drawn label whose line the drawing's scale refused vanished: its ink was neither
    # a label nor left for the OCR route (a real set's overall read by OCR was lost so).
    path = tmp_path / "lost.pdf"
    path.write_bytes(_pdf([_dimension(300, 400, 200, reach=20) + stroke_ops('24"', 343, 203)]))
    monkeypatch.setattr(dl, "_beside_on_scale", lambda found, others, segments: [])
    result = dl.PageLabels()
    missed: list[Any] = []
    with dl.parsed_once():
        parsed = dl._parse(path, 0)
        dl._add_drawn(result, parsed, HALVES, 60, _engines(), dl.monotonic, missed)
    assert result.labels == []
    assert [round(piece.box[0]) for piece in missed] == [343]


def test_the_agent_is_told_a_sizes_note_and_who_read_it() -> None:
    from app.review.assistant import dim_index

    label = {
        "id": "D9",
        "text": '30"',
        "side": "vendor",
        "orientation": "h",
        "route": "drawn",
        "origin": label_origin.READ_NUMBER,
        "note": "(2EQ)",
        "dim_line": [[0, 0], [10, 0]],
    }
    line = dim_index.label_line(label)
    assert "code's number reader" in line and "printed with (2EQ)" in line
