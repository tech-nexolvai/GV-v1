"""Deterministically lay out reviewed-PDF FreeText annotations for human comparison.

This is exploratory harness code only. It reads PDF annotations and page text directly;
it does not OCR, classify, normalize, evaluate, or send data to a model.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader

ID_SET_LABEL = "ID SET ELEVATION"
VENDOR_LABEL = "VENDOR'S SHOP DRAWING ELEVATION"


@dataclass(frozen=True)
class Markup:
    """A verbatim `/FreeText` annotation and its PDF rectangle."""

    text: str
    author: str
    rect: tuple[str, str, str, str]
    role: str

    @property
    def center_x(self) -> float:
        return (float(self.rect[0]) + float(self.rect[2])) / 2

    @property
    def rect_text(self) -> str:
        return "[" + ", ".join(self.rect) + "]"


@dataclass(frozen=True)
class PageRecord:
    page_number: int
    project: str
    is_cover: bool
    id_set_label: Markup | None
    vendor_label: Markup | None
    annotations: tuple[Markup, ...]


def _annotation_refs(page: Any) -> Iterable[Any]:
    annotations = page.get("/Annots")
    if annotations is None:
        return ()
    return annotations.get_object()


def _normalized_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold().replace("’", "'"))


def _label_role(text: str) -> str:
    normalized = _normalized_label(text)
    if _normalized_label(ID_SET_LABEL) in normalized:
        return "id_set_section_label"
    if _normalized_label(VENDOR_LABEL) in normalized:
        return "vendor_section_label"
    return "markup"


def _read_free_text(page: Any) -> list[Markup]:
    annotations: list[Markup] = []
    for reference in _annotation_refs(page):
        annotation = reference.get_object()
        if annotation.get("/Subtype") != "/FreeText":
            continue
        rect_values = annotation.get("/Rect")
        if rect_values is None or len(rect_values) != 4:
            raise ValueError("A /FreeText annotation is missing its four-value /Rect.")
        text = str(annotation.get("/Contents", ""))
        annotations.append(
            Markup(
                text=text,
                author=str(annotation.get("/T", "")),
                rect=tuple(str(value) for value in rect_values),
                role=_label_role(text),
            )
        )
    return annotations


def _cover_title(page: Any) -> str | None:
    """Return a cover's page text without relying on client-specific project names."""
    text = " ".join((page.extract_text() or "").split())
    return text or None


def extract_pages(pdf_path: Path) -> list[PageRecord]:
    """Read all FreeText annotations and group pages below their detected cover."""
    reader = PdfReader(pdf_path)
    free_text_by_page = [_read_free_text(page) for page in reader.pages]

    records: list[PageRecord] = []
    current_project = "Project not identified"
    for index, (page, page_annotations) in enumerate(
        zip(reader.pages, free_text_by_page, strict=True)
    ):
        next_annotations = free_text_by_page[index + 1] if index + 1 < len(reader.pages) else []
        next_has_sections = {annotation.role for annotation in next_annotations} >= {
            "id_set_section_label",
            "vendor_section_label",
        }
        is_cover = not page_annotations and next_has_sections
        if is_cover:
            current_project = _cover_title(page) or "Project cover without extractable text"

        id_set_label = next(
            (
                annotation
                for annotation in page_annotations
                if annotation.role == "id_set_section_label"
            ),
            None,
        )
        vendor_label = next(
            (
                annotation
                for annotation in page_annotations
                if annotation.role == "vendor_section_label"
            ),
            None,
        )
        records.append(
            PageRecord(
                page_number=index + 1,
                project=current_project,
                is_cover=is_cover,
                id_set_label=id_set_label,
                vendor_label=vendor_label,
                annotations=tuple(page_annotations),
            )
        )
    return records


def _side(annotation: Markup, page: PageRecord) -> str:
    """Assign by the requested nearest section-label x-position rule."""
    if annotation.role != "markup":
        return "section_label"
    if page.id_set_label is None or page.vendor_label is None:
        return "unassigned"
    id_distance = abs(annotation.center_x - page.id_set_label.center_x)
    vendor_distance = abs(annotation.center_x - page.vendor_label.center_x)
    return "id_set" if id_distance <= vendor_distance else "vendor"


def _render_annotation(annotation: Markup) -> str:
    content = html.escape(annotation.text).replace("\n", "<br>")
    author = html.escape(annotation.author) if annotation.author else "(no /T)"
    return (
        f"<pre>{content}</pre>"
        f"author: {author}<br>"
        f"/Rect: <code>{html.escape(annotation.rect_text)}</code>"
    )


