"""Hard made-up uploads through every $0 step (no client file, no paid AI, no network).

Each test builds one small PDF set that breaks an assumption real uploads break (no titles at all,
several projects in one file, the architect's file of another project, plans with no sizes, a
scan, numbers drawn as pen strokes, millimetres with [inches], rotated pages, "NTS", two rounds,
GV's markup over a vendor value, a schedule table, 300 near-empty pages, files swapped, a sheet
twice in two revisions, a locked file, a damaged file, empty pages, twin architect views, an
accessible unit against the standard one, a mirrored unit, millimetres that disagree with the
inches, an architect whose plan and elevation disagree, and a splash sized only on the architect's
section or another sheet), puts it through the front door, the set pack, the work list (code's own pairing), the plan check and the vendor-string check, and
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

from collections.abc import Iterator, Sequence
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
    TitledView,
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
    # Never silently a general page ("flag everything", 2026-10-23): a vendor drawing whose
    # numbers are counted as printed and not read, and the page says why.
    assert "vendor" in roles, outcome.roles()
    assert vendor.get("printed", 0) > vendor.get("read", 0), vendor
    assert _says(outcome, "can't be read (drawn as lines, not text)")
    assert outcome.work is not None
    assert not any("a table, notes or pictures page" in s.reason for s in outcome.work.skipped)


def test_millimetres_with_inches_are_read_as_inches(tmp_path: Path) -> None:
    outcome = run_pipeline(tmp_path, [_vendor(_elevation("VENDOR ELEVATION", labeller=mm_text))])
    _honest_basics(outcome)
    # 610 [24"]: 24", never 610".
    assert sorted(outcome.label_inches()) == sorted(VALUES)
    _no_reds(outcome)


def test_millimetres_that_disagree_with_the_inches_are_not_taken_silently(tmp_path: Path) -> None:
    """Settled with GV: "inches count; ignore millimetres". 660 [27"] (660 mm is 26") is 27",
    its millimetres kept as printed and marked ignored: never red, never a question."""
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
    ignored = [label for label in outcome.labels() if label.get("mm_ignored")]
    assert [(label["inches"], label["mm"]) for label in ignored] == [("27", "660")]
    assert all(label.get("units") != "disagrees" for label in outcome.labels())
    assert sorted(outcome.label_inches()) == sorted(Fraction(v) for v in (24, 27, 18, 69))
    _no_reds(outcome)
    assert outcome.vendor is not None and outcome.vendor.unknowns == []


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
    # Only the newest copy is checked; the older one says so (owner, 2026-10-23).
    current = [item for item in outcome.work.items if item.newest_round]
    assert [item.vendor.page for item in current] == [2], current
    older = [s.reason for s in outcome.work.skipped if s.file == "shop.pdf" and s.page == 1]
    assert older == ["an older copy of page 2 (REV 0; page 2 is REV 1): not checked (code)"]
    _no_reds(outcome)


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
    # Owner, 2026-10-23: compared with the governing view (the elevation, for cabinet widths),
    # GV asks the architect, never red against the vendor because of it.
    assert outcome.paired() == {1: ("p1002",)}
    assert _says(outcome, "the architect's own drawings disagree")
    assert outcome.work is not None
    [item] = outcome.work.items
    assert item.views_disagree is not None and item.views_disagree.governing == "elevation"
    from app.review.assistant.classification import classify
    from app.review.assistant.simple_check.worklist import architect_views_findings

    asked = [classify(f, proved=False) for f in architect_views_findings(outcome.work.items)]
    assert [a.outcome for a in asked] == ["rule_note", "rule_note"]
    assert asked[0].settle == (
        'Architect: your plan (page 1) shows 72", your elevation (page 2) shows 74" for the '
        "vanity elevation: overall width. Please confirm which is right."
    )


# ---- a splash sized only on another view of the item (owner, 2026-10-23) -------------------------
#
# Anant: "check elevation and section drawings for splash details when they are absent from
# architectural plans". The vendor's plan prints a 4" back splash; the architect's sheet holds a
# plan (no splash size) and, beside it, a section. The splash item is what a helper records when
# the paired plan prints nothing (made up here, the same shape).

_PLAN_BOX = {"x0": 100.0, "y0": 290.0, "x1": 240.0, "y1": 680.0, "file": "arch.pdf", "page": 1}


def _splash_set(
    tmp_path: Path,
    section_notes: Sequence[tuple[str, float, float]] = ((('4" BACKSPLASH'), 780.0, 400.0),),
    *,
    elevation_notes: Sequence[tuple[str, float, float]] = (),
    strokes: Sequence[tuple[str, float, float]] = (),
    more: Sequence[Sheet] = (),
    plan_words: Sequence[tuple[str, float, float]] = (),
) -> Outcome:
    views = [
        TitledView("1", "VANITY PLAN", 120, 120, notes=plan_words, runs=[Run((36,), x=120, y=200)]),
        TitledView(
            "2",
            "VANITY SECTION",
            760,
            120,
            notes=section_notes,
            strokes=strokes,
            runs=[Run((12, 12), x=760, y=200)],
        ),
    ]
    if elevation_notes:
        views.append(
            TitledView(
                "3",
                "VANITY ELEVATION",
                440,
                120,
                notes=elevation_notes,
                runs=[Run((18, 18), x=440, y=200)],
            )
        )
    return run_pipeline(
        tmp_path,
        [
            _vendor(Sheet("VANITY PLAN", words=['4" BACKSPLASH'], runs=[Run((18, 18))])),
            _architect(Sheet(None, views=views), *more),
        ],
    )


def _splash(
    item: str = "Vanity: back splash height", vendor: str = '4"', **more: Any
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": item,
        "vendor": vendor,
        "architect": "no size printed on the plan",
        "status": "check",
        "keep_reason": "architect_prints_nothing",
        "inches": {"vendor": str(Fraction(vendor.strip('"').split()[0]))} if vendor else {},
        "checks": {"printed_vendor": "match"},
        "arch_box": dict(_PLAN_BOX),
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
    }
    finding.update(more)
    return finding


def _looked(outcome: Outcome, finding: dict[str, Any], **given: Any) -> Any:
    from app.review.assistant import splash_views

    facts = given.pop("facts", None) or splash_views.Facts.from_pack(outcome.pack)
    search = splash_views.companion_views(outcome.work, finding, facts)
    assert search.found, "the vendor drawing is paired with the architect's sheet"
    return splash_views.settle(finding, search, facts.phrases, **given)


def _group(finding: dict[str, Any]) -> str:
    from app.review.assistant import proof

    return str(proof.proof_group(finding))


def test_splash_sized_on_the_same_sheets_section_is_compared(tmp_path: Path) -> None:
    outcome = _splash_set(tmp_path)
    _honest_basics(outcome)
    settled = _looked(outcome, _splash())
    assert settled is not None
    found = settled.finding
    # (a) the section beside the paired plan prints 4" BACKSPLASH: compared, Right.
    assert found["status"] == "same" and _group(found) == "right"
    assert found["architect"] == '4" BACKSPLASH'
    assert found["splash_views"]["looked_at"][0]["how"] == "the same sheet"
    assert {v["kind"] for v in found["splash_views"]["looked_at"]} == {"plan", "section"}
    assert settled.added == []


def test_splash_that_differs_is_red_only_with_the_blind_proof(tmp_path: Path) -> None:
    from app.review.assistant import proof

    outcome = _splash_set(tmp_path, ((('6" BACKSPLASH'), 780.0, 400.0),))
    found = _looked(outcome, _splash()).finding
    # (b) code read both values and they differ: a difference that needs the pairing proof.
    assert found["status"] == "different"
    assert found.get("paired_by") is None and proof.needs_pairing(found)
    assert not proof.code_proved({**found, "trust_level": "confirmed", "witnesses": ["printed"]})
    assert _group(found) != "clouded"
    # With the blind look agreeing (the devil's advocate passed it), the usual red path.
    passed = {
        **found,
        "trust_level": "confirmed",
        "witnesses": ["printed"],
        "critic": {"model": "devil's advocate", "result": "passed"},
    }
    assert proof.code_proved(passed)


def test_splash_views_that_disagree_ask_the_architect(tmp_path: Path) -> None:
    from app.review.assistant.classification import classify

    outcome = _splash_set(
        tmp_path,
        ((('6" BACKSPLASH'), 780.0, 400.0),),
        elevation_notes=((('4" BACKSPLASH'), 450.0, 420.0),),
    )
    settled = _looked(outcome, _splash())
    # (c) the section governs (6"), the elevation says 4": GV asks the architect, never red
    # because of it; the vendor is compared with the section.
    assert settled.finding["architect"] == '6" BACKSPLASH'
    assert settled.finding["status"] == "different"
    [asked] = settled.added
    assert asked["source"] == "cross_view" and asked["reference_side"] == "architect"
    said = classify(asked, proved=False)
    assert said.outcome == "rule_note"
    assert "section" in asked["why"] and "elevation" in asked["why"]


def test_splash_never_compared_with_another_part_or_kind(tmp_path: Path) -> None:
    # (e) a thickness printed, the vendor's height: not like for like.
    outcome = _splash_set(tmp_path, ((('1 1/2" THK BACKSPLASH'), 780.0, 400.0),))
    found = _looked(outcome, _splash()).finding
    assert found["status"] == "check" and _group(found) == "not_compared"
    assert "shows no splash size" in found["splash_views"]["why"]
    # (f) a side splash printed, the vendor's back splash.
    outcome = _splash_set(tmp_path / "side", ((('4" SIDE SPLASH'), 780.0, 400.0),))
    found = _looked(outcome, _splash()).finding
    assert found["status"] == "check"


def test_splash_nothing_printed_says_the_true_reason(tmp_path: Path) -> None:
    from app.review.assistant.classification import classify

    outcome = _splash_set(tmp_path, ())
    found = _looked(outcome, _splash()).finding
    # Nothing found: Not compared, naming every view looked at.
    assert found["status"] == "check" and _group(found) == "not_compared"
    why = classify(found, proved=False).why
    assert why.startswith("the architect shows no splash size (looked at:")
    assert "the plan 'VANITY PLAN' (page 1)" in why
    assert "the section 'VANITY SECTION' (page 1)" in why


def test_splash_drawn_in_pen_strokes_is_never_guessed(tmp_path: Path) -> None:
    from app.review.assistant import splash_views

    # (j) the section's number is drawn in pen strokes code cannot read (the meeting set's
    # section A5): the view is found, nothing is read or guessed, never red, and the reason says
    # so. The pen-stroke number is listed as unread, as the reading coverage lists it.
    outcome = _splash_set(tmp_path, (), strokes=(('4"', 790.0, 420.0),))
    facts = splash_views.Facts.from_pack(outcome.pack)
    unread = splash_views.Facts(
        phrases=facts.phrases, unread={("arch.pdf", 1): ((786.0, 362.0, 800.0, 374.0),)}
    )
    found = _looked(outcome, _splash(), facts=unread).finding
    assert found["status"] == "check" and _group(found) == "not_compared"
    assert found["architect"] == "no size printed on the plan"
    why = found["splash_views"]["why"]
    assert "pen strokes code cannot read yet: 1 in the section 'VANITY SECTION' (page 1)" in why


def test_splash_general_note_is_never_the_size(tmp_path: Path) -> None:
    # (k) a general note outside every view ("U.O.N.") is not the item's size: never a Right.
    outcome = _splash_set(
        tmp_path, (), plan_words=(('COUNTERTOP W/ 4" BACKSPLASH U.O.N.', 960.0, 60.0),)
    )
    found = _looked(outcome, _splash()).finding
    assert found["status"] == "check"


def test_splash_never_taken_from_the_brand_prototype(tmp_path: Path) -> None:
    from app.review.assistant import splash_views

    # (g) the paired sheet is the brand prototype's: no view of it is used.
    outcome = _splash_set(tmp_path)
    facts = splash_views.Facts.from_pack(outcome.pack)
    prototype = splash_views.Facts(
        phrases=facts.phrases, roles={("arch.pdf", 1): "brand_prototype"}
    )
    settled = _looked(outcome, _splash(), facts=prototype)
    assert settled is not None
    assert settled.finding["status"] == "check"
    assert "brand prototype" in settled.finding["splash_views"]["why"]


def test_splash_length_never_pairs_with_a_tops_width(tmp_path: Path) -> None:
    from app.review.assistant import splash_views

    # (l) the meeting set: the vendor's 36" splash length against the architect plan's 36" top
    # width was a Right. Not like for like: never a Right.
    outcome = _splash_set(tmp_path, ())
    facts = splash_views.Facts.from_pack(outcome.pack)
    right = _splash(
        item="Makeup counter: back splash length",
        vendor='36"',
        architect='36"',
        status="same",
        dims={
            "architect": {"text": '36"', "box": [150, 300, 170, 310], "file": "arch.pdf", "page": 1}
        },
    )
    down = splash_views.downgrade_unlike(right, facts.phrases)
    assert down is not None and down["status"] == "check" and _group(down) == "not_compared"
    # (i) an item whose architect prints a size (a splash band in a height) is not looked for.
    banded = _splash(item="Vanity: counter height to top of splash", vendor='38"', architect='38"')
    assert splash_views.candidates([banded], splash_views._rules()) == []


def test_splash_drawn_without_a_size_is_measured_by_scale(tmp_path: Path) -> None:
    from app.review.assistant import scale_measure, view_scales

    outcome = _splash_set(tmp_path, ())
    # The helper's spot round the splash drawn in the section (inside the view's measured scale).
    tight = {"x0": 800.0, "y0": 300.0, "x1": 812.0, "y1": 330.0, "file": "arch.pdf", "page": 1}
    vendor = {"id": "D1", "text": '4"', "inches": "4", "orientation": "v", "route": "text"}
    finding = _splash(arch_box=tight, dims={"vendor": vendor})
    settled = _looked(outcome, finding).finding
    assert settled["status"] == "check"

    class Measured:
        value_in, plus_minus_in, scale_ratio = 5.0, 0.1, 12.0
        snapped = ("line", "line")

    found = scale_measure.measure_item(
        settled,
        pdf=outcome.uploads / "arch.pdf",
        scales=view_scales.scales_on(outcome.pack, "arch.pdf", 1),
        measure=lambda *a, **k: Measured(),
    )
    # (h) drawn, no size printed: measured by scale (the #1909 step that runs next), a Likely
    # problem when more than 1/4" off, never red.
    assert found is not None and found["differs"]
    measured = scale_measure._with_measure(settled, found)
    assert _group(measured) == "likely_problem"


def test_splash_followed_through_a_callout_to_its_section_sheet(tmp_path: Path) -> None:
    # The architect's pointer: a callout "3 / A-501" printed on the plan leads to view 3 of the
    # sheet numbered A-501, which prints the splash; followed only on an exact match.
    section_sheet = Sheet(
        None,
        sheet_number="A-501",
        views=[
            TitledView(
                "3",
                "VANITY SECTION",
                760,
                120,
                notes=[('4" BACKSPLASH', 780, 400)],
                runs=[Run((12, 12), x=760, y=200)],
            ),
            TitledView("4", "BASE DETAIL", 300, 120, runs=[Run((10, 10), x=300, y=200)]),
        ],
    )
    outcome = run_pipeline(
        tmp_path,
        [
            _vendor(Sheet("VANITY PLAN", words=['4" BACKSPLASH'], runs=[Run((18, 18))])),
            _architect(
                Sheet(
                    None,
                    views=[
                        TitledView(
                            "1",
                            "VANITY PLAN",
                            120,
                            120,
                            notes=[("A-501", 150, 560), ("3", 156, 575)],
                            runs=[Run((36,), x=120, y=200)],
                        ),
                        TitledView(
                            "2", "MIRROR ELEVATION", 760, 120, runs=[Run((30,), x=760, y=200)]
                        ),
                    ],
                ),
                section_sheet,
            ),
        ],
    )
    settled = _looked(outcome, _splash())
    found = settled.finding
    assert found["status"] == "same", found.get("splash_views")
    assert found["vs_page"] == 2
    hows = {v["how"] for v in found["splash_views"]["looked_at"]}
    assert "a callout" in hows


def test_splash_never_from_an_older_copy_or_several_look_alikes(tmp_path: Path) -> None:
    import json

    from app.review.assistant import splash_views
    from app.review.assistant.simple_check.worklist import build_worklist

    # The same item named on two other sheets: code needs a clear winner, so neither is used
    # (and the reason says so).
    other = Sheet(
        None,
        views=[
            TitledView(
                "5",
                "VANITY SECTION",
                760,
                120,
                notes=[('6" BACKSPLASH', 780, 400)],
                runs=[Run((12, 12), x=760, y=200)],
            ),
            TitledView("6", "VANITY DETAIL", 300, 120, runs=[Run((10, 10), x=300, y=200)]),
        ],
    )
    outcome = _splash_set(tmp_path, (), more=(other, other))
    index = {
        f"arch.pdf#{page}": {"file": "arch.pdf", "page": page, "item": "VANITY", "views": []}
        for page in (1, 2, 3)
    }
    (outcome.pack / "set_index.json").write_text(json.dumps({"version": 1, "pages": index}))
    outcome.work = build_worklist(outcome.pack, "shop.pdf")
    facts = splash_views.Facts.from_pack(outcome.pack)
    found = _looked(outcome, _splash(), facts=facts).finding
    assert found["status"] == "check"
    assert "2 other sheets name the same item" in found["splash_views"]["why"]
    # One of them an older copy of the other: the newer one alone is the item's other sheet.
    older = splash_views.Facts(
        phrases=facts.phrases, index=facts.index, older=frozenset({("arch.pdf", 2)})
    )
    found = _looked(outcome, _splash(), facts=older).finding
    assert found["architect"] == '6" BACKSPLASH' and found["vs_page"] == 3


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
