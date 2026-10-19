"""Fillers named only by a leader note (`filler_leaders`): which dimension the note points at.

Made-up geometry and a made-up PDF (no client data). In points of the page as shown (origin top
left): a 16 pt wide filler strip at x 100-116 and a cabinet at 116-260 (both y 100-300), a toe kick
band at y 284-300 starting at the cabinet, the filler's 4" and the cabinet's 36" printed in the
string above, a 40" overall above that, and a 4" toe-kick height at the left. The note
"Filler Left" sits below the drawing with a leader running right and up into the filler, ending
inside the toe-kick's height band (the case a nearest-label rule gets wrong).
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.review.assistant import filler_leaders as fl
from app.review.assistant import filler_range as fr
from app.review.assistant.decision_resolver import settings_in_force

NOTE_BOX = (40.0, 325.0, 80.0, 335.0)
LEADER = ((84.0, 330.0), (108.0, 330.0), (108.0, 295.0))
ARROW = (((108.0, 295.0), (106.0, 299.0)), ((108.0, 295.0), (110.0, 299.0)))
FILLER = (100.0, 100.0, 116.0, 300.0)
CABINET = (116.0, 100.0, 260.0, 300.0)
TOE_KICK = (116.0, 284.0, 400.0, 300.0)
FRAME = (20.0, 20.0, 580.0, 380.0)


def _label(ident: str, text: str, inches: str, orientation: str, line: Any) -> dict[str, Any]:
    (x0, y0), (x1, y1) = line
    return {
        "id": ident,
        "text": text,
        "inches": inches,
        "side": "vendor",
        "route": "text",
        "orientation": orientation,
        "dim_line": [[x0, y0], [x1, y1]],
        "box": [min(x0, x1), min(y0, y1) - 9, max(x0, x1), min(y0, y1) - 1],
    }


LABELS = [
    _label("D1", '4"', "4", "h", ((100.0, 80.0), (116.0, 80.0))),
    _label("D2", '36"', "36", "h", ((116.0, 80.0), (260.0, 80.0))),
    _label("D3", '40"', "40", "h", ((100.0, 60.0), (260.0, 60.0))),
    _label("D4", '4"', "4", "v", ((90.0, 284.0), (90.0, 300.0))),
    _label("D5", '50"', "50", "v", ((85.0, 100.0), (85.0, 300.0))),
]


def _geometry(
    paths: Sequence[Sequence[tuple[float, float]]] = (LEADER, *ARROW),
    rects: Sequence[tuple[float, float, float, float]] = (FILLER, CABINET, TOE_KICK, FRAME),
) -> fl.Geometry:
    return fl.Geometry(tuple(tuple(p) for p in paths), tuple(rects))


def _notes(text: str = "Filler Left") -> list[tuple[str, tuple[float, float, float, float]]]:
    return fl.filler_notes([{"text": text, "box": list(NOTE_BOX)}])


# ---- the notes -----------------------------------------------------------------------------------


def test_a_note_that_says_only_filler_is_a_filler_note() -> None:
    for text in ("Filler Left", "Filler Right", "FILLER (TYP.)", "filler"):
        assert _notes(text), text
    for text in ("Wall Left", '4" FILLER', "FILLER PANEL", "As Per Site", "Filler 2"):
        assert not _notes(text), text


# ---- the link ------------------------------------------------------------------------------------


def test_the_leader_links_the_note_to_the_filler_width_not_the_nearest_label() -> None:
    """The leader ends inside the filler strip, also inside the toe kick's height band: the
    filler's narrow side (its width) is measured by D1, never the toe-kick height D4."""
    assert fl.leader_end(NOTE_BOX, _geometry().paths) == (108.0, 295.0)
    assert fl.object_at((108.0, 295.0), _geometry().rects) == FILLER
    assert fl.link(_notes(), _geometry(), LABELS) == {"D1": "Filler Left"}


