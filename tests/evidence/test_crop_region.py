"""Cutting one region out of a stored full-page picture without decoding all of it (#952)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from evidence.crop import (
    EVIDENCE_CONTEXT_MARGIN_PT,
    BoxCropSpec,
    RenderedPage,
    crop_pixel_box,
    cut_png_region,
    decode_rgb_png,
    encode_png,
    pixel_box,
    png_size,
)

WIDTH, HEIGHT = 40, 30


def _pixels() -> bytes:
    """Every pixel distinct, so a region shifted by one pixel cannot pass for the right one."""
    return b"".join(bytes((x, y, (x * 7 + y) % 256)) for y in range(HEIGHT) for x in range(WIDTH))


def _expected(box: tuple[int, int, int, int]) -> bytes:
    left, top, right, bottom = box
    rgb = _pixels()
    return b"".join(
        rgb[(y * WIDTH + left) * 3 : (y * WIDTH + right) * 3] for y in range(top, bottom)
    )


@pytest.mark.parametrize(
    "box", [(0, 0, WIDTH, HEIGHT), (3, 4, 17, 9), (39, 29, 40, 30), (0, 29, 40, 30)]
)
def test_the_region_is_exactly_those_pixels(box: tuple[int, int, int, int]) -> None:
    cut = cut_png_region(encode_png(WIDTH, HEIGHT, _pixels()), box)

    width, height, rgb = decode_rgb_png(cut)
    assert (width, height) == (box[2] - box[0], box[3] - box[1])
    assert rgb == _expected(box)


@pytest.mark.parametrize(
    "box", [(0, 0, 41, 30), (-1, 0, 5, 5), (5, 5, 5, 6), (0, 0, 40, 31), (10, 10, 5, 20)]
)
def test_a_region_outside_the_picture_is_refused_not_clamped(
    box: tuple[int, int, int, int],
) -> None:
    with pytest.raises(ValueError, match="does not lie inside"):
        cut_png_region(encode_png(WIDTH, HEIGHT, _pixels()), box)


def test_only_this_modules_pngs_are_cut() -> None:
    with pytest.raises(ValueError):
        cut_png_region(b"not a png", (0, 0, 1, 1))


def test_the_stated_size_is_read_without_decoding() -> None:
    assert png_size(encode_png(WIDTH, HEIGHT, _pixels())) == (WIDTH, HEIGHT)


def test_a_box_on_a_page_known_by_size_is_the_box_a_crop_is_cut_by() -> None:
    """`pixel_box` and `crop_pixel_box` are one computation, so both views cut the same paper."""
    version = uuid4()
    spec = BoxCropSpec(
        document_version_id=version,
        page=0,
        left=Decimal("0.2"),
        top=Decimal("0.3"),
        right=Decimal("0.45"),
        bottom=Decimal("0.5"),
        context_margin_pt=EVIDENCE_CONTEXT_MARGIN_PT,
        dpi=72,
    )
    page = RenderedPage(
        document_version_id=version,
        page_index=0,
        page_content_hash="0" * 64,
        rotation=0,
        render_failed=False,
        width_px=WIDTH,
        height_px=HEIGHT,
        dpi=72,
        rgb_bytes=_pixels(),
    )

    assert pixel_box(WIDTH, HEIGHT, spec) == crop_pixel_box(page, spec) == (0, 0, 27, 24)
