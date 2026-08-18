"""What the manifest knows about a package's pages — and what it refuses to pretend it knows.

§9 of `docs/DESIGN_EXTRACTION.md` asks for the refusal paths to be asserted, not only the happy
ones, and here the refusals *are* the feature. A manifest that drops an unreadable page, renumbers
the pages after it, or guesses at a page's type produces a review that looks complete; nothing
downstream can tell, because every later stage only ever sees what the manifest handed it.

The fixtures are deliberately synthetic. `data/drawings/` is empty, and a fixture built to look like
a real drawing set would be encoding today's guess about real drawing sets as ground truth. These
check the logic; characterisation tests come with the drawings.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from extraction.manifest import PageManifest, PageRecord, RawPage, build_manifest
from vocabulary.page_types import PageType

DOCUMENT = uuid4()

#: Stated by these tests, never by the code under test. What separates a page carrying a text layer
#: from a scan with a stamp on it is empirical, and nothing in `data/drawings/` can say yet.
MINIMUM_VECTOR_CHARACTERS = 20

LETTER_WIDTH = Decimal("612.00")
LETTER_HEIGHT = Decimal("792.00")


def _raw(
    index: int = 0,
    *,
    content: bytes | None = None,
    rotation: int = 0,
    characters: int = 500,
    unreadable_reason: str | None = None,
    width_pt: Decimal = LETTER_WIDTH,
    height_pt: Decimal = LETTER_HEIGHT,
) -> RawPage:
    return RawPage(
        index=index,
        content=f"page {index}".encode() if content is None else content,
        width_pt=width_pt,
        height_pt=height_pt,
        rotation=rotation,
        vector_character_count=characters,
        unreadable_reason=unreadable_reason,
    )


def _manifest(*pages: RawPage) -> PageManifest:
    return build_manifest(
        pages or (_raw(0),),
        DOCUMENT,
        minimum_vector_characters=MINIMUM_VECTOR_CHARACTERS,
    )


def _record(index: int = 0, **overrides: object) -> PageRecord:
    record = PageRecord(
        index=index,
        content_hash=hashlib.sha256(f"page {index}".encode()).hexdigest(),
        width_pt=LETTER_WIDTH,
        height_pt=LETTER_HEIGHT,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
    )
    return dataclasses.replace(record, **overrides) if overrides else record


# ---------------------------------------------------------------------------
# A page that could not be read is recorded, never dropped
# ---------------------------------------------------------------------------


def test_a_page_that_could_not_be_read_is_kept_and_marked() -> None:
    """**The failure this module exists to prevent.** A package that silently loses page 2 produces
    a review that looks complete, and the pages after it are renumbered on the way out."""
    manifest = _manifest(
        _raw(0),
        _raw(
            1, content=b"", characters=0, unreadable_reason="the page object could not be decoded"
        ),
        _raw(2),
    )

    assert len(manifest.pages) == 3, "the unreadable page is still in the manifest"
    assert manifest.pages[1].render_failed is True
    assert manifest.pages[1].index == 1, "the pages after it keep their numbers"
    assert manifest.pages[2].index == 2
    assert [page.render_failed for page in manifest.pages] == [False, True, False]


def test_an_unreadable_page_is_never_reported_as_carrying_a_text_layer() -> None:
    """It would route the page to the vector lane, which finds nothing, while the manifest says the
    page was read. Both halves of that are wrong and neither is visible downstream."""
    manifest = _manifest(_raw(0, content=b"", characters=0, unreadable_reason="render failed"))

    assert manifest.pages[0].has_vector_text is False
    assert manifest.pages[0].render_failed is True


def test_a_reader_cannot_claim_characters_from_a_page_it_could_not_read() -> None:
    """Two observations that contradict each other. Accepting both would let the contradiction
    through as a page that is simultaneously unread and full of text."""
    with pytest.raises(ValueError, match="cannot also report characters"):
        RawPage(
            index=0,
            content=b"",
            width_pt=LETTER_WIDTH,
            height_pt=LETTER_HEIGHT,
            rotation=0,
            vector_character_count=40,
            unreadable_reason="render failed",
        )


def test_a_gap_in_the_page_numbers_is_refused() -> None:
    """A reader that lost a page cannot make the loss invisible by handing over a shorter list."""
    with pytest.raises(ValueError, match="none missing"):
        _manifest(_raw(0), _raw(2))


def test_a_repeated_page_number_is_refused() -> None:
    with pytest.raises(ValueError, match="none missing"):
        _manifest(_raw(0), _raw(0))


def test_pages_out_of_order_are_refused_rather_than_quietly_sorted() -> None:
    """Sorting them would answer a question nobody asked. A reader whose page order is wrong has a
    problem worth seeing, and reordering here would hide it behind a manifest that looks fine."""
    with pytest.raises(ValueError, match="in order"):
        _manifest(_raw(1), _raw(0))


def test_a_manifest_with_no_pages_is_refused() -> None:
    """Every later stage fans out over the manifest. Fanning out over nothing produces a review that
    looks complete and read nothing at all."""
    with pytest.raises(ValueError, match="no pages"):
        PageManifest(document_version_id=DOCUMENT, pages=())


# ---------------------------------------------------------------------------
# The content hash: proof a re-run read the same page
# ---------------------------------------------------------------------------


def test_the_content_hash_is_the_sha256_of_what_was_read() -> None:
    manifest = _manifest(_raw(0, content=b"%PDF page one"))

    assert manifest.pages[0].content_hash == hashlib.sha256(b"%PDF page one").hexdigest()


def test_the_same_bytes_hash_the_same_and_different_bytes_do_not() -> None:
    """The point of the hash: a re-run can prove it read the same page rather than assume it."""
    first = _manifest(_raw(0, content=b"identical"))
    again = _manifest(_raw(0, content=b"identical"))
    altered = _manifest(_raw(0, content=b"identical."))

    assert first.pages[0].content_hash == again.pages[0].content_hash
    assert altered.pages[0].content_hash != first.pages[0].content_hash


def test_a_content_hash_that_is_not_a_sha256_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        _record(content_hash="not-a-digest")


def test_an_uppercase_digest_is_refused_so_two_spellings_never_look_like_two_pages() -> None:
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        _record(content_hash=hashlib.sha256(b"x").hexdigest().upper())


# ---------------------------------------------------------------------------
# The vector-text threshold is the caller's, and it decides the extraction lane
# ---------------------------------------------------------------------------


def test_the_minimum_vector_character_count_is_a_required_keyword_argument() -> None:
    """The number separating "this page has a text layer" from "this page needs OCR" is empirical,
    and `data/drawings/` is empty. A default would be today's guess shipped as ground truth, and a
    positional argument could be supplied by accident."""
    parameter = inspect.signature(build_manifest).parameters["minimum_vector_characters"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_a_scanned_page_has_no_vector_text_and_a_vector_page_does() -> None:
    """This is the whole decision: B2.2 (vector) or B2.4 (OCR). A scanned sheet routed to the vector
    lane yields no dimensions at all, and the manifest still says the page was read."""
    manifest = _manifest(_raw(0, characters=0), _raw(1, characters=4_000))

    assert manifest.pages[0].has_vector_text is False
    assert manifest.pages[1].has_vector_text is True


def test_the_threshold_actually_changes_the_answer() -> None:
    """Otherwise it is a parameter in name only. A scan carrying a plot stamp is exactly the page
    that sits between the two answers, and which one it gets is the caller's to state."""
    stamped_scan = (_raw(0, characters=12),)

    lenient = build_manifest(stamped_scan, DOCUMENT, minimum_vector_characters=5)
    strict = build_manifest(stamped_scan, DOCUMENT, minimum_vector_characters=50)

    assert lenient.pages[0].has_vector_text is True
    assert strict.pages[0].has_vector_text is False


