"""The pre-flight health report (`preflight.py`): what code finds before money is spent. $0.

Made-up page maps and pack files only (no client values): each line says what was found, whether
the check goes on and who acts; only a swapped, broken or other-project file stops the check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.review.assistant import preflight
from app.review.assistant.preflight import Inputs, PairEstimate, build, duplicate_pages

VENDOR = "vendor.pdf"
ARCH = "architect.pdf"
#: Words a reviewer should never read on the screen.
INTERNAL = ("p10", "matcher", "gap closer", "prover", "advocate", "worklist", "helper", "lens")


def _half(role: str) -> dict[str, Any]:
    return {"box": [0, 0, 100, 100], "role": role, "heading": None, "round": None}


def _page_map(
    vendor_pages: int = 4, architect_pages: int = 3, area: bool = False
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = [
        {
            "file": VENDOR,
            "page": n,
            "role": "vendor",
            "halves": [_half("vendor")],
            "area": None,
        }
        for n in range(1, vendor_pages + 1)
    ]
    pages += [
        {"file": ARCH, "page": n, "role": "architect", "halves": [_half("architect")]}
        for n in range(1, architect_pages + 1)
    ]
    out: dict[str, Any] = {"files": [VENDOR, ARCH], "pages": pages}
    if architect_pages:
        out["upload_roles"] = {VENDOR: "vendor", ARCH: "architect"}
    else:
        out["files"] = [VENDOR]
    return out


def _listing(**extra: Any) -> list[dict[str, Any]]:
    return [
        {"name": VENDOR, "pages": 4, "sha256": "aaa", **extra.get("vendor", {})},
        {"name": ARCH, "pages": 3, "sha256": "bbb", **extra.get("architect", {})},
    ]


def _labels(texts: list[str], mm: bool = False) -> list[dict[str, Any]]:
    return [{"text": t, "mm": "940" if mm else None} for t in texts]


def _dims(pages: dict[tuple[str, int], list[dict[str, Any]]], notes: dict[Any, Any] | None = None):  # type: ignore[no-untyped-def]
    return {
        "pages": [
            {"file": f, "page": p, "labels": labels, "scale_notes": (notes or {}).get((f, p), [])}
            for (f, p), labels in pages.items()
        ]
    }


def _ids(report: preflight.Report) -> list[str]:
    return [line.id for line in report.lines]


def _line(report: preflight.Report, id_: str) -> preflight.Line:
    return next(line for line in report.lines if line.id == id_)


def _plain(report: preflight.Report) -> None:
    for line in report.lines:
        lowered = line.words.lower()
        assert not any(word in lowered for word in INTERNAL), line.words


def test_a_healthy_two_file_set_goes_on_and_says_the_pairs_are_an_estimate() -> None:
    report = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            reading_state="ready",
            pairs=PairEstimate(vendor_pages=4, paired=1, to_ask=3),
        )
    )
    assert report.stops == ()
    matched = _line(report, "matched")
    assert matched.estimate and "first guess" in matched.words and "1 of 4" in matched.words
    assert "final numbers come with the result" in matched.words
    assert report.out()["estimate"] is True
    assert _line(report, "pages").words == "4 vendor pages to check · 3 architect pages."
    _plain(report)


def test_after_the_run_the_numbers_are_confirmed_and_unmatched_pages_say_who_acts() -> None:
    report = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            reading_state="ready",
            pairs=PairEstimate(4, 1, 0, (2, 3, 4), confirmed=True),
        )
    )
    assert report.stops == ()
    assert _line(report, "matched").words == (
        "1 of 4 vendor pages matched to the architect's drawings."
    )
    needs = _line(report, "needs_drawing")
    assert needs.level == "warn" and needs.goes_on and needs.who == "GV"
    assert needs.pages == (2, 3, 4) and "3 vendor pages have no architect drawing" in needs.words


def test_no_vendor_page_with_any_possible_architect_page_stops_as_another_project() -> None:
    report = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            reading_state="ready",
            pairs=PairEstimate(4, 0, 0, (1, 2, 3, 4)),
        )
    )
    (stop,) = report.stops
    assert stop.id == "other_project" and not stop.goes_on and stop.who == "you"
    assert report.out()["stops"] == ["other_project"]


def test_pages_waiting_for_the_ai_matcher_never_stop_the_check() -> None:
    report = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            reading_state="ready",
            pairs=PairEstimate(4, 0, 4, ()),
        )
    )
    assert report.stops == ()


def test_a_locked_damaged_or_empty_file_stops_before_money_is_spent() -> None:
    locked = build(
        Inputs(
            listing=_listing(
                architect={
                    "intake_problem": "this file is locked with a password, so it could "
                    "not be read"
                }
            ),
            page_map=_page_map(),
            main_file=VENDOR,
        )
    )
    (stop,) = locked.stops
    assert stop.id == "broken_file" and "locked with a password" in stop.words
    assert stop.todo and "password" in stop.todo
    empty = build(
        Inputs(listing=_listing(vendor={"pages": 0}), page_map=_page_map(), main_file=VENDOR)
    )
    assert [s.id for s in empty.stops] == ["broken_file"]
    failed = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            reading_state="failed",
            reading_why="the reading crashed",
        )
    )
    assert "could not be read (the reading crashed)" in failed.stops[0].words


def test_the_same_file_uploaded_as_both_sides_stops() -> None:
    report = build(
        Inputs(
            listing=_listing(architect={"sha256": "aaa"}),
            page_map=_page_map(),
            main_file=VENDOR,
        )
    )
    (stop,) = report.stops
    assert stop.id == "files_swapped" and "same file as the vendor's" in stop.words


def test_an_architect_file_that_reads_like_a_shop_drawing_is_a_note_and_goes_on() -> None:
    # Every page of the "architect's" file: inch sizes with millimetres beside them, "SHOP
    # DRAWING" printed, no feet-and-inches and no architectural scale. For GV's countertops the
    # cabinet maker's shop drawings ARE the right reference (client meeting, 2026-10-23): the
    # reviewer's choice is trusted, the check goes on, nobody has to act.
    sizes = ['24"', '36"', '18"', '30"', '12"', '6"']
    dims = _dims({(ARCH, n): _labels(sizes, mm=True) for n in (1, 2, 3)})
    texts = {(ARCH, n): "MILLWORK SHOP DRAWING FOR APPROVAL" for n in (1, 2, 3)}
    report = build(
        Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, dims=dims, texts=texts)
    )
    assert report.stops == ()
    line = _line(report, "reference_shop_drawing")
    assert line.level == "note" and line.goes_on and line.who == "nobody" and line.todo is None
    assert line.words == preflight.SHOP_REFERENCE_WORDS
    assert "architect's" in line.words and "used as the reference you chose" in line.words
    assert all(not entry["stops"] for entry in report.facts["findings_so_far"])


def test_an_architect_file_printing_inches_only_is_never_called_swapped() -> None:
    # Real architects sometimes dimension in inches (no feet): too little to tell, never a stop.
    sizes = ['24"', '36"', '18"', '30"', '12"', '6"']
    dims = _dims({(ARCH, n): _labels(sizes) for n in (1, 2, 3)})
    report = build(Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, dims=dims))
    assert report.stops == ()
    # An architect page with feet and inches, or an architectural scale, is the architect's.
    feet = _dims(
        {
            (ARCH, n): _labels(["3'-6\"", "2'-0\"", "1'-4\"", "4'-0\"", '6"'], mm=True)
            for n in (1, 2, 3)
        }
    )
    texts = {(ARCH, n): "SHOP DRAWING" for n in (1, 2, 3)}
    assert (
        build(
            Inputs(
                listing=_listing(), page_map=_page_map(), main_file=VENDOR, dims=feet, texts=texts
            )
        ).stops
        == ()
    )


def _index(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f"{e['file']}#{e['page']}": e for e in entries}


def test_a_combined_sheet_set_counts_pages_with_and_without_the_architect() -> None:
    page_map = {
        "files": [VENDOR],
        "pages": [
            {"file": VENDOR, "page": 1, "halves": [_half("architect"), _half("vendor")]},
            {"file": VENDOR, "page": 2, "halves": [_half("vendor")]},
        ],
    }
    report = build(Inputs(listing=_listing()[:1], page_map=page_map, main_file=VENDOR))
    assert _line(report, "matched").words == "1 vendor page with the architect's drawing beside it."
    assert _line(report, "needs_drawing").pages == (2,)


def test_unreadable_pages_scans_hand_drawing_millimetres_and_no_scale() -> None:
    health = {
        "pages": [
            {"file": VENDOR, "page": 1, "facts": {"picture_share": 0.9, "typed_text": 0}},
            {"file": VENDOR, "page": 2, "facts": {"drawn_letters": True, "typed_text": 3}},
        ]
    }
    coverage = {
        "pages": [
            {
                "file": VENDOR,
                "page": 3,
                "sides": {"vendor": {"read": 0}},
                "not_read": [{"reason": "mm_row", "count": 6}],
            },
        ]
    }
    dims = _dims({(VENDOR, 4): _labels(['1"', '2"', '3"', '4"', '5"'])})
    scales = {"pages": [{"file": VENDOR, "page": 4, "scales": []}]}
    report = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            reading_state="ready",
            unreadable={
                (VENDOR, 2): "page 2 could not be read: it took too long",
                (ARCH, 1): "page 1 could not be read: broken",
            },
            health=health,
            coverage=coverage,
            dims=dims,
            scales=scales,
        )
    )
    unread = [line for line in report.lines if line.id == "unreadable"]
    assert [line.words for line in unread] == [
        "1 vendor page can't be read (it took too long): not checked.",
        "1 architect page can't be read (broken): not used for matching.",
    ]
    assert _line(report, "scanned").pages == (1,)
    assert _line(report, "hand_drawn").pages == (2,)
    mm = _line(report, "millimetres")
    assert mm.pages == (3,) and mm.who == "the vendor"
    assert _line(report, "no_scale").pages == (4,)
    assert all(line.goes_on for line in report.lines)


def test_exact_copies_are_found_and_checked_once() -> None:
    text = "CABINET ELEVATION " * 10
    texts = {(VENDOR, 1): text, (VENDOR, 2): "OTHER SHEET " * 20, (VENDOR, 3): text}
    dims = _dims({(VENDOR, p): _labels(['24"', '30"']) for p in (1, 2, 3)})
    assert duplicate_pages(VENDOR, dims, texts) == {3: 1}
    # The same words but other sizes: not a copy.
    other = _dims({(VENDOR, 1): _labels(['24"']), (VENDOR, 3): _labels(['25"'])})
    assert duplicate_pages(VENDOR, other, texts) == {}
    report = build(
        Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, texts=texts, dims=dims)
    )
    assert report.duplicates == {3: 1}
    assert "page 3 = page 1" in _line(report, "duplicates").words


def test_gv_markup_on_a_vendor_file_beside_an_architect_file_is_noted() -> None:
    health = {"pages": [{"file": VENDOR, "page": 1, "facts": {"markups": 12}}]}
    report = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            health=health,
            combined={VENDOR: True},
        )
    )
    line = _line(report, "gv_markup")
    assert line.who == "you" and line.todo and "clean file" in line.todo
    # GV's own combined review sheets (one file) carry GV's notes by design: no line.
    page_map = _page_map(architect_pages=0)
    single = build(
        Inputs(
            listing=_listing()[:1],
            page_map=page_map,
            main_file=VENDOR,
            health=health,
            combined={VENDOR: True},
        )
    )
    assert "gv_markup" not in _ids(single)


def test_spec_book_and_job_values_lines() -> None:
    report = build(
        Inputs(
            listing=_listing(),
            page_map=_page_map(),
            main_file=VENDOR,
            missing_values=("Sink inside width", "Countertop overhang"),
        )
    )
    spec = _line(report, "no_spec")
    assert spec.words.startswith("Not checked: materials, grade and hardware") and spec.who == "GV"
    values = _line(report, "values_needed")
    assert values.words.startswith("2 job values not entered yet: Sink inside width, Countertop")
    assert values.who == "you" and values.goes_on and "Ask GV" not in values.words


def test_a_long_job_values_list_stays_short() -> None:
    names = tuple(f"Value {n}" for n in range(1, 10))
    report = build(
        Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, missing_values=names)
    )
    words = _line(report, "values_needed").words
    assert "Value 4 and 5 more." in words and "Value 5" not in words


def test_gather_reads_the_pack_and_the_index_when_present(tmp_path: Path) -> None:
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "page_map.json").write_text(json.dumps(_page_map()), encoding="utf-8")
    (folder / "status.json").write_text(json.dumps({"state": "ready"}), encoding="utf-8")
    (folder / "vendor_stickers.json").write_text(
        json.dumps({"file": VENDOR, "stickers": [{"page": 1}, {"page": 2}]}), encoding="utf-8"
    )
    inputs = preflight.gather(folder, listing=_listing(), main_file=VENDOR)
    assert inputs.index is None and inputs.stickers == 2 and inputs.page_map is not None
    (folder / "architect_index.json").write_text(
        json.dumps({"pages": {f"{ARCH}#1": {"file": ARCH, "page": 1, "projects": ["X"]}}}),
        encoding="utf-8",
    )
    assert preflight.gather(folder, listing=_listing(), main_file=VENDOR).index == {
        f"{ARCH}#1": {"file": ARCH, "page": 1, "projects": ["X"]}
    }
    report = build(inputs)
    assert "gv_markup" in _ids(report)


def test_a_broken_upload_is_said_even_without_a_page_map(tmp_path: Path) -> None:
    from app.review.assistant.chat_service import broken_words

    locked = [
        {
            "name": ARCH,
            "pages": 3,
            "intake_problem": "this file is locked with a password, " "so it could not be read",
        }
    ]
    found = broken_words(locked, None)
    assert found is not None and found["stops"] == ["broken_file"]
    assert broken_words([{"name": VENDOR, "pages": 4}], None) is None
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "status.json").write_text(
        json.dumps({"state": "failed", "reason": "the PDF could not be opened"}), encoding="utf-8"
    )
    failed = broken_words([{"name": VENDOR, "pages": 4}], folder)
    assert failed is not None
    assert "could not be read (the PDF could not be opened)" in failed["lines"][0]["words"]


def test_a_one_page_shop_drawing_uploaded_as_the_architects_is_noted_and_kinds_count() -> None:
    dims = _dims({(ARCH, 1): _labels(['24"', '30"', '18"', '72"', '6"'])})
    texts = {(ARCH, 1): "MILLWORK SHOP DRAWING SUBMITTED FOR APPROVAL"}
    listing = [
        {"name": VENDOR, "pages": 1, "sha256": "aaa", "kind": "the vendor's shop drawings"},
        {"name": ARCH, "pages": 1, "sha256": "bbb", "kind": "the architect's drawings"},
    ]
    # No upload roles in the page map: the uploads' kinds say which file is the architect's.
    page_map = {"files": [VENDOR, ARCH], "pages": []}
    report = build(
        Inputs(listing=listing, page_map=page_map, main_file=VENDOR, dims=dims, texts=texts)
    )
    assert report.stops == ()
    assert "reference_shop_drawing" in _ids(report)
    # The same bytes uploaded as both sides (the pack then reads one file): the one stop.
    same = [{**listing[0]}, {**listing[1], "sha256": "aaa"}]
    assert (
        "same file"
        in build(Inputs(listing=same, page_map=page_map, main_file=VENDOR)).stops[0].words
    )


def test_an_architect_file_with_no_readable_size_is_a_warning_not_a_stop() -> None:
    dims = _dims({(VENDOR, 1): _labels(['24"']), (ARCH, 1): []})
    report = build(Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, dims=dims))
    assert report.stops == ()
    line = _line(report, "no_sizes")
    assert line.level == "warn" and line.goes_on and line.who == "the architect"


# ---- projects: exactly the pairing's own grouping of the set index ----------------------------


def _messy_pack(tmp_path: Path) -> Path:
    """A two-file pack whose set index prints many names for two projects (made-up names): the
    vendor's company, a scope phrase, long and short forms, a prototype divider."""
    folder = tmp_path / "pack"
    folder.mkdir()
    drawn = [{"box": [0, 0, 100, 100], "role": "vendor", "heading": None, "round": None}]
    arch_half = [{"box": [0, 0, 100, 100], "role": "architect", "heading": None, "round": None}]
    page_map = {
        "files": [VENDOR, ARCH],
        "upload_roles": {VENDOR: "vendor", ARCH: "architect"},
        "pages": [
            {"file": VENDOR, "page": 1, "halves": []},
            {"file": VENDOR, "page": 2, "halves": drawn},
            {"file": VENDOR, "page": 3, "halves": drawn},
            {"file": VENDOR, "page": 4, "halves": []},
            {"file": VENDOR, "page": 5, "halves": drawn},
            {"file": ARCH, "page": 1, "halves": []},
            {"file": ARCH, "page": 2, "halves": arch_half},
            {"file": ARCH, "page": 3, "halves": []},
            {"file": ARCH, "page": 4, "halves": arch_half},
        ],
    }
    reads = [
        (VENDOR, 1, ["NORTHFIELD GRAND HOTEL"], "shop drawing", True),
        (
            VENDOR,
            2,
            ["ACME MILLWORK CO", "NORTHFIELD GRAND HOTEL - PA MW MATRIX"],
            "shop drawing",
            False,
        ),
        (VENDOR, 3, ["Public Area millwork"], "shop drawing", False),
        (VENDOR, 4, ["Lakeview Inn"], "shop drawing", True),
        (VENDOR, 5, ["ACME MILLWORK CO"], "shop drawing", False),
        (ARCH, 1, ["NORTHFIELD GRAND HOTEL, TEXAS"], "architectural", True),
        (ARCH, 2, ["Northfield Grand Hotel"], "architectural", False),
        (ARCH, 3, ["Brand Look Prototype"], "prototype", True),
        (ARCH, 4, [], "unknown", False),
    ]
    view = {"title": "ELEVATION 1", "kind": "elevation", "prints_sizes": True, "whole_floor": False}
    index = {
        f"{f}#{n}": {
            "file": f,
            "page": n,
            "role": "architect" if f == ARCH else "vendor",
            "projects": names,
            "set_kind": kind,
            "set_words": [],
            "introduces": introduces,
            "views": [view] if f == ARCH and n == 2 else [],
        }
        for f, n, names, kind, introduces in reads
    }
    files = {
        "page_map.json": page_map,
        "set_index.json": {"version": 1, "pages": index},
        "dims.json": {"pages": []},
        "annotations.json": [],
        "status.json": {"state": "ready"},
    }
    for name, document in files.items():
        (folder / name).write_text(json.dumps(document), encoding="utf-8")
    return folder


