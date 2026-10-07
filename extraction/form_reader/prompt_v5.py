"""Versioned form-first reading instructions (v5).

The built-in prompt is deliberately value-agnostic. It contains no customer drawing examples or
values, and it is not the measured prompt: the measured reading guidance quotes the client's own
labels, so it lives in a private file outside this public repository (#972) and is joined here to
the public answer-layout section that the strict parser checks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from extraction.product_context import product_context_line, with_product
from vocabulary.semantic_types import ProductType

PROMPT_ID = "form-reader-v5"
TEMPLATE_ID = "countertop-form-json-v5"
PRIVATE_PROMPT_PREFIX = "form-reader-private-"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

BUILT_IN_GUIDANCE_V5 = """You read one page image from a cabinet or millwork drawing package. Identify the vendor's shop drawing on the page yourself; no pre-confirmed drawing role is supplied. Copy printed dimension labels from that vendor view into the requested form. Do not calculate, add, subtract, round, or correct values. A separate deterministic parser reads the printed text and separate software makes every comparison and verdict. Your classification is a suggestion only.

Find each countertop run shown on the vendor drawing, in top-to-bottom then left-to-right order. For each run, return its printed overall dimension if one exists and the horizontal chain of piece dimensions in left-to-right order. Do not include vertical dimensions, appliance position offsets, notes, scales, title-block numbers, or reviewer markup. Never infer a missing number. If the page has no vendor countertop run, return an empty countertops array.

For an overall dimension, set overall_scope to run only if its two ends span exactly the listed countertop pieces. If it spans a wall or anything outside those pieces, use wall. If uncertain, use unknown. Never add chain pieces to derive an overall.

For each dimension, copy the entire printed label into text. Report whole, numerator and denominator as decimal digit strings when clear, otherwise empty strings. These components are cross-checks, not a calculation. Mark stacked true only for a visibly stacked fraction. Mark combined true for a single printed label containing multiple measurements, equal-split notation, or another compound label. Mark readable false if clipped, overlapped, or uncertain. The box is the tight rectangle around the printed characters only, as [x0,y0,x1,y1] integer coordinates from 0 to 1000 relative to this image. Boxes are display hints only and must not affect copied text or classification. If uncertain about a piece kind, use unknown. Allowed kinds: filler, cabinet, cabinets_equal, appliance_space, end_panel, unknown."""

# The answer layout the strict parser and the Kimi JSON schema check. Public and value-free; it is
# appended to whichever guidance is in use, so the shape never depends on a private file.
OUTPUT_CONTRACT_V5 = """Return exactly one JSON object and no prose, matching this shape. Include every shown key. Use position 0 for overall; use 1-based left-to-right positions for chain entries. Set overall_scope to exactly one of run, wall, or unknown:
{"countertops":[{"view_title":"","overall_scope":"run","overall":{"position":0,"text":"","whole":"","numerator":"","denominator":"","stacked":false,"kind":"unknown","combined":false,"readable":true,"box":[0,0,0,0]},"chain":[{"position":1,"text":"","whole":"","numerator":"","denominator":"","stacked":false,"kind":"unknown","combined":false,"readable":true,"box":[0,0,0,0]}]}],"notes":""}

Use null for an absent overall or absent box. Empty strings are allowed only for a missing component; if text itself cannot be read, set readable false. Do not include a field not present in the shape."""

SYSTEM_PROMPT_V5 = f"{BUILT_IN_GUIDANCE_V5}\n\n{OUTPUT_CONTRACT_V5}"


def page_prompt(page_index: int, product: ProductType | None = None) -> str:
    """Supply a stable page identifier without asking the model to echo it into its answer.

    With a product (#994) the drawing set's product comes first, as one plain line of context; the
    system text and the private guidance are unchanged. Without one the text is exactly as before.
    """
    if isinstance(page_index, bool) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    request = f"Read the attached page image. Internal page index: {page_index}. Return JSON only."
    if product is None:
        return request
    return f"{product_context_line(product)}\n{request}"


@dataclass(frozen=True, slots=True)
class FormPrompt:
    """The full system instructions sent to both readers, and the identity recorded with them."""

    system_text: str
    prompt_id: str
    template_id: str = TEMPLATE_ID
    product: ProductType | None = None
    """The drawing set's product, told to the reader as one line of each page request (#994).
    Set only through :meth:`for_product`, which records it in ``prompt_id``."""

    def for_product(self, product: ProductType | None) -> FormPrompt:
        """These instructions for one drawing set: same system text, the product line on each page
        request, and the product recorded in the prompt id. ``None`` returns them unchanged."""
        if self.product is not None:
            raise ValueError("this prompt already names a product")
        if product is None:
            return self
        return replace(self, product=product, prompt_id=with_product(self.prompt_id, product))


BUILT_IN_PROMPT = FormPrompt(system_text=SYSTEM_PROMPT_V5, prompt_id=PROMPT_ID)


def prompt_from_guidance_file(path: Path) -> FormPrompt:
    """Join private reading guidance to the public answer layout; identify it by content hash.

    The file must live outside the repository working tree, so the client's examples it quotes
    cannot be committed by accident, and it must hold some guidance. The recorded prompt id is a
    hash of the full instructions: a run states exactly which text it used without storing it.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved == _REPOSITORY_ROOT or _REPOSITORY_ROOT in resolved.parents:
        raise ValueError(
            "GV_FORM_READER_PROMPT_FILE must be outside the repository: it quotes client drawings"
        )
    try:
        guidance = resolved.read_bytes().decode("utf-8").strip()
    except FileNotFoundError as error:
        raise ValueError(f"GV_FORM_READER_PROMPT_FILE does not exist: {resolved}") from error
    if not guidance:
        raise ValueError("GV_FORM_READER_PROMPT_FILE is empty")
    system_text = f"{guidance}\n\n{OUTPUT_CONTRACT_V5}"
    digest = hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:12]
    return FormPrompt(system_text=system_text, prompt_id=f"{PRIVATE_PROMPT_PREFIX}{digest}")
