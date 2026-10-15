"""Anant's Yes/No page for the keyed line answers, and turning his answers into confirmed keys (#1321).

`scripts/pariksha_row_review_page.py`. Invented keys, an invented box map and tiny invented pictures
in temporary folders; no database, no model call, no client value.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from eval.pariksha.keys import KEY_FORMAT, AnswerKey, RowAt, RowTruth, read_key
from eval.pariksha.row_geometry import ROW_BOXES_FORMAT
from eval.smriti.library import read_library

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_library
import pariksha_row_review_page as page_script

REPO = Path(__file__).resolve().parents[2]
PAGE_A = "a" * 64
PAGE_B = "b" * 64
LINE = {"y": 40, "x0": 20, "x1": 160}
CABINETS = {"y": 60, "x0": 30, "x1": 150}


def _picture(shade: int) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (200, 100), (255, 255, shade))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _setup(tmp_path: Path) -> dict[str, Any]:
    pictures = tmp_path / "store" / "reader-questions"
    pictures.mkdir(parents=True)
    shas = {}
    for page, shade in ((PAGE_A, 200), (PAGE_B, 220)):
        data = _picture(shade)
        sha = hashlib.sha256(data).hexdigest()
        (pictures / f"rows-{sha}.png").write_bytes(data)
        shas[page] = sha
    boxes = tmp_path / "boxes.json"
    boxes.write_text(
        json.dumps(
            {
                "format": ROW_BOXES_FORMAT,
                "pictures": [
                    {
                        "picture_sha256": shas[page],
                        "page_sha256": page,
                        "boxes": [CABINETS, LINE],
                        "how": "invented",
                        "px_per_pt": 1,
                    }
                    for page in (PAGE_A, PAGE_B)
                ],
            }
        )
    )
    keys = tmp_path / "keys"
    keys.mkdir()

    def key(name: str, cases: list[dict[str, Any]]) -> Path:
        path = keys / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "format": KEY_FORMAT,
                    "agent": "rekha",
                    "name": name,
                    "version": "1",
                    "row_tolerance_pt": 1,
                    "cases": cases,
                }
            )
        )
        return path

    def case(page: str, answer: dict[str, Any]) -> dict[str, Any]:
        return {
            "family": "slot-row-choice",
            "page_sha256": page,
            "question_id": "p1:row-choice",
            "answer": answer,
            "source": "invented by eye",
        }

    first = key("first", [case(PAGE_A, {"row_at": LINE}), case(PAGE_B, {"kind": "no_countertop"})])
    # A second key holding the same answer to one question: one card, both keys updated.
    second = key("second", [case(PAGE_A, {"row_at": LINE})])
    library = tmp_path / "library.json"
    for path in (first, second):
        assert (
            pariksha_library.main(
                [
                    "register",
                    "--library",
                    str(library),
                    "--key",
                    str(path),
                    "--confirmed-by",
                    "Claude by eye",
                    "--label",
                    "Claude-made, not confirmed by a person",
                ]
            )
            == 0
        )
    names = tmp_path / "names.json"
    names.write_text(json.dumps({PAGE_A: "Invented Set, page 3", PAGE_B: "Invented Set, page 4"}))
    return {
        "library": library,
        "boxes": boxes,
        "pictures": tmp_path / "store",
        "names": names,
        "shas": shas,
        "first": first,
        "second": second,
    }


def _page(setup: dict[str, Any], out: Path) -> int:
    return page_script.main(
        [
            "page",
            "--library",
            str(setup["library"]),
            "--row-boxes",
            str(setup["boxes"]),
            "--pictures",
            str(setup["pictures"]),
            "--page-names",
            str(setup["names"]),
            "--out",
            str(out),
        ]
    )


def test_the_page_has_one_card_per_keyed_line_answer_with_the_picture_first(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    out = tmp_path / "review" / "rows.html"

    assert _page(setup, out) == 0

    html = out.read_text()
    assert html.count('class="card"') == 2  # PAGE_A once though two keys hold it
    assert "Invented Set, page 3" in html and "Invented Set, page 4" in html
    assert html.count("data:image/png;base64,") >= 3  # close-up + whole page, and a whole page
    assert "Copy answers" in html
    for word in ("Yes", "No", "A different line"):
        assert f">{word}<" in html
    # The picture comes before the question on every card.
    first_card = html.split('class="card"')[1]
    assert first_card.index("<img") < first_card.index(">Yes<")
    # Each box on the whole picture can be clicked to name a different line.
    assert len(re.findall(r'data-box="\d"', first_card)) == 2
    assert "Claude-made" in html
    # Written once.
    assert _page(setup, out) == 2


def test_the_page_is_never_written_inside_a_git_checkout(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    inside = REPO / "rows-review-should-not-exist.html"

    assert _page(setup, inside) == 2
    assert not inside.exists()


def _answers(setup: dict[str, Any], *answers: dict[str, Any]) -> Path:
    path = Path(setup["library"]).parent / "answers.json"
    path.write_text(json.dumps({"format": page_script.ANSWERS_FORMAT, "answers": list(answers)}))
    return path


def _answer(setup: dict[str, Any], page: str, said: str, **extra: Any) -> dict[str, Any]:
    first = read_key(setup["first"])
    assert isinstance(first, AnswerKey)
    (case,) = [case for case in first.cases if case.question.page_sha256 == page]
    return {
        "page_sha256": page,
        "question_id": "p1:row-choice",
        "shown_truth": str(case.truth),
        "picture_sha256": setup["shas"][page],
        "said": said,
        **extra,
    }


def _apply(setup: dict[str, Any], answers: Path, *extra: str) -> int:
    return page_script.main(
        [
            "apply",
            "--answers",
            str(answers),
            "--library",
            str(setup["library"]),
            "--row-boxes",
            str(setup["boxes"]),
            "--who",
            "anant",
            "--day",
            "2026-01-02",
            *extra,
        ]
    )


def test_a_yes_marks_the_truth_confirmed_in_every_key_that_holds_it(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    answers = _answers(setup, _answer(setup, PAGE_A, "yes"), _answer(setup, PAGE_B, "unsure"))

    assert _apply(setup, answers, "--update-library") == 0

    library = read_library(setup["library"])
    names = sorted(entry.version for entry in library.entries)
    assert names == ["1+confirmed-2026-01-02", "1+confirmed-2026-01-02"]
    for entry in library.entries:
        key = read_key(Path(entry.path))
        assert isinstance(key, AnswerKey)
        confirmed = {case.question.page_sha256: case.confirmed_by for case in key.cases}
        assert confirmed[PAGE_A] == "anant"
        assert confirmed.get(PAGE_B, "") == ""  # "not sure" changes nothing
        assert "1 of" in entry.confirmed_by
    # The old key files stay as they were: a key is never edited in place.
    assert json.loads(setup["first"].read_text())["version"] == "1"


def test_a_different_line_or_a_no_replaces_the_truth_and_says_who(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    answers = _answers(
        setup,
        _answer(setup, PAGE_A, "other_line", box=1),
        _answer(setup, PAGE_B, "not_among_boxes"),
    )

    assert _apply(setup, answers) == 0

    written = sorted(Path(setup["first"]).parent.glob("first.confirmed-2026-01-02.json"))
    (path,) = written
    key = read_key(path)
    assert isinstance(key, AnswerKey)
    by_page = {case.question.page_sha256: case for case in key.cases}
    assert by_page[PAGE_A].truth == RowTruth(
        box=None, kind="row", at=RowAt.of(CABINETS["y"], CABINETS["x0"], CABINETS["x1"])
    )
    assert by_page[PAGE_A].confirmed_by == "anant"
    assert "box 1" in by_page[PAGE_A].source
    assert by_page[PAGE_B].truth == RowTruth(box=None, kind="not_among_boxes")
    assert by_page[PAGE_B].picture_sha256 == setup["shas"][PAGE_B]


def test_an_answer_to_a_truth_the_key_no_longer_holds_is_refused(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    stale = _answer(setup, PAGE_A, "yes")
    stale["shown_truth"] = "box 7"

    assert _apply(setup, _answers(setup, stale)) == 2
    assert not list(Path(setup["first"]).parent.glob("*.confirmed-*.json"))


@pytest.mark.parametrize(
    "broken",
    [
        {"said": "maybe"},
        {"said": "other_line"},  # no box
        {"said": "other_line", "box": 9},
        {"page_sha256": "e" * 64},
    ],
)
def test_malformed_answers_are_refused(tmp_path: Path, broken: dict[str, Any]) -> None:
    setup = _setup(tmp_path)
    answer = {**_answer(setup, PAGE_A, "yes"), **broken}

    assert _apply(setup, _answers(setup, answer)) == 2
