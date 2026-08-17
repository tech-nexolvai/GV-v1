"""The shared vocabulary: what a thing on a drawing *means*.

`SemanticType`, `ProductType`, `OperandSource` and the rest name concepts, not decisions. Three
packages need them for unrelated reasons — `rules/` to say what a check is about, `evidence/` to
label what was read, `extraction/` to type an item it found on a page — and none of those uses tells
the others anything about how a verdict is reached.

**Why they moved here.** They lived in `rules/`, which made naming a concept indistinguishable from
importing the rule engine. `docs/DESIGN_EXTRACTION.md` §2 forbids `extraction/` from importing
`rules/` — *"an extractor that knows which rule is coming is an extractor that can be tuned to
satisfy it"* — and that reasoning is about rule **logic**: tolerances, thresholds, which check fires.
A word for "countertop overall width" carries none of it.

The rule was already being broken. `evidence/canonical.py` imported `rules.semantic_types` and
nothing caught it, because the isolation guard only covered `verdict/` and `rules/`. Moving the
vocabulary makes the import table true again rather than amending it to fit what the code does.

Like `units/`, this package **imports nothing** from the project. That is what lets everything else
depend on it safely, and a test asserts it.
"""

from vocabulary.semantic_types import *
