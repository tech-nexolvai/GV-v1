"""The redline is the deliverable, so its failure modes are asserted before its happy path.

Three of these tests matter more than the rest.

**A finding that could not be marked is still in the report.** `DESIGN_PRODUCT.md` §3.2 says a
result that renders as blank space recreates the failure the whole abstention design exists to
prevent — silence reading as approval. Four different ways of failing to place a finding are
exercised here, and each one asserts the finding is *in the output text*, not merely that nothing
crashed.

**The original page survives.** Asserted by extracting the source text out of the finished redline.
A rasterising renderer would still produce a picture that looked correct, so nothing short of
reading the text back proves it.

**Rotation places correctly.** The expected PDF coordinates for 0/90/180/270 are worked out in the
test from the page geometry rather than by calling the transform, so the test would still fail if
the transform and this module drifted together.

The fixtures are deliberately plain rectangles on a small page. `data/drawings/` is empty, and a
fixture dressed up to look like a real elevation would encode today's guess about real elevations
as ground truth without testing anything more.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from evidence.coordinates import PageTransform, PdfPoint, StoredPoint
from evidence.polygon import Polygon
from reports.redline import (
    OUTCOME_STYLES,
    PageMismatchError,
    RedlinePackage,
    RedlinePage,
    ReportMode,
    VendorApprovalUnavailable,
    label_anchor,
    polygon_pdf_points,
    render_redline,
    text_angle,
)
from storage.local import LocalStore
from storage.store import StoredArtifact
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity
from verdict.trace import CalculationTrace, TracedOperand

DOCUMENT: Final = UUID("11111111-1111-1111-1111-111111111111")
REVISION: Final = UUID("22222222-2222-2222-2222-222222222222")

#: Deliberately not square, so an axis swapped in the rotation maths cannot pass unnoticed.
WIDTH: Final = Decimal(400)
HEIGHT: Final = Decimal(200)
BOX: Final = (Decimal(0), Decimal(0), WIDTH, HEIGHT)

ORIGINAL_TEXT: Final = "SHEET A-101 VENDOR ORIGINAL CONTENT"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _source_pdf(*rotations: int) -> bytes:
    """A PDF of plain pages carrying searchable text, each with the given `/Rotate`.

    The text is what proves the original survived the overlay, so every page carries its own
    distinguishable line rather than a shared one — a renderer that dropped a page and duplicated
    another would otherwise look identical.
    """
    writer = PdfWriter()
    for index, rotation in enumerate(rotations):
        page = PdfReader(_blank_page(f"{ORIGINAL_TEXT} {index}")).pages[0]
        if rotation:
            page.rotate(rotation)
        writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _blank_page(text: str) -> BytesIO:
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(float(WIDTH), float(HEIGHT)), invariant=1)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(20, 100, text)
    canvas.showPage()
    canvas.save()
    buffer.seek(0)
    return buffer


def _transform(rotation: int = 0) -> PageTransform:
    return PageTransform(dpi=72, rotation=rotation, media_box=BOX, crop_box=BOX)


def _package(*rotations: int) -> RedlinePackage:
    rotations = rotations or (0,)
    return RedlinePackage(
        package_revision_id=REVISION,
        source_pdf=_source_pdf(*rotations),
        pages=tuple(
            RedlinePage(
                document_version_id=DOCUMENT,
                page=index,
                source_index=index,
                transform=_transform(rotation),
            )
            for index, rotation in enumerate(rotations)
        ),
    )


def _polygon(page: int = 0, *, document: UUID | None = None) -> Polygon:
    """The same rectangle `_reference` encodes, as a `Polygon` the tests can place directly."""
    return Polygon(
        points=(
            StoredPoint(Decimal("0.10"), Decimal("0.20")),
            StoredPoint(Decimal("0.50"), Decimal("0.20")),
            StoredPoint(Decimal("0.50"), Decimal("0.40")),
            StoredPoint(Decimal("0.10"), Decimal("0.40")),
        ),
        space="stored",
        document_version_id=document or DOCUMENT,
        page=page,
    )


def _reference(
    page: int = 0,
    *,
    document: UUID | None = None,
    space: str = "stored",
    points: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """The exact JSON shape `evidence/gate.py` seals onto a verdict operand."""
    resolved = points or (
        ("0.10", "0.20"),
        ("0.50", "0.20"),
        ("0.50", "0.40"),
        ("0.10", "0.40"),
    )
    return json.dumps(
        {
            "document_version_id": str(document or DOCUMENT),
            "page": page,
            "polygon": [[x, y] for x, y in resolved],
            "space": space,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _trace() -> CalculationTrace:
    return CalculationTrace(
        operation="within_tolerance",
        operands=(
            TracedOperand(name="width", value=Fraction(6010), source="SHOP", evidence_ref=None),
        ),
        intermediates=(),
        comparison="6010 vs 6012",
        tolerance=None,
        arithmetic_unit=None,
        outcome=Outcome.FAIL,
        engine_version="test",
        operation_version="1",
    )


def _finding(
    rule_id: str = "CT-1",
    outcome: Outcome = Outcome.FAIL,
    *,
    refs: tuple[str, ...] = (),
    severity: Severity = Severity.CRITICAL,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        outcome=outcome,
        severity=severity,
        reason="the countertop is 2 mm wider than the cabinets beneath it",
        snapshot_id="snapshot-0000000000",
        engine_version="test",
        trace=_trace() if outcome in (Outcome.PASS, Outcome.FAIL) else None,
        evidence_refs=refs,
    )


def _render(
    package: RedlinePackage, findings: Sequence[object], store_root: Path
) -> StoredArtifact:
    return render_redline(
        package,
        findings,  # type: ignore[arg-type]
        ReportMode.INTERNAL,
        LocalStore(store_root),
    )


def _text(store_root: Path, key: str) -> str:
    """All the words in the finished redline, with the line breaks taken out.

    The summary pages wrap to the sheet width, so a sentence a test asserts on is split across
    lines at a column that depends on the page size. Collapsing whitespace asserts on what the
    report says rather than on where it happened to wrap.
    """
    joined = " ".join(text for text, _ in _read_back(store_root, key))
    return " ".join(joined.split())


def _read_back(store_root: Path, key: str) -> list[tuple[str, int]]:
    """The extracted text and `/Rotate` of every page, read out before the handle closes.

    pypdf reads objects lazily, so anything left until after the store closes the file raises
    instead of returning a page.
    """
    with LocalStore(store_root).get(key) as handle:
        return [(page.extract_text(), page.rotation % 360) for page in PdfReader(handle).pages]


def _content_stream(store_root: Path, key: str, index: int = 0) -> str:
    """The drawing operators of one finished page, as text."""
    with LocalStore(store_root).get(key) as handle:
        contents = PdfReader(handle).pages[index].get_contents()
        assert contents is not None
        return contents.get_data().decode("latin-1")


# ---------------------------------------------------------------------------
# The one that matters most: a finding with no polygon is still reported
# ---------------------------------------------------------------------------


def test_a_finding_with_no_evidence_reference_is_listed_rather_than_dropped(
    tmp_path: Path,
) -> None:
    """An abstention has no operands, so it has no geometry — and it is still the report's job.

    This is the normal case, not an edge case. `NOT_FOUND` means nothing was read, which means
    there is nothing on the sheet to point at. A redline that showed only what it could place would
    quietly drop exactly the findings a reviewer most needs to see.
    """
    finding = _finding("CT-9", Outcome.NOT_FOUND)
    artifact = _render(_package(), [finding], tmp_path)

    text = _text(tmp_path, artifact.key)
    assert "CT-9" in text
    assert "NOT_FOUND" in text
    assert "no evidence reference" in text


def test_a_finding_whose_reference_is_not_geometry_is_listed(tmp_path: Path) -> None:
    """`evidence_refs` is typed as free text and other producers put opaque ids in it."""
    finding = _finding("CT-3", Outcome.FAIL, refs=("p3:poly-1",))
    artifact = _render(_package(), [finding], tmp_path)

    text = _text(tmp_path, artifact.key)
    assert "CT-3" in text
    assert "not a recorded page and polygon" in text


def test_a_finding_whose_evidence_is_on_a_page_outside_this_redline_is_listed(
    tmp_path: Path,
) -> None:
    """Ordinary: a shop-drawing redline whose finding cites the architectural set.

    Listing it keeps the rest of the report. Raising would throw away every other finding over a
    situation that is expected, and drawing it on whichever page happened to be to hand would put
    a highlight on the wrong sheet — the failure the typed coordinate spaces exist to prevent.
    """
    other_document = uuid4()
    finding = _finding("CT-4", Outcome.FAIL, refs=(_reference(page=0, document=other_document),))
    artifact = _render(_package(), [finding], tmp_path)

    text = _text(tmp_path, artifact.key)
    assert "CT-4" in text
    assert "not one of the pages in this redline" in text
    assert str(other_document) in text


def test_a_finding_whose_recorded_polygon_is_degenerate_is_listed(tmp_path: Path) -> None:
    """A zero-area polygon cannot be drawn, and `Polygon` refuses to construct one."""
    flat = _reference(points=(("0.10", "0.20"), ("0.50", "0.20"), ("0.30", "0.20")))
    finding = _finding("CT-5", Outcome.FAIL, refs=(flat,))
    artifact = _render(_package(), [finding], tmp_path)

    text = _text(tmp_path, artifact.key)
    assert "CT-5" in text
    assert "not a usable page region" in text


def test_a_reference_in_another_coordinate_space_is_listed_not_placed(tmp_path: Path) -> None:
    """Placing an image-space polygon with a stored-space transform would look plausible and be
    wrong. Refusing to place it, and saying so, is the only safe answer."""
    finding = _finding("CT-6", Outcome.FAIL, refs=(_reference(space="image"),))
    artifact = _render(_package(), [finding], tmp_path)

    assert "'image' coordinate space" in _text(tmp_path, artifact.key)


def test_every_finding_reaches_the_report_one_way_or_the_other(tmp_path: Path) -> None:
    """The invariant the acceptance criterion is really about: nothing disappears."""
    findings = [
        _finding("CT-A", Outcome.FAIL, refs=(_reference(),)),
        _finding("CT-B", Outcome.NOT_FOUND),
        _finding("CT-C", Outcome.REVIEW_REQUIRED, refs=("opaque-ref",)),
        _finding("CT-D", Outcome.NO_APPLICABLE_RULE),
        _finding("CT-E", Outcome.PASS, refs=(_reference(),)),
    ]
    artifact = _render(_package(), findings, tmp_path)

    text = _text(tmp_path, artifact.key)
    for finding in findings:
        assert finding.rule_id in text, f"{finding.rule_id} vanished from the redline"


def test_a_partly_placed_finding_still_says_what_could_not_be_placed(tmp_path: Path) -> None:
    """Two of three operands shown is not the same as all three, and the report should not imply
    it is."""
    finding = _finding("CT-7", Outcome.FAIL, refs=(_reference(), "opaque-ref"))
    artifact = _render(_package(), [finding], tmp_path)

    assert "not a recorded page and polygon" in _text(tmp_path, artifact.key)


def test_the_summary_section_is_written_even_when_nothing_was_left_off(tmp_path: Path) -> None:
    """An absent section and an empty one look identical to a reader, and one of them is a lie."""
    finding = _finding("CT-8", Outcome.FAIL, refs=(_reference(),))
    artifact = _render(_package(), [finding], tmp_path)

    text = _text(tmp_path, artifact.key)
    assert "Findings not marked on any drawing page" in text
    assert "Every finding in this report is marked on a drawing page" in text


def test_no_applicable_rule_is_listed_under_what_was_not_checked(tmp_path: Path) -> None:
    """§3.2 names this section specifically: 'no rule covered it' is not 'it was fine'."""
    artifact = _render(_package(), [_finding("CT-0", Outcome.NO_APPLICABLE_RULE)], tmp_path)

    assert "What was not checked" in _text(tmp_path, artifact.key)


# ---------------------------------------------------------------------------
# The original drawing is a layer underneath, not a picture of itself
# ---------------------------------------------------------------------------


def test_the_original_page_content_survives_the_overlay(tmp_path: Path) -> None:
    """A rasterising renderer produces something that still looks right, so look at the text."""
    finding = _finding("CT-1", Outcome.FAIL, refs=(_reference(),))
    artifact = _render(_package(), [finding], tmp_path)

    extracted, _ = _read_back(tmp_path, artifact.key)[0]
    assert f"{ORIGINAL_TEXT} 0" in extracted
    assert "CT-1" in extracted


def test_every_source_page_is_carried_over_marked_or_not(tmp_path: Path) -> None:
    """A vendor must not receive a document that looks like the whole set and is not."""
    package = _package(0, 0, 0)
    finding = _finding("CT-1", Outcome.FAIL, refs=(_reference(page=1),))
    artifact = _render(package, [finding], tmp_path)

    pages = _read_back(tmp_path, artifact.key)
    assert len(pages) >= 4
    for index in range(3):
        assert f"{ORIGINAL_TEXT} {index}" in pages[index][0]


def test_page_rotation_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    package = _package(0, 90, 180, 270)
    artifact = _render(package, [_finding("CT-1", Outcome.FAIL, refs=(_reference(),))], tmp_path)

    pages = _read_back(tmp_path, artifact.key)
    assert [rotation for _, rotation in pages[:4]] == [0, 90, 180, 270]


# ---------------------------------------------------------------------------
# Placement — through B8.1's transforms, and correct on a rotated page
# ---------------------------------------------------------------------------


#: Where the stored point (0.25, 0.75) lands in PDF user space on a 400x200 crop box at 72 dpi,
#: worked out from the page geometry rather than by calling the transform. The four answers are
#: the four corners of a rectangle, so a rotation handled as its opposite cannot pass.
EXPECTED_PLACEMENT: Final = {
    0: (Decimal(100), Decimal(50)),
    90: (Decimal(300), Decimal(50)),
    180: (Decimal(300), Decimal(150)),
    270: (Decimal(100), Decimal(150)),
}


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_stored_point_lands_where_the_page_rotation_puts_it(rotation: int) -> None:
    polygon = Polygon(
        points=(
            StoredPoint(Decimal("0.25"), Decimal("0.75")),
            StoredPoint(Decimal("0.30"), Decimal("0.75")),
            StoredPoint(Decimal("0.30"), Decimal("0.80")),
        ),
        space="stored",
        document_version_id=DOCUMENT,
        page=0,
    )
    placed = polygon_pdf_points(polygon, _transform(rotation))

    expected_x, expected_y = EXPECTED_PLACEMENT[rotation]
    assert (placed[0].x, placed[0].y) == (expected_x, expected_y)


def test_the_four_rotations_do_not_place_a_polygon_in_the_same_spot() -> None:
    """Guards the test above: if the transform ignored rotation entirely, the parametrised
    assertions could only fail on three of four cases, and a table copied from a buggy run would
    look consistent. Four distinct answers is the property that matters."""
    assert len(set(EXPECTED_PLACEMENT.values())) == 4


def test_placement_stays_exact_and_never_becomes_a_float() -> None:
    placed = polygon_pdf_points(_polygon(), _transform())
    for point in placed:
        assert isinstance(point.x, Decimal)
        assert isinstance(point.y, Decimal)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_the_mark_reaches_the_page_at_the_coordinates_the_transform_gives(
    tmp_path: Path, rotation: int
) -> None:
    """Closes the loop between the arithmetic and the file.

    The tests above prove `polygon_pdf_points` is right; this proves those are the numbers that
    actually get written into the merged page, on a rotated sheet as well as an upright one. A
    correct transform whose output the renderer then re-derived would pass everything else here.
    """
    package = _package(rotation)
    finding = _finding("CT-1", Outcome.FAIL, refs=(_reference(),))
    artifact = _render(package, [finding], tmp_path)

    expected = polygon_pdf_points(_polygon(), _transform(rotation))
    stream = _content_stream(tmp_path, artifact.key)
    assert f"{expected[0].x} {expected[0].y} m" in stream
    for point in expected[1:]:
        assert f"{point.x} {point.y} l" in stream


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_label_text_is_turned_the_same_way_the_viewer_turns_the_page(rotation: int) -> None:
    """Right place, unreadable text is still a redline a reviewer cannot use."""
    assert text_angle(rotation) == rotation


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (Decimal(40), Decimal(120))),
        (90, (Decimal(40), Decimal(20))),
        (180, (Decimal(200), Decimal(20))),
        (270, (Decimal(200), Decimal(120))),
    ],
)
def test_the_label_starts_from_the_corner_that_reads_as_top_left(
    rotation: int, expected: tuple[Decimal, Decimal]
) -> None:
    """A different corner of the same box for each rotation, because "top left" is a property of
    the page as it is read, not of PDF user space. The box below spans x 40..200, y 20..120."""
    box = (
        PdfPoint(Decimal(40), Decimal(20)),
        PdfPoint(Decimal(200), Decimal(20)),
        PdfPoint(Decimal(200), Decimal(120)),
        PdfPoint(Decimal(40), Decimal(120)),
    )
    anchor = label_anchor(box, rotation)
    assert (anchor.x, anchor.y) == expected


def test_text_angle_refuses_a_rotation_a_pdf_cannot_carry() -> None:
    with pytest.raises(ValueError, match="0, 90, 180 or 270"):
        text_angle(45)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_rotated_page_still_renders_and_keeps_its_content(tmp_path: Path, rotation: int) -> None:
    package = _package(rotation)
    finding = _finding("CT-1", Outcome.FAIL, refs=(_reference(),))
    artifact = _render(package, [finding], tmp_path)

    extracted, actual = _read_back(tmp_path, artifact.key)[0]
    assert actual == rotation
    assert f"{ORIGINAL_TEXT} 0" in extracted


# ---------------------------------------------------------------------------
# Refusals — a mark in the wrong place is worse than no report
# ---------------------------------------------------------------------------


def test_a_transform_whose_rotation_disagrees_with_the_page_raises(tmp_path: Path) -> None:
    package = RedlinePackage(
        package_revision_id=REVISION,
        source_pdf=_source_pdf(90),
        pages=(
            RedlinePage(
                document_version_id=DOCUMENT, page=0, source_index=0, transform=_transform(0)
            ),
        ),
    )
    with pytest.raises(PageMismatchError, match="rotated 90 degrees"):
        _render(package, [_finding()], tmp_path)


def test_a_negative_rotate_value_is_read_as_the_turn_it_actually_is(tmp_path: Path) -> None:
    """`/Rotate -90` and `/Rotate 270` are the same page. Reporting a mismatch between them would
    refuse a perfectly good drawing and send someone hunting for a bug in the transform."""
    reader = PdfReader(BytesIO(_source_pdf(0)))
    writer = PdfWriter(clone_from=reader)
    writer.pages[0][NameObject("/Rotate")] = NumberObject(-90)
    out = BytesIO()
    writer.write(out)

    package = RedlinePackage(
        package_revision_id=REVISION,
        source_pdf=out.getvalue(),
        pages=(
            RedlinePage(
                document_version_id=DOCUMENT, page=0, source_index=0, transform=_transform(270)
            ),
        ),
    )
    artifact = _render(package, [_finding("CT-1", Outcome.FAIL, refs=(_reference(),))], tmp_path)
    assert "CT-1" in _text(tmp_path, artifact.key)


def test_a_transform_built_for_a_different_page_size_raises(tmp_path: Path) -> None:
    """Every mark on the page would be somewhere plausible and wrong. That is not a list entry."""
    other = (Decimal(0), Decimal(0), Decimal(600), Decimal(800))
    package = RedlinePackage(
        package_revision_id=REVISION,
        source_pdf=_source_pdf(0),
        pages=(
            RedlinePage(
                document_version_id=DOCUMENT,
                page=0,
                source_index=0,
                transform=PageTransform(dpi=72, rotation=0, media_box=other, crop_box=other),
            ),
        ),
    )
    with pytest.raises(PageMismatchError, match="describes a different page"):
        _render(package, [_finding()], tmp_path)


def test_a_page_mapped_past_the_end_of_the_pdf_raises(tmp_path: Path) -> None:
    package = RedlinePackage(
        package_revision_id=REVISION,
        source_pdf=_source_pdf(0),
        pages=(
            RedlinePage(
                document_version_id=DOCUMENT, page=4, source_index=4, transform=_transform()
            ),
        ),
    )
    with pytest.raises(PageMismatchError, match="page\\(s\\)"):
        _render(package, [_finding()], tmp_path)


def test_a_vendor_render_refuses_without_clearance(
    tmp_path: Path,
) -> None:
    """ADR-0010 forbids a computed dimension reaching a vendor without sign-off, and this module
    cannot establish sign-off: it holds no session and is given finding values with no row
    identity. So it refuses unless something that can establish it already has, which is what makes
    `reports.publication.render_vendor_redline` the only route to a vendor document rather than the
    recommended one. Its own gate is asserted in `tests/reports/test_vendor_redline.py`."""
    with pytest.raises(VendorApprovalUnavailable, match="ADR-0010"):
        render_redline(_package(), [_finding()], ReportMode.VENDOR, LocalStore(tmp_path))


def test_two_pages_claiming_the_same_page_number_are_rejected() -> None:
    with pytest.raises(ValueError, match="same document version and page"):
        RedlinePackage(
            package_revision_id=REVISION,
            source_pdf=_source_pdf(0, 0),
            pages=(
                RedlinePage(
                    document_version_id=DOCUMENT, page=0, source_index=0, transform=_transform()
                ),
                RedlinePage(
                    document_version_id=DOCUMENT, page=0, source_index=1, transform=_transform()
                ),
            ),
        )


def test_two_pages_claiming_the_same_place_in_the_pdf_are_rejected() -> None:
    with pytest.raises(ValueError, match="same position in the source PDF"):
        RedlinePackage(
            package_revision_id=REVISION,
            source_pdf=_source_pdf(0),
            pages=(
                RedlinePage(
                    document_version_id=DOCUMENT, page=0, source_index=0, transform=_transform()
                ),
                RedlinePage(
                    document_version_id=DOCUMENT, page=1, source_index=0, transform=_transform()
                ),
            ),
        )


def test_an_empty_package_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one page"):
        RedlinePackage(package_revision_id=REVISION, source_pdf=b"%PDF-1.4", pages=())


def test_a_package_with_no_bytes_is_rejected() -> None:
    with pytest.raises(ValueError, match="no drawing to mark up"):
        RedlinePackage(
            package_revision_id=REVISION,
            source_pdf=b"",
            pages=(
                RedlinePage(
                    document_version_id=DOCUMENT, page=0, source_index=0, transform=_transform()
                ),
            ),
        )


def test_a_negative_page_number_is_rejected() -> None:
    with pytest.raises(ValueError, match="zero or greater"):
        RedlinePage(document_version_id=DOCUMENT, page=-1, source_index=0, transform=_transform())


def test_a_page_without_a_transform_is_rejected() -> None:
    with pytest.raises(TypeError, match="must be a PageTransform"):
        RedlinePage(
            document_version_id=DOCUMENT,
            page=0,
            source_index=0,
            transform="0,0,400,200",  # type: ignore[arg-type]
        )


def test_findings_must_be_findings(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="only Finding values"):
        _render(_package(), ["CT-1: FAIL"], tmp_path)


def test_the_mode_must_be_a_report_mode(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="must be a ReportMode"):
        render_redline(_package(), [_finding()], "internal", LocalStore(tmp_path))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Policy: no AGPL, and every outcome is visible
# ---------------------------------------------------------------------------


def test_the_renderer_uses_the_bsd_pdf_libraries_and_no_agpl_one() -> None:
    """`AGENTS.md` §2.8. PyMuPDF is the most natural library for this job and the forbidden one;
    the licence guard covers the installed set, and this covers this module's own imports."""
    source = Path("reports/redline.py").read_text(encoding="utf-8")
    assert "from pypdf import" in source
    assert "from reportlab" in source
    for forbidden in ("fitz", "pymupdf", "PyMuPDF"):
        assert forbidden not in source


