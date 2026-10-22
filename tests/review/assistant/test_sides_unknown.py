"""#1866: a combined sheet whose sides code could not decide is never "the vendor's earlier round".

A full check on a one-round set of combined sheets (no GV heading) ended with most items "compared
only with the vendor's earlier round": the content-sides process (#1825) ran out of time on a busy
machine, the full check's plan then assumed every drawing on those pages was the vendor's, and the
round reference (#1854) took the architect's drawing for a vendor round. Here:

- the content-sides answers are kept by the PDF's bytes and the reader's code, so a pack built
  again never reads a page twice; a process stopped by its time limit keeps the pages it finished,
  and every other page says "sides unknown (code timed out)";
- a page with unknown or only assumed sides, or a label code could not place, is never a round
  match; a set with one round has no earlier round (the vendor's other drawing is said as such);
- the plan check says the sides are unknown.

Made-up sheets and values only (`tests/extraction/architect/combined_sheet.py`).

Verification for: `content_sides`, `set_pack._content_sides`, `round_reference`,
`classification._reference_problem`, `plan_check.doubts`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import classification, content_sides, plan_check, proof, set_pack
from app.review.assistant.chat_trust import judged
from app.review.assistant.round_reference import (
    ROUND_MATCH,
    compare_with_architect,
    earlier_round,
    reference_of,
    round_only,
    set_rounds,
    unchanged,
)
from tests.extraction.architect.combined_sheet import combined_sheet

SHOP_KIND = (
    "the vendor's shop drawings (a sheet may carry the architect's drawing beside the vendor's, "
    "with GV's markup on top)"
)
FILE = "shop.pdf"


# ---- content sides: kept, and what a time-out leaves -------------------------------------------


def _listing(pdf: bytes) -> list[dict[str, Any]]:
    return [
        {"name": FILE, "sha256": hashlib.sha256(pdf).hexdigest(), "pages": 1, "kind": SHOP_KIND}
    ]


def _build(tmp_path: Path, pdf: bytes, *, version: str) -> Path:
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    (uploads / FILE).write_bytes(pdf)
    # Two packs of the same PDF (another pack version): the same chat root's `set-packs/`.
    folder = tmp_path / "chats" / "set-packs" / version
    set_pack.ensure_pack(folder, uploads, _listing(pdf), background=False)
    assert set_pack.read_status(folder)["state"] == "ready"
    return folder


def _entry(folder: Path) -> dict[str, Any]:
    (entry,) = json.loads((folder / "page_map.json").read_text())["pages"]
    return dict(entry)


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_DIM_LABELS_IN_PROCESS", "1")
    monkeypatch.setenv("GV_DIM_LABELS_DRAWN_BUDGET_S", "0")


@pytest.mark.usefixtures("in_process")
def test_a_pack_built_again_never_reads_its_pages_sides_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[list[int]] = []
    real = content_sides.each_page

    def counted(pdf: Path, pages: Any) -> Any:
        reads.append(list(pages))
        return real(pdf, pages)

    monkeypatch.setattr(content_sides, "each_page", counted)
    pdf = combined_sheet(headings=False)
    first = _build(tmp_path, pdf, version="a")
    second = _build(tmp_path, pdf, version="b")
    assert reads == [[1]]  # the second pack read the kept answer
    for folder in (first, second):
        entry = _entry(folder)
        assert sorted(h["role"] for h in entry["halves"]) == ["architect", "vendor"]
        assert content_sides.SIDES_UNKNOWN not in entry
    # The cache lives beside the packs, never as a pack of its own.
    cache = tmp_path / "chats" / "set-packs" / set_pack.SIDES_CACHE
    assert len(list(cache.glob("*.json"))) == 1


def test_another_reader_or_another_pdf_is_another_key(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(combined_sheet(headings=False))
    key = content_sides.cache_key(pdf)
    assert key == content_sides.cache_key(pdf)
    pdf.write_bytes(combined_sheet(headings=False, vendor_text=False))
    assert content_sides.cache_key(pdf) != key
    assert key.endswith(content_sides._code_fingerprint())


def _timed_out(stdout: bytes) -> Any:
    def run(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout") or 1, output=stdout)

    return run


def test_a_time_out_says_sides_unknown_and_keeps_no_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GV_DIM_LABELS_IN_PROCESS", raising=False)
    monkeypatch.setenv("GV_DIM_LABELS_DRAWN_BUDGET_S", "0")
    real = subprocess.run

    def run(command: Any, *args: Any, **kwargs: Any) -> Any:
        if content_sides.__name__ in command:
            return _timed_out(b"")(command, *args, **kwargs)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(set_pack.subprocess, "run", run)
    folder = _build(tmp_path, combined_sheet(headings=False), version="a")
    entry = _entry(folder)
    assert entry[content_sides.SIDES_UNKNOWN] == "code timed out"
    assert [h["role"] for h in entry["halves"]] == ["general", "general"]
    # Nothing was kept, so the next build reads the page again.
    cache = tmp_path / "chats" / "set-packs" / set_pack.SIDES_CACHE
    assert not list(cache.glob("*.json"))
    # The plan says it on the page.
    page_map = json.loads((folder / "page_map.json").read_text())
    (doubt,) = plan_check.doubts(page_map, FILE, limit=None)
    assert doubt.kind == "heading" and "sides unknown (code timed out)" in doubt.question
    (words,) = [w for _d, _v, w in plan_check.assumptions(page_map, FILE, {})]
    assert "sides unknown (code timed out)" in words


def test_the_pages_finished_before_a_time_out_are_kept() -> None:
    entries = [
        {
            "page": page,
            "headings": [],
            "halves": [
                {"box": [10, 10, 100, 100], "role": "general"},
                {"box": [10, 200, 100, 300], "role": "general"},
            ],
        }
        for page in (1, 2, 3)
    ]
    decided = [
        {"box": [10, 10, 100, 100], "role": "architect", "reason": "x"},
        {"box": [10, 200, 100, 300], "role": "vendor", "reason": "y"},
    ]
    # Page 1 decided, page 2 read and undecided, page 3's line cut by the time-out.
    out = (
        json.dumps({"page": 1, "decided": decided})
        + "\n"
        + json.dumps({"page": 2, "decided": []})
        + '\n{"page": 3, "deci'
    ).encode()
    read = content_sides.parse_lines(out)
    assert read == {1: decided, 2: []}
    assert content_sides.apply_sides(entries, {p: d for p, d in read.items() if d}) == [1]
    unknown = {p: content_sides.TIMED_OUT for p in (1, 2, 3) if read.get(p) is None}
    assert content_sides.mark_unknown(entries, unknown) == [3]
    assert entries[0]["role"] == "architect+vendor"
    assert content_sides.SIDES_UNKNOWN not in entries[1]  # read: nothing to decide, as before
    assert entries[2][content_sides.SIDES_UNKNOWN] == "code timed out"


def test_a_process_stopped_by_its_time_limit_still_hands_over_what_it_wrote() -> None:
    """What `_content_sides` relies on: `subprocess.run`'s time-out carries the lines written."""
    script = (
        "import json, sys, time; print(json.dumps({'page': 1, 'decided': []}), flush=True); "
        "time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as stopped:
        subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=3, check=True)
    assert content_sides.parse_lines(stopped.value.stdout) == {1: []}


