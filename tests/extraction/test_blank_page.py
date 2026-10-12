"""A page is proven blank only when nothing is drawn on it beyond its frame and typed title (#1195).

Synthetic PDFs only, built here with pikepdf: a 400 x 300 point page with GV-style content (a border
rectangle along the page edges, a typed title in a `/FreeText` box or flattened into the page as a
white-backed text box), then one thing added at a time: a picture, a line, a coloured fill, a curve,
a pasted drawing (`/Stamp`), other markup, too much text.
"""

from __future__ import annotations

import io

import pikepdf
import pytest

from extraction.blank_page import MAX_TITLE_CHARACTERS, BlankPageFacts, prove_blank_pages

#: The border GV's sheet template draws on every page: a stroked rectangle along the page edges.
FRAME = b"q 1 J 1 j 0.84 w 8 8 m 392 8 l 392 292 l 8 292 l h S Q\n"
#: A flattened text box: a white background and its typed text, drawn by a form.
TEXT_BOX = (
    b"q 1 1 1 rg 0 0 0 RG 0 w 100 120 m 300 120 l 300 160 l 100 160 l h f* "
    b"BT 0 0 0 rg /Helv 24 Tf 1 0 0 1 110 130 Tm (SYNTHETIC TITLE) Tj ET Q\n"
)


def _pdf(
    content: bytes,
    *,
    forms: dict[str, bytes] | None = None,
    image: bool = False,
    annotations: list[dict[str, object]] | None = None,
    blend: str | None = None,
    graphics: dict[str, object] | None = None,
    type3: bool = False,
    resources_on_parent: bool = False,
) -> bytes:
    """One synthetic page. `graphics`: the entries of the page's `/GS1` graphics state (`blend`
    sets its `/BM`); `type3`: `/Helv` is a Type3 font; `resources_on_parent`: the resources sit on
    the `/Pages` node and the page inherits them."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 300))
    page = pdf.pages[0]
    font = _font(pdf, type3=type3)
    xobjects = pikepdf.Dictionary()
    for name, body in (forms or {}).items():
        form = pikepdf.Stream(pdf, body)
        form.Type = pikepdf.Name.XObject
        form.Subtype = pikepdf.Name.Form
        form.BBox = [0, 0, 400, 300]
        form.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(Helv=font))
        xobjects[f"/{name}"] = pdf.make_indirect(form)
    if image:
        picture = pikepdf.Stream(pdf, b"\x00\x00\x00")
        picture.Type = pikepdf.Name.XObject
        picture.Subtype = pikepdf.Name.Image
        picture.Width = 1
        picture.Height = 1
        picture.ColorSpace = pikepdf.Name.DeviceRGB
        picture.BitsPerComponent = 8
        xobjects["/Im1"] = pdf.make_indirect(picture)
    resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(Helv=font), XObject=xobjects)
    state = dict(graphics or {})
    if blend is not None:
        state["/BM"] = pikepdf.Name(blend)
    if state:
        resources.ExtGState = pikepdf.Dictionary(
            GS1=pikepdf.Dictionary({"/Type": pikepdf.Name.ExtGState, **state})
        )
    if resources_on_parent:
        del page.obj["/Resources"]
        page.obj.Parent.Resources = resources
    else:
        page.obj.Resources = resources
    page.obj.Contents = pdf.make_stream(content)
    if annotations:
        page.obj.Annots = pdf.make_indirect(
            pikepdf.Array(pdf.make_indirect(_annotation(pdf, spec)) for spec in annotations)
        )
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _font(pdf: pikepdf.Pdf, *, type3: bool = False) -> pikepdf.Object:
    if type3:
        glyph = pdf.make_stream(b"500 0 0 0 500 700 d1 0 0 500 700 re f")
        return pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type3,
                FontBBox=[0, 0, 500, 700],
                FontMatrix=[0.001, 0, 0, 0.001, 0, 0],
                CharProcs=pikepdf.Dictionary(box=glyph),
                Encoding=pikepdf.Dictionary(Differences=[65, pikepdf.Name("/box")]),
                FirstChar=65,
                LastChar=65,
                Widths=[500],
            )
        )
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont="/Helvetica"
        )
    )


def _annotation(pdf: pikepdf.Pdf, spec: dict[str, object]) -> pikepdf.Dictionary:
    """A synthetic annotation on a [100 200 300 240] rectangle; `spec` may set `"appearance": None`
    (no `/AP`) and any extra dictionary entries under `"extra"`."""
    annotation = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name(str(spec["subtype"])),
        Rect=[100, 200, 300, 240],
        Contents=pikepdf.String(str(spec.get("text", ""))),
    )
    body = spec.get("appearance", b"")
    if body is not None:
        appearance = pikepdf.Stream(pdf, body or b"")  # type: ignore[arg-type]
        appearance.Type = pikepdf.Name.XObject
        appearance.Subtype = pikepdf.Name.Form
        appearance.BBox = [0, 0, 200, 40]
        appearance.Resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(Helv=_font(pdf, type3=bool(spec.get("type3"))))
        )
        annotation.AP = pikepdf.Dictionary(N=appearance)
    for key, value in dict(spec.get("extra", {}) or {}).items():  # type: ignore[call-overload]
        annotation[key] = value
    return annotation


#: A typed title box as a PDF editor writes it: a white background rectangle and the text.
TITLE_ANNOTATION: dict[str, object] = {
    "subtype": "/FreeText",
    "text": "SYNTHETIC SECTION 1",
    "appearance": b"q 1 1 1 rg 0 0 200 40 re f BT /Helv 18 Tf 4 12 Td (SYNTHETIC SECTION 1) Tj ET Q",
}


def _facts(data: bytes) -> BlankPageFacts:
    return prove_blank_pages(data, [0])[0]


# --- proven blank ---------------------------------------------------------------------------------


def test_a_frame_and_a_typed_title_box_is_blank_and_says_what_was_checked() -> None:
    facts = _facts(_pdf(FRAME, annotations=[TITLE_ANNOTATION]))

    assert facts.blank
    assert facts == BlankPageFacts(frames=1, typed_texts=1, typed_characters=19)


def test_a_title_flattened_into_the_page_with_a_white_background_is_blank() -> None:
    """GV's title pages: the frame, and a text box flattened into a form (white fill + typed text)."""
    facts = _facts(_pdf(FRAME + b"q /BBA Do Q\n", forms={"BBA": TEXT_BOX}))

    assert facts.blank
    assert (facts.frames, facts.white_paint, facts.typed_texts, facts.drawn_paths) == (1, 1, 1, 0)


