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


def test_finds_gvs_sticker_and_tick_and_not_the_vendors_ink() -> None:
    with pikepdf.open(io.BytesIO(_drawing_pdf())) as pdf:
        found = gv_stickers.find(pdf)
        overlays = gv_stickers.find_overlays(pdf)
    # The tick is GV's mark in the drawing's markup layer (drawn after its own ink, 2026-10-27).
    assert [(s.page, s.text, s.kind) for s in found] == [(1, '25-1/2"', "note"), (1, "", "mark")]
    x0, y0, x1, y1 = found[0].box
    # Page as shown, origin top-left: the form box 98..162 x 98..122 (y up) on an 800-high page.
    assert (round(x0), round(y0), round(x1), round(y1)) == (98, 678, 162, 702)
    # The black note in the same layer may be the vendor's: listed as unsure, never hidden.
    assert [(s.kind, tuple(round(v) for v in s.box)) for s in overlays] == [
        ("overlay", (100, 585, 160, 600))
    ]


def test_a_note_with_any_black_ink_is_the_vendors() -> None:
    mixed = b"q 1 1 0.5 rg 100 100 60 20 re f 0 g BT /F1 8 Tf 105 106 Td (AB-012) Tj ET Q"
    with pikepdf.open(io.BytesIO(_drawing_pdf({"S1": mixed}))) as pdf:
        assert [s.kind for s in gv_stickers.find(pdf)] == ["mark"]  # only the tick


def test_a_big_coloured_part_is_never_a_sticker() -> None:
    pdf_bytes = _drawing_pdf()
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        form = pdf.pages[0].obj.Annots[0].AP.N.Resources.XObject.S1
        form.BBox = pikepdf.Array([0, 0, 1000, 800])  # the whole drawing
        assert [s.kind for s in gv_stickers.find(pdf)] == ["mark"]  # only the tick


def test_hidden_copy_empties_the_sticker_only(tmp_path: Path) -> None:
    source = tmp_path / "set.pdf"
    source.write_bytes(_drawing_pdf())
    before = source.read_bytes()
    copy, found = gv_stickers.hidden_copy(source)
    assert [s.text for s in found] == ['25-1/2"', ""]
    with pikepdf.open(copy) as pdf:
        xobjects = pdf.pages[0].obj.Annots[0].AP.N.Resources.XObject
        assert xobjects.S1.read_bytes() == b""
        assert b"610" in xobjects.V1.read_bytes()  # the vendor's own value stays
        assert xobjects.T1.read_bytes() == b""  # GV's tick
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


# ---- GV's markup flattened into a pasted drawing (2026-10-27) -------------------------------------

#: GV's markup tool's outlet: a yellow box and a red frame round a symbol drawn in black.
FRAME = b"q 1 1 0.5 rg 400 400 16 9 re f Q /O1 Do q 1 0 0 RG 400 400 16 9 re S Q"
OUTLET = b"q 0 G 402 402 m 414 402 l 414 407 l 402 407 l h S Q"
#: GV's red cloud round a bay (colour only, no text).
CLOUD = b"q 1 0 0 RG 200 200 m 380 200 l 380 380 l 200 380 l h S Q"
#: Black slats pasted onto a bay (GV's or the vendor's: code cannot tell).
SLATS = b"q 0 G 600 300 m 800 300 l S 600 320 m 800 320 l S 600 340 m 800 340 l S Q"
_OWN_INK = b"q 0 G 10 10 m 900 10 l S Q"


