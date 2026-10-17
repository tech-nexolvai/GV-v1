"""Read under GV's markup (#1548): the reviewers' annotations removed, boxes baked into a pasted
drawing hidden, the drawing's own text listed. $0, no network.

The page is synthetic (built in the test), never a client file: a pasted vendor drawing (`/Stamp`)
that prints 46" and 3'-1"; a yellow box GV baked inside the drawing as a sub-drawing over the 46";
a yellow rectangle GV baked into the drawing's own content over the 3'-1"; a grey hatch rectangle of
the vendor's own; and GV's typed note (FreeText) and box (Square) over the area.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pikepdf
import pytest
from PIL import Image

from app.review.assistant.chat_tools import (
    HELPER_TOOLS,
    LEAD_TOOLS,
    PARALLEL_TOOLS,
    ChatTools,
    new_step,
)
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.under_markup import read_under_markup
from tests.review.assistant.chat_fakes import FakeSandbox

WIDTH, HEIGHT = 600, 400

# The stamp's drawing, in its own space (300 x 200, placed at 100,100 on the page).
_DRAWING = (
    b"q 0 0 0 RG 1 w 10 10 m 290 10 l S Q "
    b'BT /F1 12 Tf 0 0 0 rg 40 120 Td (46") Tj ET '
    b"BT /F1 12 Tf 0 0 0 rg 160 60 Td (3'-1\") Tj ET "
    # The vendor's own grey hatch box: never touched.
    b"q 0.6 0.6 0.6 rg 200 150 30 20 re f Q "
    # GV's yellow box baked into the drawing's own content, over 3'-1".
    b"q 1 1 0 rg 150 52 50 22 re f Q "
    # GV's yellow box baked in as a small sub-drawing, over 46".
    b"q 1 0 0 1 32 112 cm /Box Do Q"
)
_YELLOW_FORM = b"q 1 1 0 rg 0 0 40 22 re f Q"


def _page_pdf(path: Path) -> Path:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(WIDTH, HEIGHT))
    page = pdf.pages[0]
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    box_form = pdf.make_stream(_YELLOW_FORM)
    box_form.Type = pikepdf.Name.XObject
    box_form.Subtype = pikepdf.Name.Form
    box_form.BBox = [0, 0, 40, 22]
    drawing = pdf.make_stream(_DRAWING)
    drawing.Type = pikepdf.Name.XObject
    drawing.Subtype = pikepdf.Name.Form
    drawing.BBox = [0, 0, 300, 200]
    drawing.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=font), XObject=pikepdf.Dictionary(Box=box_form)
    )
    stamp = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Stamp,
            Rect=[100, 100, 400, 300],
            AP=pikepdf.Dictionary(N=drawing),
            F=4,
        )
    )

    def plain(kind: str, rect: list[int], body: bytes, text: str) -> Any:
        form = pdf.make_stream(body)
        form.Type = pikepdf.Name.XObject
        form.Subtype = pikepdf.Name.Form
        form.BBox = [0, 0, rect[2] - rect[0], rect[3] - rect[1]]
        return pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name(f"/{kind}"),
                Rect=rect,
                AP=pikepdf.Dictionary(N=form),
                Contents=pikepdf.String(text),
                T=pikepdf.String("Reviewer A"),
                F=4,
            )
        )

    note = plain("FreeText", [120, 250, 220, 280], b"1 0 0 rg 0 0 100 30 re f", "GV: needs 37")
    square = plain("Square", [250, 140, 330, 180], b"0 0 1 RG 2 w 1 1 78 38 re S", "")
    far = plain("FreeText", [450, 350, 590, 390], b"1 0 0 rg 0 0 140 40 re f", "far away")
    page.obj.Annots = pdf.make_indirect(pikepdf.Array([stamp, note, square, far]))
    pdf.save(path)
    return path


def _yellow(png: bytes) -> int:
    with Image.open(io.BytesIO(png)) as picture:
        rgb = np.asarray(picture.convert("RGB"), dtype=int)
    return int(((rgb[:, :, 0] > 200) & (rgb[:, :, 1] > 200) & (rgb[:, :, 2] < 80)).sum())


def _red(png: bytes) -> int:
    with Image.open(io.BytesIO(png)) as picture:
        rgb = np.asarray(picture.convert("RGB"), dtype=int)
    return int(((rgb[:, :, 0] > 200) & (rgb[:, :, 1] < 80) & (rgb[:, :, 2] < 80)).sum())


def _grey(png: bytes) -> int:
    with Image.open(io.BytesIO(png)) as picture:
        rgb = np.asarray(picture.convert("RGB"), dtype=int)
    grey = (abs(rgb[:, :, 0] - 153) < 12) & (abs(rgb[:, :, 1] - 153) < 12)
    return int(grey.sum())


def test_the_reviewers_marks_go_and_the_vendors_values_show(tmp_path: Path) -> None:
    pdf = _page_pdf(tmp_path / "set.pdf")
    found = read_under_markup(pdf, 1, (100, 100, 400, 300))
    # The page's own picture had GV's yellow boxes and red note; under the markup they are gone,
    # and the vendor's grey hatch stays.
    assert _yellow(found.as_shown) > 500 and _red(found.as_shown) > 500
    assert _yellow(found.under) == 0 and _red(found.under) == 0
    assert _grey(found.under) > 100
    assert found.hidden_forms == 1 and found.hidden_fills == 1 and found.drawings == 1
    kinds = sorted(item["kind"] for item in found.removed)
    assert kinds == ["FreeText", "Square"]
    assert found.removed_elsewhere == 1
    texts = [word["text"] for word in found.words]
    assert '46"' in texts and any(t.replace("\u2019", "'") == "3'-1\"" for t in texts)
    lines = "\n".join(found.lines())
    assert "Removed 2 reviewer annotation(s)" in lines and "GV: needs 37" in lines
    assert "emptied 1 small filled-box sub-drawing" in lines
    # The upload itself is never changed.
    with pikepdf.open(pdf) as again:
        assert len(again.pages[0].obj.Annots) == 4


def test_a_page_or_box_not_on_the_file_is_told_plainly(tmp_path: Path) -> None:
    pdf = _page_pdf(tmp_path / "set.pdf")
    with pytest.raises(ValueError, match="no page 2"):
        read_under_markup(pdf, 2, (0, 0, 10, 10))
    with pytest.raises(ValueError, match="not on page 1"):
        read_under_markup(pdf, 1, (900, 900, 950, 950))


def test_the_tool_returns_both_pictures_and_what_it_did(tmp_path: Path) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    _page_pdf(workspace.uploads / "set.pdf")
    tools = ChatTools(
        workspace=workspace,
        sandbox=FakeSandbox(workspace),
        uploads=[{"name": "set.pdf", "pages": 1, "kind": "the vendor's"}],
    )
    result = tools.read_under_markup(
        {"page": 1, "box": {"x0": 100, "y0": 100, "x1": 400, "y1": 300}},
        new_step("u1", "read_under_markup", {}),
        by="helper:2",
    )
    assert result.is_error is False and result.step["ok"] is True
    images = [block for block in result.content if block["type"] == "image"]
    assert len(images) == 2
    text = "\n".join(block["text"] for block in result.content if block["type"] == "text")
    assert "Removed 2 reviewer annotation(s)" in text
    assert result.step["label"] == "Read under GV's markup on page 1"
    assert tools.seen[-1]["by"] == "helper:2" and tools.seen[-1]["how"] == "under_markup"
    bad = tools.read_under_markup(
        {"page": 7, "box": {"x0": 0, "y0": 0, "x1": 9, "y1": 9}},
        new_step("u2", "read_under_markup", {}),
    )
    assert bad.is_error is True and "no page 7" in bad.content[0]["text"]


def test_lead_and_helpers_have_the_tool_and_it_runs_in_parallel() -> None:
    assert "read_under_markup" in {tool["name"] for tool in LEAD_TOOLS}
    assert "read_under_markup" in {tool["name"] for tool in HELPER_TOOLS}
    assert "read_under_markup" in PARALLEL_TOOLS


def test_a_stored_answer_with_zoom_and_read_under_markup_steps_reads_back() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.review.assistant.chat_contract import ChatMessageOut

    steps = [
        {"type": "step", "step": {"id": "1", "tool": "zoom", "label": "Zoomed in on page 2"}},
        {
            "type": "step",
            "step": {"id": "2", "tool": "read_under_markup", "label": "Read under GV's markup"},
        },
    ]
    message = ChatMessageOut.model_validate(
        {
            "id": str(uuid4()),
            "role": "assistant",
            "created_at": datetime.now(UTC),
            "text": "x",
            "blocks": steps,
        }
    )
    assert [block.step.tool for block in message.blocks or []] == ["zoom", "read_under_markup"]  # type: ignore[union-attr]
