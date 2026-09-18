"""Prepare unverified gold-set candidates from reviewed-PDF markup annotations.

This is an exploratory annotation aid, not a gold-case author. It does not copy the source PDF or
create an ``answer_key.json``. Instead, it writes a rendered page and mechanical candidate crops to
configured ignored directories; the real evaluator would apply an answer key's human semantic labels
to extracted candidates, and these annotations are not human-confirmed truth.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from eval.gold_set.schema import GoldCase, GroundTruth
from eval.scorecard import render, score_package
from evidence.crop import encode_png

SCHEMA_VERSION = 1
UNVERIFIED = "UNVERIFIED"
PENDING = "PENDING_HUMAN_CONFIRMATION"
DIMENSION_TEXT = re.compile(r'(?<![A-Za-z0-9])\d+(?:-\d+/\d+|/\d+|\.\d+)?"')


@dataclass(frozen=True)
class Candidate:
    """One reviewer-selected dimension annotation, retained verbatim and untyped."""

    candidate_id: str
    project: str
    page: int
    author: str
    raw_text: str
    rect: tuple[float, float, float, float]
    annotation_subtype: str
    annotation_index: int
    reviewer_label: str
    scope_confidence: str


def _read_candidates(csv_path: Path, *, page: int) -> list[Candidate]:
    """Select dimension-shaped FreeText markup without assigning semantics.

    This is the deliberately broad legacy fallback. A focused review batch should pass a selection
    manifest, rather than let syntax decide whether a dimension is in scope.
    """
    candidates: list[Candidate] = []
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        for row in rows:
            if row["page"] != str(page) or row["annotation_role"] != "markup":
                continue
            raw_text = row["contents"]
            # This is deliberately a syntactic filter, not a semantic classifier.  The entire
            # annotation text stays verbatim in the candidate for a person to assess.
            if DIMENSION_TEXT.search(raw_text) is None:
                continue
            rect = tuple(
                float(value.strip())
                for value in row["rect"].removeprefix("[").removesuffix("]").split(",")
            )
            if len(rect) != 4:
                raise ValueError(f"annotation /Rect is not four coordinates: {row['rect']!r}")
            candidates.append(
                Candidate(
                    candidate_id=f"page-{page:02d}-candidate-{len(candidates) + 1:02d}",
                    project=row["project"],
                    page=page,
                    author=row["author_T"],
                    raw_text=raw_text,
                    rect=(rect[0], rect[1], rect[2], rect[3]),
                    annotation_subtype="/FreeText",
                    annotation_index=0,
                    reviewer_label="dimension-looking reviewer markup",
                    scope_confidence="UNCERTAIN_REVIEWER_TO_CLASSIFY",
                )
            )
    if not candidates:
        raise ValueError(f"no dimension-looking /FreeText annotations found on page {page}")
    return candidates


def _rect_from_csv(row: dict[str, str]) -> tuple[float, float, float, float]:
    """Parse an extraction CSV rectangle without changing its coordinate system."""
    rect = tuple(
        float(value.strip()) for value in row["rect"].removeprefix("[").removesuffix("]").split(",")
    )
    if len(rect) != 4:
        raise ValueError(f"annotation /Rect is not four coordinates: {row['rect']!r}")
    return rect[0], rect[1], rect[2], rect[3]


def _selection_entries(selection_path: Path, *, page: int) -> list[dict[str, str | int]]:
    """Load a human-authored scope selection; labels are not semantic types."""
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection.get("page") != page:
        raise ValueError(f"selection page {selection.get('page')!r} does not match --page {page}")
    entries = selection.get("candidates")
    if not isinstance(entries, list) or not entries:
        raise ValueError("selection must name at least one candidate annotation")
    required = {"annotation_index", "reviewer_label", "scope_confidence"}
    seen: set[int] = set()
    validated: list[dict[str, str | int]] = []
    for entry in entries:
        if not isinstance(entry, dict) or required - set(entry):
            raise ValueError(f"selection entry is missing {sorted(required)}: {entry!r}")
        index = entry["annotation_index"]
        label, confidence = entry["reviewer_label"], entry["scope_confidence"]
        if not isinstance(index, int) or index < 1 or index in seen:
            raise ValueError(f"annotation_index must be a unique positive integer: {index!r}")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("reviewer_label must be non-empty text")
        if confidence not in {"CONFIDENT", "UNCERTAIN_REVIEWER_TO_CLASSIFY"}:
            raise ValueError("scope_confidence must be CONFIDENT or UNCERTAIN_REVIEWER_TO_CLASSIFY")
        seen.add(index)
        validated.append(
            {
                "annotation_index": index,
                "reviewer_label": label,
                "scope_confidence": confidence,
            }
        )
    return validated


def _selection_notes(selection_path: Path, *, page: int) -> list[str]:
    """Return reviewer-visible omissions that must not silently disappear from a focused cut."""
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection.get("page") != page:
        raise ValueError(f"selection page {selection.get('page')!r} does not match --page {page}")
    notes = selection.get("unscaffolded_visible_dimension_notes", [])
    if not isinstance(notes, list) or not all(
        isinstance(note, str) and note.strip() for note in notes
    ):
        raise ValueError("unscaffolded_visible_dimension_notes must be a list of non-empty text")
    return notes


def _vendor_boundary(csv_path: Path, *, page: int) -> float:
    """Return the bottom edge of the vendor label from the reviewed-markup CSV."""
    with csv_path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["page"] == str(page) and row["annotation_role"] == "vendor_section_label":
                return _rect_from_csv(row)[1]
    raise ValueError(f"page {page} has no vendor section label in {csv_path}")


def _read_selected_candidates(
    pdf_path: Path, csv_path: Path, *, page: int, selection_path: Path, project: str
) -> list[Candidate]:
    """Read exact `/Contents` from human-selected vendor markup without assigning semantics."""
    annotation_refs = PdfReader(pdf_path).pages[page - 1].get("/Annots", [])
    annotations = (
        annotation_refs.get_object() if hasattr(annotation_refs, "get_object") else annotation_refs
    )
    vendor_boundary = _vendor_boundary(csv_path, page=page)
    candidates: list[Candidate] = []
    for entry in _selection_entries(selection_path, page=page):
        index = int(entry["annotation_index"])
        if index > len(annotations):
            raise ValueError(
                f"selection annotation {index} is outside page {page}'s annotation list"
            )
        annotation = annotations[index - 1].get_object()
        subtype = str(annotation.get("/Subtype"))
        raw_text = annotation.get("/Contents")
        rect_values = annotation.get("/Rect")
        if subtype not in {"/FreeText", "/Line"}:
            raise ValueError(f"annotation {index} is {subtype}, not exact-text reviewer markup")
        if not isinstance(raw_text, str) or DIMENSION_TEXT.search(raw_text) is None:
            raise ValueError(f"annotation {index} has no dimension-shaped exact /Contents")
        if rect_values is None or len(rect_values) != 4:
            raise ValueError(f"annotation {index} has no four-coordinate /Rect")
        rect = tuple(float(value) for value in rect_values)
        if max(rect[1], rect[3]) >= vendor_boundary:
            raise ValueError(f"annotation {index} is not below the vendor section label")
        candidates.append(
            Candidate(
                candidate_id=f"page-{page:02d}-candidate-{len(candidates) + 1:02d}",
                project=project,
                page=page,
                author=str(annotation.get("/T") or ""),
                raw_text=raw_text,
                rect=(rect[0], rect[1], rect[2], rect[3]),
                annotation_subtype=subtype,
                annotation_index=index,
                reviewer_label=str(entry["reviewer_label"]),
                scope_confidence=str(entry["scope_confidence"]),
            )
        )
    return candidates


def _read_ppm(path: Path) -> tuple[int, int, bytes]:
    """Read Poppler's binary PPM output without introducing an image-library dependency."""
    data = path.read_bytes()
    cursor = 0

    def token() -> bytes:
        nonlocal cursor
        while cursor < len(data) and data[cursor] in b" \t\r\n":
            cursor += 1
        if cursor < len(data) and data[cursor] == ord("#"):
            while cursor < len(data) and data[cursor] not in b"\r\n":
                cursor += 1
            return token()
        start = cursor
        while cursor < len(data) and data[cursor] not in b" \t\r\n":
            cursor += 1
        return data[start:cursor]

    if token() != b"P6":
        raise ValueError("Poppler did not produce a binary PPM (P6) image")
    width, height, max_value = (int(token()) for _ in range(3))
    if max_value != 255:
        raise ValueError(f"expected an 8-bit PPM, got max value {max_value}")
    if cursor >= len(data) or data[cursor] not in b" \t\r\n":
        raise ValueError("PPM header is not separated from its raster by whitespace")
    cursor += 1
    rgb = data[cursor:]
    if len(rgb) != width * height * 3:
        raise ValueError("PPM pixel length does not match its declared size")
    return width, height, rgb


