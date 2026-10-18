"""Code-paired dimensions as findings by themselves: Wrong or Right with proof, $0, no AI (#1632).

The set pack's `pairs.json` holds architect against vendor dimensions code paired by itself
(`paired_by: code`): by what both ends land on (`dim_anchors`: walls, the floor, the same outlet) or
by drawn position (`dim_pairs`). A pair code made is proof by itself that the two numbers measure
the same thing (rule R4). Until now such a pair counted only when the AI happened to report the
item; on Set 3 the AI never reported two outlet spacings code had paired (s3-28, s3-29), so they
stayed out of the answer. This module turns every pair into an item by itself:

- **The values** are the printed labels code read (`dims.json`), compared exactly: more than 1/16"
  apart is Wrong (the architect's value is what it should be), else Right. Both sides must be
  printed: a pair of an architect's chain with one vendor label (`kind: sum`) is never Wrong, since
  the architect never prints that total (orchestrator, 2026-10-18). When the chain's sum differs
  from the vendor's label the item is Not compared, "Not reviewed: architect gives no overall"
  ("architect's parts add to 110\"; vendor shows 107 3/4\""); when it agrees it is Right.
- **The item** is named from what the pair's ends land on ("left wall to outlet 1 of the row at
  46\" (centre)"), under the page's area. The pair defines the item, never the value: a 9" on the
  same page that belongs to another item is a different item.
- **One item once.** The same pair printed twice (two vendor labels of one value against the same
  architect dimensions) is one item. An item the answer already has keeps its id: an AI finding
  that names the same vendor label (or, without labels, the same two values at the same spot) is
  re-proved by code's pair (`proved_changes`: code's values, status and pairing win on proof).
- **Product scope**: the finding book files a code-made item by its words, its page's words and its
  area (`product_scope.refiled`), as for the other code checks; an out-of-scope item is not
  recorded.

The full check runs it at its finish next to the vendor consistency and finish checks
(`add_to_answer`), before the free escalation and the proof groups.

Measured on Set 3 ($0, a local replay of a stored full check on a fresh set pack): Wrong 16 -> 18,
Right 24 -> 33, Not compared 65 -> 66; 0 false reds against the answer key (`score.py`'s matching)
and no disputed key item in red. 2 Wrong and 9 Right added; 2 of the answer's own items re-proved
to Wrong, 2 moved to "architect gives no overall" (summed chains), 1 such chain added.
Verification: `tests/review/assistant/test_pair_findings.py` (made-up labels).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CREATED_BY",
    "TOLERANCE",
    "add_to_answer",
    "check_pack",
    "findings_from",
    "proved_changes",
    "same_item",
]

CREATED_BY: Final = "pairs"
#: Two printed values further apart than this are different (a sixteenth: the drawings' own step).
TOLERANCE: Final = Fraction(1, 16)
_LANDS: Final = re.compile(r"land on:\s*(?P<span>.+?),\s*on both drawings", re.IGNORECASE)
_AREA_NUMBER: Final = re.compile(r"^\s*\d{1,3}\s*[.)]\s*")
#: The sources of an item that compares the vendor with a drawing (the advocate's).
_PAIRED_SOURCES: Final = frozenset({"architect", "brand_prototype", "gv_review"})


def _fraction(value: object) -> Fraction | None:
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _inches_text(value: Fraction) -> str:
    return str(import_module("app.review.assistant.ledger").format_inches(value))


def _labels(dims: Mapping[str, Any]) -> dict[tuple[str, int, str], Mapping[str, Any]]:
    out: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for entry in dims.get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        file, page = str(entry.get("file")), int(entry.get("page") or 0)
        for raw in entry.get("labels") or []:
            if isinstance(raw, Mapping) and raw.get("id"):
                out[(file, page, str(raw["id"]))] = raw
    return out


def _areas(page_map: Mapping[str, Any] | None) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for entry in (page_map or {}).get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("area"), str):
            name = _AREA_NUMBER.sub("", entry["area"]).strip() or entry["area"].strip()
            if name and isinstance(entry.get("page"), int):
                out[(str(entry.get("file")), entry["page"])] = name
    return out


def _provenance(raw: Mapping[str, Any], file: str, page: int) -> dict[str, Any]:
    """Which label a side was built from (the `printed` check reads it), as the other code checks."""
    return {
        "id": raw.get("id"),
        "page": page,
        "file": file,
        "text": raw.get("text"),
        "inches": raw.get("inches"),
        "route": raw.get("route") or "text",
        "side": raw.get("side"),
        "box": list(raw.get("box") or []),
    }


def _box(boxes: Sequence[Sequence[float]], file: str, page: int) -> dict[str, Any] | None:
    found = [b for b in boxes if isinstance(b, Sequence) and len(b) == 4]
    if not found:
        return None
    return {
        "page": page,
        "x0": min(b[0] for b in found),
        "y0": min(b[1] for b in found),
        "x1": max(b[2] for b in found),
        "y1": max(b[3] for b in found),
        "file": file,
    }


def _span_words(how: object) -> str | None:
    """What the pair's ends land on, from its own `how` ("the left wall to outlet 1 of ...")."""
    found = _LANDS.search(str(how or ""))
    return " ".join(found.group("span").split()) if found else None


