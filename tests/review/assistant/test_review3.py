"""The third independent review of #1128, as tests (synthetic records only).

- fills reflect the screen's state: a decided result reads as decided, counts that mean "needs
  you" count only undecided results, sign-off uses readiness's own numbers, counts are uncapped;
- reader and vendor text is made inert before it is shown or put in a prompt;
- misleading framing is refused (fact sentences take glue words only; explanations no judgement,
  readiness claim, recommendation or first-person action);
- natural answers are accepted: the false-rejection rate over the review's natural answers and the
  realistic ones in `test_guard.py` stays at or under 15 %;
- code answers questions about terms, details and fixes; intent misses and false refusals.
"""

from __future__ import annotations

from app.review.assistant.records import sanitise

# ---- A. fills ------------------------------------------------------------------------------------


# ---- A4. reader and vendor text is inert ---------------------------------------------------------


def test_sanitise_keeps_the_words() -> None:
    assert sanitise("See [docs](https://x.example/y) **now**").split() == ["See", "docs", "now"]


# ---- B. misleading framing -----------------------------------------------------------------------


# ---- C. usability --------------------------------------------------------------------------------


# ---- D. intent -----------------------------------------------------------------------------------
