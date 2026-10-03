"""The markup yardstick scores only under GV's boxes, and a box is never the vendor's value (#850).

Every note, reading and key entry here is invented: one or two pages, boxes in a frame running 0 to
100, numbers no client drawing was read for. The four the issue names are
`test_a_gv_number_recorded_as_a_vendor_reading_is_counted`,
`test_a_sealed_wrong_reading_is_counted`, `test_a_site_with_no_reading_is_not_found_not_correct`
and `test_a_box_never_stands_in_for_the_vendors_value`.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from eval.markup_yardstick import (
    Box,
    GvNote,
    KeyEntry,
    KeyStatus,
    PlacedFinding,
    Reading,
    ReadState,
    Seal,
    SiteKind,
    Yardstick,
    measure,
    render,
    site_of,
)


def _box(left: int, top: int, right: int, bottom: int) -> Box:
    return (Decimal(left), Decimal(top), Decimal(right), Decimal(bottom))


#: The one box most tests put GV's note in.
BOX = _box(10, 10, 20, 14)

#: GV's number in the note, and a different one the vendor drew.
GV_TEXT = '30 1/4"'
GV = Fraction(121, 4)
VENDOR = Fraction(57, 2)


def _note(text: str = GV_TEXT, box: Box = BOX, page: int = 0) -> GvNote:
    return GvNote(page_index=page, text=text, box=box)


def _reading(
    value: Fraction | None,
    *,
    reading_id: str = "r1",
    box: Box = BOX,
    page: int = 0,
    markup: bool = False,
    lane: str | None = None,
    status: str | None = None,
    seals: tuple[Seal, ...] = (),
) -> Reading:
    return Reading(
        reading_id=reading_id,
        page_index=page,
        box=box,
        route="extraction.annotations" if markup else "vision-reader",
        from_markup=markup,
        value=value,
        lane=lane,
        status=status,
        seals=seals,
    )


def _seal(value: Fraction, status: str = "CORROBORATED", role: str = "SHOP") -> Seal:
    return Seal(observation_id="o1", document_role=role, status=status, value=value)


def _key(
    value: Fraction, raw_text: str | None, *, item_id: str = "k1", box: Box = BOX, page: int = 0
) -> KeyEntry:
    return KeyEntry(
        key="synthetic-key",
        item_id=item_id,
        page_index=page,
        box=box,
        value=value,
        raw_text=raw_text,
    )


def _one(yardstick: Yardstick) -> dict[str, int]:
    assert len(yardstick.sites) == 1
    return yardstick.counts()


# ---------------------------------------------------------------------------
# What a box is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ('30 1/4"', SiteKind.SINGLE, GV),
        ('30-1/4"', SiteKind.SINGLE, GV),
        ('30 1/4" (2EQ)', SiteKind.SINGLE, GV),
        ("30 1/4", SiteKind.SINGLE, GV),
        ('4"+7"', SiteKind.SUM, None),
        ('4" + 7"(panel)', SiteKind.SUM, None),
        ('trim on site (4" panel)', SiteKind.NOTE, None),
    ],
)
def test_a_note_holding_a_dimension_is_a_site(
    text: str, kind: SiteKind, value: Fraction | None
) -> None:
    site = site_of(_note(text))

    assert site is not None
    assert site.kind is kind
    assert site.gv_value == value


@pytest.mark.parametrize("text", ["TAG-104", "(VIF)", "by others", "B", "trim on site"])
def test_a_note_holding_no_dimension_is_no_site(text: str) -> None:
    assert site_of(_note(text)) is None


def test_a_note_box_with_no_area_is_refused() -> None:
    with pytest.raises(ValueError, match="no area"):
        measure([_note(box=_box(10, 10, 10, 14))], [])


# ---------------------------------------------------------------------------
# The four the issue names
# ---------------------------------------------------------------------------


def test_a_gv_number_recorded_as_a_vendor_reading_is_counted() -> None:
    """A vendor reading under the box says exactly what the box says. With no key to show the box
    only rewrites the unit, it is GV's number taken as the vendor's."""
    counts = _one(measure([_note()], [_reading(GV)]))

    assert counts["gv_taken_readings"] == 1
    assert counts["gv_taken_sites"] == 1


def test_gvs_number_put_on_a_vendor_reading_by_an_observation_is_counted() -> None:
    """The reading said something else, and an observation that seals it says GV's number — a
    reviewer's correction to the box's value, say. The verdict would use the observation's."""
    sealed = _reading(VENDOR, seals=(_seal(GV, status="HUMAN_CONFIRMED"),))

    assert _one(measure([_note()], [sealed]))["gv_taken_readings"] == 1


def test_the_box_itself_held_by_an_observation_is_counted_at_any_kind_of_box() -> None:
    """#802: the reviewer's own note given a side. At a sum as much as at a single value."""
    box_as_shop = _reading(None, markup=True, seals=(_seal(Fraction(11)),))

    for text in (GV_TEXT, '4"+7"'):
        assert _one(measure([_note(text)], [box_as_shop]))["gv_taken_readings"] == 1


