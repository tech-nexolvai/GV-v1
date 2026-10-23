"""The ink check knows the file's kind (the colour rule, 2026-10-19), $0, synthetic files only.

A vendor's own shop drawings uploaded as an ordinary file may print every dimension in red or
blue: that is the vendor's ink, so a value's colour never stops a pairing there (it stopped five
true items in one run: "the vendor value is in colour: GV's markup"). On GV's combined review
sheets a value in colour is still GV's markup, exactly as before. Every gate that looks at the
vendor value's colour is covered:

- the devil's advocate's code check (`full_check_advocate.code_checks`, the `ink` check);
- the blind pick's "vendor ink is not black" (`full_check_advocate.decide`, `round_picks.link`);
- the simple check's own ink check (`simple_check.verify._ink_check`);
- how the roster and the simple check tell the file's kind (the upload itself, strict when not
  known);
- the text read under GV's markup (`under_markup`: the escalation's under-the-markup read and the
  missing-item check's colour look), marked coloured on GV's combined sheets only.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pikepdf
import pytest
from PIL import Image

from app.review.assistant import round_picks, under_markup
from app.review.assistant import verify_value as vv
from app.review.assistant.full_check_advocate import Check, code_checks, decide, ink_colour
from app.review.assistant.full_check_roster import _Run
from app.review.assistant.simple_check import run as simple_run
from app.review.assistant.simple_check.verify import _ink_check

_VENDOR = {
    "id": "D9",
    "page": 2,
    "file": "shop.pdf",
    "side": "vendor",
    "box": [100.0, 100.0, 120.0, 112.0],
    "orientation": "h",
    "inches": "12",
    "route": "text",
}
_ARCHITECT = {
    "id": "D14",
    "page": 1,
    "file": "architect.pdf",
    "side": "architect",
    "box": [100.0, 100.0, 120.0, 112.0],
    "orientation": "h",
    "inches": "23",
    "route": "text",
}


def _red_crop() -> tuple[int, float] | None:
    """The ink of a crop of a value printed in red (a vendor sheet that prints dimensions red)."""
    picture = Image.new("RGB", (40, 20), "white")
    for x in range(5, 35):
        for y in range(6, 14):
            picture.putpixel((x, y), (220, 20, 20))
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return ink_colour(out.getvalue())


def _ink(checks: list[Check]) -> Check:
    return next(c for c in checks if c.name == "ink")


# ---- the advocate's code check --------------------------------------------------------------------


def test_a_red_value_on_an_ordinary_upload_is_the_vendors_ink() -> None:
    red = _red_crop()
    assert red is not None and red[1] > 0.25  # the crop really is coloured
    found = code_checks(_VENDOR, _ARCHITECT, vendor_ink=red, combined_sheet=False)
    assert _ink(found).outcome == "pass"
    assert all(c.outcome == "pass" for c in found)
    # Its ink need not even be looked at: colour says nothing on an ordinary upload.
    assert _ink(code_checks(_VENDOR, _ARCHITECT, combined_sheet=False)).outcome == "pass"


def test_a_red_value_on_gvs_combined_sheet_is_still_gvs_markup() -> None:
    red = _red_crop()
    for found in (
        code_checks(_VENDOR, _ARCHITECT, vendor_ink=red, combined_sheet=True),
        code_checks(_VENDOR, _ARCHITECT, vendor_ink=red),  # not told: the strict rule
    ):
        assert _ink(found).outcome == "fail" and "GV's markup" in _ink(found).why
    black = code_checks(_VENDOR, _ARCHITECT, vendor_ink=(40, 0.0), combined_sheet=True)
    assert _ink(black).outcome == "pass"
    unknown = code_checks(_VENDOR, _ARCHITECT, vendor_ink=None, combined_sheet=True)
    assert _ink(unknown).outcome == "unknown"


# ---- the blind pick's "not black" ------------------------------------------------------------------

_SPANS = {"from": "left_end", "to": "sink_centre"}
_REPLY = {
    "pick": "p1.D14",
    "vendor_spans": _SPANS,
    "architect_spans": _SPANS,
    "vendor_ink_black": "no",
}
_DECIDE: dict[str, Any] = {
    "mode": "ai_pair_plus_code",
    "checks": [Check(n, "pass") for n in ("ink", "annotation", "roles", "axis", "round")],
    "printed": True,
    "helper_pick": "p1.D14",
    "advocate": _REPLY,
    "extent": ("same_extent", "both ends land on the same drawn lines"),
    "code_paired": False,
}


def test_the_blind_picks_not_black_is_information_only_on_an_ordinary_upload() -> None:
    assert decide(**_DECIDE, combined_sheet=False).result == "passed"
    for verdict in (decide(**_DECIDE, combined_sheet=True), decide(**_DECIDE)):
        assert verdict.result == "objects" and "in colour" in verdict.reason


def _round_finding() -> dict[str, Any]:
    box = {"page": 2, "file": "shop.pdf", "x0": 100.0, "y0": 100.0, "x1": 120.0, "y1": 112.0}
    return {
        "id": "F1",
        "item": "Vanity top: sink centre from left",
        "category": "countertop",
        "status": "same",
        "vendor": '12"',
        "architect": '12"',
        "page": 2,
        "vendor_box": box,
        "dims": {"vendor": dict(_VENDOR)},
        "round_match": {"compared": "earlier_round"},
    }


def _link(combined: bool | None) -> tuple[dict[str, Any], str]:
    # One file whose page 1 is the architect's drawing (the set pack's set pages).
    labels = {("shop.pdf", 1): [{**_ARCHITECT, "file": "shop.pdf", "inches": "12"}]}
    extra: dict[str, Any] = {} if combined is None else {"combined_sheet": combined}
    return round_picks.link(
        _round_finding(),
        {**_REPLY, "architect_pick": "D14"},
        vendor=_VENDOR,
        labels=labels,
        main_file="shop.pdf",
        checks=lambda v, a: [Check("ink", "pass")],
        ends=lambda v, a: ("same_extent", "both ends land on the same drawn lines"),
        **extra,
    )


def test_a_round_pick_on_an_ordinary_upload_is_not_refused_for_colour() -> None:
    _, outcome = _link(False)
    assert outcome == "linked"
    for combined in (True, None):
        linked, outcome = _link(combined)
        assert outcome == "refused"
        assert "in colour" in str(linked["round_match"][round_picks.ROUND_PICK]["why"])


# ---- the simple check's own ink check --------------------------------------------------------------


def test_the_simple_checks_ink_check_knows_the_file_kind() -> None:
    picture = {**_VENDOR, "route": "derived", "origin": "picture"}  # read from pixels
    red = lambda label: (120, 1.0)
    assert _ink_check(picture, red, "ordinary").outcome == "pass"
    assert _ink_check(picture, red, "combined").outcome == "fail"
    assert _ink_check(picture, red, None).outcome == "fail"  # not known: the strict rule
    assert _ink_check(picture, None, "ordinary").outcome == "pass"


# ---- the file's kind, from the upload itself -------------------------------------------------------


def _pdf(path: Path, stamp: bool) -> Path:
    """A one-page PDF whose value is printed in red; `stamp`: a drawing pasted on it as a stamp
    (GV's combined sheet)."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    body = b'1 0 0 rg BT /F1 9 Tf 100 300 Td (12") Tj ET'
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, 600, 400],
        Resources=resources,
        Contents=pdf.make_stream(b"" if stamp else body),
    )
    pdf.pages.append(pikepdf.Page(page))
    if stamp:
        appearance = pdf.make_stream(body)
        appearance["/Type"] = pikepdf.Name.XObject
        appearance["/Subtype"] = pikepdf.Name.Form
        appearance["/BBox"] = [0, 0, 300, 320]
        appearance["/Resources"] = resources
        pdf.pages[0].obj["/Annots"] = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.Stamp,
                        Rect=[250, 50, 550, 370],
                        AP=pikepdf.Dictionary(N=appearance),
                    )
                )
            ]
        )
    pdf.save(path)
    return path


