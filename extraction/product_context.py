"""The one line of context a reader is told about the drawing set's product (#994).

The reviewer says at upload what a drawing set is for. The form reader and the slot (crop) reader
are told it in one plain sentence, so they know what kind of drawing they are looking at. That is
all it is: the private reading guidance is not changed, no evidence rule reads it, and a value
still counts only when it is printed on the drawing and two readers copy it identically.

**It changes what a model is sent, so it changes the prompt's identity.** A run that told the
readers the product records ``+product=<value>`` after the prompt id, so a measurement taken with
the line can never be confused with one taken without it. With no product (a set from before
#994), nothing is added: the request and the id are exactly what they were.

Source: issue #994 · Verification: `tests/extraction/test_product_context.py`.
"""

from __future__ import annotations

from typing import Final

from vocabulary.semantic_types import ProductType

__all__ = ["PRODUCT_SUFFIX", "product_context_line", "with_product"]

#: What a prompt id gains when the request carried the product line.
PRODUCT_SUFFIX: Final = "+product="


def product_context_line(product: ProductType) -> str:
    """The sentence sent to a reader, e.g. ``This drawing set was submitted for: countertop.``"""
    return f"This drawing set was submitted for: {ProductType(product).value}."


def with_product(prompt_id: str, product: ProductType | None) -> str:
    """The prompt id a request with (or without) the product line is recorded under."""
    if not prompt_id.strip():
        raise ValueError("a prompt id must be stated")
    if PRODUCT_SUFFIX in prompt_id:
        raise ValueError(f"prompt id {prompt_id!r} already names a product")
    if product is None:
        return prompt_id
    return f"{prompt_id}{PRODUCT_SUFFIX}{ProductType(product).value}"
