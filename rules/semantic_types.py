"""Re-export of the shared vocabulary, which now lives in `vocabulary/`.

These names were defined here, and forty-two modules import them from this path. Moving the
definitions without leaving this behind would have meant rewriting every one of those imports in the
same change that moved them — a large diff in which a genuine mistake would be invisible.

So the definitions moved and this stayed. New code should import from `vocabulary` directly; this
module exists so that nothing had to.

Why they moved at all: naming a concept was indistinguishable from importing the rule engine.
`docs/DESIGN_EXTRACTION.md` §2 forbids `extraction/` from importing `rules/`, and `#165` needs
`SemanticType` to type an item it found on a drawing. See `vocabulary/__init__.py`.
"""

from __future__ import annotations

from vocabulary.semantic_types import *
from vocabulary.semantic_types import __all__ as _vocabulary_all

__all__ = list(_vocabulary_all)