@pytest.mark.parametrize("owner", [_Run, simple_run._Run], ids=["roster", "simple"])
def test_the_file_kind_comes_from_the_upload_and_is_strict_when_unknown(
    tmp_path: Path, owner: Any
) -> None:
    _pdf(tmp_path / "shop.pdf", stamp=False)
    _pdf(tmp_path / "combined.pdf", stamp=True)
    assert vv.combined_sheet((tmp_path / "combined.pdf").read_bytes()) is True
    holder = SimpleNamespace(
        engine=SimpleNamespace(tools=SimpleNamespace(workspace=SimpleNamespace(uploads=tmp_path)))
    )
    assert owner._combined(holder, {"id": "D1", "file": "shop.pdf"}) is False
    assert owner._combined(holder, {"id": "D1", "file": "combined.pdf"}) is True
    assert owner._combined(holder, {"id": "D1", "file": "missing.pdf"}) is True
    assert owner._combined(SimpleNamespace(), {"id": "D1", "file": "shop.pdf"}) is True


# ---- the text under GV's markup --------------------------------------------------------------------


def test_text_under_the_markup_is_marked_coloured_only_on_gvs_combined_sheet(
    tmp_path: Path,
) -> None:
    ordinary = _pdf(tmp_path / "shop.pdf", stamp=False)
    combined = _pdf(tmp_path / "combined.pdf", stamp=True)
    # The red 12" is at (100, 300) of the page (origin bottom-left): shown top-left at (100, 100);
    # inside the stamp it sits at (350, 350) of the page: shown at (350, 50).
    plain = under_markup.read_under_markup(ordinary, 1, (90, 85, 140, 110))
    pasted = under_markup.read_under_markup(combined, 1, (340, 35, 400, 60))
    assert [w["text"] for w in plain.words] == ['12"']
    assert [w["coloured"] for w in plain.words] == [False]  # the drawing's own red ink
    assert [w["text"] for w in pasted.words] == ['12"']
    assert [w["coloured"] for w in pasted.words] == [True]  # may be GV's markup
    assert not any("may be GV's" in line for line in plain.lines())
    assert any("may be GV's" in line for line in pasted.lines())