def test_every_outcome_has_a_visual_treatment() -> None:
    """§3.2: none of the outcomes is 'nothing'. A missing entry here renders as blank space."""
    assert set(OUTCOME_STYLES) == set(Outcome)


def test_the_outcomes_are_told_apart_by_more_than_their_label() -> None:
    """Distinct colours, so a reviewer scanning a sheet sees the difference before reading it."""
    assert len({style.stroke for style in OUTCOME_STYLES.values()}) == len(Outcome)


def test_a_failure_is_drawn_more_prominently_than_a_pass() -> None:
    assert OUTCOME_STYLES[Outcome.FAIL].line_width > OUTCOME_STYLES[Outcome.PASS].line_width
    assert OUTCOME_STYLES[Outcome.REVIEW_REQUIRED].line_width > (
        OUTCOME_STYLES[Outcome.PASS].line_width
    )


# ---------------------------------------------------------------------------
# The stored artifact
# ---------------------------------------------------------------------------


def test_the_redline_is_stored_as_a_content_addressed_pdf(tmp_path: Path) -> None:
    finding = _finding("CT-1", Outcome.FAIL, refs=(_reference(),))
    artifact = _render(_package(), [finding], tmp_path)

    assert artifact.key.startswith(f"redlines/{REVISION}/internal/")
    assert artifact.key.endswith(f"{artifact.sha256}.pdf")
    assert artifact.size > 0
    assert LocalStore(tmp_path).exists(artifact.key)


def test_the_same_inputs_produce_the_same_redline(tmp_path: Path) -> None:
    """`AGENTS.md` §2.7 wants artifacts reproducible. Without the invariant flag ReportLab stamps
    the creation time into the file, and a re-render becomes a different artifact for no reason a
    reader could see."""
    finding = _finding("CT-1", Outcome.FAIL, refs=(_reference(),))
    first = _render(_package(), [finding], tmp_path)
    second = _render(_package(), [finding], tmp_path)

    assert first.sha256 == second.sha256
    assert first.key == second.key