def test_the_box_recorded_with_no_side_is_neither_taken_nor_found() -> None:
    """The markup route records every note. That is GV's text kept as GV's, which is right; it is
    not a vendor reading, so it does not make the site found either."""
    result = measure([_note()], [_reading(GV, markup=True)]).sites[0]

    assert result.gv_taken == ()
    assert not result.found


def test_a_sealed_wrong_reading_is_counted() -> None:
    """The key gives the vendor's value; an observation able to enter a verdict holds another."""
    wrong = _reading(Fraction(29), seals=(_seal(Fraction(29)),))

    result = measure([_note()], [wrong], [_key(VENDOR, '28 1/2"')])
    counts = _one(result)

    assert counts["agreed_or_sealed_wrong_readings"] == 1
    assert counts["agreed_or_sealed_wrong_sites"] == 1
    assert result.sites[0].read is ReadState.WRONG


def test_an_agreed_wrong_reading_is_counted() -> None:
    """Two readers from different vendors gave the same wrong number: recorded at extraction as a
    raw candidate with the lane named."""
    agreed = _reading(Fraction(29), lane="SECOND_READER", status="RAW_CANDIDATE")

    counts = _one(measure([_note()], [agreed], [_key(VENDOR, '28 1/2"')]))

    assert counts["agreed_or_sealed_wrong_readings"] == 1


def test_a_site_with_no_reading_is_not_found_not_correct() -> None:
    """The key gives the vendor's value and nothing was read there. That is a miss, not a match."""
    result = measure([_note()], [], [_key(VENDOR, '28 1/2"')])
    counts = _one(result)

    assert not result.sites[0].found
    assert result.sites[0].read is ReadState.NOT_FOUND
    assert counts["not_found"] == 1
    assert counts["keyed_not_found"] == 1
    assert counts["read_right"] == 0
    assert counts["found"] == 0


def test_a_box_never_stands_in_for_the_vendors_value() -> None:
    """**The mutation this issue names.** No key: the only number at the site is GV's. A reading of
    it is GV's number taken, never a right reading, and the site has no vendor's value at all."""
    result = measure([_note()], [_reading(GV)])
    site = result.sites[0]
    counts = result.counts()

    assert site.key_status is KeyStatus.NONE
    assert site.vendor_value is None
    assert site.read is ReadState.UNKEYED
    assert counts["keyed"] == 0
    assert counts["read_right"] == 0
    assert counts["gv_taken_readings"] == 1


# ---------------------------------------------------------------------------
# Found, and read right
# ---------------------------------------------------------------------------


def test_a_reading_that_holds_no_value_does_not_make_a_site_found() -> None:
    assert not measure([_note()], [_reading(None)]).sites[0].found


def test_a_reading_of_the_vendors_value_is_right() -> None:
    result = measure([_note()], [_reading(VENDOR)], [_key(VENDOR, '28 1/2"')])

    assert result.sites[0].read is ReadState.RIGHT
    assert result.counts()["read_right"] == 1
    assert result.counts()["gv_taken_readings"] == 0


def test_one_right_and_one_wrong_reading_are_mixed() -> None:
    readings = [_reading(VENDOR), _reading(Fraction(29), reading_id="r2")]

    result = measure([_note()], readings, [_key(VENDOR, '28 1/2"')])

    assert result.sites[0].read is ReadState.MIXED


def test_a_wrong_reading_nobody_agreed_on_or_sealed_is_wrong_but_not_must_be_zero() -> None:
    """A lone misreading, a conflict, and a seal that may not enter a verdict: all wrong, and none
    of them evidence anybody trusted."""
    readings = [
        _reading(Fraction(29)),
        _reading(Fraction(31), reading_id="r2", lane="SECOND_READER", status="CONFLICTING"),
        _reading(
            Fraction(33), reading_id="r3", seals=(_seal(Fraction(33), status="RAW_CANDIDATE"),)
        ),
    ]

    result = measure([_note()], readings, [_key(VENDOR, '28 1/2"')])

    assert result.sites[0].read is ReadState.WRONG
    assert result.counts()["agreed_or_sealed_wrong_readings"] == 0


# ---------------------------------------------------------------------------
# Where the vendor's value comes from
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gv_text", "key_text"),
    [(GV_TEXT, GV_TEXT), (GV_TEXT, '30-1/4"'), ("30 1/4", GV_TEXT), (GV_TEXT, None)],
)
def test_a_key_that_repeats_the_box_is_refused(gv_text: str, key_text: str | None) -> None:
    """A person shown the box may read the box. GV's number in GV's unit — or with the inch mark a
    key requires added to a box that had none — cannot be told from that, so it is no vendor's
    value."""
    result = measure([_note(gv_text)], [_reading(GV)], [_key(GV, key_text)])
    site = result.sites[0]

    assert site.key_status is KeyStatus.REPEATS_BOX
    assert site.vendor_value is None
    assert result.counts()["key_repeats_box"] == 1
    assert result.counts()["read_right"] == 0
    assert result.counts()["gv_taken_readings"] == 1


