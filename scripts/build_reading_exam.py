"""Build the reading exam from saved set packs: the sampled labels, their crops, and the key a person confirms (rebuild plan Step 1A).

Two steps, because the proposals are read between them:

    # 1. sample the labels and cut their crops
    python scripts/build_reading_exam.py --pack setA=<pack folder> --pack setB=<...> \\
        --out ~/gv-local/exam/reading/<YYYY-MM-DD>
    # 2. after the proposals are read: the key and the page a person confirms it on
    python scripts/build_reading_exam.py --finish ~/gv-local/exam/reading/<YYYY-MM-DD> \\
        --proposals <proposals.tsv>

Step 1 writes `crops/` (per label: the code-cut crop at 300 dpi, the 90 and 270 turns of a
sideways one, and a wider context picture with the label's box in red), `candidates.tsv` (the key
with no proposal yet), `sheets/` (the crops in numbered rows, to read the proposals from) and
`README.txt` (which packs, how many of each kind). Step 2 joins `proposals.tsv` (`id`,
`proposed_text`, `proposed_turn`, `confidence`) to the candidates and writes `key.tsv` and
`index.html`.

The labels, crops and key hold the client's printed values and file names, so `--out` must be
outside this repository (it refuses one inside it), and no file there is ever replaced: step 1
refuses a folder that is not empty, and step 2 refuses one that already has a key. The crops are
cut by the set pack's own renderer (`set_pack.crop_picture`, pdfium), from each pack's
drawings-only copy of the PDF, without the pack's page cache, so nothing in a pack is written.
It makes no AI call and touches no database.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - the usual script preamble
    sys.path.insert(0, str(ROOT))

from eval.exam import reading

#: The dpi every crop is cut at.
DPI = 300
#: A reading sheet's rows, and the height (pixels) each picture is fitted to on it.
SHEET_ROWS = 6
SHEET_ROW_PX = 190


def _packs(values: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values:
        tag, _, folder = value.partition("=")
        if not tag or not folder:
            raise SystemExit(f"--pack wants TAG=FOLDER, not {value!r}")
        out[tag] = Path(folder).expanduser()
    return out


def _outside_repo(folder: Path) -> None:
    resolved = folder.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise SystemExit(f"{folder} is inside the repository; the key holds client values")


def _render(pdf: Path, page: int, box: reading.Box) -> Any:
    """The set pack's crop (`set_pack.crop_picture`) of `box` at `DPI`, as a PIL picture."""
    import io

    from PIL import Image

    from app.review.assistant import set_pack

    crop = set_pack.crop_picture(pdf, page, box, DPI)
    return crop, Image.open(io.BytesIO(crop.png)).convert("RGB")


def _turned(picture: Any, turn: int) -> Any:
    """The picture turned clockwise by `turn` (`ink_labels._frame`: 90 reads up the page)."""
    from PIL import Image

    if turn == 90:
        return picture.transpose(Image.Transpose.ROTATE_270)
    if turn == 270:
        return picture.transpose(Image.Transpose.ROTATE_90)
    return picture


def _boxed(picture: Any, shown: reading.Box, box: reading.Box) -> Any:
    """The context picture with the label's box drawn in red. `shown` is the area the picture
    covers (points), so the box lands where the label is."""
    from PIL import ImageDraw

    scale_x = picture.width / (shown[2] - shown[0])
    scale_y = picture.height / (shown[3] - shown[1])
    out = picture.copy()
    ImageDraw.Draw(out).rectangle(
        (
            (box[0] - shown[0]) * scale_x - 2,
            (box[1] - shown[1]) * scale_y - 2,
            (box[2] - shown[0]) * scale_x + 2,
            (box[3] - shown[1]) * scale_y + 2,
        ),
        outline=(220, 0, 0),
        width=2,
    )
    return out


