"""The simple full check's compare prompt: short, fixed, one per run (cached), plus the item's text.

The system prompt is the same for every drawing of a run (the GV rules and the product scope of the
run are appended to it, in the same words for every call), so Anthropic's prompt cache reads it
back for every call after the first. Everything that changes per drawing is in the user message:
the pictures (each introduced by one line), then the item's text (`item_text`).

What the prompt insists on (CONTRACT-SIMPLE, 2026-10-19):

- the architect's drawings are the truth; the vendor's drawing is what is checked;
- every vendor label is accounted for (a pair or `unaccounted_labels`);
- two dimensions that do not measure the same two points are `different_points`, never
  `different` (underside against top of a counter, a run to a soffit against the top of a unit, a
  splash band inside one of them, an overall against a part);
- room features (ceilings, soffits, doors, artwork, switches, lights) are `millwork: false`;
- a number with no code label (pen strokes, hand lettering, an EQ mark) is still compared, as a
  `dimension_without_label` with the value read and its box on both pictures;
- label ids are code's; the model never types a value for a labelled dimension.

One worked example with made-up values shows the form. The model reasons in its own thinking
(adaptive thinking runs before the form is filled), so the form has no reasoning field.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

from app.review.assistant.simple_check.schema import END_WORDS, Label, Picture, WorkItem

__all__ = [
    "EXAMPLE_REPLY",
    "SYSTEM_PROMPT",
    "item_text",
    "repair_text",
    "system_prompt",
]

#: The worked example's answer (made-up values; no real drawing).
EXAMPLE_REPLY: Final = {
    "work_item": "p12.v2",
    "pairs": [
        {
            "vendor_label": "p12.D3",
            "architect_label": "p11.D2",
            "measures": "overall width of the run, wall to wall",
            "vendor_from": "wall_face (left)",
            "vendor_to": "wall_face (right)",
            "architect_from": "wall_face (left)",
            "architect_to": "wall_face (right)",
            "verdict": "same",
            "note": "",
        },
        {
            "vendor_label": "p12.D4",
            "architect_label": "p11.D5",
            "measures": "floor to the counter",
            "vendor_from": "floor",
            "vendor_to": "underside_of_counter",
            "architect_from": "floor",
            "architect_to": "top_of_counter",
            "verdict": "different_points",
            "note": "vendor to the stone's underside, architect to its top",
        },
        {
            "vendor_label": "p12.D6",
            "architect_label": "p11.D7",
            "measures": "left wall to the sink centre line",
            "vendor_from": "wall_face (left)",
            "vendor_to": "centre_line (sink)",
            "architect_from": "wall_face (left)",
            "architect_to": "centre_line (sink)",
            "verdict": "different",
            "note": "",
        },
        {
            "vendor_label": "p12.D8",
            "architect_label": None,
            "measures": "depth of the toe kick",
            "vendor_from": "cabinet_side (front)",
            "vendor_to": "other (toe kick back)",
            "architect_from": "",
            "architect_to": "",
            "verdict": "no_architect_value",
            "note": "",
        },
    ],
    "other_items": [
        {
            "kind": "missing_on_vendor",
            "what": "outlet in the backsplash, right of the sink",
            "vendor": None,
            "architect": "duplex outlet, 1'-6\" from the right wall",
            "vendor_image": None,
            "vendor_box_px": None,
            "architect_image": 2,
            "architect_box_px": [1410, 620, 1475, 690],
            "verdict": "different",
            "millwork": True,
        },
        {
            "kind": "dimension_without_label",
            "what": "height of the upper shelf (hand-lettered on both)",
            "vendor": '60"',
            "architect": "5'-0\"",
            "vendor_image": 1,
            "vendor_box_px": [212, 340, 268, 372],
            "architect_image": 2,
            "architect_box_px": [188, 300, 252, 330],
            "verdict": "same",
            "millwork": True,
        },
        {
            "kind": "note",
            "what": "light fixture above the run",
            "vendor": None,
            "architect": "pendant light",
            "vendor_image": None,
            "vendor_box_px": None,
            "architect_image": 2,
            "architect_box_px": [900, 40, 980, 120],
            "verdict": "no_architect_value",
            "millwork": False,
        },
    ],
    "unaccounted_labels": ["p12.D9"],
    "same_drawing": "yes",
}

_EXAMPLE_INPUT: Final = """Work item p12.v2 (vendor round 2). Image 1: the vendor drawing. Image 2: the architect drawing p11.a1.
Vendor labels: p12.D3 10'-0" | p12.D4 34 1/2" | p12.D6 2'-3" | p12.D8 3" | p12.D9 1/2"
Architect labels (p11.a1): p11.D2 10'-0" | p11.D5 2'-11 3/4" | p11.D7 2'-0"
(D4 runs to the stone's underside; the architect's D5 to the counter's top line. D9 is a small value whose ends cannot be seen. The architect draws an outlet right of the sink that the vendor omits, and a pendant light. Both print a shelf height by hand with no label.)"""

SYSTEM_PROMPT: Final = f"""You compare one millwork vendor's shop drawing with the architect's drawing(s) of the same area, for a review that GV, the millwork contractor, sends back to the vendor. The architect's drawings are the truth; the vendor's drawing is what is checked.

You get pictures cut by code: the vendor drawing and the architect drawing(s), the governing architect drawing first (a detail or the larger scale). On each picture code drew a blue tag with an id (D17) beside every printed dimension it found. The text lists every tag as p<page>.D<n> with the value code read. You answer in the JSON form; code then checks every value and every pair on the drawing before anything is reported, so give code ids and boxes, not opinions.

Rules:
1. Account for EVERY vendor label in the list: put it in pairs (with the architect label that measures the same thing, or null), or, only when you cannot tell what it measures, in unaccounted_labels. Never leave one out.
2. Use the label ids exactly as listed (p23.D17). Never type a value for a labelled dimension: code reads it.
3. For each pair, say what it measures and where each end lands on each drawing. Start each end with one of: {", ".join(END_WORDS)}; then a few words in brackets, e.g. "centre_line (fridge outlet)".
4. Verdicts:
   - same: both dimensions run between the same two points and the values are equal;
   - different: the same two points, and the values differ;
   - different_points: the two dimensions do not measure the same thing (underside against top of a counter, a run to a soffit or ceiling against the top of a unit, a splash band inside one of them, an overall against a part of it, two different outlets);
   - no_architect_value: the architect gives no dimension for it;
   - unsure: the drawings do not settle it (an end you cannot see, a value you cannot read).
   Say different only when you are sure both ends are the same points.
5. other_items, for what has no label:
   - dimension_without_label: a number with no blue tag (drawn in pen strokes, hand-lettered, an EQ mark): the value as you read it on each side, and its box on each picture. Compare these as carefully as labelled ones;
   - missing_on_vendor: something the architect shows (an outlet, a sink, an opening, a tag, a dimension) that the vendor drawing leaves out: the architect's value or tag and its box;
   - added_on_vendor: something on the vendor drawing the architect does not show;
   - layout_count: doors against drawers, the number of bays, shelves or fronts;
   - tag: a finish, material or fixture code that differs or is missing;
   - note: a written note the other drawing contradicts.
   Boxes are [x0, y0, x1, y1] in pixels on the picture you name (vendor_image, architect_image: the image numbers). Every item has at least one box.
   millwork is true for the millwork itself (cabinets, tops, splashes, panels, shelves, fronts, sinks, appliance openings, and outlets or cut-outs in the millwork); false for room features (ceilings, soffits, walls, doors, artwork, switches, lights, fixtures outside the unit).
6. The blue tags are code's. The vendor's drawing is its black and grey ink: coloured ink inside a drawing (red, yellow or highlighted notes and dimensions) is earlier review markup, never a vendor value.
7. same_drawing: yes when the architect drawing(s) show the same thing as the vendor drawing (the same unit, run or elevation, whatever differs in its sizes); no when they plainly show something else (another room, another unit or wall); unsure when you cannot tell.
Keep notes short; reason before you answer.

Example (made-up values):
{_EXAMPLE_INPUT}
Answer:
{json.dumps(EXAMPLE_REPLY, separators=(",", ":"))}"""


def system_prompt(
    rules_summary: str = "", scope_words: str = "All products", *, packs: str = ""
) -> str:
    """The run's system prompt: `SYSTEM_PROMPT`, the products in scope, what their product packs add
    (`packs`: `product_packs.pack_prompt_parts(products).compare_text()`, their checklists, what
    counts as ours, extra end words, worked examples; #1776) and GV's rules for them. The same bytes
    for every drawing of a run (the prompt cache keys on them)."""
    rules = rules_summary.strip() or "- none given for this run"
    added = f"\n\n{packs.strip()}" if packs.strip() else ""
    return (
        f"{SYSTEM_PROMPT}\n\nProducts in scope for this review: {scope_words}. Compare every "
        f"dimension and item of the drawing; code files each under its product.{added}\n\nGV's own rules "
        "for these products (standards the vendor must follow even where the architect is "
        f"silent; code applies them to the values):\n{rules}"
    )


def _label_line(label: Label) -> str:
    bits = [label.text]
    if label.inches is not None and label.text.replace('"', "").strip() != label.inches:
        bits.append(f"= {label.inches} in")
    if label.route == "drawn":
        bits.append("(pen strokes read by code)")
    return f"{label.ref} {' '.join(bits)}"


def _labels(labels: Sequence[Label]) -> str:
    return " | ".join(_label_line(label) for label in labels) or "none"


def _images_of(pictures: Sequence[Picture], ref: str) -> str:
    numbers = [str(p.number) for p in pictures if p.drawing == ref]
    return ", ".join(numbers) or "none"


def item_text(item: WorkItem, pictures: Sequence[Picture]) -> str:
    """The item's text, after its pictures: what each drawing is, and every label with its value."""
    vendor = item.vendor
    lines = [
        f"Work item {item.id}: the vendor drawing on page {item.page}"
        + (f", round {item.round}" if item.round is not None else "")
        + (" (the newest round of this area)" if item.newest_round else " (a later round exists)")
        + ".",
        f"Area: {item.area or 'not named'}" + (f" ({item.section})" if item.section else "") + ".",
    ]
    if item.products:
        lines.append(f"Products named on it: {', '.join(item.products)}.")
    lines.append(
        f"Vendor drawing {vendor.ref} (images {_images_of(pictures, vendor.ref)})"
        + (f", titled {'; '.join(vendor.titles)}" if vendor.titles else "")
        + "."
    )
    lines.append(
        f"VENDOR LABELS ({len(vendor.labels)}; each goes in a pair or in unaccounted_labels): "
        + _labels(vendor.labels)
    )
    for n, drawing in enumerate(item.architect):
        role = "the governing architect drawing" if n == 0 else "another architect drawing"
        about = [f"page {drawing.page}"]
        if drawing.titles:
            about.append("; ".join(drawing.titles))
        if drawing.scale_note:
            about.append(f"scale {drawing.scale_note}")
        if drawing.detail:
            about.append("a detail")
        lines.append(
            f"Architect drawing {drawing.ref}, {role} ({', '.join(about)}; images "
            f"{_images_of(pictures, drawing.ref)}). Labels: {_labels(drawing.labels)}"
        )
    if item.paired_by:
        lines.append(
            "The vendor's and the architect's drawings are separate files: code paired this "
            f"architect drawing with the vendor drawing ({item.paired_by}). If it plainly shows "
            "something else, answer same_drawing no."
        )
    if item.architect_only_tags:
        lines.append(
            "Tags code found on the architect drawing and not on the vendor's: "
            + ", ".join(item.architect_only_tags)
            + "."
        )
    if item.reference:
        lines.append(
            "The area's brand prototype ("
            + ", ".join(d.ref for d in item.reference)
            + ") is reference only and not shown: the architect governs."
        )
    lines.append(
        f"Answer for work item {item.id} in the JSON form. Every vendor label above appears once, "
        "in a pair or in unaccounted_labels; numbers without a tag go in other_items as "
        "dimension_without_label, with boxes."
    )
    return "\n".join(lines)


def repair_text(item: WorkItem, problems: Sequence[str]) -> str:
    """The one follow-up turn after code's checks reject an answer."""
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"Code checked your answer for work item {item.id} and found these problems:\n{listed}\n"
        "Answer again with the whole JSON form, fixed. Keep everything that was right."
    )
