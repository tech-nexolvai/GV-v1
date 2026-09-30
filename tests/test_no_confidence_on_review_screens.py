"""A reviewer is never shown a machine confidence score for a reading.

`docs/V1_TARGET_AND_GAP.md` §1 states the rule the verdict path already follows: *"Confidence scores
are never consulted, because confidence is exactly what failed us: Nova returned `60` for a crop
reading `46"` at high confidence, and the old gate accepted it."*

The gate stopped consulting confidence. The reviewer screens did not, and that is the same mistake
one layer up — the human is the last line of defence, and a number that looks like authority beside a
reading is exactly what erodes it.

**What made it indefensible rather than merely debatable.** Measured on the 17-page `AI_Set_2.pdf`
run of 2026-09-29, `confidence` is written by exactly one route:

    rapidocr                 903 candidates   903 with confidence     0 with a value
    bedrock-ministral-3-3b    70 candidates     0 with confidence    59 with a value
    bedrock-nova-2-lite      268 candidates     0 with confidence    42 with a value

Every vision reader records `confidence=None`. RapidOCR — which read line-work as `一` and `口` and
produced not one usable number — reported 0.50 to 0.9999, mean 0.718. So the score appeared *only* on
the readings least worth trusting, and never on those from the best reader (#720, #703).

The column is still written and still queryable. This is about what a reviewer is shown.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "main" / "src"

#: A rendered confidence: the field reaching JSX, rather than the word in a comment or a class name.
RENDERED = re.compile(r"\{[^{}]*\bcandidate\.confidence\b[^{}]*\}")


def test_no_reviewer_screen_renders_a_reading_confidence() -> None:
    """**Input: every page and component. Outcome: none renders `candidate.confidence`.**"""
    offenders = [
        str(path.relative_to(FRONTEND))
        for path in sorted(FRONTEND.rglob("*.tsx"))
        if RENDERED.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"a confidence score is shown to a reviewer in: {offenders}"