def test_a_note_with_no_leader_or_two_leaders_links_nothing() -> None:
    assert fl.link(_notes(), _geometry(paths=()), LABELS) == {}
    other = ((80.0, 326.0), (300.0, 326.0), (300.0, 200.0))
    assert fl.link(_notes(), _geometry(paths=(LEADER, other)), LABELS) == {}


def test_a_leader_that_forks_links_nothing() -> None:
    fork = ((108.0, 330.0), (200.0, 330.0))
    assert fl.link(_notes(), _geometry(paths=(LEADER[:2], LEADER[1:], fork)), LABELS) == {}


def test_a_leader_in_pieces_is_followed_end_to_end() -> None:
    pieces = (LEADER[:2], LEADER[1:], *ARROW)
    assert fl.link(_notes(), _geometry(paths=pieces), LABELS) == {"D1": "Filler Left"}


def test_a_leader_ending_outside_any_object_or_on_an_unmeasured_one_links_nothing() -> None:
    assert fl.link(_notes(), _geometry(rects=(CABINET, TOE_KICK)), LABELS) == {}
    # The filler's width is not dimensioned (only the cabinet and the overall are).
    assert fl.link(_notes(), _geometry(), [la for la in LABELS if la["id"] != "D1"]) == {}


def test_a_label_two_notes_point_at_is_linked_to_neither() -> None:
    second_box = (40.0, 345.0, 80.0, 355.0)
    second = ((84.0, 350.0), (104.0, 350.0), (104.0, 290.0))
    notes = [*_notes(), ("Filler Right", second_box)]
    assert fl.link(notes, _geometry(paths=(LEADER, second)), LABELS) == {}


def test_a_note_that_is_not_a_filler_note_links_nothing() -> None:
    assert fl.link(_notes("Wall Left"), _geometry(), LABELS) == {}


# ---- the filler range rule uses the link ---------------------------------------------------------

RULES_MD = """| Setting | In force | Where it comes from | Used by |
|---|---|---|---|
| Filler width, maximum (`filler_max`) | 2" | the rulebook's default | CAB-FILLER-001 |
| Filler width, minimum (`filler_min`) | 1" | the rulebook's default | CAB-FILLER-001 |
"""
LIMITS = fr.limits_from(settings_in_force(RULES_MD))


def _dims(labels: list[dict[str, Any]]) -> dict[str, Any]:
    return {"files": ["set.pdf"], "pages": [{"file": "set.pdf", "page": 1, "labels": labels}]}


def test_a_linked_dimension_is_judged_as_a_filler_and_others_are_not() -> None:
    linked = {("set.pdf", 1): fl.link(_notes(), _geometry(), LABELS)}
    (made,) = fr.findings_from(_dims(LABELS), None, LIMITS, linked)
    assert made["status"] == "check" and made["vendor"] == '4"'
    assert made["dims"]["vendor"]["id"] == "D1"
    # Without the link, no label says "filler": nothing is judged (never the toe kick's 4").
    assert fr.findings_from(_dims(LABELS), None, LIMITS) == []


# ---- a made-up PDF, read as the set pack reads it ----------------------------------------------------


def _stream(body: bytes) -> bytes:
    data = zlib.compress(body)
    return b"<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _pdf(content: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Resources << /Font << /F1 5 0 R"
            b" >> >> /Contents 4 0 R >>"
        ),
        _stream(content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start,
    )
    return bytes(out)


def _text(x: float, y: float, text: str, size: float = 8) -> bytes:
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET ".encode("latin-1")


def _line(*points: tuple[float, float]) -> bytes:
    path = " ".join(f"{x} {y} {'m' if n == 0 else 'l'}" for n, (x, y) in enumerate(points))
    return f"0 0 0 RG 0.5 w {path} S ".encode()


def _dim(x0: float, x1: float, y: float, text: str) -> bytes:
    """A horizontal dimension in PDF space (y up): its line, extension lines, ticks, its value."""
    body = (
        _line((x0, y), (x1, y))
        + _line((x0, y - 90), (x0, y + 3))
        + _line((x1, y - 90), (x1, y + 3))
    )
    body += _line((x0 - 1.5, y - 1.5), (x0 + 1.5, y + 1.5))
    body += _line((x1 - 1.5, y - 1.5), (x1 + 1.5, y + 1.5))
    return body + _text((x0 + x1) / 2 - 5, y + 2, text)