def _render_page(
    pdf_path: Path, *, page: int, dpi: int, output_path: Path
) -> tuple[int, int, bytes]:
    """Render exactly one PDF page with Poppler and save a deterministic PNG under data/."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_path.parent) as temporary:
        prefix = Path(temporary) / "page"
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                str(dpi),
                "-singlefile",
                str(pdf_path),
                str(prefix),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        width, height, rgb = _read_ppm(prefix.with_suffix(".ppm"))
    output_path.write_bytes(encode_png(width, height, rgb))
    return width, height, rgb


def _crop_box(
    rect: tuple[float, float, float, float],
    *,
    crop_box: tuple[float, float, float, float],
    dpi: int,
    width: int,
    height: int,
    margin_pt: int,
) -> tuple[int, int, int, int]:
    """Map an annotation's PDF /Rect (bottom-left origin) to rendered image pixels."""
    left, bottom, right, top = rect
    crop_left, crop_bottom, crop_right, crop_top = crop_box
    scale = dpi / 72
    if crop_right <= crop_left or crop_top <= crop_bottom:
        raise ValueError("PDF crop box does not describe a visible page")
    x0 = math.floor((min(left, right) - margin_pt - crop_left) * scale)
    x1 = math.ceil((max(left, right) + margin_pt - crop_left) * scale)
    y0 = math.floor((crop_top - max(bottom, top) - margin_pt) * scale)
    y1 = math.ceil((crop_top - min(bottom, top) + margin_pt) * scale)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"annotation /Rect {rect!r} produces an empty crop")
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        raise ValueError(
            f"annotation /Rect {rect!r} plus {margin_pt}-point context extends outside the page; "
            "refusing to silently clamp evidence"
        )
    return x0, y0, x1, y1


