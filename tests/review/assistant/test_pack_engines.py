"""The engines read the product packs in scope (#1776, GENERALITY §4.3). $0: no model, no PDF.

What is asserted, with the template pack (`vocabulary/products/_template/`, a made-up "widget")
active in a test registry, and with the repository's own registry (kitchen only):

1. **Room words** = the core room words minus the in-scope products' words, plus their
   `room_words_extra`; with only the kitchen in scope, exactly the core list. A mirror is room
   context in a cabinet check and ours in a check of mirrors.
2. **End words** = the core end kinds plus the in-scope packs' end words (strict, centred), compared
   in either order; the kitchen's end kinds are unchanged.
3. **The prompts**: the compare call's system prompt adds the in-scope packs' checklists, what counts
   as ours, the extra end words and the packs' examples, the same bytes every time; the blind look's
   text is empty for the kitchen and names the widget's "ours" otherwise.
4. **The attribute and count checks**: code reads the printed callouts at the answer's boxes; the
   same value is Right, different values are a red candidate for the blind look, anything code
   cannot read keeps today's Not-compared reason; a kitchen check is untouched.
5. **SemanticType** = core codes + the packs' codes: a pack's code resolves and a GV rule naming it
   is accepted; an unknown code is still refused.

Also: shared words file an item by its drawing's product; the reader reads `Ø24"` and `24"x36"`
only when an active pack asks for them; `impact` and the screens' end words read the packs.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.review.assistant import impact, product_packs, size_callouts
from app.review.assistant.product_scope import named_products
from app.review.assistant.simple_check import pack_checks, prompts
from app.review.assistant.simple_check import verify as verify_module
from app.review.assistant.simple_check.blind import BLIND_SYSTEM, question
from app.review.assistant.simple_check.verify import (
    _ROOM,
    _ROOM_WORDS,
    BlindAnswer,
    Item,
    Pack,
    Region,
    end_kind,
    ends_match,
    verify_answer,
    with_blind,
)
from vocabulary.product_registry import (
    DEFAULT_REGISTRY,
    REGISTRY_VARIABLE,
    registry,
    registry_data,
    reset_registry,
)
from vocabulary.semantic_types import SemanticType, UnknownVocabularyError, resolve_semantic

TEMPLATE = DEFAULT_REGISTRY.parent / "products" / "_template"
FILE = "set.pdf"
VENDOR_PAGE, ARCH_PAGE = 2, 1


@pytest.fixture
def widget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The repository's registry plus the template pack, active, as `widget`; mirrors active too
    (a starter product whose words are core room words)."""
    data = registry_data(DEFAULT_REGISTRY)
    packs = tmp_path / "packs"
    shutil.copytree(TEMPLATE, packs / "widget")
    pack = yaml.safe_load((packs / "widget" / "pack.yaml").read_text(encoding="utf-8"))
    pack.update(
        active=True,
        room_words_extra=["switch plate"],
        shared_words=["drawer"],
        impact_words=["knob finish"],
    )
    (packs / "widget" / "pack.yaml").write_text(yaml.safe_dump(pack), encoding="utf-8")
    for entry in data["products"]:
        if entry["id"] == "mirrors":
            entry["active"] = True
    data["packs_folder"] = "packs"
    data["products"].append("widget")
    path = tmp_path / "products.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv(REGISTRY_VARIABLE, str(path))
    reset_registry()
    try:
        yield path
    finally:
        monkeypatch.delenv(REGISTRY_VARIABLE, raising=False)
        reset_registry()


# ---- 1. room words --------------------------------------------------------------------------------


def test_the_kitchen_room_words_are_the_core_list() -> None:
    assert product_packs.room_pattern(_ROOM) == _ROOM
    with product_packs.scoped(["cabinet"]):
        assert product_packs.room_pattern(_ROOM) == _ROOM
        assert _ROOM_WORDS.search("bottom of mirror") is not None


def test_a_product_in_scope_is_not_room_context(widget: Path) -> None:
    with product_packs.scoped(["mirrors"]):
        assert _ROOM_WORDS.search("bottom of mirror") is None
        assert _ROOM_WORDS.search("light switch") is not None
    with product_packs.scoped(["cabinet"]):
        assert _ROOM_WORDS.search("bottom of mirror") is not None  # not ours in a cabinet check
    with product_packs.scoped(["widget"]):
        assert _ROOM_WORDS.search("the switch plate beside it") is not None  # room_words_extra
    with product_packs.scoped(["cabinet"]):
        assert _ROOM_WORDS.search("the switch plate beside it") is not None  # \bswitch (core)
        assert _ROOM_WORDS.search("widget plate") is None


# ---- 2. end words ---------------------------------------------------------------------------------