def _sheet() -> bytes:
    """The made-up elevation in PDF space (y up = 400 - shown y)."""
    body = b"0 0 0 RG 0.5 w 100 100 16 200 re S 116 100 144 200 re S 116 100 284 16 re S "
    body += _dim(100, 116, 320, '4"') + _dim(116, 260, 320, '36"') + _dim(100, 260, 345, '40"')
    body += _text(40, 66, "Filler Left")
    body += _line((76, 70), (108, 70), (108, 105))
    body += _line((108, 105), (106, 101)) + _line((108, 105), (110, 101))
    return _pdf(body)


def test_a_made_up_sheet_links_its_filler_note(tmp_path: Path) -> None:
    from app.review.assistant import dim_labels

    pdf = tmp_path / "set.pdf"
    pdf.write_bytes(_sheet())
    read = dim_labels.page_labels(pdf, 1, [{"box": [0, 0, 600, 400], "role": "vendor"}])
    four = [la for la in read.labels if la.get("text") == '4"']
    assert len(four) == 1 and four[0].get("dim_line"), read.labels
    linked = fl.linked_on_page(pdf, 1, read.labels, read.phrases)
    assert linked == {str(four[0]["id"]): "Filler Left"}
    dims = {
        "pages": [{"file": "set.pdf", "page": 1, "labels": read.labels, "phrases": read.phrases}]
    }
    (made,) = fr.findings_from(dims, None, LIMITS, {("set.pdf", 1): linked})
    assert made["status"] == "check" and made["vendor"] == '4"'


# ---- a CAD arrowhead at the leader's end (a CAD sheet, #1758) ----------------------------------------------

#: A CAD leader often ends in a filled, closed arrowhead: a thin triangle whose base sits on the
#: leader's end (a hair off it) and whose tip points into the object. Its corners are not a
#: rectangle's, and it is a shape, not a piece of the leader.
ARROWHEAD = ((107.1, 295.0), (108.0, 289.7), (108.9, 295.0), (107.1, 295.0), (107.1, 295.0))


def test_a_closed_arrowhead_is_not_a_rectangle() -> None:
    assert fl._closed_box(ARROWHEAD) is None
    square = ((10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0), (10.0, 10.0))
    assert fl._closed_box(square) == (10.0, 10.0, 20.0, 20.0)


def test_a_closed_arrowhead_is_not_followed_as_a_piece_of_the_leader() -> None:
    """The leader ends at the arrowhead's base (within a point of its corner): the arrowhead is
    never taken as the leader's next piece, so the leader's end stays in the filler strip."""
    assert fl.leader_end(NOTE_BOX, (LEADER, ARROWHEAD)) == (108.0, 295.0)


def _arrowhead_sheet() -> bytes:
    """The made-up elevation with its leader ending in a closed, filled CAD arrowhead (PDF space,
    y up): the leader stops at the arrowhead's base, 0.9 pt from its corner."""
    body = b"0 0 0 RG 0.5 w 100 100 16 200 re S 116 100 144 200 re S 116 100 284 16 re S "
    body += _dim(100, 116, 320, '4"') + _dim(116, 260, 320, '36"') + _dim(100, 260, 345, '40"')
    body += _text(40, 66, "Filler Left")
    body += _line((76, 70), (108, 70), (108, 105))
    body += b"0 0 0 rg 107.1 105 m 108 110.3 l 108.9 105 l h f "
    return _pdf(body)