def test_an_empty_page_is_blank() -> None:
    assert _facts(_pdf(b"")).blank


def test_a_frame_drawn_as_a_re_rectangle_through_a_scaled_form_is_still_the_frame() -> None:
    form = b"0 0 0 RG 1 w 4 4 192 142 re S\n"
    facts = _facts(_pdf(b"q 2 0 0 2 0 0 cm /F1 Do Q\n", forms={"F1": form}))

    assert facts.blank and facts.frames == 1


def test_a_clip_paints_nothing() -> None:
    assert _facts(_pdf(FRAME + b"q 50 50 100 100 re W n Q\n")).blank


# --- not blank: something is drawn ----------------------------------------------------------------


def test_a_picture_is_not_blank() -> None:
    facts = _facts(_pdf(FRAME + b"q 100 0 0 100 50 50 cm /Im1 Do Q\n", image=True))

    assert not facts.blank and facts.images == 1


def test_an_inline_picture_is_not_blank() -> None:
    inline = b"q 10 0 0 10 50 50 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q\n"

    facts = _facts(_pdf(FRAME + inline))

    assert not facts.blank and facts.images == 1


def test_a_picture_inside_a_form_is_not_blank() -> None:
    facts = _facts(_pdf(b"/F1 Do\n", forms={"F1": b"q 10 0 0 10 5 5 cm /Im1 Do Q"}, image=True))

    assert not facts.blank


@pytest.mark.parametrize(
    "ink",
    [
        b"0 0 0 RG 2 w 100 100 m 300 100 l S\n",  # a line (an underline under the title, say)
        b"0 0 0 RG 120 120 160 40 re S\n",  # a box mid-page: not the frame
        b"0.5 g 120 120 160 40 re f\n",  # a grey fill
        b"1 0 0 rg 120 120 160 40 re f\n",  # a coloured fill
        b"0 0 0 RG 100 100 m 150 200 200 200 250 100 c S\n",  # a curve
        b"0 0 0 RG 1 1 1 rg 120 120 160 40 re B\n",  # white filled, black stroked
        b"/Pattern cs /P1 scn 120 120 160 40 re f\n",  # a pattern: not known to be white
    ],
    ids=["line", "box", "grey-fill", "coloured-fill", "curve", "fill-and-stroke", "pattern"],
)
def test_drawn_vector_ink_is_not_blank(ink: bytes) -> None:
    facts = _facts(_pdf(FRAME + ink))

    assert not facts.blank and facts.drawn_paths == 1


