"""Prepare unverified gold-set candidates from reviewed-PDF FreeText markup.

This is an exploratory annotation aid, not a gold-case author.  It copies no drawings and
does not create an ``answer_key.json``: the real evaluator would apply the answer key's human
semantic labels to extracted candidates, and these annotations are not human-confirmed truth.
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
    """One dimension-looking reviewer annotation, retained verbatim and untyped."""

    candidate_id: str
    project: str
    page: int
    author: str
    raw_text: str
    rect: tuple[float, float, float, float]


def _read_candidates(csv_path: Path, *, page: int) -> list[Candidate]:
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
                )
            )
    if not candidates:
        raise ValueError(f"no dimension-looking /FreeText annotations found on page {page}")
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
    while cursor < len(data) and data[cursor] in b" \t\r\n":
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
    x0 = max(0, math.floor((min(left, right) - margin_pt - crop_left) * scale))
    x1 = min(width, math.ceil((max(left, right) + margin_pt - crop_left) * scale))
    y0 = max(0, math.floor((crop_top - max(bottom, top) - margin_pt) * scale))
    y1 = min(height, math.ceil((crop_top - min(bottom, top) + margin_pt) * scale))
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"annotation /Rect {rect!r} produces an empty crop")
    if crop_right <= crop_left or crop_top <= crop_bottom:
        raise ValueError("PDF crop box does not describe a visible page")
    return x0, y0, x1, y1


def _crop_rgb(rgb: bytes, *, width: int, box: tuple[int, int, int, int]) -> tuple[int, int, bytes]:
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
            "rect_pdf_points": list(candidate.rect),
            "evidence_crop": crop_file,
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
    candidates: list[Candidate], *, output_path: Path, sheet_title: str, sheet_number: str
) -> None:
    lines = [
        "# Gold-set candidate annotation checklist",
        "",
        (
            "These are reviewer-markup candidates, not gold truth. Do not rename any candidate to "
            "`answer_key.json` or run it through the real grader until every pending field has been "
            "confirmed by a human."
        ),
        "",
        f"Sheet: {sheet_title}; identifier: {sheet_number}; reviewed-set page: {candidates[0].page}.",
        "",
        "For each candidate:",
        "",
        "1. Open the full rendered page and its linked crop; verify the full reviewer text against ink.",
        "2. Have a reviewer state the authoritative exact value (or record a disagreement).",
        "3. Have a reviewer assign the semantic type and source; do not infer either from the text.",
        "4. Have a reviewer identify the item and any ARCH-to-SHOP match.",
        "5. Have a reviewer state the check, outcome, and plain-English reason, or leave it absent.",
        (
            "6. Only then create a real `answer_key.json` with exact values, source-document hashes, "
            "and both source PDFs as required by `docs/GOLD_SET_FORMAT.md`."
        ),
        "",
        "| Candidate | Reviewer markup status | PDF /Rect | Crop | Gold-set fields |",
        "| --- | --- | --- | --- | --- |",
    ]
    for candidate in candidates:
        rect = "[" + ", ".join(f"{coordinate:g}" for coordinate in candidate.rect) + "]"
        lines.append(
            f"| {candidate.candidate_id} | {UNVERIFIED} | `{rect}` | "
            f"`crops/{candidate.candidate_id}.png` | all PENDING human confirmation |"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_candidate_scaffold(path: Path) -> dict[str, Any]:
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
    required = ("semantic_type", "authoritative_correct_value", "source", "item_id")
    if any(fields.get(name) != PENDING for name in required):
        raise ValueError(f"{path} contains a non-pending gold-set answer field")
    expected = fields.get("expected_finding", {})
    if any(expected.get(name) != PENDING for name in ("check", "outcome", "reason")):
        raise ValueError(f"{path} contains a non-pending expected finding")
    return payload


def _write_dry_run(candidate_paths: list[Path], output_path: Path) -> None:
    """Load candidate scaffolds and exercise the scorecard's all-pending rendering safely."""
    payloads = [_load_candidate_scaffold(path) for path in candidate_paths]
    # score_package only reads `id` and `ground_truth`; model_construct avoids pretending this
    # candidate-only scaffold has the complete provenance and confirmed fields GoldCase requires.
    pending_case = GoldCase.model_construct(
        id="pending-human-confirmation",
        ground_truth=GroundTruth(observations=(), matches=(), expected_findings=()),
    )
    scorecard = score_package(pending_case, findings=(), observations=())
    lines = [
        "Candidate gold-set scaffold dry run — PENDING CONFIRMATION",
        "",
        f"Loaded {len(payloads)} candidate scaffold file(s).",
        "Eligible real GoldCase answer keys: 0.",
        (
            "No candidate was passed to scripts/evaluate_goldset.py: its real pipeline would apply "
            "answer-key semantic labels, and every candidate label is intentionally pending."
        ),
        "",
        render(scorecard),
        "",
        (
            "Pending confirmation: authoritative value, semantic type, source, item, ARCH-to-SHOP "
            "match, expected check/outcome/reason, two source PDFs, and provenance hashes."
        ),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _arguments() -> argparse.Namespace:
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
    args = _arguments()
    if args.page < 1 or args.dpi < 1 or args.margin_pt < 1:
        raise ValueError("page, dpi, and crop margin must be positive")
    candidates = _read_candidates(args.csv, page=args.page)
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

    full_page = args.exploration_dir / f"page-{args.page:02d}.png"
    width, height, rgb = _render_page(args.pdf, page=args.page, dpi=args.dpi, output_path=full_page)
    crop_dir = args.exploration_dir / "crops"
    candidate_dir = args.goldset_dir / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_paths: list[Path] = []
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
        payload = _candidate_payload(
            candidate,
            sheet_title=args.sheet_title,
            sheet_number=args.sheet_number,
            crop_file=str(crop_path),
        )
        candidate_path = candidate_dir / f"{candidate.candidate_id}.candidate.json"
        candidate_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        candidate_paths.append(candidate_path)

    _write_checklist(
        candidates,
        output_path=args.exploration_dir / "annotation_checklist.md",
        sheet_title=args.sheet_title,
        sheet_number=args.sheet_number,
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
