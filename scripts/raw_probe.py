"""Ask a model, in prose and outside the seam, what it can read in one region of a drawing.

EXPLORATORY, and deliberately **not** the extraction path. `OpenModelAdapter` is one validated
reading per call and refuses prose, which is the contract; this posts the same image to the same
endpoint with a plain "list every dimension you can read" prompt, to see what a model does when
nothing constrains it.

It exists because the answer was worth knowing and is not reassuring. On a real elevation
(1720×880 px) `minicpm-v` produced **429 lines containing one distinct token**, misspelled; on half
the same region, six plausible readings and then the same token 604 times. Enumeration over a dense
sheet does not lose recall gracefully, it degenerates — which is why `extraction/vector_first.py`
selects regions from the sheet's own geometry and asks one question per region.

Nothing here writes a candidate, and nothing downstream may consume its output. If a run of this
ever looks better than the seam, the thing to change is the seam.

**What it needs.** A local Ollama with a vision model pulled, and the model named:

    ollama pull minicpm-v && ollama serve
    GV_OPENMODEL_ID=minicpm-v:latest python scripts/raw_probe.py REGIONS.json --region full-sheet

The manifest is the one `scripts/explore_reader.py` takes; `--region` names which entry to send.
The PDF, the boxes and the results stay under `data/`, which is not tracked.

Source: issue #539, from the `AI_Set 2` reader probe.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import crop_box_pt

#: The unconstrained question. Kept here rather than in `extraction/models/sanitisation.py` because
#: it is not part of the seam's contract and must never be mistaken for it: the seam asks for one
#: reading and one polygon in a validated shape, and this asks for a list in prose.
LIST_TASK = (
    "This is a cabinet shop-drawing elevation. List every dimension label you can read, exactly "
    "as written, one per line. Do not calculate, convert, or infer anything. If a label is "
    "unreadable, write UNREADABLE. Reply with the list only."
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--region", required=True, help="which manifest region id to send")
    parser.add_argument("--out", type=Path, help="write the prose reply here")
    parser.add_argument("--dpi", type=int, help="override the manifest's render resolution")
    parser.add_argument(
        "--include-markup",
        action="store_true",
        help="render the reviewer's annotations into the crop as well",
    )
    parser.add_argument("--timeout", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    model = os.environ.get("GV_OPENMODEL_ID")
    if not model:
        sys.exit("set GV_OPENMODEL_ID to the model to probe, and make sure it is running")
    base_url = os.environ.get("GV_OPENMODEL_BASE_URL", "http://localhost:11434/v1")

    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    regions = {region["id"]: region for region in manifest["regions"]}
    if arguments.region not in regions:
        sys.exit(f"{arguments.region!r} is not in the manifest; it has {sorted(regions)}")
    box = tuple(Decimal(str(value)) for value in regions[arguments.region]["box_pt"])
    dpi = arguments.dpi or int(manifest.get("render_dpi", VISION_CROP_DPI))

    crop = crop_box_pt(
        Path(manifest["pdf"]).read_bytes(),
        int(manifest["page"]) - 1,
        box,  # type: ignore[arg-type]
        dpi=dpi,
        include_markup=arguments.include_markup,
    )
    print(f"model={model} region={arguments.region} dpi={dpi} crop_bytes={len(crop)}", flush=True)

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(crop).decode("ascii")
                            },
                        },
                        {"type": "text", "text": LIST_TASK},
                    ],
                }
            ],
            # Zero, so two runs of the same crop are comparable. A model that varies its answer
            # between identical calls is one nothing could corroborate.
            "temperature": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=arguments.timeout) as response:
        answer = json.loads(response.read())
    text = str(answer["choices"][0]["message"]["content"])
    lines = text.splitlines()

    print(
        f"--- {round(time.monotonic() - started, 1)}s, {len(lines)} lines, "
        f"{len({line.strip() for line in lines if line.strip()})} distinct ---",
        flush=True,
    )
    print(text, flush=True)

    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(text, encoding="utf-8")
        print(f"written: {arguments.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