def test_a_vector_line_inside_a_flattened_text_box_is_not_blank() -> None:
    underlined = TEXT_BOX + b"q 0 0 0 RG 2.5 w 100 118 m 300 118 l S Q\n"

    facts = _facts(_pdf(FRAME + b"/BBA Do\n", forms={"BBA": underlined}))

    assert not facts.blank and facts.drawn_paths == 1


def test_a_frame_that_does_not_run_along_the_page_edges_is_ink() -> None:
    half = b"0 0 0 RG 100 75 200 150 re S\n"

    assert not _facts(_pdf(half)).blank


def test_white_paint_under_an_unusual_blend_mode_is_ink() -> None:
    """A white fill under "Difference" darkens white paper: it is not known to leave no mark."""
    facts = _facts(_pdf(FRAME + b"q /GS1 gs 1 g 120 120 160 40 re f Q\n", blend="/Difference"))

    assert not facts.blank and facts.drawn_paths == 1


def test_a_shading_is_ink() -> None:
    facts = _facts(_pdf(FRAME + b"/Sh1 sh\n"))

    assert not facts.blank


def test_a_pasted_drawing_is_not_blank() -> None:
    facts = _facts(_pdf(FRAME, annotations=[{"subtype": "/Stamp"}, TITLE_ANNOTATION]))

    assert not facts.blank and facts.stamps == 1


@pytest.mark.parametrize("subtype", ["/Line", "/Square", "/Ink", "/Polygon", "/Highlight"])
def test_other_markup_is_not_blank(subtype: str) -> None:
    facts = _facts(_pdf(FRAME, annotations=[{"subtype": subtype}]))

    assert not facts.blank and facts.other_annotations == 1


def test_a_text_box_whose_appearance_draws_more_than_its_box_is_not_blank() -> None:
    callout = dict(TITLE_ANNOTATION, appearance=b"0 0 0 RG 0 0 m 50 30 l S")

    assert not _facts(_pdf(FRAME, annotations=[callout])).blank


def test_more_typed_text_than_a_title_is_not_blank() -> None:
    notes = b"BT /Helv 8 Tf 20 20 Td (" + b"N" * (MAX_TITLE_CHARACTERS + 1) + b") Tj ET\n"

    facts = _facts(_pdf(FRAME + notes))

    assert not facts.blank and facts.typed_characters == MAX_TITLE_CHARACTERS + 1
    assert _facts(_pdf(FRAME + notes.replace(b"N" * (MAX_TITLE_CHARACTERS + 1), b"N" * 9))).blank


# --- fail-safe ------------------------------------------------------------------------------------


def test_a_page_that_cannot_be_read_is_never_blank() -> None:
    facts = prove_blank_pages(b"not a pdf", [0])[0]

    assert not facts.blank and facts.unreadable is not None


def test_an_xobject_that_is_missing_is_never_blank() -> None:
    facts = _facts(_pdf(FRAME + b"/Nowhere Do\n"))

    assert not facts.blank and facts.unreadable is not None


def test_a_page_that_is_not_in_the_file_is_never_blank() -> None:
    assert not prove_blank_pages(_pdf(b""), [3])[3].blank


# --- review fixes (#1195 review) ------------------------------------------------------------------


def test_text_in_a_type3_font_is_drawing_so_the_page_is_not_proven_blank() -> None:
    """A Type3 glyph is a little drawing of its own (its `CharProcs` paint paths), so text set in
    one is not typed text."""
    in_page = _facts(_pdf(FRAME + b"BT /Helv 24 Tf 20 20 Td (A) Tj ET\n", type3=True))
    in_box = _facts(_pdf(FRAME, annotations=[dict(TITLE_ANNOTATION, type3=True)]))

    assert not in_page.blank and in_page.unreadable is not None and "Type3" in in_page.unreadable
    assert not in_box.blank and in_box.unreadable is not None


@pytest.mark.parametrize(
    "content",
    [
        b"BT /Nowhere 24 Tf 20 20 Td (TITLE) Tj ET\n",  # a font that is not in the resources
        b"BT 20 20 Td (TITLE) Tj ET\n",  # no font set at all
    ],
    ids=["missing-font", "no-font"],
)
def test_text_in_a_font_that_cannot_be_found_is_not_proven_blank(content: bytes) -> None:
    facts = _facts(_pdf(FRAME + content))

    assert not facts.blank and facts.unreadable is not None


