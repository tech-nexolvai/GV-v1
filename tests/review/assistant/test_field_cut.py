"""The vendor's field cut (`field_cut`): never compared with the architect; the top allows it.

Made-up labels and values only (no client values). Settled with GV: 1" per wall end, added at the
factory and trimmed on site; a project value replaces it.
"""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import field_cut
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.field_cut import page_field_cuts, per_end_from, vet
from app.review.assistant.sandbox import SandboxRefused
from tests.review.assistant.test_chat_findings import workspace_with_pdf


def _label(label_id: str, side: str, text: str, inches: str, box: list[float]) -> dict[str, Any]:
    return {
        "id": label_id, "text": text, "inches": inches, "box": box, "side": side,
        "orientation": "h", "route": "text", "dim_line": None,
    }  # fmt: skip


#: A vendor top: a 61" overall over the 60" top, the 1" field cut at its wall end, FIELD CUT
#: printed beside it; the architect's 60" and its 1" overhang at the other end.
LABELS = [
    _label("D1", "vendor", '61"', "61", [300, 40, 315, 50]),
    _label("D2", "vendor", '60"', "60", [300, 70, 315, 80]),
    _label("D3", "vendor", '1"', "1", [180, 55, 190, 65]),
    _label("D4", "vendor", '4"', "4", [450, 60, 460, 70]),
    _label("D5", "architect", "60", "60", [300, 400, 315, 410]),
    _label("D6", "architect", "1", "1", [380, 400, 390, 410]),
]
WORDS = [{"text": "FIELD CUT", "box": [60, 90, 110, 100], "side": "vendor"}]


def test_a_field_cut_needs_its_words_on_the_drawing() -> None:
    cuts = page_field_cuts(LABELS, WORDS)
    assert cuts.labels == {"D3"}  # never the architect's 1" (D6), never the 4" splash
    assert cuts.overalls == {"D1": 1}  # the 61" includes one field cut
    assert not page_field_cuts(LABELS, [])  # no FIELD CUT words: an ordinary 1"
    architect_words = [{**WORDS[0], "side": "architect"}]
    assert not page_field_cuts(LABELS, architect_words)


def test_the_words_beside_a_lone_label_name_it() -> None:
    lone = [_label("D3", "vendor", '1"', "1", [60, 105, 70, 115])]
    assert page_field_cuts(lone, WORDS).labels == {"D3"}
    far = [_label("D3", "vendor", '1"', "1", [700, 500, 710, 510])]
    assert not page_field_cuts(far, WORDS).labels


def test_a_project_field_cut_replaces_gvs_one_inch() -> None:
    rules = (
        "| Setting | Value | From | Used by |\n"
        '| Field cut per wall end (`field_cut`) | 1 1/2" | project value | CT-WIDTH-001 |\n'
    )
    assert per_end_from(rules) == Fraction(3, 2)
    assert per_end_from(None) == field_cut.DEFAULT_PER_END == 1
    labels = [
        _label("D1", "vendor", '61 1/2"', "61.5", [300, 40, 315, 50]),
        _label("D2", "vendor", '60"', "60", [300, 70, 315, 80]),
        _label("D3", "vendor", '1 1/2"', "1.5", [180, 55, 190, 65]),
    ]
    assert page_field_cuts(labels, WORDS, Fraction(3, 2)).labels == {"D3"}
    assert not page_field_cuts(labels, WORDS).labels  # 1 1/2" is not GV's 1"


def _dims(vendor: str, architect: str | None = None) -> dict[str, Any]:
    by_id = {label["id"]: label for label in LABELS}
    out: dict[str, Any] = {"vendor": {**by_id[vendor], "page": 1}}
    if architect is not None:
        out["architect"] = {**by_id[architect], "page": 2}
    return out