def write_markdown(records: list[PageRecord], output_path: Path) -> None:
    """Write the human-readable side-by-side annotation table."""
    lines = [
        "# Reviewed drawing-set markup extraction",
        "",
        (
            "Deterministic `/FreeText` extraction only. Text, author (`/T`), and rectangle (`/Rect`) "
            "are reported verbatim; no values have been normalized or evaluated."
        ),
        "",
        (
            "Side assignment uses each annotation rectangle's horizontal centre and the nearest "
            "section label rectangle's horizontal centre."
        ),
        "",
    ]
    current_project: str | None = None
    for page in records:
        if page.project != current_project:
            current_project = page.project
            lines.extend([f"## Project: {html.escape(current_project)}", ""])

        cover_note = " (project cover)" if page.is_cover else ""
        lines.extend([f"### Page {page.page_number}{cover_note}", ""])
        if page.id_set_label is not None and page.vendor_label is not None:
            lines.extend(
                [
                    "Section-label positions:",
                    "",
                    (
                        f"- ID SET ELEVATION: x={page.id_set_label.center_x:g}; "
                        f"/Rect `{page.id_set_label.rect_text}`; /T `{page.id_set_label.author}`"
                    ),
                    (
                        f"- VENDOR'S SHOP DRAWING ELEVATION: x={page.vendor_label.center_x:g}; "
                        f"/Rect `{page.vendor_label.rect_text}`; /T `{page.vendor_label.author}`"
                    ),
                    "",
                ]
            )
        elif page.annotations:
            lines.extend(["Section labels: not present on this page.", ""])

        id_set = [
            annotation for annotation in page.annotations if _side(annotation, page) == "id_set"
        ]
        vendor = [
            annotation for annotation in page.annotations if _side(annotation, page) == "vendor"
        ]
        unassigned = [
            annotation for annotation in page.annotations if _side(annotation, page) == "unassigned"
        ]
        if not page.annotations:
            lines.extend(["No `/FreeText` annotations.", ""])
            continue

        id_cell = "<hr>".join(_render_annotation(annotation) for annotation in id_set) or "—"
        vendor_cell = "<hr>".join(_render_annotation(annotation) for annotation in vendor) or "—"
        lines.extend(
            [
                "<table>",
                "<thead><tr><th>ID-SET-side markup</th><th>VENDOR-side markup</th></tr></thead>",
                "<tbody><tr>",
                f'<td valign="top">{id_cell}</td>',
                f'<td valign="top">{vendor_cell}</td>',
                "</tr></tbody>",
                "</table>",
                "",
            ]
        )
        if unassigned:
            lines.extend(
                [
                    "Unassigned markup (a section label was unavailable):",
                    "",
                    *[f"- {_render_annotation(annotation)}" for annotation in unassigned],
                    "",
                ]
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(records: list[PageRecord], output_path: Path) -> None:
    """Write a one-row-per-FreeText audit file, including the two section labels."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "project",
                "page",
                "is_project_cover",
                "side",
                "annotation_role",
                "contents",
                "author_T",
                "rect",
                "center_x",
                "id_set_label_center_x",
                "vendor_label_center_x",
            ),
        )
        writer.writeheader()
        for page in records:
            for annotation in page.annotations:
                writer.writerow(
                    {
                        "project": page.project,
                        "page": page.page_number,
                        "is_project_cover": page.is_cover,
                        "side": _side(annotation, page),
                        "annotation_role": annotation.role,
                        "contents": annotation.text,
                        "author_T": annotation.author,
                        "rect": annotation.rect_text,
                        "center_x": f"{annotation.center_x:g}",
                        "id_set_label_center_x": (
                            f"{page.id_set_label.center_x:g}" if page.id_set_label else ""
                        ),
                        "vendor_label_center_x": (
                            f"{page.vendor_label.center_x:g}" if page.vendor_label else ""
                        ),
                    }
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf", type=Path, help="Reviewed drawing-set PDF to inspect")
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("data/exploration/set2_review_markup.md"),
        help="Ignored Markdown output path (default: %(default)s)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("data/exploration/set2_review_markup.csv"),
        help="Ignored CSV output path (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = extract_pages(args.input_pdf)
    write_markdown(records, args.markdown)
    write_csv(records, args.csv)
    markup_count = sum(len(page.annotations) for page in records)
    cover_pages = [page.page_number for page in records if page.is_cover]
    print(
        f"Extracted {markup_count} /FreeText annotations from {len(records)} pages; "
        f"project covers: {', '.join(str(page) for page in cover_pages)}."
    )
    print(f"Wrote {args.markdown} and {args.csv}.")


if __name__ == "__main__":
    main()
