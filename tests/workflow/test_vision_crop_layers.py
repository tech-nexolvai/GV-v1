"""The crop a vision reader is sent holds the vendor's drawing and none of the reviewer's notes (#742).

On the 17-page client set, 186 of 3,613 planned vision crops overlapped a reviewer note, and each was
cut from a full-page render with the note painted in. A reader shown the reviewer's `46 1/2"` over the
vendor's label can return it as the vendor's reading. These tests go through `DatabaseStages`: a real
stamp, a real note over it, and the bytes the reader actually receives.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy.orm import Session

from evidence.crop import decode_rgb_png
from extraction.rasterise import render_page
from extraction.reader import read_pages
from storage.local import LocalStore
from tests.extraction.test_annotations import _appearance, _pdf, _stamp
from tests.workflow.test_stacked_fraction_route import (
    DIMENSION_LINE,
    _requests_for,
    session,
    store,
)

__all__ = ["session", "store"]  # the fixtures, re-exported so pytest finds them here

pytest_plugins = ("tests.app.postgres_fixture",)

#: A two-digit label beside a dimension line, and a reviewer's solid red note laid over the label.
#: Appearance space is page space plus (50, 450), so the label is at page x 60..68, y 70..75.5 and the
#: note covers page 55..85 x 65..90.
REVIEWED_SHEET = _pdf(
    annotations=[
        _stamp(appearance_object=7),
        (
            b"<< /Type /Annot /Subtype /FreeText /Rect [55 65 85 90] /Contents (46 1/2) "
            b"/DA (/Helv 10 Tf 1 0 0 rg) /AP << /N 8 0 R >> >>"
        ),
    ],
    extra_objects=[
        _appearance(
            DIMENSION_LINE
            + b"0.2 w 110 520 m 113.6 525.5 l 110 525.5 l S\n"
            + b"114.5 520 m 118.1 525.5 l 114.5 525.5 l S\n"
        ),
        _appearance(b"1 0 0 rg 0 0 30 25 re f\n", bbox=b"[0 0 30 25]", matrix=b"[1 0 0 1 0 0]"),
    ],
)


def _red_pixels(png: bytes) -> int:
    width, height, rgb = decode_rgb_png(png)
    return sum(
        1
        for offset in range(0, width * height * 3, 3)
        if rgb[offset] > 200 and rgb[offset + 1] < 60 and rgb[offset + 2] < 60
    )


def test_the_vision_reader_is_never_shown_the_reviewer_note(
    session: Session, store: LocalStore
) -> None:
    requests = _requests_for(session, store, REVIEWED_SHEET)

    assert requests, "no region reached the vision reader, so this test proves nothing"
    assert [_red_pixels(request.crop) for request in requests] == [0] * len(requests)


def test_the_note_is_where_the_crop_would_show_it() -> None:
    """The control: rendered with both layers, the page does have red where the crops are cut, so the
    test above is not passing because the note missed the label."""
    digest = hashlib.sha256(read_pages(REVIEWED_SHEET)[0].content).hexdigest()
    page = render_page(
        REVIEWED_SHEET,
        0,
        document_version_id=uuid4(),
        page_content_hash=digest,
        dpi=150,
        maximum_pixels=10_000_000,
        vendor_only=False,
    )
    # page x 60..68, y 70..75.5 pt at 150 dpi on a 300 pt page: image x 125..142, y 469..480
    red = 0
    for y in range(469, 480):
        for x in range(125, 142):
            offset = (y * page.width_px + x) * 3
            red += page.rgb_bytes[offset] > 200 and page.rgb_bytes[offset + 1] < 60
    assert red > 0