def test_vet_refuses_the_field_cut_against_a_drawing_and_allows_the_top() -> None:
    cuts = page_field_cuts(LABELS, WORDS)
    refused = vet(_dims("D3", "D6"), status="same", source="architect", architect="1", cuts=cuts)
    assert refused is not None and refused.outcome == "refuse"
    assert "never compared with the architect" in refused.words
    # A value only (no architect label) against the architect's drawing: refused too.
    by_value = vet(_dims("D3"), status="different", source="architect", architect='2"', cuts=cuts)
    assert by_value is not None and by_value.outcome == "refuse"
    allowed = vet(
        _dims("D1", "D5"), status="different", source="architect", architect="60", cuts=cuts
    )
    assert allowed is not None and allowed.outcome == "allowed" and allowed.field_cuts == 1
    assert '61"' in allowed.words and '60"' in allowed.words
    # Not the architect's value plus the field cut: the rule says nothing (the item stays).
    assert (
        vet(_dims("D1"), status="different", source="architect", architect='58"', cuts=cuts) is None
    )
    # The vendor's own string check is not a comparison with a drawing.
    assert (
        vet(_dims("D1"), status="different", source="vendor_arithmetic", architect='60"', cuts=cuts)
        is None
    )
    assert (
        vet(_dims("D4", "D5"), status="different", source="architect", architect="", cuts=cuts)
        is None
    )


def _book(tmp_path: Path) -> FindingBook:
    workspace = workspace_with_pdf(tmp_path, pages=2)
    folder = tmp_path / "pack"
    folder.mkdir()
    file = json.loads((workspace.root / "uploads.json").read_text())[0]["name"]
    vendor = [label for label in LABELS if label["side"] == "vendor"]
    architect = [label for label in LABELS if label["side"] == "architect"]
    dims = {
        "version": 1,
        "files": [file],
        "pages": [
            {"file": file, "page": 1, "labels": vendor, "phrases": WORDS},
            {"file": file, "page": 2, "labels": architect, "phrases": []},
        ],
    }
    (folder / "dims.json").write_text(json.dumps(dims), encoding="utf-8")
    workspace = dataclasses.replace(workspace, set_pack=folder)
    listing = json.loads((workspace.root / "uploads.json").read_text())
    return FindingBook(workspace=workspace, listing=listing, page_count=2)


def _item(**given: Any) -> dict[str, Any]:
    return {
        "page": 1, "item": "Vanity top: length", "category": "other", "status": "different",
        "verified": False, "source": "architect", "why": "They differ.", **given,
    }  # fmt: skip


def test_the_book_never_records_the_field_cut_against_the_architect(tmp_path: Path) -> None:
    book = _book(tmp_path)
    with pytest.raises(SandboxRefused, match="field cut"):
        book.report(
            _item(vendor_dim="D3", architect_dim="p2.D6", status="same", vs_page=2), "helper:1"
        )
    assert not book.live()
    # The vendor's other 1"-free sizes are recorded as before.
    finding, _notes = book.report(
        _item(vendor_dim="D4", architect_dim="p2.D6", vs_page=2), "helper:1"
    )
    assert finding["status"] == "different" and "field_cut" not in finding


def test_the_tops_length_allows_the_field_cut(tmp_path: Path) -> None:
    book = _book(tmp_path)
    finding, notes = book.report(
        _item(vendor_dim="D1", architect_dim="p2.D5", vs_page=2), "helper:1"
    )
    assert finding["status"] == "same"
    assert finding["field_cut"]["outcome"] == "allowed"
    assert finding["why"].startswith('The vendor\'s 61" includes the 1" field cut')
    assert any("field cut" in note for note in notes)
    # A code check's item on the field cut is left out, never shown.
    added = book.add_code_finding(
        {
            **_item(status="same", architect='1"', vendor='1"'),
            "dims": {"vendor": {**LABELS[2], "page": 1}, "architect": {**LABELS[5], "page": 2}},
        },
        created_by="pairs",
    )
    assert added is None


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("FIELD CUT", True),
        ('1" FIELD CUT AT WALL', True),
        ("FIELD CUT ON SITE.", True),
        ("(FIELD-CUT, TYP.)", True),
        # Sentences that only use the words: a filler trimmed on site, a note on the overall.
        ('(5" FILLER TO FIELD CUT', False),
        ("Fillers will be field cut as per requirement", False),
        ('dimensions including (extra top) field cut: 92" W', False),
        ("SCRIBE", False),
    ],
)
def test_only_a_field_cut_label_counts_as_its_words(text: str, label: bool) -> None:
    assert field_cut.field_cut_label_words(text) is label
    words = [{"text": text, "box": WORDS[0]["box"], "side": "vendor"}]
    assert bool(page_field_cuts(LABELS, words).labels) is label
