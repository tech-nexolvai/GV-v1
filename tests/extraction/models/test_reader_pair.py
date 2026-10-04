"""The new reader pair, Qwen3-VL + Nova 2 Lite as the trial measured it, and its teaching prompt (#907).

Verification for: `extraction/models/nova.py` (the `qwen3-vl-235b` and `nova-2-lite-taught` reader
definitions) and `extraction/models/sanitisation.py` (`ReadingPrompt`, the teaching prompt v2).

**The rules held here.** Agreement must be cross-vendor (#775): Qwen and Amazon are two vendors, and
the two Nova 2 Lite readers one. Each reader's setup is a measurement it cites, like a coordinate
space. The teaching prompt is versioned — its id changes when a word does — identical for every crop,
given only to the readers it was measured to help, and its examples are values on no answer key.
"""

from __future__ import annotations

import csv
import re
from fractions import Fraction
from pathlib import Path

import pytest

from evidence.corroborate import independence_key
from extraction.models.nova import (
    NOVA_2_LITE_EXTRACTOR,
    NOVA_2_LITE_MODEL_ID,
    NOVA_2_LITE_TAUGHT_EXTRACTOR,
    QWEN3_VL_235B_EXTRACTOR,
    QWEN3_VL_235B_MODEL_ID,
    UPRIGHT_SHARPER_TEMPLATE_ID,
    VISION_READERS,
    AnswerFormat,
    ReaderPicture,
    _ReaderDefinition,
    vision_config_for_extractor,
    vision_configs_from_environment,
)
from extraction.models.sanitisation import (
    JSON_READING_TASK,
    TEACHING_CONVENTIONS,
    TEACHING_EXAMPLES,
    TEACHING_READING_PROMPT,
    ReadingPrompt,
)
from extraction.models.validation import CoordinateMode
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound

REPO = Path(__file__).resolve().parents[3]


def _reader(key: str) -> _ReaderDefinition:
    return next(reader for reader in VISION_READERS if reader.key == key)


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def test_qwen_is_defined_as_a_json_reader_taught_on_the_picture_as_cut() -> None:
    """**Qwen refuses forced tool use with an image** (#668), so it answers on the plain-JSON path,
    held to a schema; taught, because the prompt helped it most; on the crop as cut, because turning
    and enlarging slightly hurt it (Reading upgrade v2, §3b)."""
    qwen = _reader("qwen3-vl-235b")

    assert (qwen.model_id, qwen.extractor) == (QWEN3_VL_235B_MODEL_ID, QWEN3_VL_235B_EXTRACTOR)
    assert QWEN3_VL_235B_MODEL_ID == "qwen.qwen3-vl-235b-a22b"
    assert qwen.answer is AnswerFormat.JSON_SCHEMA
    assert qwen.prompt is TEACHING_READING_PROMPT
    assert qwen.picture is ReaderPicture.AS_CUT
    assert qwen.coordinate_mode is CoordinateMode.CROP
    assert qwen.coordinate_measured is False
    assert qwen.answers_readably
    assert qwen.setup_measurement is not None and "#728" in qwen.setup_measurement
    assert "#664" in qwen.coordinate_measurement


def test_nova_2_lite_as_the_trial_measured_it_is_its_own_reader() -> None:
    """**The same model, read the way the trial measured it**: taught, in plain JSON — it refused
    `outputConfig` — shown the label upright and sharper. Its own key and extractor, so the tool
    reader keeps the behaviour #641 and the reading agent (#757) were measured with."""
    taught = _reader("nova-2-lite-taught")
    tool = _reader("nova-2-lite")

    assert taught.model_id == tool.model_id == NOVA_2_LITE_MODEL_ID
    assert (taught.extractor, tool.extractor) == (
        NOVA_2_LITE_TAUGHT_EXTRACTOR,
        NOVA_2_LITE_EXTRACTOR,
    )
    assert taught.answer is AnswerFormat.JSON_TEXT
    assert taught.prompt is TEACHING_READING_PROMPT
    assert taught.picture is ReaderPicture.UPRIGHT_SHARPER
    assert taught.coordinate_mode is CoordinateMode.CROP
    assert (tool.answer, tool.prompt, tool.picture) == (
        AnswerFormat.TOOL,
        None,
        ReaderPicture.AS_CUT,
    )
    assert tool.coordinate_mode is CoordinateMode.PIXELS