def _crop_rgb(rgb: bytes, *, width: int, box: tuple[int, int, int, int]) -> tuple[int, int, bytes]:
    """Copy one validated pixel box from a row-major RGB page image."""
    left, top, right, bottom = box
    row_stride = width * 3
    start, stop = left * 3, right * 3
    cropped = b"".join(
        rgb[row * row_stride + start : row * row_stride + stop] for row in range(top, bottom)
    )
    return right - left, bottom - top, cropped


def _candidate_payload(
    candidate: Candidate,
    *,
    sheet_title: str,
    sheet_number: str,
    crop_file: str,
) -> dict[str, Any]:
    """Build a candidate-only record with every gold-answer field explicitly pending."""
    return {
        "schema": "reviewed-goldset-candidate/v1",
        "schema_version": SCHEMA_VERSION,
        "status": PENDING,
        "candidate_id": candidate.candidate_id,
        "sheet": {
            "project": candidate.project,
            "page": candidate.page,
            "sheet_title": sheet_title,
            "sheet_number": sheet_number,
        },
        "reviewer_markup_candidate": {
            "status": UNVERIFIED,
            "raw_text": candidate.raw_text,
            "comment": "from reviewer markup, confirm against the drawing",
            "author_T": candidate.author,
            "annotation_subtype": candidate.annotation_subtype,
            "annotation_index": candidate.annotation_index,
            "rect_pdf_points": list(candidate.rect),
            "evidence_crop_relative_to_exploration_dir": crop_file,
        },
        "reviewer_scope": {
            "label": candidate.reviewer_label,
            "confidence": candidate.scope_confidence,
            "comment": "scope label only; it is not a semantic type",
        },
        "gold_set_answer_key_fields": {
            "semantic_type": PENDING,
            "authoritative_correct_value": PENDING,
            "source": PENDING,
            "item_id": PENDING,
            "expected_finding": {
                "check": PENDING,
                "outcome": PENDING,
                "reason": PENDING,
            },
            "arch_shop_match": PENDING,
            "provenance_human_annotator": PENDING,
        },
        "not_loadable_as_gold_case_reason": (
            "Candidate-only scaffold: a human must confirm the value, semantic type, source, item, "
            "and reviewer verdict before an answer_key.json may be authored."
        ),
    }


