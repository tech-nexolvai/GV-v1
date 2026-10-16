"""Findings with code-made evidence, page coverage, and the marked-up PDF (chat v2, CONTRACT2).

What makes a GV Review answer different from plain Claude's:

- **Every difference is a finding** the agent records with `report_finding` (page, item, what the
  architect shows, what the vendor shows, status, boxes). The app crops the evidence itself, from
  the ORIGINAL upload page at each box (`chat_drawing.crop_png`, 170 dpi, padded 24 points), into
  the chat's pictures folder, and the screen shows the two crops side by side. A box that is not
  given, or not on its page, simply has no picture: never an error to the reviewer.
- **Coverage** (`set_coverage`): page by page, same / different / not comparable / not opened. The
  map always lists every page of the vendor's file; a page never recorded is "not opened".
- **The marked-up PDF** is drawn by code from an answer's findings (`make_markup_file`): no AI call,
  the same file every time for the same answer.

Helpers report findings too: they are marked `created_by: helper:<n>` and `verified: false`, and
the lead re-reports or updates what it checked itself (`update_finding`, `drop_finding`).

**Which file a box is on.** A box may name its `file` (a name in /mnt/user-data/uploads). Without
one, an architect box is on the architect's file when the set has one, else on the vendor's file
(a combined sheet carries both drawings); a vendor box is on the vendor's file.

Nothing here imports a renderer: `chat_drawing` is loaded by name when it is first needed
(`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from app.review.assistant.sandbox import UPLOADS, ChatWorkspace, SandboxRefused

__all__ = [
    "CITE",
    "COVERAGE_CHECK_PREFIX",
    "DISMISS_RECORDED",
    "DROP_FINDING",
    "FINDING_TOOL_NAMES",
    "HELPER_FINDING_TOOL_NAMES",
    "REPORT_FINDING",
    "SET_COVERAGE",
    "UPDATE_FINDING",
    "FindingBook",
    "MarkupFile",
    "coverage_note",
    "live_findings",
    "main_upload",
    "make_markup_file",
    "make_report_file",
    "markup_name",
    "page_ranges",
    "products_words",
    "read_listing",
]

_log = logging.getLogger(__name__)

CATEGORIES: Final = ("countertop", "cabinet", "other")
STATUSES: Final = ("different", "check", "same")
SOURCES: Final = ("architect", "brand_prototype", "rules", "vendor_arithmetic", "gv_review")
COVERAGE_STATUSES: Final = ("same", "different", "not_comparable", "not_opened")
MAX_FINDINGS: Final = 300
_MAX_ITEM: Final = 160
_MAX_VALUE: Final = 200
_MAX_WHY: Final = 600
_MAX_NOTE: Final = 300
_MAX_COVERAGE_PAGES: Final = 2000
#: How the engine's "pages missing from coverage" note starts (so a test server can tell it apart
#: from a reviewer's question).
COVERAGE_CHECK_PREFIX: Final = "Coverage check:"

_BOX_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page", "x0", "y0", "x1", "y1"],
    "properties": {
        "page": {"type": "integer", "minimum": 1, "description": "Page of that file (from 1)"},
        "x0": {"type": "number"},
        "y0": {"type": "number"},
        "x1": {"type": "number"},
        "y1": {"type": "number"},
        "file": {
            "type": "string",
            "description": "The upload's file name, only when the set has more than one PDF",
        },
    },
    "description": (
        "Where the item is: PDF points of the original upload page as shown, origin top-left "
        "(as pdftotext -bbox prints; points = pixels x 72 / dpi). The app crops the evidence here."
    ),
}
_FINDING_FIELDS: Final[Mapping[str, Any]] = {
    "page": {"type": "integer", "minimum": 1, "description": "The vendor's page"},
    "vs_page": {"type": "integer", "minimum": 1, "description": "The architect's page, if another"},
    "item": {"type": "string", "description": "What it is, short (e.g. 'Vanity top depth')"},
    "category": {"type": "string", "enum": list(CATEGORIES)},
    "architect": {
        "type": "string",
        "description": "What the architect (or the source) shows, as printed, inches",
    },
    "vendor": {"type": "string", "description": "What the vendor shows, as printed, inches"},
    "status": {"type": "string", "enum": list(STATUSES)},
    "why": {"type": "string", "description": "Why it matters, one short line"},
    "verified": {
        "type": "boolean",
        "description": "True only when you read both values on the drawings yourself",
    },
    "arch_box": _BOX_SCHEMA,
    "vendor_box": _BOX_SCHEMA,
    "source": {"type": "string", "enum": list(SOURCES)},
    "parts": {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 40,
        "description": "vendor_arithmetic: the printed parts of a dimension string, as printed",
    },
    "printed_total": {
        "type": "string",
        "description": "vendor_arithmetic: the printed overall the parts should add up to",
    },
    "rule_check_id": {
        "type": "string",
        "description": "rules: the id of the GV check in review_results this finding is about",
    },
    "record_id": {
        "type": "string",
        "description": "The id of a difference GV Review already recorded that this finding is "
        "(a code check's id, or ai-review:p<page>:<key>)",
    },
    "round": {
        "type": "integer",
        "minimum": 1,
        "description": "Which round of the set (1 = first submittal) when the set has rounds; "
        "name the same item the same way in every round",
    },
}
_REQUIRED: Final = frozenset(
    {"page", "item", "category", "architect", "vendor", "status", "verified", "source"}
)

REPORT_FINDING: Final[Mapping[str, Any]] = {
    "name": "report_finding",
    "description": (
        "Record one difference (or a 'check' or a 'same') between the vendor's drawing and the "
        "architect's (or the brand prototype, GV's rules, or the vendor's own arithmetic). The "
        "reviewer sees it in a table with evidence pictures the app crops at your boxes. Returns "
        "its finding_id."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_REQUIRED),
        "properties": dict(_FINDING_FIELDS),
    },
}
UPDATE_FINDING: Final[Mapping[str, Any]] = {
    "name": "update_finding",
    "description": (
        "Change fields of a finding of this answer (e.g. verified true after you checked a "
        "helper's finding, or a corrected value or box). Give only the fields that change."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["finding_id"],
        "properties": {"finding_id": {"type": "string"}, **_FINDING_FIELDS},
    },
}
DROP_FINDING: Final[Mapping[str, Any]] = {
    "name": "drop_finding",
    "description": "Remove a finding of this answer that you checked and found wrong.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["finding_id", "reason"],
        "properties": {
            "finding_id": {"type": "string"},
            "reason": {"type": "string", "description": "Why, one short line"},
        },
    },
}
SET_COVERAGE: Final[Mapping[str, Any]] = {
    "name": "set_coverage",
    "description": (
        "Record which pages of the vendor's file you compared (merged into the coverage map the "
        "reviewer sees). Whole-set answers must account for every page."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["pages"],
        "properties": {
            "pages": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["page", "status"],
                    "properties": {
                        "page": {"type": "integer", "minimum": 1},
                        "pair_page": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "The architect's page it was compared with",
                        },
                        "status": {"type": "string", "enum": list(COVERAGE_STATUSES)},
                        "note": {
                            "type": "string",
                            "description": "Why (needed for not_comparable and not_opened)",
                        },
                    },
                },
            }
        },
    },
}
DISMISS_RECORDED: Final[Mapping[str, Any]] = {
    "name": "dismiss_recorded",
    "description": (
        "Set aside a difference GV Review already recorded (review_results) that you checked and "
        "found not to be a difference, with your reason. Every recorded difference must come "
        "back as a finding or be dismissed."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["record_id", "reason"],
        "properties": {
            "record_id": {"type": "string"},
            "reason": {"type": "string", "description": "Why, one short line"},
        },
    },
}
CITE: Final[Mapping[str, Any]] = {
    "name": "cite",
    "description": (
        "Back a value you state outside a finding (a size, a count read off a drawing, a page "
        "count): where it is printed (page and box), so the app checks it and shows the evidence. "
        "For a fact about a file itself give `fact` instead of a box."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {
            "value": {"type": "string", "description": "The value as you state it"},
            "page": {"type": "integer", "minimum": 1},
            "box": _BOX_SCHEMA,
            "fact": {
                "type": "string",
                "enum": ["page_count", "file_name"],
                "description": "A fact about the file: its page count, or its name",
            },
            "file": {"type": "string", "description": "The upload, when the set has 2+ PDFs"},
        },
    },
}
FINDING_TOOL_NAMES: Final = frozenset(
    {"report_finding", "update_finding", "drop_finding", "set_coverage", "dismiss_recorded", "cite"}
)
HELPER_FINDING_TOOL_NAMES: Final = frozenset({"report_finding", "set_coverage"})


_PRODUCT_WORDS: Final = {"countertop": "Countertops", "cabinet": "Cabinets"}


def products_words(products: Sequence[str]) -> str:
    """["countertop", "cabinet"] as "Countertops and Cabinets"."""
    return " and ".join(_PRODUCT_WORDS.get(p, p) for p in products) or "All products"


# ---- uploads -------------------------------------------------------------------------------------


def read_listing(workspace: ChatWorkspace) -> list[dict[str, Any]]:
    """The chat's uploads as last copied in (`uploads.json`), or none."""
    manifest = workspace.root / "uploads.json"
    try:
        listing = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in listing if isinstance(item, dict)] if isinstance(listing, list) else []