def test_messy_names_group_into_the_pairings_projects_never_raw_names(tmp_path: Path) -> None:
    from app.review.assistant import preflight_pairs

    summary = preflight_pairs.project_summary(_messy_pack(tmp_path))
    assert summary is not None
    groups = {g.name: g for g in summary.vendor}
    assert set(groups) == {"NORTHFIELD GRAND HOTEL", "Lakeview Inn"}, groups
    assert groups["NORTHFIELD GRAND HOTEL"].architect and groups[
        "NORTHFIELD GRAND HOTEL"
    ].vendor_pages == (1, 2, 3)
    assert not groups["Lakeview Inn"].architect and groups["Lakeview Inn"].vendor_pages == (4, 5)
    report = build(
        Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, projects=summary)
    )
    assert report.stops == ()
    several = [line for line in report.lines if line.id == "several_projects"]
    assert len(several) == 1
    assert "ACME" not in several[0].words and "Public Area" not in several[0].words
    assert "NORTHFIELD GRAND HOTEL; Lakeview Inn" in several[0].words or (
        "Lakeview Inn; NORTHFIELD GRAND HOTEL" in several[0].words
    )
    (missing,) = [line for line in report.lines if line.id == "project_missing"]
    assert "Lakeview Inn" in missing.words and missing.pages == (4, 5) and missing.who == "GV"
    # The brand prototype is never a project of the architect's file.
    assert summary.architect == ("NORTHFIELD GRAND HOTEL, TEXAS",)