def _write_checklist(
    candidates: list[Candidate],
    *,
    crop_files: dict[str, str],
    output_path: Path,
    sheet_title: str,
    sheet_number: str,
    unscaffolded_notes: list[str],
) -> None:
    """Write a human checklist using the crop references recorded in candidate JSON."""
    lines = [
        "# Board Room 1 cabinet-run candidate review",
        "",
        "For each row, compare the crop with the full page, then fill the blanks; every candidate is UNVERIFIED until GVI-007 confirms it.",
        "",
        f"Sheet: {sheet_title}; identifier: {sheet_number}; reviewed-set page: {candidates[0].page}.",
        "",
        "| Candidate | Cabinet-run label | Scope confidence | Crop | Exact value | What it is (type) | Vendor right/wrong | Correct value if wrong |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for candidate in candidates:
        rect = "[" + ", ".join(f"{coordinate:g}" for coordinate in candidate.rect) + "]"
        lines.append(
            f"| {candidate.candidate_id} | {candidate.reviewer_label} | "
            f"{candidate.scope_confidence} | `{crop_files[candidate.candidate_id]}` |  |  |  |  |"
        )
        lines.append(f"<!-- {candidate.candidate_id}: markup {UNVERIFIED}; /Rect {rect} -->")
    if unscaffolded_notes:
        lines.extend(["", "## Visible dimensions requiring manual addition", ""])
        lines.extend(f"- {note}" for note in unscaffolded_notes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_candidate_scaffold(path: Path) -> dict[str, Any]:
    """Validate the deliberate pending shape; it is not the production GoldCase loader."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "reviewed-goldset-candidate/v1":
        raise ValueError(f"{path} has an unknown candidate schema")
    if payload.get("status") != PENDING:
        raise ValueError(f"{path} is not explicitly pending human confirmation")
    candidate = payload.get("reviewer_markup_candidate", {})
    if candidate.get("status") != UNVERIFIED or not isinstance(candidate.get("raw_text"), str):
        raise ValueError(f"{path} does not retain an unverified, verbatim reviewer candidate")
    fields = payload.get("gold_set_answer_key_fields", {})
    required = (
        "semantic_type",
        "authoritative_correct_value",
        "source",
        "item_id",
        "arch_shop_match",
        "provenance_human_annotator",
    )
    if any(fields.get(name) != PENDING for name in required):
        raise ValueError(f"{path} contains a non-pending gold-set answer field")
    expected = fields.get("expected_finding", {})
    if any(expected.get(name) != PENDING for name in ("check", "outcome", "reason")):
        raise ValueError(f"{path} contains a non-pending expected finding")
    return payload


def dry_run_candidate_scaffolds(directory: Path) -> str:
    """Load pending candidate files and render an explicitly unmeasured scorecard."""
    candidate_paths = sorted(directory.glob("*.candidate.json"))
    if not candidate_paths:
        raise ValueError(f"{directory} contains no candidate scaffold files")
    payloads = [load_candidate_scaffold(path) for path in candidate_paths]
    # `model_construct` avoids pretending a pending scaffold has confirmed fields or provenance.
    pending_case = GoldCase.model_construct(
        id="pending-human-confirmation",
        ground_truth=GroundTruth(observations=(), matches=(), expected_findings=()),
    )
    scorecard = score_package(pending_case, findings=(), observations=())
    return "\n".join(
        [
            "Candidate gold-set scaffold dry run — PENDING CONFIRMATION",
            "",
            f"Loaded {len(payloads)} candidate scaffold file(s).",
            "Eligible real GoldCase answer keys: 0.",
            (
                "No candidate was passed to the real pipeline: it would apply answer-key semantic "
                "labels, and every candidate label is intentionally pending."
            ),
            "",
            render(scorecard),
            "",
            (
                "Pending confirmation: authoritative value, semantic type, source, item, ARCH-to-SHOP "
                "match, expected check/outcome/reason, two source PDFs, and provenance hashes."
            ),
        ]
    )


def _write_dry_run(candidate_paths: list[Path], output_path: Path) -> None:
    """Load candidate scaffolds and exercise the scorecard's all-pending rendering safely."""
    if not candidate_paths:
        raise ValueError("no candidate paths to dry-run")
    lines = dry_run_candidate_scaffolds(candidate_paths[0].parent).splitlines()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _arguments() -> argparse.Namespace:
    """Parse explicit user-supplied metadata and ignored output locations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Reviewed drawing PDF; it remains in place")
    parser.add_argument("csv", type=Path, help="FreeText extraction CSV from the reviewed PDF")
    parser.add_argument("--page", type=int, required=True, help="One-based reviewed-PDF page")
    parser.add_argument("--dpi", type=int, default=150, help="Render resolution (default: 150)")
    parser.add_argument(
        "--margin-pt", type=int, default=18, help="Mechanical crop context in PDF points"
    )
    parser.add_argument("--sheet-title", required=True, help="Human-established sheet title")
    parser.add_argument("--sheet-number", required=True, help="Human-established sheet identifier")
    parser.add_argument("--project", required=True, help="Human-established project identity")
    parser.add_argument(
        "--selection",
        type=Path,
        help=(
            "Ignored JSON manifest naming selected annotation indices and reviewer-facing scope labels. "
            "Required for a focused batch."
        ),
    )
    parser.add_argument(
        "--exploration-dir",
        type=Path,
        required=True,
        help="Ignored render/checklist output directory under data/exploration/",
    )
    parser.add_argument(
        "--goldset-dir",
        type=Path,
        required=True,
        help="Ignored candidate scaffold directory under data/goldset/",
    )
    return parser.parse_args()


def main() -> None:
    """Render one reviewed page, emit pending candidates, and run the safe scorecard dry run."""
    args = _arguments()
    if args.page < 1 or args.dpi < 1 or args.margin_pt < 1:
        raise ValueError("page, dpi, and crop margin must be positive")
    reader = PdfReader(args.pdf)
    if args.page > len(reader.pages):
        raise ValueError(f"page {args.page} is outside this {len(reader.pages)}-page PDF")
    page = reader.pages[args.page - 1]
    if page.rotation % 360 != 0:
        raise ValueError(
            "this scaffold only supports unrotated PDF pages; refuse rather than guess"
        )
    crop_box = tuple(float(value) for value in page.cropbox)
    if len(crop_box) != 4:
        raise ValueError("PDF crop box is not four coordinates")

    candidates = (
        _read_selected_candidates(
            args.pdf, args.csv, page=args.page, selection_path=args.selection, project=args.project
        )
        if args.selection is not None
        else _read_candidates(args.csv, page=args.page)
    )
    unscaffolded_notes = (
        _selection_notes(args.selection, page=args.page) if args.selection is not None else []
    )

    full_page = args.exploration_dir / f"page-{args.page:02d}.png"
    width, height, rgb = _render_page(args.pdf, page=args.page, dpi=args.dpi, output_path=full_page)
    crop_dir = args.exploration_dir / "crops"
    candidate_dir = args.goldset_dir / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_paths: list[Path] = []
    crop_files: dict[str, str] = {}
    for candidate in candidates:
        crop = _crop_box(
            candidate.rect,
            crop_box=(crop_box[0], crop_box[1], crop_box[2], crop_box[3]),
            dpi=args.dpi,
            width=width,
            height=height,
            margin_pt=args.margin_pt,
        )
        crop_width, crop_height, crop_rgb = _crop_rgb(rgb, width=width, box=crop)
        crop_path = crop_dir / f"{candidate.candidate_id}.png"
        crop_path.parent.mkdir(parents=True, exist_ok=True)
        crop_path.write_bytes(encode_png(crop_width, crop_height, crop_rgb))
        crop_file = crop_path.relative_to(args.exploration_dir).as_posix()
        crop_files[candidate.candidate_id] = crop_file
        payload = _candidate_payload(
            candidate,
            sheet_title=args.sheet_title,
            sheet_number=args.sheet_number,
            crop_file=crop_file,
        )
        candidate_path = candidate_dir / f"{candidate.candidate_id}.candidate.json"
        candidate_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        candidate_paths.append(candidate_path)

    _write_checklist(
        candidates,
        crop_files=crop_files,
        output_path=args.exploration_dir / "annotation_checklist.md",
        sheet_title=args.sheet_title,
        sheet_number=args.sheet_number,
        unscaffolded_notes=unscaffolded_notes,
    )
    _write_dry_run(candidate_paths, args.exploration_dir / "dry_run_scorecard.txt")
    print(
        f"Prepared {len(candidates)} UNVERIFIED candidate scaffold(s) for page {args.page}; "
        f"rendered {full_page} and {len(candidates)} mechanical crop(s)."
    )
    print(
        f"Gold-set candidates: {args.goldset_dir}; dry run: {args.exploration_dir / 'dry_run_scorecard.txt'}"
    )


if __name__ == "__main__":
    main()
