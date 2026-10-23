"""An architect page in a set of two files is the architect file's own page, never the vendor's.

A real two-file upload (the vendor's PDF and the architect's PDF) showed a helper naming an
architect label it had read on the architect's own page, with no page given: code looked it up on
the architect's page with the VENDOR's page number (another hotel's sheet) and read a keynote there
as "the architect's" value. A verifier gave a vendor string the architect's value read the same
way, and the architect guard searched every architect page for a run of the same sizes, so a vendor
page with no architect drawing paired with it got "the architect prints ..." from an unrelated one.

- `fill_from_labels`: in a two-file set an architect label is looked up on the page the label
  names (`p12.D7`; the work list's `p1012.D7` is page 12) or on `vs_page`, never on the vendor's
  page number by default;
- `typed_architect_spot`: an architect box typed without a page, or on the vendor's own page
  number, with no `vs_page` and no file named, is refused; `p1012`-style numbers become page 12;
- `apply_proofs`: a vendor string never takes an architect label or box (its "should be" is code's
  sum);
- the roster's architect guard: separate uploads search only the architect pages paired with the
  vendor page, and none when nothing is paired.

Made-up pages and values only. No network, no paid call.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster
from app.review.assistant.dim_findings import (
    LabelRefused,
    fill_from_labels,
    own_architect_page,
    typed_architect_spot,
)
from app.review.assistant.dim_index import parse_ref

VENDOR = "vendor.pdf"
ARCHITECT = "architect.pdf"


def _label(file: str, page: int, number: int, text: str, inches: str, side: str) -> dict[str, Any]:
    return {
        "id": f"D{number}",
        "file": file,
        "page": page,
        "text": text,
        "inches": inches,
        "side": side,
        "route": "text",
        "box": [100.0, 100.0, 120.0, 110.0],
    }


class _Labels:
    """The set's labels by (file, page), looked up as `DimLabels.resolve` does."""

    def __init__(self, *labels: dict[str, Any]) -> None:
        self.by_page: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for label in labels:
            self.by_page.setdefault((label["file"], label["page"]), []).append(label)

    def resolve(self, ref: object, file: str, page: int) -> dict[str, Any]:
        parsed = parse_ref(ref)
        if parsed is None:
            raise LookupError(f"{ref!r} is not a label id")
        named, number = parsed
        for label in self.by_page.get((file, named or page), []):
            if label["id"] == f"D{number}":
                return label
        raise LookupError(f"page {named or page} has no label D{number}")


#: The architect's page 12 prints 36" as D7; its page 30 (the vendor's page number) prints a
#: keynote "8.2" as D7 too; the vendor's page 30 prints 37".
LABELS = _Labels(
    _label(ARCHITECT, 12, 7, '36"', "36", "architect"),
    _label(ARCHITECT, 30, 7, "8.2", "8.2", "architect"),
    _label(VENDOR, 30, 3, '37"', "37", "vendor"),
)


def _fill(raw: dict[str, Any], *, architect: str | None = ARCHITECT) -> dict[str, Any]:
    out, _dims, _notes = fill_from_labels(
        {"page": 30, **raw}, LABELS, vendor_file=VENDOR, architect_file=architect
    )
    return out


def test_a_bare_architect_label_is_never_read_on_the_vendors_page_number() -> None:
    with pytest.raises(LabelRefused, match="never read as the architect's"):
        _fill({"architect_dim": "D7", "vendor_dim": "D3"})


def test_an_architect_label_named_with_its_page_or_vs_page_is_read_there() -> None:
    by_vs_page = _fill({"architect_dim": "D7", "vs_page": 12})
    assert by_vs_page["architect"] == '36"' and by_vs_page["arch_box"]["page"] == 12
    assert by_vs_page["arch_box"]["file"] == ARCHITECT
    by_ref = _fill({"architect_dim": "p12.D7"})
    assert by_ref["architect"] == '36"' and by_ref["vs_page"] == 12
    # The work list's number for the architect's page 12 (p1012).
    by_set_page = _fill({"architect_dim": "p1012.D7"})
    assert by_set_page["architect"] == '36"' and by_set_page["vs_page"] == 12


def test_a_label_naming_the_vendors_own_page_or_another_than_vs_page_is_refused() -> None:
    with pytest.raises(LabelRefused, match="never read as the architect's"):
        _fill({"architect_dim": "p30.D7"})
    with pytest.raises(LabelRefused, match="name one architect page"):
        _fill({"architect_dim": "p12.D7", "vs_page": 14})
    # Said in so many words (vs_page is the vendor's page number): the model's word, read there.
    assert _fill({"architect_dim": "D7", "vs_page": 30})["architect"] == "8.2"