def test_the_kitchen_end_kinds_are_unchanged() -> None:
    from app.review.assistant.simple_check import verify

    ends = verify._ends()
    assert [k for k, _p in ends.kinds] == [k for k, _p in verify._END_KINDS]
    assert ends.strict == verify._STRICT and ends.centred == verify._CENTRED and not ends.names
    assert end_kind("top_of_counter (stone)")[0] == "counter_top"
    assert end_kind("widget_centre (knob)")[0] == "centre"  # "centre" is the core's word


def test_a_pack_adds_its_end_words_compared_either_way(widget: Path) -> None:
    with product_packs.scoped(["widget"]):
        assert end_kind("widget_centre (left knob)") == ("widget_centre", "left")
        assert end_kind("Top of widget") == ("top_of_widget", None)
        same = ends_match("floor", "widget_centre", "widget_centre", "floor")
        assert same[0] == "same"
        # Two strict ends that differ are different points; a centre line named alone may be the
        # widget's centre (centred).
        assert ends_match("floor", "top_of_widget", "floor", "top_of_counter")[0] == "different"
        assert ends_match("floor", "centre_line", "floor", "widget_centre")[0] == "same"
    with product_packs.scoped(["cabinet"]):
        assert end_kind("top_of_widget")[0] == "other"  # not in scope: not its words


# ---- 3. prompts ---------------------------------------------------------------------------------


def test_the_compare_prompt_adds_the_packs_in_scope_with_the_same_bytes(widget: Path) -> None:
    kitchen = product_packs.pack_prompt_parts(["countertop", "cabinet"])
    text = kitchen.compare_text()
    assert "What to compare for each product in scope:" in text
    assert "Cabinets:\n- widths: Each cabinet and filler width" in text
    assert "Ours (millwork: true)" not in text and "Example for" not in text
    assert kitchen.blind_text() == ""

    parts = product_packs.pack_prompt_parts(["widget"])
    text = parts.compare_text()
    assert "Widgets:\n- dimensions:" in text
    assert "- Widgets: the widget itself, its knob and its mounting plate." in text
    assert "widget_centre (centre of widget, widget centre line); top_of_widget" in text
    assert "Example for widgets (made-up values):" in text and '"(3) KNOBS"' in text
    assert "the widget itself" in parts.blind_text() and "top_of_widget" in parts.blind_text()

    one = prompts.system_prompt("- rule", "Widgets", packs=text)
    assert one == prompts.system_prompt("- rule", "Widgets", packs=text)
    assert one.startswith(prompts.SYSTEM_PROMPT)
    assert one.index("Products in scope") < one.index("Example for widgets") < one.index("- rule")
    assert prompts.system_prompt("- rule", "Cabinets") == prompts.system_prompt(
        "- rule", "Cabinets", packs=""
    )


def test_the_blind_look_asks_plainly_about_a_spec_or_a_count() -> None:
    for kind in ("attribute", "count"):
        asked = question(kind, None, None)
        assert "pick: none" in asked and "architect_to: other" in asked
    assert "property (a specification value)" in question("attribute", None, None)
    assert BLIND_SYSTEM.endswith("Answer in the form.")


# ---- 4. the attribute and count checks ----------------------------------------------------------


@dataclass
class _Set:
    vendor_text: str
    architect_text: str

    def pack(self) -> Pack:
        whole = [0.0, 0.0, 600.0, 800.0]

        def half(role: str) -> dict[str, Any]:
            return {"box": whole, "role": role, "round": None}

        page_map = {
            "files": [FILE],
            "pages": [
                {"file": FILE, "page": ARCH_PAGE, "halves": [half("architect")], "area": "A1"},
                {"file": FILE, "page": VENDOR_PAGE, "halves": [half("vendor")], "area": "A1"},
            ],
        }
        dims = {
            "pages": [
                {
                    "file": FILE,
                    "page": VENDOR_PAGE,
                    "labels": [],
                    "phrases": [{"text": self.vendor_text, "box": [100, 100, 220, 112]}],
                },
                {
                    "file": FILE,
                    "page": ARCH_PAGE,
                    "labels": [],
                    "phrases": [{"text": self.architect_text, "box": [300, 200, 420, 212]}],
                },
            ]
        }
        return Pack.from_documents(dims=dims, page_map=page_map, main_file=FILE)


ITEM = Item(
    "p2.v1",
    Region(FILE, VENDOR_PAGE, (0.0, 0.0, 600.0, 800.0)),
    (Region(FILE, ARCH_PAGE, (0.0, 0.0, 600.0, 800.0)),),
    products=("widget",),
)


