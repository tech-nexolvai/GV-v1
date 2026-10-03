"""What a cabinet code looks like, and what a finish code looks like (#868).

A vendor prints a code on the cabinets in an elevation. The part suggester
(`extraction/model/part_proposals.py`) attaches one to the part it sits over, for a person to confirm
or correct. This is the closed list of shapes it accepts as a cabinet code.

**A shape, never a decoding.** The digits in a cabinet code often say how wide the cabinet is, and
nothing here reads them. A part's width is a reading linked to the part; a width taken out of a code
would be a second, unchecked source for the very number a check compares (#748 plan).

**Closed, and approved by the admin.** The three cabinet shapes below were approved as proposed on
2026-10-03 (#868), with three-letter prefixes kept out. A shape is matched against the whole text of
one reading, and text that no shape matches is not a code, however code-like it looks. A new
vendor's naming is added here on purpose, with the admin's approval, not caught by a looser pattern.

**Finish codes are refused by shape, and that is enforced rather than assumed.** A vendor's drawing
prints finish and material codes on the same faces — letters, a dash, a number — and one taken for a
cabinet code would name a finish as a cabinet model. `is_cabinet_code` refuses any text a finish
shape matches, even when a cabinet shape matches it too, so a cabinet shape widened later cannot let
a finish code through.

**Capital letters only, and no spaces.** The text is matched exactly as it was read. A reader that
lowercased or split a code produced something the vendor did not print, and a person sees the miss
rather than a guess at what was meant.

`CABINET_CODES_VERSION` changes whenever either list does, so suggestions made under a different list
are told apart from these (`PartProposal.source_version`).

Source: issue #868; #748 plan, step 3. Verification: tests/extraction/model/test_part_proposals.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "CABINET_CODES_VERSION",
    "CABINET_CODE_SHAPES",
    "FINISH_CODE_SHAPES",
    "CodeShape",
    "is_cabinet_code",
    "is_finish_code",
]

#: Changes whenever a shape is added, removed or altered in either list below.
CABINET_CODES_VERSION: Final = "1"

#: A dash as the drawings print one: the ASCII hyphen, or the minus sign some text runs carry.
_DASH: Final = "[-−]"


@dataclass(frozen=True, slots=True)
class CodeShape:
    """One shape of code, in words a reviewer can check and as the pattern that matches it."""

    name: str
    description: str
    pattern: str
    """Matched against the whole text, never a part of it."""

    compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "compiled", re.compile(self.pattern))

    def matches(self, text: str) -> bool:
        """Whether `text`, all of it, has this shape."""
        return self.compiled.fullmatch(text) is not None


#: The cabinet-code shapes, approved by the admin as proposed on 2026-10-03 (#868).
#:
#: Each starts with one or two capital letters and then a digit other than zero. A letter and a
#: single digit with nothing after them is left out, because view and detail marks have that shape;
#: so are three letters, because an appliance model number printed beside a cabinet has them.
CABINET_CODE_SHAPES: Final[tuple[CodeShape, ...]] = (
    CodeShape(
        name="type-size",
        description=(
            "one or two capital letters, then two to four digits not starting with zero, then "
            "optionally L or R"
        ),
        pattern=r"[A-Z]{1,2}[1-9][0-9]{1,3}[LR]?",
    ),
    CodeShape(
        name="type-size-p",
        description=(
            "one or two capital letters, one to three digits not starting with zero, the letter P, "
            "one to four more digits, then optionally L or R"
        ),
        pattern=r"[A-Z]{1,2}[1-9][0-9]{0,2}P[0-9]{1,4}[LR]?",
    ),
    CodeShape(
        name="type-size-dash-digit",
        description=(
            "one or two capital letters, two or three digits not starting with zero, a dash, then "
            "one digit other than zero"
        ),
        pattern=rf"[A-Z]{{1,2}}[1-9][0-9]{{1,2}}{_DASH}[1-9]",
    ),
)

#: The finish- and material-code shapes, which are never cabinet codes.
FINISH_CODE_SHAPES: Final[tuple[CodeShape, ...]] = (
    CodeShape(
        name="finish",
        description=(
            "one to three capital letters, a dash, two or three digits, optionally a capital "
            "letter, then any number of further dash-separated groups of capitals and digits"
        ),
        pattern=rf"[A-Z]{{1,3}}{_DASH}[0-9]{{2,3}}[A-Z]?(?:{_DASH}[A-Z0-9]+)*",
    ),
)


def is_finish_code(text: str) -> bool:
    """Whether `text`, all of it, has a finish-code shape."""
    return any(shape.matches(text) for shape in FINISH_CODE_SHAPES)


def is_cabinet_code(text: str) -> bool:
    """Whether `text`, all of it, has a cabinet-code shape and no finish-code shape."""
    if is_finish_code(text):
        return False
    return any(shape.matches(text) for shape in CABINET_CODE_SHAPES)