@pytest.mark.parametrize("value", [1.0, Decimal(20), "20"])
def test_a_threshold_that_is_not_a_whole_number_of_characters_is_refused(value: object) -> None:
    """A float or Decimal threshold makes the boundary depend on rounding, and this boundary decides
    whether a page is read at all. A count of characters is exact by construction, which is also why
    it has no NaN case to guard."""
    with pytest.raises(TypeError, match="must be an int"):
        build_manifest((_raw(0),), DOCUMENT, minimum_vector_characters=value)  # type: ignore[arg-type]


def test_a_boolean_threshold_is_refused() -> None:
    """`True == 1` in Python, so a stray flag would silently become a threshold of one character."""
    with pytest.raises(TypeError, match="must be an int"):
        build_manifest((_raw(0),), DOCUMENT, minimum_vector_characters=True)


def test_a_threshold_of_zero_is_refused() -> None:
    """Zero is not a lower threshold, it is no threshold: it declares that a page with no
    extractable text has a text layer, sending every scanned sheet down the vector lane."""
    with pytest.raises(ValueError, match="at least 1"):
        build_manifest((_raw(0),), DOCUMENT, minimum_vector_characters=0)


def test_a_negative_threshold_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        build_manifest((_raw(0),), DOCUMENT, minimum_vector_characters=-1)