def test_a_leader_ending_in_a_closed_arrowhead_links_the_filler_width(tmp_path: Path) -> None:
    """On a real CAD sheet the arrowhead was taken as the leader's next piece and as the smallest
    rectangle at its end, so nothing was linked; it is ignored, and the strip is found."""
    from app.review.assistant import dim_labels

    pdf = tmp_path / "set.pdf"
    pdf.write_bytes(_arrowhead_sheet())
    geometry = fl.page_geometry(pdf, 1)
    assert not any(r[2] - r[0] < 3 and r[3] - r[1] < 8 for r in geometry.rects), geometry.rects
    read = dim_labels.page_labels(pdf, 1, [{"box": [0, 0, 600, 400], "role": "vendor"}])
    four = [la for la in read.labels if la.get("text") == '4"']
    assert len(four) == 1 and four[0].get("dim_line"), read.labels
    assert fl.linked_on_page(pdf, 1, read.labels, read.phrases) == {
        str(four[0]["id"]): "Filler Left"
    }


# ---- sheets with several elevations, and chains drawn as one line (a CAD sheet, #1758) ------------------


def _tick(x: float, y: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """A slash tick crossing a horizontal dimension line at (x, y)."""
    return ((x - 2.0, y + 2.0), (x + 2.0, y - 2.0))


def test_the_nearest_of_stacked_elevations_strings_is_the_fillers_dimension() -> None:
    """A sheet draws the same run three times, one under the other: each elevation's filler is
    dimensioned at the same x, so three labels run along the strip's width. The one whose line is
    next to the strip (just above it) measures it; the others belong to the elevations below."""
    lower = _label("D6", '4"', "4", "h", ((100.0, 480.0), (116.0, 480.0)))
    lowest = _label("D7", '4"', "4", "h", ((100.0, 880.0), (116.0, 880.0)))
    assert fl.link(_notes(), _geometry(), [*LABELS, lower, lowest]) == {"D1": "Filler Left"}


def test_two_labels_as_near_the_strip_link_nothing() -> None:
    other = _label("D6", '4"', "4", "h", ((100.0, 320.0), (116.0, 320.0)))
    assert fl.link(_notes(), _geometry(), [*LABELS, other]) == {}


def _chain_geometry(ticks: Sequence[float] = (100.0, 116.0, 260.0)) -> fl.Geometry:
    """The string drawn as ONE line from the filler's left edge to the cabinet's right edge,
    cut by slash ticks: the reader of labels could not find the filler's own part of it."""
    chain = ((100.0, 80.0), (260.0, 80.0))
    return _geometry(paths=(LEADER, *ARROW, chain, *(_tick(x, 80.0) for x in ticks)))


def _unlined(label: dict[str, Any], line: Any = None) -> dict[str, Any]:
    return {**label, "dim_line": line}


def test_a_label_with_no_line_found_is_linked_by_the_drawn_ticks_at_the_strips_edges() -> None:
    """The label's own line was not found (or the whole string was taken for it): the drawing
    shows a dimension line at the label's row, cut by ticks exactly at the strip's two edges, with
    the label's middle between them. That is the strip's width."""
    for line in (None, [[100.0, 80.0], [260.0, 80.0]]):
        labels = [_unlined(LABELS[0], line), *LABELS[1:]]
        assert fl.link(_notes(), _chain_geometry(), labels) == {"D1": "Filler Left"}, line


def test_no_tick_at_a_strip_edge_or_a_tick_inside_it_links_nothing() -> None:
    labels = [_unlined(LABELS[0]), *LABELS[1:]]
    assert fl.link(_notes(), _chain_geometry(ticks=(100.0, 260.0)), labels) == {}
    assert fl.link(_notes(), _chain_geometry(ticks=(100.0, 108.0, 116.0, 260.0)), labels) == {}


def test_a_leader_ending_on_a_box_that_is_not_a_strip_links_nothing() -> None:
    """A filler is a strip (long and narrow). A leader whose end lands only in a cabinet-shaped
    box (the strip itself not drawn as a closed rectangle) would take the cabinet's width (a
    56" "filler"): nothing is linked."""
    box = (100.0, 100.0, 260.0, 300.0)
    labels = [_label("D9", '56"', "56", "h", ((100.0, 80.0), (260.0, 80.0)))]
    assert fl.link(_notes(), _geometry(rects=(box, FRAME)), labels) == {}