def _finding(
    pair: Mapping[str, Any],
    file: str,
    page: int,
    labels: Mapping[tuple[str, int, str], Mapping[str, Any]],
    area: str | None,
) -> dict[str, Any] | None:
    """One pair as a finding, or None when its labels are not both printed, one vendor label and
    values code can read."""
    if pair.get("paired_by") != "code" or pair.get("printed_both") is False:
        return None
    vendors = pair.get("vendor") or []
    architect = pair.get("architect") or {}
    if len(vendors) != 1 or not isinstance(architect, Mapping):
        return None
    arch_page = int(architect.get("page") or page)
    ids = [str(i) for i in architect.get("ids") or [architect.get("id")] if i]
    vendor_raw = labels.get((file, page, str(vendors[0].get("id"))))
    arch_raws = [labels.get((file, arch_page, i)) for i in ids]
    if vendor_raw is None or not arch_raws or any(raw is None for raw in arch_raws):
        return None
    if len(ids) > 1 and any(str(raw.get("route") or "text") != "text" for raw in arch_raws if raw):
        return None  # a chain is summed only from labels printed as text
    vendor_in = _fraction(vendor_raw.get("inches"))
    parts = [_fraction(raw.get("inches")) for raw in arch_raws if raw is not None]
    if vendor_in is None or any(p is None for p in parts):
        return None
    arch_in = sum((p for p in parts if p is not None), Fraction(0))
    chain = len(ids) > 1
    vendor_text = " ".join(str(vendor_raw.get("text") or "").split())
    arch_texts = [" ".join(str(raw.get("text") or "").split()) for raw in arch_raws if raw]
    arch_text = _inches_text(arch_in) if chain else arch_texts[0]
    off = vendor_in - arch_in
    different = abs(off) > TOLERANCE
    span = _span_words(pair.get("how")) or "the same two points on both drawings"
    method = "by what both ends land on" if pair.get("method") == "anchors" else "by drawn position"
    printed = (
        f"the architect's chain {' + '.join(arch_texts)} = {arch_text}" if chain else arch_text
    )
    why = f"Code paired these two dimensions {method} ({span}), no scale used. " + (
        f"The architect prints no overall here, only parts ({' + '.join(arch_texts)} = "
        f"{arch_text}); the vendor prints {vendor_text}. A total the architect never prints is "
        "not compared."
        if different and chain
        else (
            f"The architect prints {printed}; the vendor prints {vendor_text}, "
            f"{_inches_text(abs(off))} {'more' if off > 0 else 'less'}."
            if different
            else f"Both print the same: the architect {printed}, the vendor {vendor_text}."
        )
    )
    vendor_ref = f"p{page}.{vendors[0].get('id')}"
    finding: dict[str, Any] = {
        "page": page,
        "item": f"{area + ': ' if area else ''}{span}"[:160],
        "category": "other",
        "architect": arch_text,
        "vendor": vendor_text,
        "status": ("check" if chain else "different") if different else "same",
        "why": why,
        "verified": False,
        "source": "architect",
        "arch_box": _box([raw.get("box") or () for raw in arch_raws if raw], file, arch_page),
        "vendor_box": _box([vendor_raw.get("box") or ()], file, page),
        "dims": {"vendor": _provenance(vendor_raw, file, page)},
        "paired_by": "code",
        "created_by": CREATED_BY,
        "ledger_ids": [vendor_ref, *(f"p{arch_page}.{i}" for i in ids)],
    }
    if arch_page != page:
        finding["vs_page"] = arch_page
    if chain:
        # What the Not-compared row says (`classification`): the parts, their sum, the vendor's.
        finding["architect_parts"] = {
            "parts": arch_texts,
            "total": arch_text,
            "vendor": _inches_text(vendor_in),
        }
    else:
        assert arch_raws[0] is not None
        finding["dims"]["architect"] = _provenance(arch_raws[0], file, arch_page)
    return finding