# ---------------------------------------------------------------------------
# Rotation is recorded as printed, never normalised away
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_the_rotation_is_carried_through_exactly(rotation: int) -> None:
    """A crop taken without it lands on the wrong part of the sheet, and everything looks right on
    unrotated test pages — which is why 90/180/270 are required cases, not optional ones."""
    assert _manifest(_raw(0, rotation=rotation)).pages[0].rotation == rotation


def test_a_rotation_the_pdf_specification_does_not_allow_is_refused() -> None:
    with pytest.raises(ValueError, match="0, 90, 180 or 270"):
        _raw(0, rotation=45)


# ---------------------------------------------------------------------------
# 0-based inside, 1-based for a reviewer — both named
# ---------------------------------------------------------------------------


def test_the_internal_index_is_zero_based_and_the_displayed_one_is_not() -> None:
    """The off-by-one that puts a highlight box on the wrong sheet and then reads as a real
    disagreement about the drawing."""
    manifest = _manifest(_raw(0), _raw(1), _raw(2))

    assert [page.index for page in manifest.pages] == [0, 1, 2]
    assert [page.display_index for page in manifest.pages] == [1, 2, 3]
    assert manifest.display_index(0) == 1
    assert manifest.display_index(2) == 3


def test_the_display_index_of_a_page_that_does_not_exist_raises() -> None:
    """Returning `index + 1` would put a real-looking page number on a citation pointing nowhere."""
    manifest = _manifest(_raw(0), _raw(1))

    with pytest.raises(IndexError, match="no page at index"):
        manifest.display_index(2)


def test_a_negative_index_is_refused_rather_than_counted_from_the_end() -> None:
    """Python would happily answer for `pages[-1]`, and the answer would be about a different page
    from the one that was asked about."""
    with pytest.raises(ValueError, match="zero or greater"):
        _manifest(_raw(0), _raw(1)).display_index(-1)


# ---------------------------------------------------------------------------
# Classification fails to unknown, not to a guess
# ---------------------------------------------------------------------------


def test_building_a_manifest_never_classifies_a_page() -> None:
    """`docs/DESIGN_EXTRACTION.md` §3.2: `None` is a real outcome. The manifest records what a page
    is *made of*, never what it *is* — deciding that is B6.2's job, and a manifest that guessed
    would put a countertop width on a cabinet elevation with no tolerance check able to catch it."""
    manifest = _manifest(_raw(0), _raw(1))

    assert all(page.page_type is None for page in manifest.pages)


def test_an_unclassified_page_still_carries_everything_else() -> None:
    """It is still extracted. `None` excludes it from `scope: same_view`, not from the package."""
    page = _manifest(_raw(0, characters=900)).pages[0]

    assert page.page_type is None
    assert page.has_vector_text is True
    assert page.render_failed is False


def test_a_page_type_outside_the_vocabulary_is_refused() -> None:
    """A free string can be written, stored, matched against nothing and never noticed."""
    with pytest.raises(TypeError, match="PageType vocabulary"):
        _record(page_type="elevation")