def _answer(kind: str, verdict: str = "different", what: str = "knob finish") -> dict[str, Any]:
    return {
        "work_item": "p2.v1",
        "pairs": [],
        "other_items": [
            {
                "kind": kind,
                "what": what,
                "vendor": "as the model read it",
                "architect": "as the model read it",
                "vendor_box": [95, 95, 225, 117],
                "architect_box": [295, 195, 425, 217],
                "architect_page": ARCH_PAGE,
                "verdict": verdict,
                "millwork": True,
            }
        ],
        "unaccounted_labels": [],
    }


def _one(drawings: _Set, answer: dict[str, Any], products: tuple[str, ...]) -> Any:
    found = verify_answer(answer, ITEM, drawings.pack(), products=products)
    assert len(found.items) == 1
    return found.items[0]


def test_attribute_values_read_the_packs_words(widget: Path) -> None:
    finish = registry().get("widget").attributes[0]  # type: ignore[union-attr]
    assert pack_checks.attribute_values(finish, "KNOB: POLISHED CHROME") == {"CHROME"}
    assert pack_checks.attribute_values(finish, "knob finish brass") == {"BRASS"}
    assert pack_checks.attribute_values(finish, "CHROME TRIM") == set()  # no label word
    assert pack_checks.counts(r"\bKNOBS?\b", "PROVIDE (3) KNOBS") == {3}
    assert pack_checks.counts(r"\bKNOBS?\b", "KNOBS: 2") == {2}
    assert pack_checks.counts(r"\bKNOBS?\b", 'KNOBS 2" DIA') == set()  # a size, not a count


def test_an_attribute_code_reads_the_same_is_right(widget: Path) -> None:
    drawings = _Set("KNOB FINISH: BRASS", "KNOB: BRASS")
    found = _one(drawings, _answer("tag", "same"), ("widget",))
    assert found.outcome == "right"
    assert found.finding["status"] == "same" and found.finding["vendor"] == "BRASS"


def test_an_item_the_ai_called_different_is_never_made_right_by_one_equal_value(
    widget: Path,
) -> None:
    """The AI said the callouts differ; code reading one attribute equal does not prove the rest
    of the callout is (#1776 review: "BRASS, (2) KNOBS" against "BRASS, (3) KNOBS"): the item keeps
    its usual reason, never Right."""
    drawings = _Set("KNOB FINISH: BRASS", "KNOB: BRASS")
    found = _one(drawings, _answer("tag", "different"), ("widget",))
    assert found.outcome != "right"


