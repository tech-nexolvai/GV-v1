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
    assert made["status"] == "different" and made["vendor"] == '4"'
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
    assert made["status"] == "different" and made["vendor"] == '4"'