def findings_from(
    pairs: Mapping[str, Any] | None,
    dims: Mapping[str, Any] | None,
    page_map: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Every code pair of `pairs.json` (its pages' lists) as one finding, Wrong first; the same pair
    printed twice once."""
    if not isinstance(pairs, Mapping) or not isinstance(dims, Mapping):
        return []
    labels = _labels(dims)
    areas = _areas(page_map)
    out: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in pairs.get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            continue
        file, page = str(entry.get("file")), int(entry["page"])
        for pair in entry.get("pairs") or []:
            if not isinstance(pair, Mapping):
                continue
            made = _finding(pair, file, page, labels, areas.get((file, page)))
            if made is None:
                continue
            arch_ids = tuple(made["ledger_ids"][1:])
            key = (file, page, arch_ids, made["dims"]["vendor"]["inches"])
            if key in seen:
                # The same pair printed twice (two views of one drawing): one item.
                seen[key]["ledger_ids"].append(made["ledger_ids"][0])
                continue
            seen[key] = made
            out.append(made)
    order = {"different": 0, "check": 1, "same": 2}
    return sorted(out, key=lambda f: (order[f["status"]], f["page"]))


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_pack(folder: Path | None) -> list[dict[str, Any]]:
    """The findings of a set pack's code pairs (`pairs.json`, `dims.json`, `page_map.json`)."""
    if folder is None:
        return []
    return findings_from(
        _read(folder / "pairs.json"), _read(folder / "dims.json"), _read(folder / "page_map.json")
    )


# ---- the answer's own items ----------------------------------------------------------------------


def _vendor_ref(finding: Mapping[str, Any]) -> str | None:
    vendor = (finding.get("dims") or {}).get("vendor")
    if not isinstance(vendor, Mapping) or not vendor.get("id"):
        return None
    return f"p{vendor.get('page') or finding.get('page')}.{vendor.get('id')}"


def _overlap(a: object, b: object) -> bool | None:
    """Whether two boxes touch; None when either is missing."""
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return None
    try:
        return not (
            float(a["x1"]) < float(b["x0"])
            or float(b["x1"]) < float(a["x0"])
            or float(a["y1"]) < float(b["y0"])
            or float(b["y1"]) < float(a["y0"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def same_item(
    finding: Mapping[str, Any], live: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The answer's finding that is this pair's item, or None. The pair defines the item, never a
    value: a finding on the vendor label code paired (any of them, when the pair is printed twice).
    Without labels: one on the same page with the same vendor value at the same vendor spot (its
    architect reading may differ: code's pair wins), or, when a spot is missing, the same two
    values. A finding on another vendor label is another item, whatever its values."""
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    refs = {str(r) for r in finding.get("ledger_ids") or []}
    vendor = to_inches(str(finding.get("vendor") or ""))
    architect = to_inches(str(finding.get("architect") or ""))
    for other in live:
        if other.get("page") != finding.get("page"):
            continue
        if str(other.get("source") or "") not in _PAIRED_SOURCES:
            continue
        ref = _vendor_ref(other)
        if ref is not None:
            if ref in refs:
                return other
            continue
        if vendor is None or to_inches(str(other.get("vendor") or "")) != vendor:
            continue
        touching = _overlap(other.get("vendor_box"), finding.get("vendor_box"))
        if touching or (
            touching is None and to_inches(str(other.get("architect") or "")) == architect
        ):
            return other
    return None


def proved_changes(code: Mapping[str, Any], existing: Mapping[str, Any]) -> dict[str, Any]:
    """What an existing finding takes from code's pair of the same item: code's values, status,
    labels and pairing (code wins on proof; a summed chain makes it Not compared); its id, name and
    pictures stay unless it has no architect spot."""
    changes: dict[str, Any] = {
        "architect": code["architect"],
        "vendor": code["vendor"],
        "status": code["status"],
        "dims": dict(code["dims"]),
        "paired_by": "code",
        "why": " ".join(f"{existing.get('why') or ''} {code['why']}".split())[:600],
    }
    if code.get("architect_parts"):
        # A summed chain: Not compared, whatever the answer's own reading said (never red).
        changes["architect_parts"] = dict(code["architect_parts"])
    if not existing.get("arch_box") and code.get("arch_box"):
        changes["arch_box"] = code["arch_box"]
    if not existing.get("vendor_box") and code.get("vendor_box"):
        changes["vendor_box"] = code["vendor_box"]
    return changes


# ---- the full check's hook -----------------------------------------------------------------------


def add_to_answer(engine: Any) -> dict[str, int] | None:
    """The engine's hook (a full check, at its finish, $0): each code pair added through the
    finding book (its checks, trust rule and product scope), or proved onto the answer's own
    finding of the same item. Returns how many were added and re-proved."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    found = check_pack(engine.tools.workspace.set_pack)
    if not found:
        return None
    book = engine.book
    counts = {"added": 0, "proved": 0}
    with engine.finding_lock:
        live = book.live()
        for finding in found:
            existing = same_item(finding, live)
            if existing is not None:
                if existing.get("paired_by") == "code":
                    continue
                changed = book.prove_by_code(str(existing["id"]), proved_changes(finding, existing))
                if changed is not None:
                    engine._show_finding(changed)
                    live = [changed if f.get("id") == changed["id"] else f for f in live]
                    counts["proved"] += 1
                continue
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is not None:
                engine._show_finding(added)
                live.append(added)
                counts["added"] += 1
    return counts