def test_the_grouping_finds_another_project_and_stops() -> None:
    from app.review.assistant.preflight import ProjectGroup, ProjectSummary

    summary = ProjectSummary(
        vendor=(ProjectGroup("Harbor View Hotel", (1, 2), architect=False),),
        architect=("Lakeside Clinic",),
    )
    report = build(
        Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, projects=summary)
    )
    (stop,) = report.stops
    assert stop.id == "other_project" and "Lakeside Clinic" in stop.words
    # Without the grouping (no set index yet): no project line and no project stop.
    plain = build(Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR))
    assert not {"several_projects", "project_missing", "other_project"} & set(_ids(plain))


def test_before_the_index_unmatched_pages_are_an_estimate() -> None:
    pairs = PairEstimate(4, 1, 0, (2, 3, 4))
    guess = _line(
        build(Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, pairs=pairs)),
        "needs_drawing",
    )
    assert (
        guess.estimate and guess.level == "note" and guess.words.startswith("About 3 vendor pages")
    )
    indexed = _line(
        build(
            Inputs(
                listing=_listing(),
                page_map=_page_map(),
                main_file=VENDOR,
                pairs=pairs,
                index={f"{ARCH}#1": {"file": ARCH, "page": 1}},
            )
        ),
        "needs_drawing",
    )
    assert not indexed.estimate and indexed.level == "warn"


def test_pages_the_matcher_answered_are_not_sure_never_no_drawing() -> None:
    pairs = PairEstimate(4, 1, 0, (), unsure=(2, 3, 4), confirmed=True)
    report = build(Inputs(listing=_listing(), page_map=_page_map(), main_file=VENDOR, pairs=pairs))
    assert "needs_drawing" not in _ids(report)
    assert _line(report, "not_sure").words.startswith("3 vendor pages could not be matched")
    assert report.stops == ()
