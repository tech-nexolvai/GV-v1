"""The dimension labels the agent picks from (#1558): looked up by id, pure code, no PDF here.

Code lists every printed dimension of the set once (`dim_labels.py`, the set pack's `dims.json`):
each has a page id (`D17`), the value as printed, its inches and its box. The agent names a label
(`vendor_dim: "D42"` in `report_finding`, `marks` in `zoom`) and code supplies the rest. This
module finds a label by its id and says how to name it:

- `"D17"` is label 17 of the page the field belongs to (a finding's vendor side: its `page`; its
  architect side: its `vs_page`, else its `page`); `"p23.D17"` (or `"p23 D17"`) names the page;
- a page whose labels are not in `dims.json` yet (the pack is still being made) is read now by
  the text route (a few seconds, once per page and process): its ids are the same as the pack's,
  because the pack numbers the text labels first.

The labels' words for the model (`label_lines`) and the to-do the full check's helpers answer
(`checklist_label_gaps`) are here too. `dim_labels` (which parses PDFs) is loaded by name only
when a page must be read now, so this module stays light (`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from app.review.assistant.finding_checks import to_inches

__all__ = [
    "DIMS_FILE",
    "DimLabels",
    "checklist_label_gaps",
    "label_line",
    "label_lines",
    "labels_for",
    "parse_ref",
]

_log = logging.getLogger(__name__)

DIMS_FILE: Final = "dims.json"
_MODULE: Final = "app.review.assistant.dim_labels"
_REF: Final = re.compile(r"^\s*(?:p\.?\s*(\d{1,4})\s*[.:\s]\s*)?D(\d{1,4})\s*$", re.IGNORECASE)
#: Labels named in one gap line before "and N more".
_GAP_IDS: Final = 12


def parse_ref(ref: object) -> tuple[int | None, int] | None:
    """`"D17"` -> (None, 17); `"p23.D17"` -> (23, 17); anything else -> None."""
    if not isinstance(ref, str):
        return None
    found = _REF.match(ref)
    if found is None:
        return None
    return (int(found.group(1)) if found.group(1) else None), int(found.group(2))


def _inches(label: Mapping[str, Any]) -> Decimal | None:
    try:
        return Decimal(str(label.get("inches")))
    except (InvalidOperation, ValueError):
        return None


def _inches_words(value: object) -> str:
    """Inches as a drawing writes them: `18"`, `14 1/4"`."""
    try:
        exact = Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return str(value)
    whole, rest = divmod(exact, 1)
    if not rest:
        return f'{whole}"'
    return f'{whole} {rest}"' if whole else f'{rest}"'


def label_line(label: Mapping[str, Any]) -> str:
    """One label in a line: `D17 36" (vendor, h, text)`, plus the millimetres' agreement; a
    derived label says its value and where it comes from (`D23 EQ = 18" (equal part of D18, ...)`).
    """
    bits = [str(label.get("side") or "unknown"), str(label.get("orientation") or "")]
    derived = label.get("derived")
    if isinstance(derived, Mapping):
        bits.insert(0, f"equal part of {derived.get('from')}, by code")
        text = f"{label.get('text')} = {_inches_words(label.get('inches'))}"
        return f"{label.get('id')} {text} ({', '.join(b for b in bits if b)})"
    if label.get("origin") == "picture":
        bits.append("read from a pasted picture, two OCR engines")  # #1830
    elif label.get("origin") == "ocr":
        bits.append("read at its spot, two OCR engines")  # #1848
    elif label.get("route") == "drawn":
        bits.append("drawn ink, OCR")
    if label.get("units") == "disagrees":
        bits.append("mm and inches disagree")
    elif label.get("mm_ignored"):
        bits.append("its mm do not match: inches count, mm ignored")
    if label.get("dim_line") is None:
        bits.append("no dimension line found")
    elif label.get("scale_ok") is False:
        bits.append("its line is off the drawing's scale")
    return f"{label.get('id')} {label.get('text')} ({', '.join(b for b in bits if b)})"


def label_lines(labels: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(label_line(label) for label in labels)


class DimLabels:
    """The labels of one set: from the pack's `dims.json`, else read now. Thread-safe."""

    def __init__(self, folder: Path | None, uploads: Path) -> None:
        self.folder = folder
        self.uploads = uploads
        self._lock = threading.Lock()
        self._page_locks: dict[tuple[str, int], threading.Lock] = {}
        self._read: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._dims: tuple[float, dict[tuple[str, int], list[dict[str, Any]]]] | None = None
        self._phrases: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._counts: dict[tuple[str, float], int] = {}

    def _from_pack(self) -> dict[tuple[str, int], list[dict[str, Any]]]:
        if self.folder is None:
            return {}
        path = self.folder / DIMS_FILE
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return {}
        with self._lock:
            if self._dims is not None and self._dims[0] == stamp:
                return self._dims[1]
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        pages: dict[tuple[str, int], list[dict[str, Any]]] = {}
        phrases: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for entry in document.get("pages") or [] if isinstance(document, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("labels"), list):
                key = (str(entry.get("file")), int(entry.get("page") or 0))
                pages[key] = [dict(label) for label in entry["labels"]]
                phrases[key] = [
                    dict(phrase)
                    for phrase in entry.get("phrases") or []
                    if isinstance(phrase, Mapping)
                ]
        with self._lock:
            self._dims = (stamp, pages)
            self._phrases = phrases
        return pages

    def phrases(self, file: str, page: int) -> list[dict[str, Any]]:
        """The page's printed phrases from the pack (`dims.json`: text, box, side), else none."""
        self._from_pack()
        with self._lock:
            return [dict(phrase) for phrase in self._phrases.get((file, page), [])]

    def pages(self) -> dict[tuple[str, int], list[dict[str, Any]]]:
        """Every page's labels code has (the pack's, and those read now), by (file, page), each
        label with its `file` and `page` (#1802: the architect's runs, searched set-wide)."""
        found = dict(self._from_pack())
        with self._lock:
            for key, labels in self._read.items():
                found.setdefault(key, labels)
        return {
            key: [{**label, "file": key[0], "page": key[1]} for label in labels]
            for key, labels in found.items()
        }

    def halves(self, file: str, page: int) -> list[Mapping[str, Any]]:
        """The page's drawing halves from the pack's page map (each with its role and round)."""
        return self._halves(file, page)

    def _halves(self, file: str, page: int) -> list[Mapping[str, Any]]:
        if self.folder is None:
            return []
        try:
            document = json.loads((self.folder / "page_map.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        for entry in document.get("pages") or [] if isinstance(document, Mapping) else []:
            if (
                isinstance(entry, Mapping)
                and entry.get("file") == file
                and entry.get("page") == page
            ):
                return [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
        return []

    def on_page(
        self, file: str, page: int, *, read_now: bool = True
    ) -> list[dict[str, Any]] | None:
        """The page's labels (each with `file` and `page`), or `None` when they cannot be had."""
        found = self._from_pack().get((file, page))
        if found is None:
            key = (file, page)
            with self._lock:
                found = self._read.get(key)
                lock = self._page_locks.setdefault(key, threading.Lock())
            if found is None and read_now:
                pdf = self.uploads / file
                if not pdf.is_file():
                    return None
                if not 1 <= page <= self._page_count(pdf):
                    # A page the file does not have (`p3.D5` named on a one-page file): no labels,
                    # never a read past its end (#1866: it raised IndexError).
                    return None
                with lock:
                    with self._lock:
                        found = self._read.get(key)
                    if found is None:
                        try:
                            module = import_module(_MODULE)
                            found = list(
                                module.page_labels(pdf, page, self._halves(file, page)).labels
                            )
                        except Exception as failed:  # noqa: BLE001 - no labels, the old path
                            _log.warning(
                                "dimension labels: page not read: %s", type(failed).__name__
                            )
                            return None
                        with self._lock:
                            self._read[key] = found
        if found is None:
            return None
        return [{**label, "file": file, "page": page} for label in found]

    def _page_count(self, pdf: Path) -> int:
        """How many pages `pdf` has (kept per file and change); 0 when it cannot be opened."""
        try:
            stamp = pdf.stat().st_mtime
        except OSError:
            return 0
        key = (str(pdf), stamp)
        with self._lock:
            known = self._counts.get(key)
        if known is not None:
            return known
        try:
            count = len(import_module("pypdf").PdfReader(str(pdf)).pages)
        except Exception:  # noqa: BLE001 - an unreadable file has no page to read
            count = 0
        with self._lock:
            self._counts[key] = count
        return count

    def resolve(self, ref: object, file: str, page: int) -> dict[str, Any]:
        """The label `ref` names (`D17` on `page`, or `p23.D17`). Raises `LookupError` with a
        plain reason the model can act on."""
        parsed = parse_ref(ref)
        if parsed is None:
            raise LookupError(f"{ref!r} is not a label id (D17, or p23.D17 for another page)")
        named_page, number = parsed
        where = named_page or page
        labels = self.on_page(file, where)
        if labels is None:
            pdf = self.uploads / file
            count = self._page_count(pdf) if pdf.is_file() else 0
            if count and not 1 <= where <= count:
                raise LookupError(
                    f"{file} has no page {where} (it has {count}): name the label on its own "
                    "page and file"
                )
            raise LookupError(
                f"the labels of page {where} cannot be read: give the value and its box instead"
            )
        for label in labels:
            if label.get("id") == f"D{number}":
                return label
        raise LookupError(
            f"page {where} has no label D{number} (it has {len(labels)}): zoom with marks: true "
            "to see them"
        )

    def in_area(
        self, file: str, page: int, box: Sequence[float], *, read_now: bool = True
    ) -> list[dict[str, Any]]:
        """The labels whose box is inside `box` (or mostly)."""
        labels = self.on_page(file, page, read_now=read_now) or []
        x0, y0, x1, y1 = box
        out: list[dict[str, Any]] = []
        for label in labels:
            lx0, ly0, lx1, ly1 = label["box"]
            cx, cy = (lx0 + lx1) / 2, (ly0 + ly1) / 2
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                out.append(label)
        return out


_SHARED: dict[tuple[str, str], DimLabels] = {}
_SHARED_LOCK: Final = threading.Lock()


def labels_for(folder: Path | None, uploads: Path) -> DimLabels:
    """One `DimLabels` per set pack and uploads folder in this process (the findings, the zoom
    tool and the checklist share the pages read now)."""
    key = (str(folder), str(uploads))
    with _SHARED_LOCK:
        found = _SHARED.get(key)
        if found is None:
            if len(_SHARED) > 64:
                _SHARED.clear()
            found = _SHARED[key] = DimLabels(folder, uploads)
        return found


# ---- the full check's to-do ----------------------------------------------------------------------

_NOT_ON: Final = re.compile(r"not on (?:the )?architect\s*:\s*([^;\n]*)", re.IGNORECASE)
_IDS: Final = re.compile(r"(?:p\.?\s*\d{1,4}\s*[.:\s]\s*)?D\d{1,4}", re.IGNORECASE)
#: A report line about one page: `CHECKLIST p23: ...` or `LABELS p23 not on architect: D3, D9`.
_LINE: Final = re.compile(r"(?:CHECKLIST|LABELS)\s+p\.?\s*(\d+)\b\s*:?(.*)", re.IGNORECASE)


def _to_do(labels: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The vendor labels of a page: its labels on a vendor drawing, or (a page with no drawing
    labelled vendor) those on unlabelled drawings."""
    vendor = [label for label in labels if label.get("side") == "vendor"]
    if vendor:
        return vendor
    return [label for label in labels if label.get("side") == "unknown"]


def _box_hit(label: Mapping[str, Any], box: object, page: int) -> bool:
    if not isinstance(box, Mapping) or box.get("page") not in (None, page):
        return False
    try:
        x0, y0, x1, y1 = (float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return False
    lx0, ly0, lx1, ly1 = (float(v) for v in label["box"])
    cx, cy = (lx0 + lx1) / 2, (ly0 + ly1) / 2
    return x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2


def checklist_label_gaps(
    report: str,
    labels_by_page: Mapping[int, Sequence[Mapping[str, Any]]],
    findings: Iterable[Mapping[str, Any]],
    strings: Iterable[Mapping[str, Any]] = (),
    readings: Iterable[Mapping[str, Any]] = (),
) -> dict[int, str]:
    """Per page, the vendor labels nobody handled, in plain words (no entry: all handled).

    A label is handled when a finding names it (`dims`) or its box holds it, when a recorded
    string on the page prints its value (a part or the overall) or holds it, when a reading on the
    page holds it, or when the helper's CHECKLIST line for the page names it under `not on
    architect:`."""
    said: dict[int, set[str]] = {}
    for match in _LINE.finditer(report or ""):
        page = int(match.group(1))
        for listed in _NOT_ON.finditer(match.group(2)):
            for ref in _IDS.findall(listed.group(1)):
                parsed = parse_ref(ref)
                if parsed is not None:
                    said.setdefault(parsed[0] or page, set()).add(f"D{parsed[1]}")
    findings = list(findings)
    strings = list(strings)
    readings = list(readings)
    gaps: dict[int, str] = {}
    for page, labels in sorted(labels_by_page.items()):
        left: list[str] = []
        for label in _to_do(labels):
            ident = str(label.get("id"))
            if ident in said.get(page, set()):
                continue
            if any(_named(finding, label, page) for finding in findings):
                continue
            if any(_in_string(entry, label, page) for entry in strings):
                continue
            if any(
                entry.get("page") == page and _box_hit(label, entry.get("box"), page)
                for entry in readings
            ):
                continue
            left.append(ident)
        if left:
            shown = ", ".join(left[:_GAP_IDS]) + (
                f" and {len(left) - _GAP_IDS} more" if len(left) > _GAP_IDS else ""
            )
            gaps[page] = (
                f"{len(left)} of {len(_to_do(labels))} vendor dimension labels not handled "
                f"(match, string, or 'not on architect'): {shown}"
            )
    return gaps


def _named(finding: Mapping[str, Any], label: Mapping[str, Any], page: int) -> bool:
    dims = finding.get("dims") or {}
    for side in ("vendor", "architect"):
        named = dims.get(side) if isinstance(dims, Mapping) else None
        if (
            isinstance(named, Mapping)
            and named.get("page") == page
            and named.get("id") == label.get("id")
        ):
            return True
    return finding.get("page") == page and _box_hit(label, finding.get("vendor_box"), page)


def _in_string(entry: Mapping[str, Any], label: Mapping[str, Any], page: int) -> bool:
    if entry.get("page") != page or entry.get("side") not in (None, "vendor"):
        return False
    if _box_hit(label, entry.get("box"), page):
        return True
    wanted = _inches(label)
    values = [*(entry.get("parts") or []), entry.get("printed_total")]
    for value in values:
        if isinstance(value, str) and to_inches(value) == wanted:
            return True
    return False
