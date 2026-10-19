"""GV Review's own marks: signed when they are drawn, never read back as the vendor's drawing.

A marked-up PDF GV Review sends (the cloud drawing, the proof drawing, the redline) can come back:
the vendor answers on it, and round 2 is uploaded with GV's clouds and notes still on the page.
Those marks are page content, not annotations, so the rule "annotations are markup" does not catch
them, and their colour must not be what decides (a vendor may draw in red too). So every overlay GV
Review draws is **signed** in a way code can recognise:

- the overlay's Form XObject carries `/PieceInfo << /GVReview << ... >> >>` (the PDF's own place
  for an application's private data on a form, PDF 32000-1 §14.5);
- it is drawn inside an optional content group (a layer) named `GV Review markup`, so a person
  can switch GV's marks off in any viewer.

A reader making the vendor's layer (`without_gv_marks`) empties every signed overlay, and also the
overlays GV wrote before they were signed, by their reserved resource names (`RESERVED_NAMES`). The
redline writer (`reports/redline.py`) is known by its reserved name only: its output is held byte
for byte by its golden test.

Source: the vendor-layer rule (reviewer markup is the answer, never the reading source), the
orchestrator's colour decision of 2026-10-19 · Verification: `tests/extraction/test_gv_marks.py`
"""

from __future__ import annotations

from typing import Any, Final

import pikepdf

__all__ = [
    "LAYER_NAME",
    "PIECE_KEY",
    "RESERVED_NAMES",
    "gv_layer",
    "is_gv_mark",
    "sign",
    "without_gv_marks",
]

#: The `/PieceInfo` key every overlay GV Review draws carries.
PIECE_KEY: Final = "/GVReview"
#: The layer GV Review's marks are drawn in.
LAYER_NAME: Final = "GV Review markup"
#: The resource names GV's overlays used before they were signed: still GV's marks.
RESERVED_NAMES: Final = frozenset({"/GVCloudDrawing", "/GVRedline939"})
#: A fixed date, so the same marks always make the same bytes.
_SIGNED_ON: Final = "D:20261019000000Z"


def sign(pdf: pikepdf.Pdf, form: Any) -> None:
    """Sign a Form XObject as GV Review's marks (`/PieceInfo /GVReview`)."""
    form["/PieceInfo"] = pikepdf.Dictionary(
        {
            PIECE_KEY: pikepdf.Dictionary(
                LastModified=pikepdf.String(_SIGNED_ON),
                Private=pikepdf.Dictionary(Kind=pikepdf.Name("/Markup"), Version=1),
            )
        }
    )


def gv_layer(pdf: pikepdf.Pdf) -> Any:
    """The document's `GV Review markup` layer (made, listed and switched on when missing)."""
    root = pdf.Root
    properties = root.get("/OCProperties")
    if not isinstance(properties, pikepdf.Dictionary):
        properties = pikepdf.Dictionary(OCGs=pikepdf.Array(), D=pikepdf.Dictionary())
        root["/OCProperties"] = properties
    groups = properties.get("/OCGs")
    if not isinstance(groups, pikepdf.Array):
        groups = pikepdf.Array()
        properties["/OCGs"] = groups
    for group in groups:
        if isinstance(group, pikepdf.Dictionary) and str(group.get("/Name", "")) == LAYER_NAME:
            return group
    layer = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String(LAYER_NAME))
    )
    groups.append(layer)
    default = properties.get("/D")
    if not isinstance(default, pikepdf.Dictionary):
        default = pikepdf.Dictionary()
        properties["/D"] = default
    for key in ("/ON", "/Order"):
        listed = default.get(key)
        if not isinstance(listed, pikepdf.Array):
            listed = pikepdf.Array()
            default[key] = listed
        listed.append(layer)
    return layer


def is_gv_mark(name: str, xobject: Any) -> bool:
    """Whether a page's XObject is an overlay GV Review drew: signed, or by its reserved name."""
    if not isinstance(xobject, pikepdf.Stream) or xobject.get("/Subtype") != pikepdf.Name.Form:
        return False
    piece = xobject.get("/PieceInfo")
    if isinstance(piece, pikepdf.Dictionary) and PIECE_KEY in piece:
        return True
    return name in RESERVED_NAMES


def without_gv_marks(pdf: pikepdf.Pdf, page: pikepdf.Page) -> int:
    """Empty every GV Review overlay drawn on `page` (in this copy only): what is left is the page
    as the vendor drew it. Returns how many were emptied."""
    resources = page.obj.get("/Resources")
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    if not isinstance(xobjects, pikepdf.Dictionary):
        return 0
    emptied = 0
    for name in list(xobjects.keys()):
        if is_gv_mark(str(name), xobjects[name]):
            blank = pdf.make_stream(b"")
            blank["/Type"] = pikepdf.Name.XObject
            blank["/Subtype"] = pikepdf.Name.Form
            blank["/BBox"] = xobjects[name].get("/BBox", pikepdf.Array([0, 0, 1, 1]))
            xobjects[name] = blank
            emptied += 1
    return emptied