def build(packs: dict[str, Path], out: Path, args: argparse.Namespace) -> int:
    """Step 1: sample, cut every crop, and write `candidates.tsv`, `sheets/` and `README.txt`."""
    _outside_repo(out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; nothing there is ever replaced")
    candidates: list[reading.Candidate] = []
    boxes: dict[tuple[str, int, int], list[reading.Box]] = {}
    versions: dict[str, Any] = {}
    for tag, folder in packs.items():
        dims = json.loads((folder / "dims.json").read_text(encoding="utf-8"))
        coverage = json.loads((folder / "reading_coverage.json").read_text(encoding="utf-8"))
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
        versions[tag] = {"folder": str(folder), "pack_version": status.get("version")}
        found, page_boxes = reading.candidates_of_pack(tag, dims, coverage)
        candidates.extend(found)
        for (index, page), listed in page_boxes.items():
            boxes[(tag, index, page)] = listed
    chosen = reading.sample(
        candidates,
        total=args.total,
        control_each=args.control_each,
        minimum=args.minimum,
        seed=args.seed,
    )
    from app.review.assistant import set_pack

    crops = out / "crops"
    crops.mkdir(parents=True)
    rows: list[reading.KeyRow] = []
    for candidate in chosen:
        pdf = packs[candidate.job] / f"{set_pack.stem(candidate.file)}_drawings_only.pdf"
        neighbours = boxes.get((candidate.job, candidate.file_index, candidate.page), [])
        cut = reading.code_crop(candidate.box, neighbours)
        crop, picture = _render(pdf, candidate.page, cut)
        if crop.dpi != DPI:
            raise SystemExit(f"{candidate.id}: cut at {crop.dpi} dpi, not {DPI}")
        paths = [f"crops/{candidate.id}.png"]
        picture.save(out / paths[0])
        if candidate.sideways:
            for turn in (90, 270):
                path = f"crops/{candidate.id}-t{turn}.png"
                _turned(picture, turn).save(out / path)
                paths.append(path)
        wide, around = _render(pdf, candidate.page, reading.context_box(candidate.box))
        context = f"crops/{candidate.id}-context.png"
        _boxed(around, wide.box, candidate.box).save(out / context)
        rows.append(
            reading.KeyRow(
                id=candidate.id,
                job=candidate.job,
                file_index=candidate.file_index,
                page=candidate.page,
                side=candidate.side,
                reason=candidate.reason,
                box=candidate.box,
                crops=tuple(paths),
                group=candidate.group,
                also=candidate.also,
                file=candidate.file,
                context=context,
                code_text=candidate.code_text,
            )
        )
    reading.write_key(out / "candidates.tsv", rows)
    sheets = _sheets(out, rows)
    by_kind = Counter(row.reason for row in rows)
    by_job = Counter((row.job, row.reason) for row in rows)
    available = Counter(candidate.reason for candidate in candidates)
    lines = [
        "Reading exam (rebuild plan Step 1A): client values, local only, never on GitHub.",
        (
            f"Built with seed {args.seed!r}, total {args.total}, {args.control_each} per control "
            f"group, at least {args.minimum} per reason where available."
        ),
        "",
        "Packs:",
        *(
            f"  {tag}: {info['folder']} (pack version {info['pack_version']})"
            for tag, info in versions.items()
        ),
        "",
        "Labels by kind (sampled of available):",
        *(
            f"  {kind}: {by_kind[kind]} of {available[kind]}"
            for kind in (*reading.REASONS, *reading.CONTROLS)
        ),
        "",
        "Labels by job and kind:",
        *(f"  {job} {kind}: {count}" for (job, kind), count in sorted(by_job.items())),
        "",
        f"{len(rows)} labels, {sum(len(row.crops) for row in rows)} crops, {sheets} reading sheets.",
    ]
    (out / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def _sheets(out: Path, rows: list[reading.KeyRow]) -> int:
    """The crops in numbered rows (`SHEET_ROWS` to a picture), to read the proposals from: each
    row is the label's number and id, its context picture, then its crop and turns enlarged.
    `sheets/sheet-NNN.tsv` says which id each row is."""
    from PIL import Image, ImageDraw, ImageFont

    folder = out / "sheets"
    folder.mkdir()
    font = ImageFont.load_default(size=28)
    count = 0
    for start in range(0, len(rows), SHEET_ROWS):
        group = rows[start : start + SHEET_ROWS]
        lines: list[Any] = []
        for number, row in enumerate(group, start + 1):
            pictures = [Image.open(out / row.context)] + [Image.open(out / p) for p in row.crops]
            fitted = []
            for picture in pictures:
                scale = min(4.0, SHEET_ROW_PX / picture.height, 520 / picture.width)
                size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
                fitted.append(picture.convert("RGB").resize(size, Image.Resampling.LANCZOS))
            width = 150 + sum(p.width + 24 for p in fitted)
            line = Image.new("RGB", (width, SHEET_ROW_PX + 20), "white")
            draw = ImageDraw.Draw(line)
            draw.text((8, 8), f"#{number}", fill="black", font=font)
            x = 150
            for position, shown in enumerate(fitted):
                line.paste(shown, (x, 10))
                draw.rectangle((x - 1, 9, x + shown.width, 10 + shown.height), outline="gray")
                if position:
                    draw.text(
                        (x + 2, 10), ("", "A", "B", "C")[min(position, 3)], fill="blue", font=font
                    )
                x += shown.width + 24
            lines.append(line)
        sheet = Image.new(
            "RGB",
            (max(line.width for line in lines), sum(line.height + 6 for line in lines)),
            (235, 235, 235),
        )
        y = 0
        for line in lines:
            sheet.paste(line, (0, y))
            y += line.height + 6
        count += 1
        sheet.save(folder / f"sheet-{count:03d}.png")
        with (folder / f"sheet-{count:03d}.tsv").open("x", encoding="utf-8") as handle:
            for number, row in enumerate(group, start + 1):
                handle.write(f"{number}\t{row.id}\t{row.reason}\n")
    return count


def finish(out: Path, proposals: Path) -> int:
    """Step 2: join the proposals to the candidates; write `key.tsv` and `index.html`."""
    _outside_repo(out)
    key, page = out / "key.tsv", out / "index.html"
    for path in (key, page):
        if path.exists():
            raise SystemExit(f"{path} exists; a reading key is never overwritten")
    rows = reading.read_key(out / "candidates.tsv")
    with proposals.open(encoding="utf-8", newline="") as handle:
        read = {row["id"]: row for row in csv.DictReader(handle, delimiter="\t")}
    unknown = sorted(set(read) - {row.id for row in rows})
    if unknown:
        raise SystemExit(f"proposals for ids not in the exam: {unknown[:5]}")
    done = []
    for row in rows:
        proposal = read.get(row.id)
        if proposal is None:
            done.append(row)
            continue
        turn = (proposal.get("proposed_turn") or "").strip()
        done.append(
            reading.with_proposal(
                row,
                proposal.get("proposed_text") or "",
                int(turn) if turn else None,
                (proposal.get("confidence") or "").strip(),
            )
        )
    reading.write_key(key, done)
    page.write_text(reading.index_html(done, "Reading exam: confirm each label"), encoding="utf-8")
    counts = Counter(row.confidence or "no proposal" for row in done)
    print(
        f"wrote {key} and {page}: {len(done)} labels, "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pack", action="append", default=[], help="TAG=FOLDER of a set pack")
    parser.add_argument("--out", type=Path, help="the new, empty folder to build the exam in")
    parser.add_argument("--total", type=int, default=reading.TOTAL)
    parser.add_argument("--control-each", type=int, default=reading.CONTROL_EACH)
    parser.add_argument("--minimum", type=int, default=reading.MINIMUM)
    parser.add_argument("--seed", default=reading.SEED)
    parser.add_argument("--finish", type=Path, help="a built exam folder to write the key in")
    parser.add_argument("--proposals", type=Path, help="the proposals TSV for --finish")
    args = parser.parse_args(argv)
    if args.finish:
        if not args.proposals:
            raise SystemExit("--finish needs --proposals")
        return finish(args.finish.expanduser(), args.proposals.expanduser())
    if not args.pack or not args.out:
        raise SystemExit("--pack (one or more) and --out are needed to build")
    return build(_packs(args.pack), args.out.expanduser(), args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
