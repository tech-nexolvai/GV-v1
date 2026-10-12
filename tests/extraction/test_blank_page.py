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
) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 300))
    page = pdf.pages[0]
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont="/Helvetica"
        )
    )
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
    if blend is not None:
        resources.ExtGState = pikepdf.Dictionary(
            GS1=pikepdf.Dictionary(Type=pikepdf.Name.ExtGState, BM=pikepdf.Name(blend))
        )
    page.obj.Resources = resources
    page.obj.Contents = pdf.make_stream(content)
    if annotations:
        page.obj.Annots = pdf.make_indirect(
            pikepdf.Array(pdf.make_indirect(_annotation(pdf, spec)) for spec in annotations)
        )
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _annotation(pdf: pikepdf.Pdf, spec: dict[str, object]) -> pikepdf.Dictionary:
    appearance = pikepdf.Stream(pdf, spec.get("appearance", b"") or b"")  # type: ignore[arg-type]
    appearance.Type = pikepdf.Name.XObject
    appearance.Subtype = pikepdf.Name.Form
    appearance.BBox = [0, 0, 200, 40]
    return pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name(str(spec["subtype"])),
        Rect=[100, 200, 300, 240],
        Contents=pikepdf.String(str(spec.get("text", ""))),
        AP=pikepdf.Dictionary(N=appearance),
    )


#: A typed title box as a PDF editor writes it: a white background rectangle and the text.
TITLE_ANNOTATION = {
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
