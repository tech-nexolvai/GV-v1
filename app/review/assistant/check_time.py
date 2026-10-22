"""How long a full check of a set usually takes, as a range from measured runs (#1883).

The live card said "usually 4-5 min" for every set; a 99-page set with a separate architect's file
took 43 minutes (its page matcher alone 18). The range now depends on the set: the content pages the
check compares, and how many vendor drawings the AI page matcher must first pair with the
architect's file. Measured roster full checks:

- Set 1, 5 content pages: about 5 minutes;
- Set 3, about 60 content pages, one combined file: about 25 minutes;
- a 99-page set with a separate architect's file (2026-10-22): 43 minutes in all: 18.5 waiting for
  the page matcher (96 drawings, 4 at a time, about 40 s a call), then 25 for the rest (the page
  helpers only 3 of them; the code checks at the end 11, the gap closers 7).

So the time grows more slowly than the pages (the helpers run side by side). The matcher now asks
8 at a time and the helpers start page by page beside it, so the range adds one call's wait at its
low end and the whole matching at its high end (as if no helper could start sooner). Anything code
cannot count (no content page) gives no range: the screen then says no time rather than a wrong
one.
"""

from __future__ import annotations

import math
from typing import Final

__all__ = ["MATCH_SECONDS_PER_CALL", "MINUTES_PER_PAGE", "START_MINUTES", "usual_minutes"]

#: A roster full check's minutes per content page compared (low, high): Set 3 measured about 0.4
#: (60 pages, 25 minutes), the 99-page set 0.25 once its pages were matched.
MINUTES_PER_PAGE: Final = (0.2, 0.32)
#: Minutes any full check takes besides its pages (low, high): getting ready, the proving steps,
#: the files (Set 1's 5 pages took about 5 minutes).
START_MINUTES: Final = (3.0, 6.0)
#: One AI page matcher call, seconds (96 drawings in 17 minutes, 4 at a time, 2026-10-22).
MATCH_SECONDS_PER_CALL: Final = 42.0
#: A range whose high end passes this many minutes is said in steps of 5 minutes.
_FIVES_ABOVE: Final = 15


def usual_minutes(pages: int, matched: int = 0, parallel: int = 8) -> tuple[int, int] | None:
    """The usual time of a full check comparing `pages` content pages, `matched` of whose vendor
    drawings the AI page matcher must first ask about (`parallel` calls at a time), as whole
    minutes (low, high); `None` when there is no page to count."""
    if pages <= 0:
        return None
    low = START_MINUTES[0] + pages * MINUTES_PER_PAGE[0]
    high = START_MINUTES[1] + pages * MINUTES_PER_PAGE[1]
    if matched > 0:
        calls = math.ceil(matched / max(1, parallel))
        low += MATCH_SECONDS_PER_CALL / 60
        high += calls * MATCH_SECONDS_PER_CALL / 60
    if high > _FIVES_ABOVE:
        return max(5, 5 * math.floor(low / 5)), 5 * math.ceil(high / 5)
    return max(1, math.floor(low)), max(math.floor(low) + 1, math.ceil(high))
