"""What went wrong, at the end of every full check (`check_problems.py`), and code's pairing estimate
before Run (`preflight_pairs.py`). Plain words only; made-up values; $0."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.review.assistant import check_problems, preflight_pairs
from app.review.assistant.preflight import Line, PairEstimate, Report
from app.review.assistant.simple_check.pairing import UNPAIRED

INTERNAL = ("p10", "matcher", "gap closer", "prover", "advocate", "lead review", "helper", "lens")


def _plain(lines: list[str]) -> None:
    for line in lines:
        lowered = line.lower()
        assert not any(word in lowered for word in INTERNAL), line


def test_an_xyz_like_check_says_what_went_wrong_in_plain_words() -> None:
    # A two-file upload where most vendor pages found no architect drawing and the money ran out.
    unpaired = {
        page: f"{UNPAIRED}: the AI page matcher picked p1029, but code chose p1011, so they disagree"
        for page in range(3, 40)
    }
    unpaired.update({50: f"{UNPAIRED}: code found no clear architect drawing for it"})
    gate = SimpleNamespace(
        report=Report(
            lines=(
                Line(
                    "needs_drawing",
                    "warn",
                    "89 vendor pages have no architect drawing in this "
                    "upload: checked only against the vendor's own drawings and GV's rules.",
                ),
                Line(
                    "unreadable", "note", "2 architect pages can't be read: not used for matching."
                ),
                Line("no_spec", "note", "Not checked: materials, grade and hardware."),
            )
        ),
        supervisor=SimpleNamespace(verdict="warn", why="Most vendor pages have no architect page."),
        run_anyway=False,
    )
    found = check_problems.problems(
        gate=gate,
        unpaired=unpaired,
        retry={60: "x"},
        failed_steps=["the drawings' printed numbers (the labels step did not finish)"],
        pages_left=[70, 71],
        unproved=3,
        why_left="out of money",
        out_of_money=True,
        price="36.65",
    )
    assert found[0] == "The fixed price ($36.65) ran out before the check finished."
    assert (
        "2 pages not compared (the money ran out): pages 70 and 71. Continue compares them."
        in found
    )
    assert "3 possible mistakes not proved yet, so not marked red. Continue proves them." in found
    assert any(
        line.startswith("One check by code did not finish (reading the printed numbers)")
        for line in found
    )
    assert (
        "37 vendor pages not compared with the architect (the architect's drawing of this item "
        "could not be identified for sure): pages 3, 4, 5, 6, 7, 8, 9, 10 and 29 more."
    ) in found
    assert any("no architect drawing could be identified" in line for line in found)
    assert any("could not be matched to the architect's drawings in time" in line for line in found)
    assert any(line.startswith("89 vendor pages have no architect drawing") for line in found)
    assert "2 architect pages can't be read: not used for matching." in found
    assert "A second look before the check said: Most vendor pages have no architect page." in found
    # Plain facts are not problems.
    assert not any("materials, grade" in line for line in found)
    _plain(found)


def test_a_stop_passed_with_run_anyway_is_listed() -> None:
    gate = SimpleNamespace(
        report=Report(
            lines=(
                Line("files_swapped", "stop", "The file looks like a shop drawing.", goes_on=False),
            )
        ),
        supervisor=SimpleNamespace(verdict="not_run", why=None),
        run_anyway=True,
    )
    assert check_problems.problems(gate=gate) == [
        "Run anyway was chosen although: The file looks like a shop drawing."
    ]


def test_nothing_went_wrong_means_an_empty_list() -> None:
    assert check_problems.problems() == []


def test_page_words() -> None:
    assert check_problems.page_words([4]) == "page 4"
    assert check_problems.page_words([9, 3, 5]) == "pages 3, 5 and 9"


def test_attach_puts_the_list_in_the_trust_block() -> None:
    import threading

    events: list[Any] = []
    engine = SimpleNamespace(
        _trust_index=0,
        _lock=threading.Lock(),
        blocks=[{"type": "trust", "trust": {"counts": {}}}],
        emit=lambda event, data: events.append((event, data)),
    )
    check_problems.attach(engine, ["One problem."])  # type: ignore[arg-type]
    assert engine.blocks[0]["trust"]["problems"] == ["One problem."]
    assert events[0][0] == "trust"


# ---- the pairing estimate ---------------------------------------------------------------------


def _pair(page: int, *, asked: tuple[str, ...], ref: str, file: str = "v.pdf") -> Any:
    return SimpleNamespace(vendor=SimpleNamespace(file=file, own_page=page, ref=ref), asked=asked)


def test_the_estimate_splits_paired_to_ask_and_none_possible() -> None:
    work = SimpleNamespace(
        file="v.pdf",
        items=[
            SimpleNamespace(
                paired_by="code",
                vendor=SimpleNamespace(own_page=1),
                architect=[SimpleNamespace(file="a.pdf", own_page=4)],
            )
        ],
        skipped=[
            SimpleNamespace(file="v.pdf", drawing="d2", reason=f"{UNPAIRED}: x", page=2),
            SimpleNamespace(file="v.pdf", drawing="d3", reason=f"{UNPAIRED}: y", page=3),
        ],
        across=[
            _pair(1, asked=("i1",), ref="d1"),
            _pair(2, asked=("i1", "i2"), ref="d2"),
            _pair(3, asked=(), ref="d3"),
        ],
    )
    before = preflight_pairs.estimate_from(work, {"d1", "d2"})
    assert before == PairEstimate(3, 1, 1, (3,), confirmed=False)
    after = preflight_pairs.estimate_from(work, set())
    assert after == PairEstimate(3, 1, 0, (3,), unsure=(2,), confirmed=True)


def test_the_estimate_is_kept_with_its_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "page_map.json").write_text("{}", encoding="utf-8")
    assert preflight_pairs.cached(tmp_path) == ("none", None)
    (tmp_path / preflight_pairs.FILE).write_text(
        json.dumps(
            {
                "key": preflight_pairs.fingerprint(tmp_path),
                "estimate": {"vendor_pages": 4, "paired": 1, "to_ask": 3, "none_possible": []},
            }
        ),
        encoding="utf-8",
    )
    state, found = preflight_pairs.cached(tmp_path)
    assert state == "ready" and found == PairEstimate(4, 1, 3, ())
    (tmp_path / "page_match.json").write_text("{}", encoding="utf-8")
    assert preflight_pairs.cached(tmp_path)[0] == "stale"