def _pasted(
    forms: list[tuple[str, bytes, list[float]]],
    *,
    before: tuple[str, ...] = (),
    rect: list[float] | None = None,
) -> bytes:
    """One pasted drawing: the sub-drawings named in `before` drawn before its own black ink, the
    others after it (its markup layer, as a viewer flattens comments)."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(1000, 800))
    xobjects = pikepdf.Dictionary()
    for name, body, bbox in forms:
        xobjects[f"/{name}"] = _form(pdf, body, bbox, font=False)
    if "/O1" not in xobjects:
        xobjects["/O1"] = _form(pdf, OUTLET, [400, 400, 416, 409], font=False)
    for name, _body, _bbox in forms:
        inner = xobjects[f"/{name}"]
        inner.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(O1=xobjects["/O1"]))
    first = b" ".join(f"/{n} Do".encode() for n in before)
    last = b" ".join(f"/{n} Do".encode() for n, _b, _x in forms if n not in before)
    appearance = pikepdf.Stream(
        pdf,
        first + b" " + _OWN_INK + b" " + last,
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=pikepdf.Array(rect or [0, 0, 1000, 800]),
        Resources=pikepdf.Dictionary(XObject=xobjects),
    )
    stamp = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name.Stamp,
        Rect=pikepdf.Array(rect or [0, 0, 1000, 800]),
        AP=pikepdf.Dictionary(N=appearance),
    )
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(stamp)]))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _kinds(data: bytes) -> tuple[list[str], list[str]]:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return (
            [s.kind for s in gv_stickers.find(pdf)],
            [s.kind for s in gv_stickers.find_overlays(pdf)],
        )


def test_gvs_flattened_cloud_and_framed_symbol_are_hidden_and_black_slats_only_listed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "set.pdf"
    source.write_bytes(
        _pasted(
            [
                ("Fm1", CLOUD, [195, 195, 385, 385]),
                ("Fm2", FRAME, [398, 398, 418, 411]),
                ("Fm3", SLATS, [598, 298, 802, 342]),
            ]
        )
    )
    copy, found, overlays = gv_stickers.hidden_copy_and_overlays(source)
    assert [s.kind for s in found] == ["mark", "symbol"]
    assert [s.kind for s in overlays] == ["overlay"]
    with pikepdf.open(copy) as pdf:
        xobjects = pdf.pages[0].obj.Annots[0].AP.N.Resources.XObject
        assert xobjects.Fm1.read_bytes() == b"" and xobjects.Fm2.read_bytes() == b""
        assert xobjects.Fm3.read_bytes() == SLATS  # black line work is never removed
        assert b"900 10 l" in pdf.pages[0].obj.Annots[0].AP.N.read_bytes()  # the drawing's ink


def test_the_drawings_own_cad_blocks_are_never_markup() -> None:
    # Black sub-drawings after the drawing's ink with no GV mark among them: its CAD blocks.
    assert _kinds(_pasted([("X1", SLATS, [598, 298, 802, 342])])) == ([], [])
    # Sub-drawings drawn before the drawing's own ink are part of it, coloured or framed.
    before = _pasted(
        [("Fm1", CLOUD, [195, 195, 385, 385]), ("Fm2", FRAME, [398, 398, 418, 411])],
        before=("Fm1", "Fm2"),
    )
    assert _kinds(before) == ([], [])


def test_a_layer_of_flattened_comments_is_known_by_their_names_too() -> None:
    assert _kinds(_pasted([("BBA", SLATS, [598, 298, 802, 342])])) == ([], ["overlay"])
    assert _kinds(_pasted([("BBA3", SLATS, [598, 298, 802, 342])])) == ([], ["overlay"])


def test_a_big_frame_or_a_frame_with_black_of_its_own_is_not_gvs_symbol() -> None:
    big = _pasted([("Fm2", FRAME, [0, 0, 1000, 800])])  # the whole drawing
    assert _kinds(big)[0] == []
    own_black = FRAME + b" q 0 G 400 400 m 416 409 l S Q"
    assert _kinds(_pasted([("Fm2", own_black, [398, 398, 418, 411])]))[0] == []


def test_gvs_symbol_pasted_on_its_own_is_hidden() -> None:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(1000, 800))
    outlet = _form(pdf, OUTLET, [400, 400, 416, 409], font=False)
    appearance = pikepdf.Stream(
        pdf,
        FRAME,
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=pikepdf.Array([400, 400, 416, 409]),
        Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(O1=outlet)),
    )
    stamp = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name.Stamp,
        Rect=pikepdf.Array([400, 400, 416, 409]),
        AP=pikepdf.Dictionary(N=appearance),
    )
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(stamp)]))
    found = gv_stickers.hide(pdf)
    assert [(s.kind, tuple(round(v) for v in s.box)) for s in found] == [
        ("symbol", (400, 391, 416, 400))
    ]
    assert pdf.pages[0].obj.Annots[0].AP.N.read_bytes() == b""


def test_the_pack_file_lists_overlays_apart(tmp_path: Path) -> None:
    sticker = gv_stickers.Sticker(3, (10.0, 20.0, 40.0, 30.0), "", "symbol")
    overlay = gv_stickers.Sticker(3, (50.0, 20.0, 90.0, 80.0), "", "overlay")
    gv_stickers.write_file(tmp_path / "A_stickers.json", "A.pdf", [sticker], [overlay])
    assert [r["kind"] for r in (gv_stickers.read_folder(tmp_path) or {})[("A.pdf", 3)]] == [
        "symbol"
    ]
    assert [r["kind"] for r in (gv_stickers.read_overlays(tmp_path) or {})[("A.pdf", 3)]] == [
        "overlay"
    ]
    assert gv_stickers.read_overlays(tmp_path / "missing") is None