def test_a_classification_is_added_by_building_a_new_record_not_by_mutating_one() -> None:
    """`AGENTS.md` §2.7: a rerun creates a new version, never an in-place edit. This is how #161 and
    #162 fill in what they learn."""
    original = _record()
    classified = dataclasses.replace(original, page_type=PageType.ELEVATION, sheet_number="A-101")

    assert original.page_type is None, "the original is untouched"
    assert classified.page_type is PageType.ELEVATION
    assert classified.sheet_number == "A-101"
    assert classified.content_hash == original.content_hash


def test_the_page_type_vocabulary_has_exactly_one_meaning_across_the_tree() -> None:
    """`app/models/document.py` carries its own copy for a database check constraint. Two enums with
    overlapping members is how a value quietly means two things, so the duplication is held in step
    by this test until the shipped model can converge on the shared vocabulary."""
    from app.models.document import PageType as PersistedPageType

    assert {member.value for member in PageType} == {
        member.value for member in PersistedPageType
    }, "the manifest vocabulary and the pages table disagree about what a page can be"


# ---------------------------------------------------------------------------
# Frozen, and built once
# ---------------------------------------------------------------------------


def test_a_manifest_cannot_be_edited_after_it_is_built() -> None:
    manifest = _manifest(_raw(0))

    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.pages = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.pages[0].render_failed = True  # type: ignore[misc]


def test_pages_must_be_a_tuple_so_the_manifest_cannot_grow_afterwards() -> None:
    with pytest.raises(TypeError, match="must be a tuple"):
        PageManifest(document_version_id=DOCUMENT, pages=[_record()])  # type: ignore[arg-type]


def test_the_document_version_must_be_a_uuid() -> None:
    """A manifest that is not pinned to one immutable upload describes no particular document."""
    with pytest.raises(TypeError, match="must be a UUID"):
        PageManifest(document_version_id="not-a-uuid", pages=(_record(),))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Page geometry is exact, or it is refused
# ---------------------------------------------------------------------------


def test_a_float_page_size_is_refused() -> None:
    """ADR-0001. A page size that arrived through binary floating point has already lost exactness,
    and everything measured against it inherits the loss."""
    with pytest.raises(TypeError, match="never a float"):
        _raw(0, width_pt=612.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_page_size_that_is_not_a_finite_number_is_refused(value: str) -> None:
    """`Decimal("Infinity") > 0` is `True`, so an infinite width sails through a plain positivity
    check; `Decimal("NaN") > 0` is `False`, so a NaN width is reported as "not positive" — a refusal
    naming a cause that is not the real one. Neither is a page size."""
    with pytest.raises(ValueError, match="finite"):
        _raw(0, width_pt=Decimal(value))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_page_with_no_size_is_not_a_page(value: str) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        _raw(0, height_pt=Decimal(value))


def test_a_blank_sheet_number_is_refused_because_blank_is_not_a_reading() -> None:
    """An empty string is indistinguishable from "nobody read it", and the difference between those
    two is the difference between a fact and a gap."""
    with pytest.raises(ValueError, match="non-empty text or None"):
        _record(sheet_number="   ")


# ---------------------------------------------------------------------------
# Serialisation round-trips exactly — it crosses a workflow boundary (B6.4)
# ---------------------------------------------------------------------------


def test_a_manifest_round_trips_through_json_unchanged() -> None:
    manifest = _manifest(
        _raw(0, rotation=90, characters=0),
        _raw(1, content=b"", characters=0, unreadable_reason="render failed"),
        _raw(2, characters=800),
    )

    restored = PageManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))

    assert restored == manifest


def test_the_decimal_page_size_survives_the_round_trip_digit_for_digit() -> None:
    """`Decimal("612.00") == Decimal("612")` is `True`, so equality alone would not notice the
    trailing zeros being dropped. A page recorded to two decimal places must come back with them,
    because the manifest's job is to say precisely what was read."""
    original = _manifest(_raw(0, width_pt=Decimal("612.00"), height_pt=Decimal("792.0")))
    restored = PageManifest.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.pages[0].width_pt.as_tuple() == Decimal("612.00").as_tuple()
    assert restored.pages[0].height_pt.as_tuple() == Decimal("792.0").as_tuple()