def test_a_text_box_may_hold_two_rectangles_inside_its_own_box_and_no_more() -> None:
    two = b"q 1 1 1 rg 0 0 200 40 re f 0 0 0 RG 1 1 198 38 re S Q"
    three = two + b" 0 0 0 RG 50 10 20 20 re S"
    outside = b"q 0 0 0 RG -150 -150 400 300 re S Q"  # a box well outside the text box

    assert _facts(_pdf(FRAME, annotations=[dict(TITLE_ANNOTATION, appearance=two)])).blank
    many = _facts(_pdf(FRAME, annotations=[dict(TITLE_ANNOTATION, appearance=three)]))
    assert not many.blank and many.drawn_paths == 1
    beyond = _facts(_pdf(FRAME, annotations=[dict(TITLE_ANNOTATION, appearance=outside)]))
    assert not beyond.blank and beyond.drawn_paths == 1


@pytest.mark.parametrize(
    "graphics",
    [
        {"/SMask": pikepdf.Dictionary(Type=pikepdf.Name.Mask, S=pikepdf.Name.Luminosity)},
        {"/TR": pikepdf.Name.Identity},
        {"/TR2": pikepdf.Name.Default},
        {"/OP": True},
        {"/op": True},
        {"/BM": pikepdf.Name.Multiply},
    ],
    ids=["soft-mask", "transfer", "transfer-2", "overprint-stroke", "overprint-fill", "multiply"],
)
def test_white_paint_under_a_graphics_state_that_may_mark_counts_as_drawn(
    graphics: dict[str, object],
) -> None:
    facts = _facts(_pdf(FRAME + b"q /GS1 gs 1 g 120 120 160 40 re f Q\n", graphics=graphics))

    assert not facts.blank and (facts.drawn_paths, facts.white_paint) == (1, 0)


def test_white_paint_under_a_plain_graphics_state_still_leaves_no_mark() -> None:
    plain = {"/CA": 0.9, "/ca": 1, "/SMask": pikepdf.Name("/None"), "/BM": pikepdf.Name.Normal}

    facts = _facts(_pdf(FRAME + b"q /GS1 gs 1 g 120 120 160 40 re f Q\n", graphics=plain))

    assert facts.blank and facts.white_paint == 1


@pytest.mark.parametrize(
    ("content", "graphics"),
    [
        (b"q /GS9 gs 1 g 120 120 160 40 re f Q\n", {"/CA": 1}),  # not among the page's states
        (b"q /GS1 gs 1 g 120 120 160 40 re f Q\n", None),  # the page has no graphics states
    ],
    ids=["missing-state", "no-states"],
)
def test_a_graphics_state_that_cannot_be_found_is_not_proven_blank(
    content: bytes, graphics: dict[str, object] | None
) -> None:
    facts = _facts(_pdf(FRAME + content, graphics=graphics))

    assert not facts.blank and facts.unreadable is not None


def test_resources_inherited_from_the_page_tree_are_read() -> None:
    """A page may take its resources from the `/Pages` node above it; they are read there, so a
    picture or a missing font is still found."""
    title = FRAME + b"BT /Helv 24 Tf 20 20 Td (TITLE) Tj ET\n"
    assert _facts(_pdf(title, resources_on_parent=True)).blank
    picture = _facts(
        _pdf(FRAME + b"q 100 0 0 100 50 50 cm /Im1 Do Q\n", image=True, resources_on_parent=True)
    )
    assert not picture.blank and picture.images == 1


@pytest.mark.parametrize(
    "box",
    [
        {**TITLE_ANNOTATION, "appearance": None},  # a text box with no appearance
        {**TITLE_ANNOTATION, "extra": {"/IT": pikepdf.Name("/FreeTextCallout")}},
        {**TITLE_ANNOTATION, "extra": {"/CL": pikepdf.Array([100, 200, 50, 50])}},
    ],
    ids=["no-appearance", "callout-intent", "callout-line"],
)
def test_a_text_box_with_no_appearance_or_a_callout_is_other_markup(box: dict[str, object]) -> None:
    facts = _facts(_pdf(FRAME, annotations=[box]))

    assert not facts.blank and facts.other_annotations == 1


def test_a_text_box_with_no_appearance_still_counts_its_characters() -> None:
    long_note = {**TITLE_ANNOTATION, "appearance": None, "text": "N" * 300}

    assert _facts(_pdf(FRAME, annotations=[long_note])).typed_characters == 300