@pytest.mark.parametrize("key_text", ["610 [24]", "2'-0\""])
def test_a_key_in_another_unit_shows_the_box_only_rewrites_the_unit(key_text: str) -> None:
    """The vendor's label states the number in millimetres or feet, and the box restates it in
    inches. GV's number is the vendor's here, so reading it is right and nothing was taken."""
    result = measure([_note('24"')], [_reading(Fraction(24))], [_key(Fraction(24), key_text)])
    counts = result.counts()

    assert result.sites[0].key_status is KeyStatus.UNIT_ONLY
    assert result.sites[0].read is ReadState.RIGHT
    assert counts["unit_only"] == 1
    assert counts["keyed"] == 1
    assert counts["gv_taken_readings"] == 0


def test_key_entries_that_disagree_give_no_vendors_value() -> None:
    keys = [_key(VENDOR, '28 1/2"'), _key(Fraction(29), '29"', item_id="k2")]

    result = measure([_note()], [_reading(VENDOR)], keys)

    assert result.sites[0].key_status is KeyStatus.DISAGREES
    assert result.sites[0].read is ReadState.UNKEYED


def test_a_key_entry_under_two_boxes_is_used_for_neither() -> None:
    notes = [_note(), _note('40"', box=_box(22, 10, 32, 14))]
    across = _key(VENDOR, '28 1/2"', box=_box(15, 10, 25, 14))

    result = measure(notes, [], [across])

    assert result.shared_key_entries == 1
    assert {site.key_status for site in result.sites} == {KeyStatus.NONE}


def test_a_key_entry_on_another_page_is_not_under_the_box() -> None:
    """A key entry on another page is not under the box, however its pixels line up."""
    result = measure([_note()], [], [_key(VENDOR, '28 1/2"', page=1)])

    assert result.sites[0].key_status is KeyStatus.NONE


# ---------------------------------------------------------------------------
# Sums and notes
# ---------------------------------------------------------------------------


def test_a_sum_is_counted_for_found_and_its_terms_are_never_gvs_number() -> None:
    """GV's terms can be the vendor's own numbers in another order, so a reading of one proves
    nothing. A sum is not one value, so no key scores it either; what was agreed there is unchecked.
    """
    agreed = _reading(Fraction(4), lane="SECOND_READER", status="RAW_CANDIDATE")

    result = measure([_note('4"+7"')], [agreed], [_key(Fraction(4), '4"')])
    site = result.sites[0]
    counts = result.counts()

    assert site.found
    assert site.key_status is KeyStatus.NOT_SCORED
    assert counts["gv_taken_readings"] == 0
    assert counts["agreed_or_sealed_wrong_readings"] == 0
    assert counts["agreed_or_sealed_unchecked_readings"] == 1


# ---------------------------------------------------------------------------
# Where "under the box" ends
# ---------------------------------------------------------------------------


def test_readings_off_the_box_are_not_at_the_site() -> None:
    elsewhere = [
        _reading(GV, reading_id="other-page", page=1),
        _reading(GV, reading_id="touching", box=_box(20, 10, 30, 14)),
        _reading(GV, reading_id="beside", box=_box(40, 10, 50, 14)),
    ]

    result = measure([_note()], elsewhere)

    assert result.sites[0].readings == ()
    assert result.counts()["gv_taken_readings"] == 0


# ---------------------------------------------------------------------------
# PASS, and the report
# ---------------------------------------------------------------------------


def test_a_pass_with_evidence_under_a_box_is_listed() -> None:
    findings = [
        PlacedFinding(finding_id="pass-here", outcome="PASS", page_index=0, box=BOX),
        PlacedFinding(finding_id="fail-here", outcome="FAIL", page_index=0, box=BOX),
        PlacedFinding(
            finding_id="pass-away", outcome="PASS", page_index=0, box=_box(60, 60, 70, 70)
        ),
    ]

    result = measure([_note()], [], findings=findings)

    assert result.counts()["pass_at_site"] == 1
    assert [finding.finding_id for finding in result.sites[0].findings] == [
        "pass-here",
        "fail-here",
    ]
    assert [finding.finding_id for finding in result.sites[0].passes] == ["pass-here"]
    assert "finding pass-here: page 1" in render(result)


def test_the_report_prints_counts_and_never_the_drawings_words() -> None:
    notes = [_note('30 1/4" (2EQ)'), _note("by others", box=_box(40, 40, 50, 50))]
    readings = [_reading(Fraction(117, 4))]

    report = render(measure(notes, readings, [_key(VENDOR, '28 1/2"')]))

    for leaked in ("30 1/4", "2EQ", "by others", "29 1/4", "117/4", "28 1/2", "57/2"):
        assert leaked not in report
    assert "False FAILs" in report
