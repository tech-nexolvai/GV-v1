"""Colours that carry text must clear WCAG AA, checked against the stylesheet as committed.

**Why a test rather than a note in the design plan.** `design-audit-fix-plan-v3.md` already asked for
this — *"#0f7dc1 with white text must meet 4.5:1 — run through a contrast checker"* — and the token
shipped at **4.45:1** with a comment beside it claiming 4.7:1. A checked instruction and an unchecked
one look identical afterwards, and the comment made the failure invisible: anyone verifying would read
the note and move on.

Values are parsed out of `tokens.css` rather than repeated here. A copy would be right on the day it
was written and would then agree with itself for ever while the stylesheet drifted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TOKENS = Path("frontend/main/src/design/tokens.css")

#: WCAG 2.1 AA for normal text. Every colour checked here is used at 11-12px, so the 3:1 allowance
#: for large text does not apply to any of them.
AA_NORMAL = 4.5


def _channel(value: int) -> float:
    channel = value / 255
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _over(colour: str, alpha: float, background: str) -> str:
    """Composite a translucent colour over an opaque one.

    A chip's text sits on the tint, not on the page, so the tint is what it must contrast against.
    Measuring against the page background would have flattered every one of these.
    """
    top, bottom = colour.lstrip("#"), background.lstrip("#")
    blended = (
        round(int(top[i : i + 2], 16) * alpha + int(bottom[i : i + 2], 16) * (1 - alpha))
        for i in (0, 2, 4)
    )
    return "#" + "".join(f"{channel:02x}" for channel in blended)


def _tokens() -> str:
    return TOKENS.read_text(encoding="utf-8")


def _token(name: str, occurrence: int = 0) -> str:
    """One token's value. The second occurrence is the `[data-theme="dark"]` override."""
    found = re.findall(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", _tokens())
    assert len(found) > occurrence, f"--{name} has no occurrence {occurrence} in tokens.css"
    return found[occurrence]


def test_the_checker_agrees_with_wcags_own_published_pairs() -> None:
    """**Calibration, and it is not ceremony.**

    This test contradicts a comment that shipped in the stylesheet. A checker that were wrong would
    replace a wrong claim with a differently wrong one and look authoritative doing it, so it is
    measured against pairs WCAG publishes before it is trusted about anything here.
    """
    assert round(_contrast("#767676", "#ffffff"), 2) == 4.54
    assert round(_contrast("#595959", "#ffffff"), 2) == 7.00
    assert round(_contrast("#000000", "#ffffff"), 2) == 21.00
    assert round(_contrast("#ffffff", "#ffffff"), 2) == 1.00


def test_the_primary_button_carries_white_text_at_aa() -> None:
    """Every blue call to action — Save, Run checks, Sign Off, Submit — is white on this colour.

    It shipped at 4.45:1 while a comment claimed 4.7:1 and the design plan asked for it to be
    verified. Both were true at once, which is how it survived.
    """
    primary = _token("color-primary")

    assert (
        _contrast("#ffffff", primary) >= AA_NORMAL
    ), f"white text on {primary} is {_contrast('#ffffff', primary):.2f}:1, under AA's {AA_NORMAL}"


def test_the_primary_hover_state_also_passes() -> None:
    """A hover that dropped below AA would fail only while the pointer is on it — the moment somebody
    is most likely to be reading the label."""
    hover = _token("color-primary-hover")

    assert _contrast("#ffffff", hover) >= AA_NORMAL


@pytest.mark.parametrize(
    ("role", "tint"),
    [("arch", "#2563eb"), ("shop", "#9333ea")],
)
def test_the_drawing_role_chips_pass_in_both_themes(role: str, tint: str) -> None:
    """**ARCH and SHOP, in light theme and dark.**

    They were hard-coded light-on-dark values: 2.28:1 and 2.29:1 in light theme, roughly half what
    is required. They read correctly in dark theme and nowhere else, which is exactly the failure a
    single-theme eye check misses.

    The distinction is not decorative. `CAB-ARCH-VS-SHOP-001` exists to compare the two drawings, so
    a reviewer misreading which sheet a page came from is reading the wrong half of a check.
    """
    for occurrence, surface in ((0, _token("bg-surface", 0)), (1, _token("bg-surface", 1))):
        colour = _token(f"role-{role}", occurrence)
        background = _over(tint, 0.12, surface)
        measured = _contrast(colour, background)
        theme = "light" if occurrence == 0 else "dark"
        assert (
            measured >= AA_NORMAL
        ), f"{role} chip in {theme} theme: {colour} on {background} is {measured:.2f}:1"


def test_no_stylesheet_hard_codes_a_role_chip_colour_again() -> None:
    """The chips are tokens now, and the point of that is the dark theme following automatically.

    A hex here would look right in whichever theme its author had open, which is how the previous
    values survived.
    """
    panel = Path("frontend/main/src/components/chat/EvidencePanel.css").read_text(encoding="utf-8")
    role_rules = re.findall(r"\.pdf-pane__role-tag--\w+\s*\{[^}]*\}", panel)

    assert role_rules, "the role-tag rules have moved; this guard is now checking nothing"
    for rule in role_rules:
        assert "#" not in rule, f"a role chip hard-codes a colour again:\n{rule}"
