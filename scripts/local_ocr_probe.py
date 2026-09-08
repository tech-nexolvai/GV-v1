"""Read drawing crops with a local OCR engine, and report what it is *unsure* about.

EXPLORATORY. It produces readings and confidences, not verdicts: nothing here runs a rule, nothing is
scored, and no reading carries a semantic type. It is also **not** an adapter — it deliberately does
not implement the `extract(request) -> ObservationCandidate` seam that `nova.py` and `openmodel.py`
share, because adopting OCR for dimension reading is a decision with its own issue and a premature
adapter would make that decision look already taken.

**Why this exists.** Two general vision models read the same eight crops from the first real client
sheet (#549) and failed in the same places: rotated labels and stacked fractions. On one crop — a
rotated `46"` — both returned a *confident wrong number* and the seam accepted it, because `60` and
`4` are well-formed dimension tokens with nothing for a validator to object to (#541).

An OCR engine differs in exactly the way that matters here: it returns a **per-detection
confidence**. That is a mechanism the vision-model path does not have. So the question this answers
is not only "does it read better" but "would a threshold have let it *abstain* on the reading that
was dangerous", which is a question about safety rather than accuracy.

**What it needs.** EasyOCR in the environment. It is not a runtime dependency of this project and
nothing here adds one:

    .venv/bin/pip install easyocr
    python scripts/local_ocr_probe.py REGIONS.json --out data/exploration/easyocr.json

The first run downloads detection and recognition models to `~/.EasyOCR` (about 100 MB) and takes a
minute or two; later runs reuse them. With the engine absent the probe says so and exits without
pretending — the same stance the Bedrock smoke test takes when no credential resolves.

**No drawing content is in this file.** The PDF path, the crop boxes, the human readings and the
render resolution all come from the manifest, which lives under `data/` and is not tracked. So do the
results. `--compare-with` reads the other providers' result files so the comparison table is
reproducible from tracked code rather than assembled by hand.

Source: issue #551, from the Nova/minicpm-v comparison in #549.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import crop_box_pt

#: Detections below this are reported as "would abstain" rather than as readings.
#:
#: **Not a tuned threshold and not a recommendation.** It is a reporting line drawn so the
#: abstention question has a concrete answer instead of a shrug, and it is an argument so a reader
#: can move it. Where the real line belongs is an empirical question that needs the reviewed answers
#: #274 owes, exactly like every other threshold in this project.
DEFAULT_ABSTAIN_BELOW = 0.80


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON listing the PDF, page and crop boxes")
    parser.add_argument("--out", type=Path, help="write the readings here as JSON")
    parser.add_argument("--only", help="run one region by id")
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="read at most this many regions. Caps the run; the default is the comparison set",
    )
    parser.add_argument(
        "--abstain-below",
        type=float,
        default=DEFAULT_ABSTAIN_BELOW,
        help=(
            "report a detection under this confidence as one a threshold would have abstained on. "
            "A reporting line, not a tuned value — see the module docstring"
        ),
    )
    parser.add_argument(
        "--include-markup",
        action="store_true",
        help="render the reviewer's annotations into the crop as well. Off by default, because a "
        "crop of two superimposed labels produced a reading that was neither of them",
    )
    parser.add_argument(
        "--compare-with",
        type=Path,
        nargs="*",
        default=(),
        help="other providers' result files, to print the side-by-side table",
    )
    return parser.parse_args()


def _engine() -> Any:
    """The OCR reader, or an explanation of why there is none.

    Imported here rather than at module scope so that `--help` works, and the comparison table can
    be reprinted from saved results, on a machine where the engine was never installed.

    `gpu=False` explicitly: this is a laptop, the crops are small, and a silent fallback to CPU with
    a warning buried in the output is worse than saying which device did the work.
    """
    try:
        import easyocr
    except ImportError:
        sys.exit(
            "easyocr is not installed in this environment, and this probe will not pretend to have "
            "read anything without it.\n"
            "  .venv/bin/pip install easyocr\n"
            "It is not a runtime dependency of this project — see the module docstring."
        )
    # English only. A language list is a real choice on drawings that carry two, and adding one
    # "just in case" changes the recognition model rather than merely widening it.
    return easyocr.Reader(["en"], gpu=False, verbose=False)


def _rotation_of(box: list[list[float]]) -> str:
    """Whether a detection's own box says the text runs across or up the crop.

    Reported because it is the property the vision models got wrong: the two crops both models
    mangled are the rotated ones, and a reader that *knows* it is looking at rotated text is
    materially different from one that guesses. Read off the box rather than assumed, the same way
    `extraction/annotations.py` reads rotation from the file rather than inferring it from a shape.
    """
    if len(box) < 4:
        return "unknown"
    (x0, y0), (x1, y1) = box[0], box[1]
    return "across" if abs(x1 - x0) >= abs(y1 - y0) else "up"


def _read_region(reader: Any, crop: bytes, *, abstain_below: float) -> dict[str, Any]:
    """Every detection in one crop, with its text, confidence and box.

    `readtext` is given the PNG bytes directly — EasyOCR accepts them — so the image the engine sees
    is byte-identical to the one the vision models were sent. Comparing two readers on two different
    renders of the same region would be comparing the renders.
    """
    started = time.monotonic()
    detections = reader.readtext(crop)
    seconds = round(time.monotonic() - started, 2)

    found = []
    for box, text, confidence in detections:
        found.append(
            {
                "text": text,
                "confidence": round(float(confidence), 4),
                "runs": _rotation_of([[float(x), float(y)] for x, y in box]),
                "box": [[round(float(x), 1), round(float(y), 1)] for x, y in box],
                "would_abstain": float(confidence) < abstain_below,
            }
        )
    return {
        "detections": found,
        "seconds": seconds,
        # The reading a caller would take: the most confident detection. Stated explicitly because
        # "what did it read" has no answer otherwise on a crop that returns three fragments, and a
        # caller that silently concatenated them would invent a token the drawing does not carry.
        "best": max(found, key=lambda item: item["confidence"]) if found else None,
    }


def _comparison(
    ocr: list[dict[str, Any]], others: tuple[Path, ...], *, abstain_below: float
) -> None:
    """Print the side-by-side table, so the acceptance is reproducible rather than transcribed."""
    columns: list[tuple[str, dict[str, dict[str, Any]]]] = []
    for path in others:
        payload = json.loads(path.read_text(encoding="utf-8"))
        readings = payload.get("readings", payload)
        name = payload.get("model") or path.stem
        columns.append((str(name), {row["id"]: row for row in readings}))

    def other(row: dict[str, Any] | None) -> str:
        if row is None:
            return "—"
        if row.get("reading") is not None:
            return f"ACCEPTED {row['reading']!r}"
        return f"refused ({row.get('refusal_reason') or row.get('outcome')})"

    print("\n--- the same crops, three readers " + "-" * 44)
    for entry in ocr:
        best = entry.get("best")
        if best is None:
            ocr_cell = "nothing detected"
        else:
            flag = " WOULD ABSTAIN" if best["would_abstain"] else ""
            ocr_cell = f"{best['text']!r} conf={best['confidence']:.2f}{flag}"
        print(f"\n{entry['id']}  (truth {entry['truth']!r}, {entry.get('difficulty', '')})")
        print(f"    easyocr      {ocr_cell}")
        for name, rows in columns:
            print(f"    {name[:12]:12} {other(rows.get(entry['id']))}")
    print(f"\n(abstention line for this report: confidence < {abstain_below})")


def main() -> int:
    arguments = _arguments()
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    data = Path(manifest["pdf"]).read_bytes()
    page_index = int(manifest["page"]) - 1
    dpi = int(manifest.get("render_dpi", VISION_CROP_DPI))

    regions = [
        region
        for region in manifest["regions"]
        if arguments.only is None or region["id"] == arguments.only
    ]
    if arguments.limit:
        regions = regions[: arguments.limit]

    reader = _engine()
    print(f"engine=easyocr(en, cpu) page={manifest['page']} dpi={dpi} regions={len(regions)}")

    readings: list[dict[str, Any]] = []
    for region in regions:
        box = tuple(Decimal(str(value)) for value in region["box_pt"])
        crop = crop_box_pt(
            data,
            page_index,
            box,  # type: ignore[arg-type]
            dpi=dpi,
            include_markup=arguments.include_markup,
        )
        result = _read_region(reader, crop, abstain_below=arguments.abstain_below)
        row = {
            "id": region["id"],
            "truth": region.get("truth", ""),
            "difficulty": region.get("difficulty", ""),
            "crop_bytes": len(crop),
            **result,
        }
        readings.append(row)
        best = row["best"]
        summary = (
            "nothing detected"
            if best is None
            else f"{best['text']!r} conf={best['confidence']:.2f} runs={best['runs']}"
        )
        print(
            f"  {region['id']:22} truth={region.get('truth', '')!r:14} -> {summary} "
            f"({len(row['detections'])} detections, {row['seconds']}s)",
            flush=True,
        )

    if arguments.compare_with:
        _comparison(readings, tuple(arguments.compare_with), abstain_below=arguments.abstain_below)

    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(
            json.dumps(
                {
                    "exploratory": (
                        "readings and confidences against what a person read off the same crop. No "
                        "rule ran, nothing is scored, and no reading carries a semantic type."
                    ),
                    "engine": "easyocr",
                    "languages": ["en"],
                    "device": "cpu",
                    "render_dpi": dpi,
                    "abstain_below": arguments.abstain_below,
                    "readings": readings,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"written: {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