def test_a_classified_page_round_trips_with_its_type_and_sheet_number() -> None:
    manifest = PageManifest(
        document_version_id=DOCUMENT,
        pages=(_record(page_type=PageType.SCHEDULE, sheet_number="A-101", sheet_title="Schedule"),),
    )

    restored = PageManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))

    assert restored == manifest
    assert restored.pages[0].page_type is PageType.SCHEDULE


def test_a_serialised_page_size_written_as_a_json_number_is_refused() -> None:
    """**The reason dimensions are serialised as strings.** A JSON number has already been through a
    float parser by the time it is read back, so the exact page size is gone and only an
    approximation of it arrived. Accepting it would launder that approximation into the manifest."""
    data = _manifest(_raw(0)).to_dict()
    pages = data["pages"]
    assert isinstance(pages, list)
    pages[0]["width_pt"] = 612.0

    with pytest.raises(TypeError, match="not a JSON number"):
        PageManifest.from_dict(data)


def test_a_serialised_record_missing_a_field_is_refused() -> None:
    data = _manifest(_raw(0)).to_dict()
    pages = data["pages"]
    assert isinstance(pages, list)
    del pages[0]["content_hash"]

    with pytest.raises(ValueError, match="missing: content_hash"):
        PageManifest.from_dict(data)


def test_a_serialised_record_with_an_unknown_field_is_refused() -> None:
    """It was written by a different version of this type. Reading it as if it were this one would
    silently drop whatever that field meant — and a manifest is exactly the thing whose meaning must
    not change quietly as it crosses a workflow boundary."""
    data = _manifest(_raw(0)).to_dict()
    pages = data["pages"]
    assert isinstance(pages, list)
    pages[0]["confidence"] = 0.91

    with pytest.raises(ValueError, match="does not understand"):
        PageManifest.from_dict(data)


def test_a_serialised_manifest_with_a_page_missing_is_refused_on_the_way_back_in() -> None:
    """The guard holds at both ends. A page lost in transit is the same failure as a page lost in
    the reader, and it must not become invisible by being written down."""
    data = _manifest(_raw(0), _raw(1), _raw(2)).to_dict()
    pages = data["pages"]
    assert isinstance(pages, list)
    del pages[1]

    with pytest.raises(ValueError, match="none missing"):
        PageManifest.from_dict(data)


def test_a_serialised_page_type_outside_the_vocabulary_is_refused() -> None:
    data = _manifest(_raw(0)).to_dict()
    pages = data["pages"]
    assert isinstance(pages, list)
    pages[0]["page_type"] = "floor_plan"

    with pytest.raises(ValueError, match="floor_plan"):
        PageManifest.from_dict(data)


def test_a_serialised_document_version_that_is_not_a_uuid_is_refused() -> None:
    data = _manifest(_raw(0)).to_dict()
    data["document_version_id"] = "the-shop-drawing"

    with pytest.raises(ValueError):
        PageManifest.from_dict(data)


def test_the_document_version_survives_the_round_trip() -> None:
    manifest = _manifest(_raw(0))
    restored = PageManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))

    assert restored.document_version_id == DOCUMENT
    assert isinstance(restored.document_version_id, UUID)


# ---------------------------------------------------------------------------
# The builder refuses input it cannot read as page observations
# ---------------------------------------------------------------------------


def test_something_other_than_a_page_observation_is_refused() -> None:
    with pytest.raises(TypeError, match="RawPage"):
        build_manifest(({"index": 0},), DOCUMENT, minimum_vector_characters=20)  # type: ignore[arg-type]


def test_a_string_is_not_a_sequence_of_pages() -> None:
    """A `str` is a sequence, and iterating one silently yields characters rather than pages."""
    with pytest.raises(TypeError, match="RawPage"):
        build_manifest("pages", DOCUMENT, minimum_vector_characters=20)  # type: ignore[arg-type]