def test_the_new_pair_is_built_from_its_definitions_with_the_words_it_is_asked_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A deployment names the pair by key** (`GV_BEDROCK_VISION_READERS`), and each config says
    how its reader answers, in which words, recorded under which id, shown which picture."""
    monkeypatch.setenv("GV_BEDROCK_VISION_READERS", "qwen3-vl-235b,nova-2-lite-taught")

    configs = {config.extractor: config for config in vision_configs_from_environment()}

    assert set(configs) == {QWEN3_VL_235B_EXTRACTOR, NOVA_2_LITE_TAUGHT_EXTRACTOR}
    qwen, nova = configs[QWEN3_VL_235B_EXTRACTOR], configs[NOVA_2_LITE_TAUGHT_EXTRACTOR]
    assert qwen.answer_format is AnswerFormat.JSON_SCHEMA
    assert nova.answer_format is AnswerFormat.JSON_TEXT
    for config in (qwen, nova):
        assert config.reading_prompt is TEACHING_READING_PROMPT
        assert config.prompt_id == TEACHING_READING_PROMPT.prompt_id
        assert config.coordinate_mode is CoordinateMode.CROP
    assert (qwen.picture, qwen.template_id) == (ReaderPicture.AS_CUT, "bounded-crop-v1")
    assert (nova.picture, nova.template_id) == (
        ReaderPicture.UPRIGHT_SHARPER,
        UPRIGHT_SHARPER_TEMPLATE_ID,
    )


def test_each_can_be_named_by_the_agent_or_a_scorecard() -> None:
    """A reader asked for no rectangle has no space to mis-measure (#664), so it can be named."""
    for extractor in (QWEN3_VL_235B_EXTRACTOR, NOVA_2_LITE_TAUGHT_EXTRACTOR):
        config = vision_config_for_extractor(extractor)
        assert config is not None and config.extractor == extractor


def test_the_pair_is_two_vendors_and_the_two_nova_readers_are_one() -> None:
    """**Cross-vendor agreement only (#775).** Qwen and Amazon count as two; Nova 2 Lite asked two
    ways is one vendor, and its two readings can never confirm each other."""
    qwen = independence_key(QWEN3_VL_235B_EXTRACTOR, QWEN3_VL_235B_MODEL_ID)
    taught = independence_key(NOVA_2_LITE_TAUGHT_EXTRACTOR, NOVA_2_LITE_MODEL_ID)
    profile = independence_key(NOVA_2_LITE_TAUGHT_EXTRACTOR, f"us.{NOVA_2_LITE_MODEL_ID}")
    tool = independence_key(NOVA_2_LITE_EXTRACTOR, NOVA_2_LITE_MODEL_ID)

    assert qwen == "vendor:qwen"
    assert taught == profile == tool == "vendor:amazon"


@pytest.mark.parametrize(
    "changes",
    [
        {"answer": AnswerFormat.JSON_SCHEMA, "prompt": None},
        {"answer": AnswerFormat.JSON_SCHEMA, "coordinate_mode": CoordinateMode.PIXELS},
        {"answer": AnswerFormat.JSON_SCHEMA, "coordinate_measured": True},
        {"answer": AnswerFormat.TOOL, "prompt": TEACHING_READING_PROMPT},
        {"answer": AnswerFormat.TOOL, "coordinate_mode": CoordinateMode.CROP},
        {"setup_measurement": None},
        {"setup_measurement": "measured somewhere, once"},
    ],
    ids=[
        "json-without-words",
        "json-with-a-rectangle",
        "json-claiming-a-space",
        "tool-with-json-words",
        "tool-placed-at-the-crop",
        "setup-uncited",
        "setup-cited-to-nothing",
    ],
)
def test_a_reader_definition_that_contradicts_itself_is_refused(changes: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "key": "someone",
        "model_id": "vendor.model",
        "extractor": "bedrock-someone",
        "coordinate_mode": CoordinateMode.CROP,
        "coordinate_measurement": "never asked for a rectangle (#907)",
        "enabled": False,
        "disabled_reason": "a test",
        "coordinate_measured": False,
        "answer": AnswerFormat.JSON_TEXT,
        "prompt": TEACHING_READING_PROMPT,
        "picture": ReaderPicture.UPRIGHT_SHARPER,
        "setup_measurement": "measured (#907)",
    }
    _ReaderDefinition(**fields)  # type: ignore[arg-type]
    fields.update(changes)
    with pytest.raises((TypeError, ValueError)):
        _ReaderDefinition(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The teaching prompt
# ---------------------------------------------------------------------------


def test_the_prompt_id_changes_when_a_word_does() -> None:
    """**Versioned by its words.** The same words give the same id; one word changed gives another,
    even where nobody moved the version — so a record can only name the words that were sent."""
    same = ReadingPrompt(
        name=TEACHING_READING_PROMPT.name,
        version=TEACHING_READING_PROMPT.version,
        system=TEACHING_READING_PROMPT.system,
        task=TEACHING_READING_PROMPT.task,
    )
    edited = ReadingPrompt(
        name=TEACHING_READING_PROMPT.name,
        version=TEACHING_READING_PROMPT.version,
        system=TEACHING_READING_PROMPT.system.replace("Never guess", "Do not guess"),
        task=TEACHING_READING_PROMPT.task,
    )

    assert same.prompt_id == TEACHING_READING_PROMPT.prompt_id
    assert edited.prompt_id != TEACHING_READING_PROMPT.prompt_id
    assert TEACHING_READING_PROMPT.prompt_id.startswith("dimension-reader-teaching-v2+")


def test_the_shipped_words_are_the_ones_measured() -> None:
    """**Pinned.** The id of the words the trial and #907's measurement asked with. A change to them
    fails here, so it is made on purpose and measured again — never slipped in."""
    assert TEACHING_READING_PROMPT.prompt_id == "dimension-reader-teaching-v2+49f87a3cc496"


def test_the_words_hold_nothing_about_any_crop() -> None:
    """**Identical across crops**: fixed strings, the drawing's data channel apart (#256)."""
    assert TEACHING_READING_PROMPT.system.endswith(TEACHING_CONVENTIONS)
    assert TEACHING_READING_PROMPT.task == JSON_READING_TASK
    assert "{" not in TEACHING_READING_PROMPT.system.replace('{"reading": null}', "")


def test_only_the_readers_it_helped_are_taught() -> None:
    """**Per reader, only where measured to help** (§3b): Qwen3-VL and Nova 2 Lite. The small
    Ministral models read worse taught; the others were not measured taught."""
    taught = {reader.key for reader in VISION_READERS if reader.prompt is TEACHING_READING_PROMPT}
    assert taught == {"qwen3-vl-235b", "nova-2-lite-taught"}


def test_every_example_is_listed_and_no_other_number_is_in_the_conventions() -> None:
    """The list `TEACHING_EXAMPLES` is what the key check reads, so it must be all of them: every
    run of digits in the conventions is an example's or one of the six list numbers."""
    for example in TEACHING_EXAMPLES:
        assert example in TEACHING_CONVENTIONS, example
    allowed = {digits for example in TEACHING_EXAMPLES for digits in re.findall(r"\d+", example)}
    allowed |= {str(number) for number in range(1, 7)}
    assert set(re.findall(r"\d+", TEACHING_CONVENTIONS)) <= allowed


def _values(token: str) -> set[tuple[str, object]]:
    """Every value a written token holds: its inches, exactly, and a dual token's millimetres."""
    found: set[tuple[str, object]] = set()
    if not token.strip() or is_compound(token):
        return found
    canonical, millimetres = canonical_notation(token)
    try:
        found.add(("in", normalise_to_inches(canonical).exact))
    except UnitNormalisationError:
        pass
    if millimetres is not None:
        found.add(("mm", Fraction(millimetres)))
    bare = token.strip()
    if bare.isdigit():
        found.add(("mm", Fraction(bare)))
    return found


#: The columns of a key's `crops.csv` that hold a value someone read; the rest are places and ticks.
_NOT_VALUES = {
    "crop_id",
    "image",
    "wide_image",
    "page",
    "left_px",
    "top_px",
    "right_px",
    "bottom_px",
    "width_px",
    "height_px",
    "stratum",
    "sources",
    "near_dimension_lines",
    "path_count",
    "note",
}


def test_no_example_is_a_value_on_any_answer_key() -> None:
    """**The admin's rule (#907): the examples must use no value that occurs on any key.** The first
    run's did, on six crops, and flattered itself (§3b). Every value read on every key —
    `data/goldset/reading-key-*/crops.csv`, the vendor's and GV's alike, inches exactly and a dual
    token's millimetres — is held against every example's. Skipped where the client's data is not
    present, as in CI; run where it is."""
    keys = sorted((REPO / "data" / "goldset").glob("reading-key-*/crops.csv"))
    if not keys:
        pytest.skip("no answer key is present here (data/ is the client's and never committed)")
    on_keys: dict[tuple[str, object], str] = {}
    for path in keys:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                for column, written in row.items():
                    if column in _NOT_VALUES or not written:
                        continue
                    for value in _values(written):
                        on_keys.setdefault(value, f"{path.parent.name}/{row.get('crop_id')}")
    examples = {value: example for example in TEACHING_EXAMPLES for value in _values(example)}

    assert examples, "the examples were read as no value at all, so nothing was checked"
    assert on_keys, "the keys were read as no value at all, so nothing was checked"
    clashes = {examples[value]: on_keys[value] for value in examples if value in on_keys}
    assert clashes == {}


def test_the_key_check_can_see_a_clash() -> None:
    """Not vacuous: a value the check must catch is caught, in both units."""
    assert ("in", Fraction(50)) in _values("4'-2\"")
    assert {("in", Fraction(40)), ("mm", Fraction(1016))} <= _values("1016 [40]")
    assert ("in", Fraction(12)) in _values("300 [12]")
