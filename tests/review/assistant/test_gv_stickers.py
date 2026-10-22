"""GV's stickers baked into a pasted drawing (#1859): found, hidden, and never the vendor's value.

Made-up drawings and values only: a pasted vendor drawing prints `610 [24"]` in black, and GV's
yellow sticker over it says `25-1/2"` in red.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pikepdf
import pytest

from app.review.assistant import classification, escalation, gv_stickers, proof
from app.review.assistant.chat_trust import judged

_FONT = pikepdf.Dictionary(
    Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
)


def _form(pdf: pikepdf.Pdf, content: bytes, bbox: list[float], font: bool = True) -> Any:
    resources = (
        pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=_FONT)) if font else pikepdf.Dictionary()
    )
    return pikepdf.Stream(
        pdf,
        content,
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=pikepdf.Array(bbox),
        Resources=resources,
    )


#: GV's sticker: a yellow box, a red border, red text.
STICKER = (
    b'q 1 1 0.5 rg 1 0 0 RG 100 100 60 20 re B 1 0 0 rg BT /F1 8 Tf 105 106 Td (25-1/2") Tj ET Q'
)
#: The vendor's own note, in black.
VENDOR_NOTE = b'q 0 g BT /F1 8 Tf 105 206 Td (610 [24"]) Tj ET Q'
#: GV's green tick: colour, no text (not a note).
TICK = b"q 0 0.5 0 rg 300 300 m 310 290 l 330 320 l f Q"


def _drawing_pdf(extra: dict[str, bytes] | None = None) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(1000, 800))
    forms = {"S1": STICKER, "V1": VENDOR_NOTE, "T1": TICK, **(extra or {})}
    xobjects = pikepdf.Dictionary(
        {f"/{name}": _form(pdf, body, [0, 0, 1000, 800]) for name, body in forms.items()}
    )
    # The pasted drawing: its own black line work, then its sub-drawings.
    content = b"q 0 G 10 10 m 900 10 l S Q " + b" ".join(f"/{name} Do".encode() for name in forms)
    appearance = pikepdf.Stream(
        pdf,
        content,
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=pikepdf.Array([0, 0, 1000, 800]),
        Resources=pikepdf.Dictionary(XObject=xobjects),
    )
    # Each sub-drawing's own box is small (a sticker is a small part of its drawing).
    for name, box in (
        ("S1", [98, 98, 162, 122]),
        ("V1", [100, 200, 160, 215]),
        ("T1", [298, 288, 332, 322]),
    ):
        appearance.Resources.XObject[f"/{name}"].BBox = pikepdf.Array(box)
    stamp = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name.Stamp,
        Rect=pikepdf.Array([0, 0, 1000, 800]),
        AP=pikepdf.Dictionary(N=appearance),
    )
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(stamp)]))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_finds_gvs_sticker_and_not_the_vendors_ink_or_a_tick() -> None:
    with pikepdf.open(io.BytesIO(_drawing_pdf())) as pdf:
        found = gv_stickers.find(pdf)
    assert [(s.page, s.text, s.kind) for s in found] == [(1, '25-1/2"', "note")]
    x0, y0, x1, y1 = found[0].box
    # Page as shown, origin top-left: the form box 98..162 x 98..122 (y up) on an 800-high page.
    assert (round(x0), round(y0), round(x1), round(y1)) == (98, 678, 162, 702)


def test_a_note_with_any_black_ink_is_the_vendors() -> None:
    mixed = b"q 1 1 0.5 rg 100 100 60 20 re f 0 g BT /F1 8 Tf 105 106 Td (AB-012) Tj ET Q"
    with pikepdf.open(io.BytesIO(_drawing_pdf({"S1": mixed}))) as pdf:
        assert gv_stickers.find(pdf) == []


def test_a_big_coloured_part_is_never_a_sticker() -> None:
    pdf_bytes = _drawing_pdf()
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        form = pdf.pages[0].obj.Annots[0].AP.N.Resources.XObject.S1
        form.BBox = pikepdf.Array([0, 0, 1000, 800])  # the whole drawing
        assert gv_stickers.find(pdf) == []


def test_hidden_copy_empties_the_sticker_only(tmp_path: Path) -> None:
    source = tmp_path / "set.pdf"
    source.write_bytes(_drawing_pdf())
    before = source.read_bytes()
    copy, found = gv_stickers.hidden_copy(source)
    assert [s.text for s in found] == ['25-1/2"']
    with pikepdf.open(copy) as pdf:
        xobjects = pdf.pages[0].obj.Annots[0].AP.N.Resources.XObject
        assert xobjects.S1.read_bytes() == b""
        assert b"610" in xobjects.V1.read_bytes()  # the vendor's own value stays
        assert xobjects.T1.read_bytes() != b""
    assert source.read_bytes() == before  # the upload is never changed


def test_the_pack_file_round_trip(tmp_path: Path) -> None:
    sticker = gv_stickers.Sticker(3, (10.0, 20.0, 40.0, 30.0), '25-1/2"', "note")
    gv_stickers.write_file(tmp_path / "A_stickers.json", "A.pdf", [sticker])
    read = gv_stickers.read_folder(tmp_path)
    assert read is not None
    assert read[("A.pdf", 3)][0]["text"] == '25-1/2"'
    assert gv_stickers.read_folder(tmp_path / "missing") is None
    assert gv_stickers.sticker_at(read[("A.pdf", 3)], (12, 22, 30, 28)) is not None
    assert gv_stickers.sticker_at(read[("A.pdf", 3)], (100, 100, 110, 110)) is None


# ---- the escalation's sticker step and the classification ---------------------------------------


def _finding(**more: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "F1",
        "page": 3,
        "item": "Run: left cabinet width",
        "vendor": '25-1/2"',
        "architect": '24"',
        "status": "different",
        "source": "architect",
        "category": "cabinet",
        "vendor_box": {"page": 3, "x0": 12, "y0": 21, "x1": 38, "y1": 29, "file": "A.pdf"},
        "checks": {"printed_vendor": "no_text", "printed_architect": "match"},
        "inches": {"vendor": "25.5", "architect": "24"},
    }
    base.update(more)
    return base


def _ctx(labels: list[dict[str, Any]] | None = None, **more: Any) -> escalation.EscalationContext:
    stickers = {
        ("A.pdf", 3): [{"page": 3, "box": [10, 20, 40, 30], "text": '25-1/2"', "file": "A.pdf"}]
    }
    return escalation.EscalationContext(
        main_file="A.pdf", labels={("A.pdf", 3): labels or []}, stickers=stickers, **more
    )


def test_a_vendor_value_inside_gvs_sticker_is_refused_with_the_vendors_own_value() -> None:
    label = {
        "id": "D4",
        "text": '610 [24"]',
        "inches": "24",
        "side": "vendor",
        "box": [14, 22, 36, 28],
    }
    result = escalation.escalate([_finding()], _ctx([label]))
    finding = result.changed[0]
    assert finding["gv_sticker"]["under"] == '610 [24"]'
    assert proof.proof_group(finding) == "not_compared"
    found = proof.classified(finding)
    assert found.reason == "gv_sticker"
    assert found.why is not None and "under GV's sticker the vendor prints 610 [24\"]" in found.why
    assert not proof.code_proved(finding)


def test_a_sticker_value_is_never_right_either() -> None:
    same = _finding(
        architect='25-1/2"', status="same", inches={"vendor": "25.5", "architect": "25.5"}
    )
    finding = escalation.escalate([same], _ctx()).changed[0]
    assert proof.proof_group(judged(finding)) == "not_compared"
    assert proof.classified(finding).reason == "gv_sticker"
    assert "not read the vendor's own value" in str(proof.classified(finding).why)


def test_a_value_the_vendor_prints_too_is_the_vendors() -> None:
    label = {
        "id": "D4",
        "text": '25 1/2"',
        "inches": "25.5",
        "side": "vendor",
        "box": [14, 22, 36, 28],
    }
    result = escalation.escalate([_finding()], _ctx([label]))
    assert all("gv_sticker" not in f for f in result.changed)


def test_another_value_near_a_sticker_is_not_refused() -> None:
    other = _finding(vendor='18"', inches={"vendor": "18", "architect": "24"})
    result = escalation.escalate([other], _ctx())
    assert all("gv_sticker" not in f for f in result.changed)


@pytest.mark.parametrize("words", ["Soft-close runners", "AB-012-CUST"])
def test_words_and_tags_inside_a_sticker_are_refused(words: str) -> None:
    stickers = {
        ("A.pdf", 3): [{"page": 3, "box": [10, 20, 40, 30], "text": words, "file": "A.pdf"}]
    }
    ctx = escalation.EscalationContext(main_file="A.pdf", stickers=stickers)
    finding = escalation.escalate(
        [_finding(vendor=words, architect="Felt pads", inches={})], ctx
    ).changed[0]
    assert classification.classify(finding, proved=False).reason == "gv_sticker"