def test_a_combined_set_still_reads_the_same_page() -> None:
    """GV's combined sheets: one file, the architect's drawing on the vendor's page."""
    labels = _Labels(_label(VENDOR, 30, 7, '36"', "36", "architect"))
    out, _dims, _notes = fill_from_labels(
        {"page": 30, "architect_dim": "D7"}, labels, vendor_file=VENDOR, architect_file=VENDOR
    )
    assert out["architect"] == '36"' and out["arch_box"]["page"] == 30


def test_a_typed_architect_box_needs_the_architects_own_page() -> None:
    box = {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}
    spot = {"vendor_file": VENDOR, "architect_file": ARCHITECT}
    with pytest.raises(LabelRefused, match="never read as the architect's"):
        typed_architect_spot({"page": 30, "arch_box": box}, None, **spot)
    with pytest.raises(LabelRefused, match="never read as the architect's"):
        typed_architect_spot({"page": 30, "arch_box": {**box, "page": 30}}, None, **spot)
    named = typed_architect_spot({"page": 30, "arch_box": {**box, "page": 1012}}, None, **spot)
    assert named["arch_box"]["page"] == 12
    assert typed_architect_spot({"page": 30, "vs_page": 1012}, None, **spot)["vs_page"] == 12
    # vs_page given now or on the finding being updated, or the architect's file named: kept.
    assert typed_architect_spot({"page": 30, "vs_page": 30, "arch_box": box}, None, **spot)
    assert typed_architect_spot({"arch_box": box}, {"page": 30, "vs_page": 12}, **spot)
    filed = {**box, "page": 30, "file": ARCHITECT}
    assert typed_architect_spot({"page": 30, "arch_box": filed}, None, **spot)["arch_box"] == filed
    # A combined set: as given.
    alone = {"vendor_file": VENDOR, "architect_file": VENDOR}
    assert typed_architect_spot({"page": 30, "arch_box": box}, None, **alone)["arch_box"] == box


def test_own_architect_page() -> None:
    assert own_architect_page(12) == 12
    assert own_architect_page(1012) == 12
    assert own_architect_page(2000) == 1000


# ---- a verifier's proof for a vendor string ------------------------------------------------------


class _ProofBook:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.findings = findings
        self.changes: list[dict[str, Any]] = []

    def live(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self.findings]

    def update(self, change: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        self.changes.append(change)
        return {**self.findings[0], "architect": "x", "vendor": "y"}, []


def test_a_vendor_string_never_takes_an_architect_spot_from_a_proof() -> None:
    string = {"id": "F5", "page": 30, "source": "vendor_arithmetic", "status": "different"}
    book = _ProofBook([string])
    engine = SimpleNamespace(
        book=book, finding_lock=threading.Lock(), _show_finding=lambda finding: None
    )
    report = (
        '{"proofs":[{"finding_id":"F5","vendor_dim":"D9","architect_dim":"D45",'
        '"arch_box":{"page":30,"x0":1,"y0":2,"x1":3,"y1":4},"outcome":"inputs_found","why":""}]}'
    )
    full_check_roster.apply_proofs(engine, report, ["F5"])  # type: ignore[arg-type]
    assert book.changes == [{"finding_id": "F5", "vendor_dim": "D9"}]


# ---- the architect guard's search ---------------------------------------------------------------


def _run(
    listing: list[dict[str, Any]], pairs: dict[int, tuple[tuple[str, int], ...]] | None
) -> Any:
    pages = {
        (ARCHITECT, 12): [{"id": "D1", "side": "architect", "file": ARCHITECT, "page": 12}],
        (ARCHITECT, 20): [{"id": "D2", "side": "architect", "file": ARCHITECT, "page": 20}],
        (VENDOR, 30): [{"id": "D3", "side": "vendor", "file": VENDOR, "page": 30}],
    }
    book = SimpleNamespace(
        listing=listing,
        main_file=VENDOR,
        dim_labels=SimpleNamespace(pages=lambda: pages),
    )
    cross = None if pairs is None else SimpleNamespace(pairs=pairs)
    return full_check_roster._Run(SimpleNamespace(book=book, _cross=cross))  # type: ignore[arg-type]


TWO_FILES = [
    {"name": VENDOR, "kind": "the vendor's shop drawings"},
    {"name": ARCHITECT, "kind": "the architect's drawings"},
]


def test_separate_uploads_search_only_the_paired_architect_pages() -> None:
    run = _run(TWO_FILES, {30: ((ARCHITECT, 12),)})
    found = run._architect_pages({"page": 30})
    assert [[label["id"] for label in labels] for labels in found] == [["D1"]]
    # No architect drawing paired with the vendor page: nothing is "the architect's".
    assert run._architect_pages({"page": 31}) == []
    assert _run(TWO_FILES, None)._architect_pages({"page": 30}) == []


def test_combined_sheets_still_search_every_architect_page() -> None:
    run = _run([{"name": VENDOR, "kind": "GV's combined sheets"}], None)
    found = run._architect_pages({"page": 30})
    assert sorted(label["id"] for labels in found for label in labels) == ["D1", "D2"]
