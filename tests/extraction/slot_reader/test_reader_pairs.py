"""The AI reader pair is a setting with a closed list of pairs (#1211).

Verification for `extraction/slot_reader/reader_pairs.py` and the two-judgment rules that use it
(`seal.py`, `walls.py`): two *different* models from one maker may seal together only as one of
the named pairs, and every other rule (code's checks, the walls rule, the stacked-fraction rule)
applies to the Gemini pair exactly as to the Claude pair. Invented values only.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.geometry.rows import Box
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.reader_pairs import (
    CLAUDE_PAIR,
    DEFAULT_READER_PAIR,
    GEMINI_FLASH,
    GEMINI_PAIR,
    GEMINI_PRO,
    OPUS,
    READER_PAIRS,
    SONNET,
    is_named_pair,
    reader_pair,
)
from extraction.slot_reader.runs import Lane, PlannedLabel
from extraction.slot_reader.seal import LabelState, ReaderAnswer, seal_label
from extraction.slot_reader.walls import (
    BACK_LEFT_RIGHT,
    Side,
    WallAnswer,
    WallInkSeen,
    seal_walls,
)
from vocabulary.wall_layouts import AUTOMATIC_WALL_SOURCE, READERS_WALL_SOURCE

VENDOR = InkAt(InkClass.VENDOR, "")
_BOX = Box(Decimal(10), Decimal(10), Decimal(20), Decimal(15))
GLYPH = PlannedLabel(
    box=_BOX,
    crop=_BOX,
    lane=Lane.GLYPHS,
    text=None,
    text_stacked=False,
    has_digit=True,
    touches_edge=False,
    ambiguous_slot=False,
    crowded=False,
    ticks_in_crop=True,
    path_boxes=(),
)
YES = Side.YES


def answer(model: str, text: str, **flags: bool) -> ReaderAnswer:
    values = {"readable": True, "no_dimension": False, "stacked": False, "combined": False}
    values.update(flags)
    return ReaderAnswer(model_id=model, text=text, **values)


def seal(*answers: ReaderAnswer, **options: object) -> object:
    settings: dict[str, object] = {
        "ink": VENDOR,
        "stacked_by_bar": False,
        "allow_stacked": False,
        "row_ambiguity": None,
        "allow_named_pair": True,
    }
    settings.update(options)
    return seal_label(GLYPH, answers=answers, **settings)  # type: ignore[arg-type]


# --- The setting's closed list --------------------------------------------------------------------


def test_the_default_pair_is_still_opus_and_sonnet() -> None:
    assert DEFAULT_READER_PAIR == "claude-opus-sonnet"
    assert reader_pair(DEFAULT_READER_PAIR) is CLAUDE_PAIR
    assert CLAUDE_PAIR.models == (OPUS, SONNET)
    assert OPUS == "anthropic.claude-opus-5-5" and SONNET == "anthropic.claude-sonnet-5-5"


def test_the_gemini_pair_is_flash_then_pro_at_a_stated_low_effort() -> None:
    assert reader_pair("gemini-flash-pro") is GEMINI_PAIR
    assert GEMINI_PAIR.models == (GEMINI_FLASH, GEMINI_PRO)
    assert GEMINI_FLASH == "google.gemini-3-8-flash"
    assert GEMINI_PRO == "google.gemini-3-1-pro-preview"
    assert GEMINI_PAIR.effort == "low"
    assert CLAUDE_PAIR.effort is None, "the Claude pair keeps GV_CLAUDE_READER_EFFORT"
    assert GEMINI_PAIR.providers == ("openrouter",)
    assert set(CLAUDE_PAIR.providers) == {"openrouter", "anthropic"}


def test_only_the_listed_pairs_exist() -> None:
    assert set(READER_PAIRS) == {"claude-opus-sonnet", "gemini-flash-pro"}
    for name in ("gemini-flash-sonnet", "", "claude-opus-gemini-flash", "Claude-Opus-Sonnet"):
        with pytest.raises(ValueError, match="GV_READER_PAIR"):
            reader_pair(name)


def test_the_settings_list_is_the_pair_list() -> None:
    from typing import get_args

    from app.config import Settings

    allowed = get_args(Settings.model_fields["reader_pair"].annotation)
    assert set(allowed) == set(READER_PAIRS)
    assert Settings.model_fields["reader_pair"].default == DEFAULT_READER_PAIR


@pytest.mark.parametrize(
    ("models", "named"),
    [
        ((OPUS, SONNET), True),
        ((SONNET, OPUS), True),
        ((GEMINI_FLASH, GEMINI_PRO), True),
        ((GEMINI_PRO, GEMINI_FLASH), True),
        ((GEMINI_FLASH, GEMINI_FLASH), False),
        ((OPUS, GEMINI_FLASH), False),
        ((GEMINI_FLASH, "google.gemini-3-8-flash-lite"), False),
        ((OPUS,), False),
        ((OPUS, SONNET, GEMINI_FLASH), False),
    ],
)
def test_a_named_pair_is_exactly_one_listed_pair(models: tuple[str, ...], named: bool) -> None:
    assert is_named_pair(models) is named


# --- "Two different models must agree, plus code checks", for the Gemini pair -------------------


def test_the_gemini_pair_agreeing_is_provisional_until_code_witnesses_it() -> None:
    outcome = seal(answer(GEMINI_FLASH, '2"'), answer(GEMINI_PRO, '2"'))

    assert outcome.state is LabelState.PROVISIONAL  # type: ignore[attr-defined]
    assert outcome.value is None  # type: ignore[attr-defined]
    assert outcome.suggestion.exact == Fraction(2)  # type: ignore[attr-defined]


def test_the_gemini_pair_differing_goes_to_the_person() -> None:
    outcome = seal(answer(GEMINI_FLASH, '2"'), answer(GEMINI_PRO, '3"'))

    assert outcome.state is LabelState.REVIEW  # type: ignore[attr-defined]


def test_one_gemini_model_twice_or_an_unlisted_google_model_never_seals() -> None:
    with pytest.raises(ValueError, match="different makers"):
        seal(answer(GEMINI_FLASH, '2"'), answer("google.gemini-3-8-flash-lite", '2"'))
    with pytest.raises(ValueError, match="different makers"):
        seal(answer(GEMINI_FLASH, '2"'), answer(GEMINI_PRO, '2"'), allow_named_pair=False)


def test_a_stacked_fraction_is_held_for_the_gemini_pair_as_for_any_pair() -> None:
    held = seal(
        answer(GEMINI_FLASH, '14 3/8"', stacked=True),
        answer(GEMINI_PRO, '14 3/8"'),
    )
    assert held.state is LabelState.REVIEW and held.reason_code == "stacked"  # type: ignore[attr-defined]


def test_reviewer_markup_is_never_read_by_the_gemini_pair() -> None:
    outcome = seal(
        answer(GEMINI_FLASH, '2"'),
        answer(GEMINI_PRO, '2"'),
        ink=InkAt(InkClass.GV, '3"'),
    )
    assert outcome.state is LabelState.REVIEW  # type: ignore[attr-defined]
    assert outcome.reason_code == "reviewer-markup"  # type: ignore[attr-defined]


def test_walls_from_the_gemini_pair_are_automatic_only_with_code_ink_and_both_ais() -> None:
    pair = (
        WallAnswer(GEMINI_FLASH, YES, YES, Side.UNSURE, "elevation"),
        WallAnswer(GEMINI_PRO, YES, YES, Side.UNSURE, "elevation"),
    )

    held = seal_walls(pair, ink=None, row_ambiguity=None)
    assert held.code == "reader-independence"

    proposed = seal_walls(pair, ink=None, row_ambiguity=None, allow_named_pair=True)
    assert (proposed.config, proposed.source) == (BACK_LEFT_RIGHT, READERS_WALL_SOURCE)

    automatic = seal_walls(
        pair,
        ink=WallInkSeen(True, True),
        row_ambiguity=None,
        stone_meets_walls=True,
        allow_named_pair=True,
    )
    assert (automatic.config, automatic.source) == (BACK_LEFT_RIGHT, AUTOMATIC_WALL_SOURCE)

    one_says_no = (pair[0], replace(pair[1], right=Side.NO))
    contradicted = seal_walls(
        one_says_no, ink=WallInkSeen(True, True), row_ambiguity=None, allow_named_pair=True
    )
    assert contradicted.code == "hatch-contradiction"