def test_verify_answer_keeps_the_checks_scope_when_given_none(
    widget: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolve loop calls `verify_answer` without products (#1776 review): inside a cabinet
    check its scope stays the cabinet's, so a mirror stays room context."""
    seen: list[tuple[str, ...]] = []

    def capture(*args: Any) -> Any:
        seen.append(product_packs.in_scope())
        raise RuntimeError("enough")

    monkeypatch.setattr(verify_module, "_verify_answer", capture)
    with product_packs.scoped(["cabinet"]), pytest.raises(RuntimeError):
        verify_module.verify_answer({}, None, None)  # type: ignore[arg-type]
    assert seen == [("cabinet",)]


def test_an_attribute_that_differs_waits_for_the_blind_look(widget: Path) -> None:
    drawings = _Set("KNOB FINISH: BRASS", "KNOB: POLISHED CHROME")
    found = _one(drawings, _answer("tag"), ("widget",))
    assert found.outcome == "candidate" and found.blind == "attribute"
    assert (found.finding["vendor"], found.finding["architect"]) == ("BRASS", "CHROME")
    agree = BlindAnswer(None, "same_thing", "other", "other", "other", "other", "yes", None)
    assert with_blind(found, agree).outcome == "red"
    apart = BlindAnswer(None, "different_points", "other", "other", "other", "other", "yes", None)
    assert with_blind(found, apart).outcome == "not_compared"
    assert with_blind(found, None, "the check was stopped").outcome == "not_compared"


def test_a_count_that_differs_waits_for_the_blind_look(widget: Path) -> None:
    drawings = _Set("(3) KNOBS", "PROVIDE (2) KNOBS")
    found = _one(drawings, _answer("layout_count", what="knobs"), ("widget",))
    assert found.outcome == "candidate" and found.blind == "count"
    assert (found.finding["vendor"], found.finding["architect"]) == ("3", "2")


@pytest.mark.parametrize(
    ("vendor", "architect", "kind", "products"),
    [
        ("KNOB FINISH: BRASS", "KNOB: CHROME", "tag", ("countertop", "cabinet")),  # kitchen
        ("KNOB FINISH: BRASS", "SEE SPEC", "tag", ("widget",)),  # nothing printed to read
        ("KNOB: BRASS OR CHROME", "KNOB: CHROME", "tag", ("widget",)),  # two values
        ("(3) KNOBS", "PROVIDE (2) KNOBS", "tag", ("widget",)),  # a count is not a tag
        ("KNOB FINISH: BRASS", "KNOB: CHROME", "added_on_vendor", ("widget",)),
    ],
    ids=["kitchen", "unread", "two-values", "wrong-kind", "other-kind"],
)
def test_what_code_cannot_read_keeps_its_reason(
    widget: Path, vendor: str, architect: str, kind: str, products: tuple[str, ...]
) -> None:
    found = _one(_Set(vendor, architect), _answer(kind), products)
    assert found.outcome == "not_compared" and not found.blind


def test_a_kitchen_check_never_reaches_the_pack_checks() -> None:
    found = _one(_Set("(3) SHELVES", "(2) SHELVES"), _answer("layout_count"), ("cabinet",))
    assert found.outcome == "not_compared" and found.reason_code == "not_proved"


# ---- 5. semantic types --------------------------------------------------------------------------


def test_a_packs_semantic_type_resolves_and_a_rule_may_name_it(widget: Path) -> None:
    from rules.schema import InputSelector

    code = SemanticType("WG_MOUNT_HT")
    assert code is SemanticType("WG_MOUNT_HT") and code.value == "WG_MOUNT_HT"
    assert code.label() == "Widget mounting height above the floor"
    assert resolve_semantic("WG_MOUNT_HT") is code
    given = {"source": "ARCH", "semantic_type": "WG_MOUNT_HT", "scope": "same_assembly"}
    assert InputSelector.model_validate({**given, "cardinality": "one"}).semantic_type is code
    with pytest.raises(UnknownVocabularyError):
        resolve_semantic("WG_LAMP_COUNT")


def test_without_the_pack_the_code_is_unknown() -> None:
    with pytest.raises(ValueError):
        SemanticType("WG_MOUNT_HT")


# ---- also -----------------------------------------------------------------------------------------


def test_a_shared_word_files_by_the_drawings_product(widget: Path) -> None:
    assert named_products("drawer front height", hints=True, every=True) == set()
    assert named_products("cabinet drawer", hints=True, every=True) == {"cabinet"}


def test_without_the_pack_a_drawer_is_a_cabinets() -> None:
    assert named_products("drawer front height", hints=True, every=True) == {"cabinet"}


@dataclass(frozen=True)
class _Char:
    text: str


def test_the_reader_splits_size_callouts() -> None:
    assert size_callouts.diameter('Ø24"') == 24
    assert size_callouts.diameter('24 1/2"Ø') == pytest.approx(24.5)
    assert size_callouts.diameter('24"') is None
    parts = size_callouts.split_w_x_h('24"Wx36"H', [_Char(c) for c in '24"Wx36"H'])
    assert [(t, v, "".join(c.text for c in g)) for t, v, g in parts] == [
        ('24"', 24, '24"'),
        ('36"', 36, '36"'),
    ]
    assert size_callouts.split_w_x_h("2x4", [_Char(c) for c in "2x4"]) == []


def test_the_reader_learns_formats_only_from_active_packs(widget: Path) -> None:
    from app.review.assistant import dim_labels

    assert size_callouts.formats() == frozenset({"diameter"})  # the widget's
    word = ('Ø24"', tuple(_Box(c) for c in 'Ø24"'))
    assert [r.inches for r in dim_labels._callout(word, size_callouts.formats())] == [24]
    assert dim_labels._callout(word, frozenset()) == []


def test_the_kitchen_registry_teaches_the_reader_nothing() -> None:
    assert size_callouts.formats() == frozenset()


@dataclass(frozen=True)
class _Box:
    text: str
    size: float = 8.0
    along: float = 0.0
    across: float = 0.0
    length: float = 4.0
    direction: tuple[float, float] = (1.0, 0.0)

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (0.0, 0.0, 4.0, 8.0)


def test_impact_reads_the_packs_impact_words(widget: Path) -> None:
    finding = {"status": "different", "item": "Knob finish"}
    with product_packs.scoped(["widget"]):
        assert impact.touches(finding)[0] == "changes fabrication (widgets: a key specification)"
        # The core's own words still come first.
        assert impact.touches({"item": "Widget height"})[0] == "changes fabrication (a height)"
    with product_packs.scoped(["cabinet"]):
        assert impact.touches(finding)[0] == "cosmetic (a tag, label or note)"


def test_the_kitchen_impact_is_unchanged() -> None:
    assert impact.touches({"item": "Knob finish"})[0] == "cosmetic (a tag, label or note)"
    assert product_packs.impact_touches() == ()


def test_the_screens_say_a_packs_end_words(widget: Path) -> None:
    words = product_packs.screen_words({"floor": "the floor"})
    assert words["widget_centre"] == "the centre of the widget" and words["floor"] == "the floor"