def _is_architect(item: Mapping[str, Any]) -> bool:
    return str(item.get("kind") or "").startswith("the architect")


def main_upload(listing: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The vendor's file (the shop drawings), else the first file."""
    for item in listing:
        if not _is_architect(item):
            return item
    return listing[0] if listing else None


def _architect_upload(listing: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((item for item in listing if _is_architect(item)), None) or main_upload(listing)


def page_ranges(pages: Sequence[int]) -> str:
    """[3, 4, 5, 9] as "3-5, 9"."""
    out: list[str] = []
    ordered = sorted(set(pages))
    start = previous = None
    for page in [*ordered, None]:
        if page is not None and previous is not None and page == previous + 1:
            previous = page
            continue
        if start is not None:
            out.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    return ", ".join(out)


def coverage_note(gaps: Sequence[int], file_name: str | None) -> str:
    where = f" of {file_name}" if file_name else ""
    return (
        f"{COVERAGE_CHECK_PREFIX} pages {page_ranges(gaps)}{where} are not accounted for in your "
        "coverage map (missing, or not opened with no note). Compare them, or record each with "
        "set_coverage and a note saying why not; then give your answer."
    )


# ---- validation ----------------------------------------------------------------------------------


def _text(given: Mapping[str, Any], name: str, limit: int, *, empty: bool = False) -> str:
    value = given.get(name)
    if not isinstance(value, str):
        raise SandboxRefused(f"{name} must be text")
    value = " ".join(value.split())
    if not value and not empty:
        raise SandboxRefused(f"{name} is empty")
    if len(value) > limit:
        raise SandboxRefused(f"{name} is longer than {limit} characters")
    return value


def _whole(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SandboxRefused(f"{name} must be a whole number from 1")
    return value


def _choice(value: object, name: str, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise SandboxRefused(f"{name} must be one of {', '.join(allowed)}")
    return str(value)


@dataclass(slots=True)
class FindingBook:
    """One message's findings and coverage. Thread-safe (helpers report from their threads)."""

    workspace: ChatWorkspace
    listing: Sequence[Mapping[str, Any]] = ()
    page_count: int = 0
    """The pages of the vendor's file (the coverage map's pages); 0 when unknown."""
    crop: Callable[[Path, int, tuple[float, float, float, float]], bytes | None] | None = None
    checker: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """Adds code's checks, inches and trust level to a finding (`chat_trust.apply_checks`)."""
    findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped: dict[str, str] = field(default_factory=dict)
    coverage: dict[int, dict[str, Any]] = field(default_factory=dict)
    coverage_used: bool = False
    dismissed: dict[str, str] = field(default_factory=dict)
    """Recorded differences the lead set aside: record id -> its reason."""
    record_ids: frozenset[str] = frozenset()
    cites: list[dict[str, Any]] = field(default_factory=list)
    _cites: int = 0
    """The recorded differences this answer must account for (set by the engine)."""
    _count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- files and boxes ---------------------------------------------------------------------------

    @property
    def main_file(self) -> str | None:
        item = main_upload(self.listing)
        return None if item is None else str(item["name"])

    def _box(self, raw: object, name: str, *, architect: bool) -> dict[str, Any] | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise SandboxRefused(f"{name} must be an object")
        unknown = set(raw) - {"page", "x0", "y0", "x1", "y1", "file"}
        if unknown:
            raise SandboxRefused(f"{name}: unknown field {min(unknown)!r}")
        page = _whole(raw.get("page"), f"{name}.page")
        coords: list[float] = []
        for key in ("x0", "y0", "x1", "y1"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SandboxRefused(f"{name}.{key} must be a number")
            if not math.isfinite(value) or abs(value) > 100_000:
                raise SandboxRefused(f"{name}.{key} is not a page position")
            coords.append(float(value))
        x0, y0, x1, y1 = coords
        file_name = raw.get("file")
        names = [str(item["name"]) for item in self.listing]
        if file_name is not None:
            if not isinstance(file_name, str):
                raise SandboxRefused(f"{name}.file must be text")
            short = file_name.strip().removeprefix(UPLOADS + "/")
            if names and short not in names:
                raise SandboxRefused(f"{name}.file: there is no upload {short!r}")
            chosen: str | None = short
        else:
            item = _architect_upload(self.listing) if architect else main_upload(self.listing)
            chosen = None if item is None else str(item["name"])
        return {
            "page": page,
            "x0": round(min(x0, x1), 2),
            "y0": round(min(y0, y1), 2),
            "x1": round(max(x0, x1), 2),
            "y1": round(max(y0, y1), 2),
            "file": chosen,
        }

    def _picture(self, box: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
        """A crop of the box's area as a chat picture: (picture id, why there is none)."""
        if box is None:
            return None, "no box given"
        if not box.get("file"):
            return None, "the set's files are not known here"
        pdf = self.workspace.uploads / str(box["file"])
        if not pdf.is_file():
            return None, f"{box['file']} is not in the uploads"
        crop = self.crop or _default_crop
        try:
            png = crop(pdf, int(box["page"]), (box["x0"], box["y0"], box["x1"], box["y1"]))
        except Exception as failed:  # noqa: BLE001 - no picture, never an error to the reviewer
            _log.warning("assistant chat evidence crop failed: %s", type(failed).__name__)
            return None, "the page could not be drawn"
        if png is None:
            return None, f"the box is not on page {box['page']} of {box['file']}"
        picture_id = str(uuid4())
        self.workspace.pictures.mkdir(parents=True, exist_ok=True)
        (self.workspace.pictures / f"{picture_id}.png").write_bytes(png)
        return picture_id, None

    # ---- findings ----------------------------------------------------------------------------------

    def _fields(self, given: Mapping[str, Any], *, partial: bool) -> dict[str, Any]:
        """The finding fields given, checked (`partial`: an update; null clears an optional one)."""
        out: dict[str, Any] = {}
        present = set(given)
        if not partial:
            missing = _REQUIRED - present
            if missing:
                raise SandboxRefused(f"missing argument {min(missing)!r}")
        if "page" in present:
            out["page"] = _whole(given["page"], "page")
            if self.page_count and out["page"] > self.page_count:
                raise SandboxRefused(
                    f"page {out['page']} is not in the vendor's file (pages 1 to {self.page_count})"
                )
        if "vs_page" in present:
            vs = given["vs_page"]
            out["vs_page"] = None if vs is None else _whole(vs, "vs_page")
        if "item" in present:
            out["item"] = _text(given, "item", _MAX_ITEM)
        if "category" in present:
            out["category"] = _choice(given["category"], "category", CATEGORIES)
        for name in ("architect", "vendor"):
            if name in present:
                out[name] = _text(given, name, _MAX_VALUE)
        if "status" in present:
            out["status"] = _choice(given["status"], "status", STATUSES)
        if "why" in present:
            out["why"] = None if given["why"] is None else _text(given, "why", _MAX_WHY, empty=True)
        if "verified" in present:
            if not isinstance(given["verified"], bool):
                raise SandboxRefused("verified must be true or false")
            out["verified"] = given["verified"]
        if "source" in present:
            out["source"] = _choice(given["source"], "source", SOURCES)
        for name, architect in (("arch_box", True), ("vendor_box", False)):
            if name in present:
                out[name] = self._box(given[name], name, architect=architect)
        if "parts" in present:
            parts = given["parts"]
            if parts is None:
                out["parts"] = None
            elif not isinstance(parts, list) or len(parts) > 40:
                raise SandboxRefused("parts must be a list of at most 40 values")
            else:
                out["parts"] = [_text({"part": p}, "part", _MAX_VALUE) for p in parts]
        for name in ("printed_total", "rule_check_id", "record_id"):
            if name in present:
                out[name] = None if given[name] is None else _text(given, name, _MAX_VALUE)
        if "round" in present:
            out["round"] = None if given["round"] is None else _whole(given["round"], "round")
        return out

    def report(self, raw: object, created_by: str) -> tuple[dict[str, Any], list[str]]:
        """A new finding (id, crops made), and the reasons any box has no picture."""
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - set(_FINDING_FIELDS)
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        fields = self._fields(raw, partial=False)
        if created_by != "lead":
            fields["verified"] = False
        with self._lock:
            if len(self.findings) >= MAX_FINDINGS:
                raise SandboxRefused(f"at most {MAX_FINDINGS} findings in one answer")
            self._count += 1
            finding_id = f"F{self._count}"
        arch_picture, arch_why = self._picture(fields.get("arch_box"))
        vendor_picture, vendor_why = self._picture(fields.get("vendor_box"))
        finding = {
            "id": finding_id,
            "page": fields["page"],
            "vs_page": fields.get("vs_page"),
            "item": fields["item"],
            "category": fields["category"],
            "architect": fields["architect"],
            "vendor": fields["vendor"],
            "status": fields["status"],
            "why": fields.get("why"),
            "verified": fields["verified"],
            "arch_box": fields.get("arch_box"),
            "vendor_box": fields.get("vendor_box"),
            "source": fields["source"],
            "parts": fields.get("parts"),
            "printed_total": fields.get("printed_total"),
            "rule_check_id": fields.get("rule_check_id"),
            "record_id": fields.get("record_id"),
            "round": fields.get("round"),
            "arch_picture_id": arch_picture,
            "vendor_picture_id": vendor_picture,
            "created_by": created_by,
        }
        if self.checker is not None:
            finding = self.checker(finding)
        with self._lock:
            self.findings[finding_id] = finding
        notes = [
            f"{side} box: no picture ({why})"
            for side, why, given in (
                ("architect", arch_why, fields.get("arch_box")),
                ("vendor", vendor_why, fields.get("vendor_box")),
            )
            if given is not None and why is not None
        ]
        return dict(finding), notes

    def _known(self, raw: Mapping[str, Any]) -> str:
        finding_id = raw.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise SandboxRefused("finding_id must be text")
        finding_id = finding_id.strip()
        with self._lock:
            if finding_id in self.dropped:
                raise SandboxRefused(f"finding {finding_id} was dropped")
            if finding_id not in self.findings:
                raise SandboxRefused(
                    f"there is no finding {finding_id} in this answer (report it again)"
                )
        return finding_id

    def update(self, raw: object) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - set(_FINDING_FIELDS) - {"finding_id"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        finding_id = self._known(raw)
        changes = self._fields({k: v for k, v in raw.items() if k != "finding_id"}, partial=True)
        if not changes:
            raise SandboxRefused("give at least one field to change")
        notes: list[str] = []
        for box, picture, side in (
            ("arch_box", "arch_picture_id", "architect"),
            ("vendor_box", "vendor_picture_id", "vendor"),
        ):
            if box in changes:
                changes[picture], why = self._picture(changes[box])
                if changes[box] is not None and why is not None:
                    notes.append(f"{side} box: no picture ({why})")
        with self._lock:
            finding = {**self.findings[finding_id], **changes}
        if self.checker is not None:
            finding = self.checker(finding)
        with self._lock:
            if finding_id in self.findings:
                self.findings[finding_id] = finding
        return dict(finding), notes

    def drop(self, raw: object) -> tuple[str, str]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"finding_id", "reason"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        finding_id = self._known(raw)
        reason = _text(raw, "reason", _MAX_NOTE)
        with self._lock:
            dropped = self.findings.pop(finding_id, None) or {}
            self.dropped[finding_id] = reason
            if dropped.get("record_id"):
                # Dropping a recorded difference's finding dismisses it, with the same reason.
                self.dismissed[str(dropped["record_id"])] = reason
        return finding_id, reason

    def dismiss(self, raw: object) -> tuple[str, str]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"record_id", "reason"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        record_id = _text(raw, "record_id", _MAX_VALUE)
        reason = _text(raw, "reason", _MAX_NOTE)
        if self.record_ids and record_id not in self.record_ids:
            raise SandboxRefused(f"there is no recorded difference {record_id!r}")
        with self._lock:
            self.dismissed[record_id] = reason
        return record_id, reason

    def cite(
        self, raw: object, printed: Callable[[Path, int, Mapping[str, Any], str], str]
    ) -> dict[str, Any]:
        """A value the answer states, backed by where it is printed (checked by `printed`) or by
        a fact about the file (checked here)."""
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"value", "page", "box", "fact", "file"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        value = _text(raw, "value", _MAX_VALUE)
        fact = raw.get("fact")
        names = [str(item["name"]) for item in self.listing]
        file_name = raw.get("file")
        if file_name is not None:
            if not isinstance(file_name, str):
                raise SandboxRefused("file must be text")
            file_name = file_name.strip().removeprefix(UPLOADS + "/")
            if names and file_name not in names:
                raise SandboxRefused(f"there is no upload {file_name!r}")
        chosen = file_name or self.main_file
        with self._lock:
            self._cites += 1
            cite_id = f"C{self._cites}"
        cite: dict[str, Any] = {
            "id": cite_id,
            "value": value,
            "fact": None,
            "page": None,
            "box": None,
            "file": chosen,
            "picture_id": None,
            "printed": "not_run",
        }
        if fact is not None:
            cite["fact"] = _choice(fact, "fact", ("page_count", "file_name"))
            item = next((i for i in self.listing if str(i["name"]) == chosen), None)
            if cite["fact"] == "file_name":
                ok = value.removeprefix(UPLOADS + "/") in names
            else:
                ok = item is not None and value.strip() == str(item.get("pages"))
            cite["printed"] = "match" if ok else "not_found"
            with self._lock:
                self.cites.append(cite)
            return dict(cite)
        given_box = raw.get("box")
        if isinstance(given_box, Mapping) and file_name and "file" not in given_box:
            given_box = {**given_box, "file": file_name}
        box = None if given_box is None else self._box(given_box, "box", architect=False)
        page = raw.get("page")
        if box is not None:
            page = box["page"]
        elif page is not None:
            page = _whole(page, "page")
        cite["page"], cite["box"] = page, box
        if box is None:
            cite["printed"] = "no_box"
            with self._lock:
                self.cites.append(cite)
            return dict(cite)
        cite["picture_id"], _why = self._picture(box)
        pdf = self.workspace.uploads / str(box["file"])
        try:
            cite["printed"] = printed(pdf, int(box["page"]), box, value)
        except Exception as failed:  # noqa: BLE001 - a check that cannot run is "not run"
            _log.warning("assistant chat cite check failed: %s", type(failed).__name__)
            cite["printed"] = "not_run"
        with self._lock:
            self.cites.append(cite)
        return dict(cite)

    def seed(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """A finding made by code from a difference GV Review recorded (full check): the lead
        checks it (boxes, verified) or drops it with a reason; it is never silently lost."""
        page = record.get("page")
        if not isinstance(page, int) or page < 1:
            return None
        with self._lock:
            self._count += 1
            finding_id = f"F{self._count}"
        code = record.get("source") == "code"
        finding: dict[str, Any] = {
            "id": finding_id,
            "page": page,
            "vs_page": None,
            "item": " ".join(str(record.get("what") or "Recorded difference").split())[:_MAX_ITEM],
            "category": record.get("category") if record.get("category") in CATEGORIES else "other",
            "architect": str(record.get("architect") or "see GV Review's record")[:_MAX_VALUE],
            "vendor": str(record.get("vendor") or "see GV Review's record")[:_MAX_VALUE],
            "status": "different",
            "why": (str(record["why"])[:_MAX_WHY] if record.get("why") else None),
            "verified": False,
            "arch_box": None,
            "vendor_box": None,
            "source": "rules" if code else "gv_review",
            "parts": None,
            "printed_total": None,
            "rule_check_id": record.get("id") if code else None,
            "record_id": record.get("record_id"),
            "round": None,
            "arch_picture_id": None,
            "vendor_picture_id": None,
            "created_by": "gv_review",
        }
        if self.checker is not None:
            finding = self.checker(finding)
        with self._lock:
            self.findings[finding_id] = finding
        return dict(finding)

    def by(self, created_by: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(f) for f in self.findings.values() if f["created_by"] == created_by]

    def live(self) -> list[dict[str, Any]]:
        """The findings not dropped, in the order they were reported."""
        with self._lock:
            return [dict(f) for f in self.findings.values()]

    def replace(self, finding: dict[str, Any]) -> bool:
        """A finding changed by code (a check's result); False when it was dropped meanwhile."""
        with self._lock:
            if finding["id"] not in self.findings:
                return False
            self.findings[finding["id"]] = finding
            return True

    def pdf_paths(self) -> dict[str, Path]:
        return {
            str(item["name"]): self.workspace.uploads / str(item["name"]) for item in self.listing
        }

    # ---- coverage ----------------------------------------------------------------------------------

    def set_coverage(self, raw: object, created_by: str) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"pages"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        pages = raw.get("pages")
        if not isinstance(pages, list) or not pages:
            raise SandboxRefused("pages must be a list of pages")
        if len(pages) > _MAX_COVERAGE_PAGES:
            raise SandboxRefused(f"at most {_MAX_COVERAGE_PAGES} pages at once")
        entries: list[dict[str, Any]] = []
        for item in pages:
            if not isinstance(item, Mapping):
                raise SandboxRefused("each page must be an object")
            extra = set(item) - {"page", "pair_page", "status", "note"}
            if extra:
                raise SandboxRefused(f"unknown page field {min(extra)!r}")
            page = _whole(item.get("page"), "page")
            if self.page_count and page > self.page_count:
                raise SandboxRefused(
                    f"page {page} is not in the vendor's file (pages 1 to {self.page_count})"
                )
            pair = item.get("pair_page")
            note = item.get("note")
            entries.append(
                {
                    "page": page,
                    "pair_page": None if pair is None else _whole(pair, "pair_page"),
                    "status": _choice(item.get("status"), "status", COVERAGE_STATUSES),
                    "note": (
                        None
                        if note is None
                        else (_text(item, "note", _MAX_NOTE, empty=True) or None)
                    ),
                    "by": created_by,
                }
            )
        with self._lock:
            for entry in entries:
                self.coverage[entry["page"]] = entry
            self.coverage_used = True
        return self.coverage_map()

    def coverage_map(self) -> dict[str, Any]:
        """Every page of the vendor's file (a page never recorded: not opened), and the counts."""
        with self._lock:
            recorded = dict(self.coverage)
        total = max(self.page_count, max(recorded, default=0))
        pages = [
            recorded.get(
                page,
                {"page": page, "pair_page": None, "status": "not_opened", "note": None, "by": None},
            )
            for page in range(1, total + 1)
        ]
        counts = {"total": total, **{status: 0 for status in COVERAGE_STATUSES}}
        for entry in pages:
            counts[entry["status"]] += 1
        return {"pages": [dict(entry) for entry in pages], "counts": counts}

    def coverage_gaps(self) -> list[int]:
        """Pages not accounted for: never recorded, or not opened with no note."""
        return [
            entry["page"]
            for entry in self.coverage_map()["pages"]
            if entry["status"] == "not_opened" and not entry["note"]
        ]


def _default_crop(pdf: Path, page: int, box: tuple[float, float, float, float]) -> bytes | None:
    drawing = import_module("app.review.assistant.chat_drawing")
    png: bytes | None = drawing.crop_png(pdf, page, box)
    return png


# ---- the marked-up PDF ---------------------------------------------------------------------------


def live_findings(blocks: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """An answer's findings as stored, in order, without the dropped ones."""
    return [
        dict(block["finding"])
        for block in blocks or []
        if block.get("type") == "finding" and isinstance(block.get("finding"), Mapping)
    ]


def markup_name(source_name: str, kind: str = "AI markup") -> str:
    stem = source_name[:-4] if source_name.lower().endswith(".pdf") else source_name
    return f"{stem} - {kind}.pdf"[:255]


@dataclass(frozen=True, slots=True)
class MarkupFile:
    name: str
    size: int
    sha256: str


def make_markup_file(
    workspace: ChatWorkspace,
    findings: Sequence[Mapping[str, Any]],
    out: Path,
    kind: str = "AI markup",
) -> MarkupFile:
    """The marked-up copy of the vendor's file, written to `out`: a cloud and a note at each
    finding's vendor box (else its architect box) when that box is on the vendor's file, the draft
    stamp on page 1, and every finding listed again on the notes page. Raises `LookupError` when
    the chat has no copy of the set's PDFs."""
    listing = read_listing(workspace)
    main = main_upload(listing)
    if main is None or not (workspace.uploads / str(main["name"])).is_file():
        raise LookupError("no uploads")
    name = str(main["name"])
    placed: list[tuple[int, Mapping[str, Any], int | None, tuple[float, ...] | None, str]] = []
    for number, finding in enumerate(findings, start=1):
        box = finding.get("vendor_box") or finding.get("arch_box")
        if not isinstance(box, Mapping):
            placed.append((number, finding, None, None, "Not marked on the drawing: no box."))
            continue
        if box.get("file") not in (None, name):
            placed.append(
                (number, finding, None, None, f"Not marked: its box is on {box.get('file')}.")
            )
            continue
        coords = tuple(float(box[key]) for key in ("x0", "y0", "x1", "y1"))
        where = f"Marked on page {box['page']}."
        placed.append((number, finding, int(box["page"]), coords, where))
    drawing = import_module("app.review.assistant.chat_drawing")
    drawing.markup_pdf(workspace.uploads / name, placed, out)
    data = out.read_bytes()
    return MarkupFile(markup_name(name, kind), len(data), hashlib.sha256(data).hexdigest())


def make_report_file(
    workspace: ChatWorkspace,
    summary: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    not_checked: Sequence[str],
    out: Path,
) -> MarkupFile:
    """The full check's report PDF (`chat_drawing.check_report_pdf`), written to `out`."""
    main = main_upload(read_listing(workspace))
    name = "drawing set" if main is None else str(main["name"])
    drawing = import_module("app.review.assistant.chat_drawing")
    drawing.check_report_pdf(out, summary, findings, not_checked)
    data = out.read_bytes()
    return MarkupFile(
        markup_name(name, "AI check report"), len(data), hashlib.sha256(data).hexdigest()
    )
