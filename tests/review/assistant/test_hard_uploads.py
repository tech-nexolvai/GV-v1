"""Hard made-up uploads through every $0 step (no client file, no paid AI, no network).

Each test builds one small PDF set that breaks an assumption real uploads break (no titles at all,
several projects in one file, the architect's file of another project, plans with no sizes, a
scan, numbers drawn as pen strokes, millimetres with [inches], rotated pages, "NTS", two rounds,
GV's markup over a vendor value, a schedule table, 300 near-empty pages, files swapped, a sheet
twice in two revisions, a locked file, a damaged file, empty pages, twin architect views, an
accessible unit against the standard one, a mirrored unit, millimetres that disagree with the
inches, and an architect whose plan and elevation disagree), puts it through the front door, the
set pack, the work list (code's own pairing), the plan check and the vendor-string check, and
asserts three things:

1. **no crash, in time**: every step finishes (a file that cannot be read fails with a plain
   reason, never an exception) within `LIMIT_S`;
2. **honest**: what cannot be read is said to be unread (never guessed); an unpaired drawing says
   why; millimetres are never taken for inches; no false red;
3. **$0**: the `no_network` fixture refuses every internet connection.

A case main does not handle yet is marked `xfail` with the plain reason; the bank's report lists
them. They are not strict: the builder who fixes one sees it pass and removes its mark.
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.review.assistant.hard_uploads import (
    ARCHITECT,
    PAGE,
    PREVIOUS_ROUND,
    Outcome,
    Run,
    Sheet,
    Upload,
    blank_pages,
    damaged_pdf,
    locked_pdf,
    mm_text,
    pdf,
    run_pipeline,
    scanned_pdf,
    with_markup,
)
from tests.review.assistant.hard_uploads import no_network as _no_network

#: A small set's whole $0 pipeline, with room for a slow CI runner (about 10 s locally).
LIMIT_S = 120.0
#: The 300-page stub (about 60 s locally).
HUGE_LIMIT_S = 420.0
WIDTHS = (24, 30, 18)
VALUES = {Fraction(v) for v in (*WIDTHS, sum(WIDTHS))}

no_network = _no_network


@pytest.fixture(autouse=True)
def _offline(no_network: Iterator[None]) -> None:
    """Every test here runs with the network refused ($0, fail closed)."""


def _elevation(title: str | None, widths: tuple[int, ...] = WIDTHS, **run: Any) -> Sheet:
    return Sheet(title, runs=[Run(widths, **run)])


def _vendor(*sheets: Sheet, name: str = "shop.pdf") -> Upload:
    return Upload(name, pdf(sheets))


def _architect(*sheets: Sheet, name: str = "arch.pdf") -> Upload:
    return Upload(name, pdf(sheets), ARCHITECT)


def _honest_basics(outcome: Outcome, limit: float = LIMIT_S) -> None:
    """What every readable set must show: built, in time, a work list, every unpaired vendor
    drawing with its reason, no red where no string is wrong."""
    assert outcome.seconds < limit, f"the $0 steps took {outcome.seconds:.0f} s"
    assert outcome.status.get("state") == "ready", outcome.status
    assert outcome.work_error is None
    assert outcome.summary
    for pair in outcome.across():
        assert pair.chosen or pair.why.strip(), f"{pair.vendor.ref} is unpaired with no reason"


def _no_reds(outcome: Outcome) -> None:
    assert outcome.vendor is not None
    assert outcome.vendor.findings == [], [f.get("item") for f in outcome.vendor.findings]


def _architect_page(item_id: str) -> int:
    """`p1002` -> the architect file's page 2 (the separate file's pages are numbered after 1000)."""
    return int(item_id[1:]) % 1000


# ---- titles, projects and the architect's file -------------------------------------------------


def test_no_headings_or_titles_at_all(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [_vendor(_elevation(None), _elevation(None, (36, 36))), _architect(_elevation(None))],
    )
    _honest_basics(outcome)
    # The roles come from the upload, not from words the sheets do not print.
    assert outcome.roles() == [
        ("shop.pdf", 1, "vendor", ["vendor"]),
        ("shop.pdf", 2, "vendor", ["vendor"]),
        ("arch.pdf", 1, "architect", ["architect"]),
    ]
    assert set(outcome.label_inches("shop.pdf")) == VALUES | {Fraction(36)}
    # The run the architect does not draw (36" + 36") is not paired with anything.
    assert outcome.paired().get(2, ()) == ()
    _no_reds(outcome)


def test_several_projects_in_one_file_never_pair_across_projects(tmp_path: Path) -> None:
    vendor = _vendor(
        Sheet("HOTEL ALPHA", title_size=36),  # a divider, big title
        _elevation("ALPHA - VANITY ELEVATION"),
        Sheet(None, words=["PROJECT NAME: BETA SUITES", "MILLWORK SHOP DRAWINGS"]),  # title block
        Sheet("VANITY ELEVATION", words=["PROJECT: BETA SUITES"], runs=[Run(WIDTHS)]),
    )
    architect = _architect(
        _elevation("ALPHA HOTEL - VANITY ELEVATION"), _elevation("BETA SUITES - VANITY ELEVATION")
    )
    outcome = run_pipeline(tmp_path, [vendor, architect])
    _honest_basics(outcome)
    paired = outcome.paired()
    # Same sizes in both projects: a pair may only be made inside its own project.
    assert {_architect_page(i) for i in paired.get(2, ())} <= {1}
    assert {_architect_page(i) for i in paired.get(4, ())} <= {2}
    _no_reds(outcome)


def test_architect_file_of_another_project_pairs_nothing(tmp_path: Path) -> None:
    vendor = _vendor(Sheet("VANITY ELEVATION", words=["PROJECT: HOTEL ALPHA"], runs=[Run(WIDTHS)]))
    architect = _architect(
        Sheet("VANITY ELEVATION", words=["PROJECT: GAMMA TOWER"], runs=[Run((20, 36, 22))])
    )
    outcome = run_pipeline(tmp_path, [vendor, architect])
    _honest_basics(outcome)
    assert outcome.paired() == {1: ()}
    assert outcome.work is not None and outcome.work.items == ()


def test_architect_file_with_only_floor_plans_and_no_sizes(tmp_path: Path) -> None:
    plans = Sheet(
        "FLOOR PLAN - LEVEL 1",
        plans=[(200, 200, 300, 200, "LOBBY"), (500, 200, 200, 200, "VANITY")],
    )
    outcome = run_pipeline(
        tmp_path, [_vendor(_elevation("VENDOR ELEVATION - VANITY")), _architect(plans)]
    )
    _honest_basics(outcome)
    # A plan with no size is no drawing to compare with: nothing is paired or compared, and the
    # vendor's page says why.
    assert outcome.label_inches("arch.pdf") == []
    assert outcome.work is not None and outcome.work.items == ()
    reasons = [s.reason for s in outcome.work.skipped if s.file == "shop.pdf"]
    assert reasons and all(r.strip() for r in reasons)


def test_files_swapped_are_noticed(tmp_path: Path) -> None:
    """The vendor's shop drawing uploaded as the architect's, and the architect's as the vendor's."""
    outcome = run_pipeline(
        tmp_path,
        [
            Upload("arch.pdf", pdf([_elevation("ARCHITECTURAL ELEVATION - VANITY")])),
            Upload(
                "shop.pdf",
                pdf(
                    [
                        Sheet(
                            "SHOP DRAWING - VANITY",
                            words=["MILLWORK SHOP DRAWING", "SUBMITTED FOR APPROVAL"],
                            runs=[Run(WIDTHS)],
                        )
                    ]
                ),
                ARCHITECT,
            ),
        ],
        main_file="arch.pdf",
    )
    _honest_basics(outcome)
    _no_reds(outcome)
    if not _says(
        outcome, "swap", "uploaded as", "looks like the vendor", "looks like the architect"
    ):
        pytest.xfail(
            "files swapped: nothing says the file uploaded as the architect's is a shop drawing "
            "(and the other way round); the check just finds no vendor drawing"
        )


# ---- reading ------------------------------------------------------------------------------------


def test_scanned_page_is_said_unread_never_guessed(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path, [Upload("shop.pdf", scanned_pdf(_elevation("VENDOR ELEVATION")))]
    )
    _honest_basics(outcome)
    # Whatever is read from the picture is a value it shows; nothing else.
    assert set(outcome.label_inches()) <= VALUES
    vendor = ((outcome.coverage or {}).get("sides") or {}).get("vendor") or {}
    health = _health(outcome)
    assert vendor.get("printed", 0) > vendor.get("read", 0) or any(
        "scanned" in words for words in health
    ), (vendor, health)


def test_numbers_drawn_as_pen_strokes_are_said_unread(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path, [_vendor(_elevation("VENDOR ELEVATION - VANITY", strokes=True))]
    )
    _honest_basics(outcome)
    # Never a guess: whatever is read is a value the strokes show.
    assert set(outcome.label_inches()) <= VALUES
    vendor = ((outcome.coverage or {}).get("sides") or {}).get("vendor") or {}
    roles = {role for _f, _p, role, _h in outcome.roles()}
    if not (vendor.get("printed", 0) > vendor.get("read", 0) or "vendor" in roles):
        pytest.xfail(
            "a page whose numbers are all pen strokes (no typed number) is taken for a general "
            "page, so nothing says its numbers could not be read"
        )


def test_millimetres_with_inches_are_read_as_inches(tmp_path: Path) -> None:
    outcome = run_pipeline(tmp_path, [_vendor(_elevation("VENDOR ELEVATION", labeller=mm_text))])
    _honest_basics(outcome)
    # 610 [24"]: 24", never 610".
    assert sorted(outcome.label_inches()) == sorted(VALUES)
    _no_reds(outcome)


@pytest.mark.xfail(
    reason='millimetres that disagree with the inches (660 [27"]: 660 mm is 26") are not '
    'noticed: code reads 27" as an ordinary label and keeps no millimetre value',
    strict=False,
)
def test_millimetres_that_disagree_with_the_inches_are_not_taken_silently(tmp_path: Path) -> None:
    printed = ('610 [24"]', '660 [27"]', '457 [18"]', '1753 [69"]')
    outcome = run_pipeline(
        tmp_path, [_vendor(_elevation("VENDOR ELEVATION", (24, 27, 18), printed=printed))]
    )
    _honest_basics(outcome)
    silent = [
        label
        for label in outcome.labels()
        if str(label.get("text", "")).startswith("660") and label.get("mm") in (None, "")
    ]
    assert not silent, silent


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_rotated_pages_read_the_same_numbers(tmp_path: Path, rotate: int) -> None:
    sheets = [_elevation("VENDOR ELEVATION")]
    sheets.append(Sheet("VENDOR ELEVATION", runs=[Run(WIDTHS)], rotate=rotate))
    outcome = run_pipeline(tmp_path, [_vendor(*sheets)])
    _honest_basics(outcome)
    item = outcome.listing[0]
    assert any("rotation" in change for change in item.get("working_changes") or [])
    for page in (1, 2):
        values = {
            Fraction(str(label["inches"])) for label in outcome.labels() if label["page"] == page
        }
        assert values == VALUES, (page, values)
    shown = (PAGE[1], PAGE[0]) if rotate in (90, 270) else PAGE
    for label in outcome.labels():
        if label["page"] == 2:
            x0, y0, x1, y1 = label["box"]
            assert 0 <= x0 < x1 <= shown[0] and 0 <= y0 < y1 <= shown[1], label


def test_not_to_scale_gives_no_red(tmp_path: Path) -> None:
    sheet = Sheet(
        "VENDOR ELEVATION", words=["SCALE: NTS"], runs=[Run(WIDTHS, drawn_scale=[1.4, 0.6, 1.1])]
    )
    outcome = run_pipeline(tmp_path, [_vendor(sheet)])
    _honest_basics(outcome)
    assert set(outcome.label_inches()) == VALUES
    _no_reds(outcome)


def test_gv_markup_over_a_vendor_value_is_never_the_vendors(tmp_path: Path) -> None:
    data = pdf([_elevation("VENDOR ELEVATION")])
    # GV's note "25"" laid over the vendor's 24" (the first chain label).
    marked = with_markup(data, 0, (260, 450, 330, 470), '25"')
    outcome = run_pipeline(tmp_path, [Upload("shop.pdf", marked)])
    _honest_basics(outcome)
    assert Fraction(25) not in outcome.label_inches()
    assert set(outcome.label_inches()) == VALUES
    assert any("markup" in words for words in _health(outcome))
    _no_reds(outcome)


def test_schedule_table_page_is_not_a_drawing_to_pair(tmp_path: Path) -> None:
    table = Sheet(
        "HARDWARE SCHEDULE",
        table=[
            ["ITEM", "SIZE", "QTY"],
            ["PULL", '6"', "12"],
            ["HINGE", "35 MM", "24"],
            ["PANEL", '24" x 30"', "2"],
        ],
    )
    outcome = run_pipeline(
        tmp_path,
        [
            _vendor(table, _elevation("VENDOR ELEVATION - VANITY")),
            _architect(_elevation("ARCH ELEVATION - VANITY")),
        ],
    )
    _honest_basics(outcome)
    paired = outcome.paired()
    assert paired.get(1, ()) == ()
    assert [_architect_page(i) for i in paired.get(2, ())] == [1]
    _no_reds(outcome)


# ---- rounds, revisions and odd files -----------------------------------------------------------


def test_two_rounds_of_the_vendors_drawings(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [
            Upload(
                "shop-r1.pdf",
                pdf([_elevation("VENDOR ELEVATION - VANITY", (24, 31, 18))]),
                PREVIOUS_ROUND.format(round=1),
            ),
            _vendor(_elevation("VENDOR ELEVATION - VANITY")),
            _architect(_elevation("ARCH ELEVATION - VANITY")),
        ],
    )
    _honest_basics(outcome)
    assert outcome.work is not None
    rounds = {item.round: item.newest_round for item in outcome.work.items}
    assert rounds == {1: False, 2: True}, rounds
    _no_reds(outcome)


def test_an_older_revision_of_the_same_sheet_is_not_checked_as_current(tmp_path: Path) -> None:
    vendor = _vendor(
        Sheet("VANITY ELEVATION  REV 0", words=["SHEET MW-101  REV 0"], runs=[Run((24, 31, 18))]),
        Sheet("VANITY ELEVATION  REV 1", words=["SHEET MW-101  REV 1"], runs=[Run(WIDTHS)]),
    )
    outcome = run_pipeline(tmp_path, [vendor, _architect(_elevation("ARCH ELEVATION - VANITY"))])
    _honest_basics(outcome)
    assert outcome.work is not None
    current = [item for item in outcome.work.items if item.newest_round]
    if len(current) > 1 and not _says(outcome, "revision", "superseded", "older", "twice"):
        pytest.xfail(
            "the same sheet twice in one upload (REV 0 and REV 1): both are checked as current, "
            "and nothing says one is an older revision"
        )


def test_password_locked_file_says_so_and_stops(tmp_path: Path) -> None:
    locked = locked_pdf(pdf([_elevation("VENDOR ELEVATION")]))
    outcome = run_pipeline(tmp_path, [Upload("shop.pdf", locked)])
    assert outcome.seconds < LIMIT_S
    assert "password" in outcome.listing[0].get("intake_problem", "")
    # Never a pack that pretends to be read.
    assert outcome.status.get("state") != "ready"
    assert outcome.labels() == []


def test_damaged_file_says_so_and_stops(tmp_path: Path) -> None:
    broken = damaged_pdf(pdf([_elevation("VENDOR ELEVATION"), _elevation("VENDOR 2")]))
    outcome = run_pipeline(tmp_path, [Upload("shop.pdf", broken)])
    assert outcome.seconds < LIMIT_S
    assert "damaged" in outcome.listing[0].get("intake_problem", "")
    assert outcome.status.get("state") != "ready"
    assert outcome.labels() == []


def test_empty_pages_are_general_pages_with_nothing_to_check(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [Upload("shop.pdf", blank_pages(3)), _architect(_elevation("ARCH ELEVATION"))],
    )
    _honest_basics(outcome)
    assert [role for f, _p, role, _h in outcome.roles() if f == "shop.pdf"] == ["general"] * 3
    assert outcome.work is not None and outcome.work.items == ()
    assert "3 general pages" in (outcome.summary or "")
    _no_reds(outcome)


def test_three_hundred_near_empty_pages_finish_in_time(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [
            Upload("shop.pdf", blank_pages(300, numbered=True)),
            _architect(_elevation("ARCH ELEVATION")),
        ],
    )
    _honest_basics(outcome, HUGE_LIMIT_S)
    assert (outcome.summary or "").startswith("300 pages")
    assert outcome.work is not None and outcome.work.items == ()


# ---- pairing look-alikes ---------------------------------------------------------------------------


def test_twin_architect_views_are_never_paired(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [
            _vendor(_elevation("VENDOR ELEVATION - VANITY")),
            _architect(_elevation("ELEVATION 1 - VANITY"), _elevation("ELEVATION 2 - VANITY")),
        ],
    )
    _honest_basics(outcome)
    # Two equal candidates: code cannot tell which one, so it pairs neither and says why.
    assert outcome.paired() == {1: ()}


@pytest.mark.xfail(
    reason="an accessible (ADA) vendor unit is still paired with the architect's standard unit: "
    "the one-side marker rule (#1893) only fires when the architect's drawing carries the marker, "
    "because the vendor's markers are left out of the facts code ranks with "
    "(worklist._ranked_across builds vendor_facts without markers / hand)",
    strict=False,
)
def test_accessible_unit_is_never_paired_with_the_standard_one(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [
            _vendor(_elevation("ACCESSIBLE VANITY (ADA) ELEVATION")),
            _architect(_elevation("STANDARD VANITY ELEVATION")),
        ],
    )
    _honest_basics(outcome)
    assert outcome.paired() == {1: ()}


def test_mirrored_unit_is_not_paired_silently(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [
            _vendor(_elevation("VANITY ELEVATION - OPP. HAND", (18, 30, 24))),
            _architect(_elevation("VANITY ELEVATION")),
        ],
    )
    _honest_basics(outcome)
    for pair in outcome.across():
        if pair.chosen:
            assert any(word in pair.why.lower() for word in ("hand", "mirror", "opposite"))


def test_architect_plan_and_elevation_that_disagree_are_said(tmp_path: Path) -> None:
    outcome = run_pipeline(
        tmp_path,
        [
            _vendor(_elevation("VENDOR ELEVATION - VANITY")),
            _architect(
                Sheet("VANITY PLAN", runs=[Run(WIDTHS)]),
                Sheet("VANITY ELEVATION", runs=[Run((24, 32, 18))]),
            ),
        ],
    )
    _honest_basics(outcome)
    _no_reds(outcome)
    if not _says(outcome, "architect's drawings disagree", "architect's own", "ask the architect"):
        pytest.xfail(
            'the architect\'s own plan (72") and elevation (74") of one unit disagree: nothing '
            "says so; the vendor's drawing is just left unpaired"
        )


# ---- helpers ------------------------------------------------------------------------------------


def _health(outcome: Outcome) -> list[str]:
    import json

    try:
        health = json.loads((outcome.pack / "health.json").read_text())
    except (OSError, ValueError):
        return []
    return [words for page in health.get("pages") or [] for words in page.get("says") or []]


def _says(outcome: Outcome, *words: str) -> bool:
    """Whether any $0 output says one of `words` (plan questions, page-map notes, the work list's
    reasons, the vendor check's grey items)."""
    texts: list[str] = [d.question for d in outcome.doubts]
    texts += [str(e) for e in (outcome.page_map or {}).get("pages") or []]
    if outcome.work is not None:
        texts += [s.reason for s in outcome.work.skipped]
        texts += [p.why for p in outcome.across()]
    if outcome.vendor is not None:
        texts += [str(u) for u in outcome.vendor.unknowns]
    joined = " ".join(texts).lower()
    return any(word.lower() in joined for word in words)
