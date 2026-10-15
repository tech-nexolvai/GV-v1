#!/usr/bin/env python3
"""Row keys by geometry (#1321): build a stored run's box map, and convert a box-number key.

No model call, no cost. The formats and the rules are in `eval/pariksha/row_geometry.py`.

    # build: prove where each numbered box of a stored run's row pictures is on its page
    python scripts/pariksha_row_boxes.py build --db <database name or URL> [--run <run id>] \\
        --pdf-root ~/gv-local/<store or folder holding the PDFs> [--pdf-root ...] \\
        --out ~/gv-local/keys/<box map>.json

    # convert: a box-number rows key into a geometry rows key (a new key file, never in place)
    python scripts/pariksha_row_boxes.py convert --key ~/gv-local/keys/rekha/<key>.json \\
        --row-boxes ~/gv-local/keys/<box map>.json [--row-boxes ...] --tolerance-pt 1 \\
        --name <new name> --version <new version> --out ~/gv-local/keys/rekha/<new key>.json

**build** reads the database only (a read-only transaction): every row question the run stored
(`slot-row-choice-*`), with the picture it showed, its page and document. Each document is found
under `--pdf-root` by its content hash (the file `<sha256>.pdf`, as the artifact store keeps it,
checked against its bytes). For each picture the page is built as the reading built it (the same
render, the same row candidates with the run's architect drawings set aside) and the picture is
drawn again with the product's own picture code; only a byte-for-byte match gives the boxes
(`prove_picture_boxes`). Every picture that is not proven is listed with the reason, and left out.

The box map and the keys name client pages by their hashes: they are written once (an existing file
is refused) and never inside a git working tree.

Exit codes: 0 done, 2 refused.

Source: issue #1321 · Verification: `tests/scripts/test_pariksha_row_boxes.py`,
`tests/eval/test_pariksha_row_geometry.py`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pariksha_scorecard import REFUSED, Refused, _database_url, _outside_git
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from eval.pariksha.keys import KEY_FORMAT, AnswerKey, KeyFormatError, dump_key, read_key
from eval.pariksha.row_geometry import (
    PictureBoxes,
    RowBoxes,
    RowBoxesError,
    prove_picture_boxes,
    read_row_boxes,
    to_geometry_key,
    write_row_boxes,
)
from eval.pariksha.stored import prompt_family

_ROW_FAMILY = "slot-row-choice"


@dataclass(frozen=True, slots=True)
class _Asked:
    """One stored row picture and the page it was drawn from."""

    picture_sha256: str
    page_sha256: str
    document_version_id: UUID
    page_index: int
    dpi: int


def _pictures(session: Session, run: UUID | None) -> dict[str, _Asked]:
    from app.models.runs import ModelInvocation

    query = select(ModelInvocation.prompt_id, ModelInvocation.reader_question_packet).where(
        ModelInvocation.reader_question_packet.is_not(None),
        ModelInvocation.prompt_id.startswith(f"{_ROW_FAMILY}-v"),
    )
    if run is not None:
        query = query.where(ModelInvocation.extraction_run_id == run)
    found: dict[str, _Asked] = {}
    for prompt_id, packet in session.execute(query):
        if prompt_family(prompt_id) != _ROW_FAMILY or not isinstance(packet, Mapping):
            continue
        images = packet.get("images")
        transform = packet.get("page_transform")
        if not isinstance(images, Mapping) or not isinstance(transform, Mapping):
            continue
        for image in images.values():
            if not isinstance(image, Mapping) or not isinstance(image.get("sha256"), str):
                continue
            asked = _Asked(
                picture_sha256=str(image["sha256"]),
                page_sha256=str(packet.get("source_page_sha256")),
                document_version_id=UUID(str(packet.get("document_version_id"))),
                page_index=int(str(packet.get("page_index", -1))),
                dpi=int(str(transform.get("dpi", 0))),
            )
            known = found.setdefault(asked.picture_sha256, asked)
            if known != asked:
                raise Refused(
                    f"picture {asked.picture_sha256[:12]} was stored for two pages "
                    f"({known.page_sha256[:12]}, {asked.page_sha256[:12]})"
                )
    return found


def _pdf_index(roots: Sequence[Path]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for root in roots:
        for path in Path(root).expanduser().rglob("*.pdf"):
            index.setdefault(path.stem, path)
    return index


def _labelled_pages(data: bytes, pages: Sequence[object], version_id: UUID) -> set[UUID]:
    """The pages that print headings (#1193), read as the reading stage reads them."""
    from extraction.annotations import read_markup_layer
    from extraction.panels import read_panels
    from extraction.reader import UnreadablePdf

    labelled: set[UUID] = set()
    for page in pages:
        try:
            layers = read_markup_layer(
                data,
                page.index,  # type: ignore[attr-defined]
                document_version_id=version_id,
                dpi=300,
            )
        except UnreadablePdf:
            continue
        layout = read_panels(layers.vendor_stamps, layers.markup)
        if layout and layout.labelled:
            labelled.add(page.id)  # type: ignore[attr-defined]
    return labelled


def _prove_document(
    session: Session, version_id: UUID, data: bytes, asked: Sequence[_Asked]
) -> list[PictureBoxes | str]:
    from app.models.document import Page
    from extraction.geometry.rows import MEASURED_SETTINGS
    from extraction.rasterise import render_page
    from extraction.rows import page_rows_and_ink
    from workflow.slot_reader import SlotPage
    from workflow.stages import (
        MAXIMUM_RENDER_PIXELS,
        _architect_candidate_filter_boxes_by_page,
        page_transform,
    )

    pages = list(
        session.scalars(
            select(Page).where(Page.document_version_id == version_id).order_by(Page.index)
        )
    )
    by_index = {page.index: page for page in pages}
    # The run's own drawings that are not the vendor's, set aside exactly as the reading did.
    architect = _architect_candidate_filter_boxes_by_page(
        session,
        tuple(page.id for page in pages),
        labelled_pages=_labelled_pages(data, pages, version_id),
    )
    results: list[PictureBoxes | str] = []
    for item in asked:
        page = by_index.get(item.page_index)
        if page is None or page.content_hash != item.page_sha256:
            results.append(
                f"picture {item.picture_sha256[:12]}: its page {item.page_index} is not the page "
                f"its question names ({item.page_sha256[:12]})"
            )
            continue
        rendered = render_page(
            data,
            page.index,
            document_version_id=version_id,
            page_content_hash=page.content_hash,
            dpi=item.dpi,
            maximum_pixels=MAXIMUM_RENDER_PIXELS,
            vendor_only=True,
        )
        rows = page_rows_and_ink(
            data,
            page.index,
            dpi=item.dpi,
            settings=MEASURED_SETTINGS,
            architect_boxes=architect.get(page.id, ()),
        )
        slot_page = SlotPage(
            page_index=page.index,
            page_id=page.id,
            document_version_id=version_id,
            rendered=rendered,
            rows=rows,
            ink=None,
            transform=page_transform(page, item.dpi),
        )
        results.append(
            prove_picture_boxes(
                slot_page, picture_sha256=item.picture_sha256, page_sha256=item.page_sha256
            )
        )
    return results


def _build(args: argparse.Namespace) -> int:
    from app.models.document import DocumentVersion

    out = _outside_git(args.out)
    if out.exists():
        raise Refused(f"{out} already exists: a box map is written once")
    pdfs = _pdf_index(args.pdf_root)
    url = _database_url(args.db)
    engine = create_engine(url)
    proven: list[PictureBoxes] = []
    refused: list[str] = []
    try:
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            try:
                asked = _pictures(session, args.run)
                by_document: dict[UUID, list[_Asked]] = {}
                for item in asked.values():
                    by_document.setdefault(item.document_version_id, []).append(item)
                for version_id, items in sorted(by_document.items(), key=lambda kv: str(kv[0])):
                    # Only the content hash: older databases lack later columns of the table.
                    version = session.scalar(
                        select(DocumentVersion.sha256).where(DocumentVersion.id == version_id)
                    )
                    path = None if version is None else pdfs.get(version)
                    if version is None or path is None:
                        refused += [
                            f"picture {item.picture_sha256[:12]}: the document's PDF was not "
                            "found under --pdf-root"
                            for item in items
                        ]
                        continue
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != version:
                        raise Refused(f"{path} does not hold the bytes its name says")
                    for result in _prove_document(session, version_id, data, items):
                        if isinstance(result, str):
                            refused.append(result)
                        else:
                            proven.append(result)
            finally:
                session.rollback()
    finally:
        engine.dispose()
    if not proven:
        raise Refused("no picture's boxes were proven: " + "; ".join(refused[:5]))
    write_row_boxes(RowBoxes.of(proven), out)
    print(f"box map: {len(proven)} picture(s) proven byte for byte, {len(refused)} not")
    for reason in refused:
        print(f"  NOT PROVEN: {reason}")
    print(f"  written: {out}")
    return 0


def _convert(args: argparse.Namespace) -> int:
    out = _outside_git(args.out)
    if out.exists():
        raise Refused(f"{out} already exists: a key is never overwritten")
    key = read_key(args.key.expanduser())
    if not isinstance(key, AnswerKey) or key.agent != "rekha":
        raise Refused(f"{args.key} is not a Rekha rows key")
    try:
        tolerance = Decimal(args.tolerance_pt)
    except InvalidOperation as error:
        raise Refused(f"--tolerance-pt {args.tolerance_pt!r} is not a number") from error
    converted = to_geometry_key(
        key,
        read_row_boxes(args.row_boxes),
        tolerance=tolerance,
        name=args.name,
        version=args.version,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as handle:
        handle.write(json.dumps(dump_key(converted), indent=1, ensure_ascii=False) + "\n")
    lines = sum(1 for case in converted.cases if getattr(case.truth, "at", None) is not None)
    print(
        f"{converted.name} {converted.version} ({KEY_FORMAT}, {converted.sha256[:12]}): "
        f"{len(converted.cases)} case(s), {lines} named by geometry within "
        f"{converted.row_tolerance_pt} pt"
    )
    print(f"  written: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="prove a stored run's row pictures' boxes")
    build.add_argument("--db", required=True, help="the stored run's database: URL or name")
    build.add_argument("--run", type=UUID, help="only this extraction run's questions")
    build.add_argument(
        "--pdf-root", type=Path, action="append", required=True, help="where the PDFs are"
    )
    build.add_argument("--out", type=Path, required=True, help="the new box map file")
    build.set_defaults(handler=_build)
    convert = commands.add_parser("convert", help="a box-number rows key as a geometry key")
    convert.add_argument("--key", type=Path, required=True)
    convert.add_argument("--row-boxes", type=Path, action="append", required=True)
    convert.add_argument("--tolerance-pt", required=True, help="the key's stated tolerance")
    convert.add_argument("--name", required=True)
    convert.add_argument("--version", required=True)
    convert.add_argument("--out", type=Path, required=True, help="the new key file")
    convert.set_defaults(handler=_convert)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (Refused, RowBoxesError, KeyFormatError, FileExistsError) as error:
        print(f"pariksha row boxes: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