def test_the_time_limit_grows_with_the_pages() -> None:
    assert set_pack.sides_timeout(1) < set_pack.sides_timeout(5) < set_pack.sides_timeout(60)
    assert set_pack.sides_timeout(5) >= 5 * set_pack.SIDES_PAGE_S


# ---- the round reference: only where code knows ------------------------------------------------


def _page(page: int, *halves: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"file": FILE, "page": page, "headings": [], "halves": list(halves), **more}


TOP = [0, 0, 600, 300]
BOTTOM = [0, 300, 600, 600]


def _box(x: float, y: float) -> dict[str, Any]:
    return {"page": 2, "x0": x - 5, "y0": y - 3, "x1": x + 5, "y1": y + 3, "file": FILE}


def _item(**changes: Any) -> dict[str, Any]:
    """A helper's item on page 2: the vendor's value (bottom drawing) against the top drawing."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 2,
        "vs_page": 2,
        "item": "Base cabinet: height",
        "category": "cabinet",
        "architect": "3'-0\"",
        "vendor": '36"',
        "status": "same",
        "source": "architect",
        "created_by": "helper:1",
        "checks": {},
        "inches": {"architect": "36", "vendor": "36", "difference": "0"},
        "vendor_box": _box(300, 450),
        "arch_box": _box(300, 150),
    }
    finding.update(changes)
    return finding


def _link(finding: dict[str, Any], page_map: dict[str, Any]) -> Any:
    return compare_with_architect([finding], labels={}, page_map=page_map, pairs={}, main_file=FILE)


def test_a_page_whose_sides_code_could_not_decide_is_never_a_round_match() -> None:
    # The full check's plan assumed every drawing on the page is the vendor's.
    assumed = {"role": "vendor", "heading_by": "code"}
    page_map = {
        "pages": [_page(2, {**assumed, "box": TOP}, {**assumed, "box": BOTTOM})],
    }
    assert reference_of(_item(), page_map, FILE) is None
    assert _link(_item(), page_map).marked == 0
    # Said so by the pack (the content reader timed out), whatever the roles say.
    unknown = {
        "pages": [
            _page(
                2,
                {"role": "architect", "box": TOP},
                {"role": "vendor", "box": BOTTOM},
                sides_unknown="code timed out",
            )
        ]
    }
    assert reference_of(_item(arch_box=_box(300, 450)), unknown, FILE) is None


def test_a_label_code_could_not_place_is_nobody_s() -> None:
    """A label inside a pasted picture on a whole-page vendor drawing: code says "unknown"."""
    page_map = {"pages": [_page(2, {"role": "vendor", "box": [0, 0, 792, 612]})]}
    label = {"id": "D9", "page": 2, "file": FILE, "text": "3'-0\"", "inches": "36"}
    unknown = _item(dims={"architect": {**label, "side": "unknown", "box": [295, 147, 305, 153]}})
    assert reference_of(unknown, page_map, FILE) is None and not round_only(unknown)
    assert _link(unknown, page_map).marked == 0
    # A label code placed on the vendor's drawing still is a round match.
    placed = _item(dims={"architect": {**label, "side": "vendor", "box": [295, 147, 305, 153]}})
    assert round_only(placed)


def test_known_sides_are_still_a_round_match() -> None:
    page_map = {
        "pages": [
            _page(2, {"role": "vendor", "box": TOP}, {"role": "vendor", "box": BOTTOM}),
        ]
    }
    reference = reference_of(_item(), page_map, FILE)
    assert reference is not None and reference.role == "vendor"
    # One assumed drawing beside a headed one: the page's sides are known (as before #1866).
    mixed = {
        "pages": [
            _page(
                2,
                {"role": "vendor", "box": TOP, "heading_by": "code"},
                {"role": "vendor", "box": BOTTOM},
            )
        ]
    }
    assert reference_of(_item(), mixed, FILE) is not None


def _two_vendor_drawings(rounds: tuple[int | None, int | None]) -> dict[str, Any]:
    return {
        "pages": [
            _page(
                2,
                {"role": "vendor", "box": TOP, "round": rounds[0]},
                {"role": "vendor", "box": BOTTOM, "round": rounds[1]},
            )
        ]
    }


def test_a_set_with_one_round_has_no_earlier_round() -> None:
    page_map = _two_vendor_drawings((None, None))
    assert set_rounds(page_map) == 1
    (marked,) = _link(_item(), page_map).changed
    match = marked[ROUND_MATCH]
    assert match["set_rounds"] == 1 and not earlier_round(match)
    assert match["words"] == "Same as the vendor's other drawing on page 2"
    finding = judged(marked)
    # Still never Right and never red: it was not compared with the architect.
    assert round_only(finding) and not proof.code_proved(finding)
    found = proof.proof_of(finding)
    assert found["group"] == "not_compared" and found["not_compared"] == "not_seen"
    assert "earlier round" not in found["reason"] and "the set has one round" in found["reason"]
    assert "earlier round" not in classification.REASON_WORDS[found["not_compared"]]
    # Nor "unchanged since an earlier round", nor "still wrong".
    assert not unchanged(finding)


def test_a_set_with_two_rounds_keeps_the_earlier_round_reason() -> None:
    page_map = _two_vendor_drawings((1, 2))
    finding = _item(arch_box={**_box(300, 150)}, vendor_box={**_box(300, 450)})
    (marked,) = _link(finding, page_map).changed
    match = marked[ROUND_MATCH]
    assert match["set_rounds"] == 2 and earlier_round(match)
    assert match["words"] == "Unchanged since round 1" and unchanged(marked)
    found = proof.proof_of(judged(marked))
    assert found["not_compared"] == "round_only"
    # A match kept before the count was recorded reads as before.
    old = {k: v for k, v in match.items() if k != "set_rounds"}
    assert earlier_round(old)


def test_a_pasted_picture_with_no_heading_is_nobody_s() -> None:
    """The architect's drawing pasted as a picture on the vendor's whole-page drawing."""
    page_map = {
        "pages": [
            _page(2, {"role": "vendor", "box": [0, 0, 792, 612]}, pictures=[[100, 100, 500, 250]])
        ]
    }
    assert reference_of(_item(), page_map, FILE) is None  # its box sits in the picture
    outside = reference_of(_item(arch_box=_box(300, 450)), page_map, FILE)
    assert outside is not None and outside.role == "vendor"
    # A picture GV headed is a drawing with its heading's role.
    headed = {
        "pages": [
            _page(
                2,
                {"role": "vendor", "box": [0, 0, 792, 612]},
                {"role": "architect", "box": [100, 100, 500, 250], "picture": True},
                pictures=[[100, 100, 500, 250]],
            )
        ]
    }
    found = reference_of(_item(), headed, FILE)
    assert found is None or found.role != "vendor"
