"""A code printed with a look-alike dash is read as the code printed with the hyphen.

A drawing's text layer may carry a minus sign (U+2212), an en dash, a non-breaking hyphen or another
look-alike where the drafter typed a hyphen. An architect's file printed its item tags `XX−213` (the
minus sign) while the vendor printed `XX-213`, and code read no tag on the architect's pages: none
of that set's vanity pages paired. `vocabulary.dashes` is the one definition of the look-alikes, and
every reader of an item tag, a finish tag, a sheet number, a view reference or a cabinet code reads
through it. Here:

- the helper maps each look-alike to the hyphen, one character for one, and nothing else;
- every reader gives the same answer for `XX-213` and for each look-alike spelling of it;
- a vendor drawing whose tag the architect prints with the minus sign pairs with the architect's
  drawing of it, as it does when both print the hyphen;
- a guard: a new tag-shaped pattern spelling the hyphen fails here until it reads through the helper
  (or matches `DASH`), or is listed as reading no drawing's code.

Made-up tags, sheets and sets only; no network, no paid call.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import (
    architect_items,
    claim_rules,
    decision_resolver,
    dim_ends,
    drawing_notes,
    filler_range,
    look_rules,
    one_card,
    product_scope,
    project_schedule,
    reviewer_context,
    schedule_index,
    set_context,
    set_pack,
    tag_reader,
    vendor_consistency,
)
from app.review.assistant.simple_check import pairing, views
from extraction.architect import sheet_index, view_matching
from extraction.architect.text import Orientation, PrintedPhrase
from extraction.geometry.rows import Box
from extraction.slot_reader import kinds
from tests.review.assistant.test_cross_file_projects import (
    ARCH,
    SHOP,
    _across,
    _index,
    _phrase,
    _with_pages,
    _work_with,
)
from vocabulary.cabinet_codes import is_cabinet_code, is_finish_code
from vocabulary.dashes import DASH, LOOKALIKE_DASHES, dashes_to_hyphen

ROOT = Path(__file__).resolve().parents[3]

#: Each look-alike, by its code point (the list the helper must hold, no more and no less).
LOOKALIKES = {
    "‐": "hyphen",
    "‑": "non-breaking hyphen",
    "‒": "figure dash",
    "–": "en dash",
    "—": "em dash",
    "―": "horizontal bar",
    "−": "minus sign",
    "﹘": "small em dash",
    "﹣": "small hyphen-minus",
    "－": "full-width hyphen-minus",
}
#: The hyphen and every look-alike: each reader must answer the same for all of them.
ALL_DASHES = ["-", *LOOKALIKES]
VARIANTS = pytest.mark.parametrize("d", list(LOOKALIKES), ids=list(LOOKALIKES.values()))
EVERY_DASH = pytest.mark.parametrize("d", ALL_DASHES, ids=["hyphen-minus", *LOOKALIKES.values()])


# ---- the helper -----------------------------------------------------------------------------------


def test_the_helper_holds_every_look_alike_and_nothing_else() -> None:
    assert set(LOOKALIKE_DASHES) == set(LOOKALIKES) and len(LOOKALIKE_DASHES) == len(LOOKALIKES)
    assert "-" not in LOOKALIKE_DASHES


@VARIANTS
def test_each_look_alike_is_the_hyphen_and_the_length_is_kept(d: str) -> None:
    text = f"see XX{d}213 and A{d}501"
    out = dashes_to_hyphen(text)
    assert out == "see XX-213 and A-501" and len(out) == len(text)
    assert re.fullmatch(rf"XX{DASH}213", f"XX{d}213")


def test_the_helper_leaves_everything_else_alone() -> None:
    text = 'XX-213 3\'-6" 1/2" _ ~ . / AB_12 ± ′ ″ ’'
    assert dashes_to_hyphen(text) == text
    assert re.fullmatch(DASH, "-") and not re.fullmatch(DASH, "_") and not re.fullmatch(DASH, "~")


# ---- every reader: the same answer for XX-213 and each look-alike spelling --------------------------


@EVERY_DASH
def test_pairing_reads_tags_and_references_with_any_dash(d: str) -> None:
    assert pairing.tags_in([f"XX{d}213"]) == frozenset({"XX213"})
    assert pairing.item_tags_in([f"see XX{d}213"]) == frozenset({"XX213"})
    assert pairing.printed_item_tag([f"XX{d}213"], "XX213") == "XX-213"
    assert pairing.printed_item_tag([f"XX{d}213{d}T"], "XX213") == "XX-213"
    assert pairing.printed_item_tag([f"XX{d}2130"], "XX213") is None
    assert pairing.references_in([f"REF 2/A{d}501"]) == frozenset({"2/A501", "A501"})
    assert pairing.markers_in([f"ADA{d}1"]) == (frozenset(), frozenset())


@EVERY_DASH
def test_the_set_context_reads_tags_with_any_dash(d: str) -> None:
    assert set_context.tags_in(f"XX{d}213") == ["XX-213"]
    assert set_context.family_of(f"XX{d}213") == "XX"
    assert set_context.item_key(f"XX{d}213T") == "XX-213"


@EVERY_DASH
def test_the_reviewer_context_reads_tags_and_models_with_any_dash(d: str) -> None:
    assert reviewer_context.same_item(f"XX{d}213", "XX-213T")
    assert reviewer_context.model_tokens([{"text": f"AB{d}CD{d}7"}]) == ["AB-CD-7"]


@EVERY_DASH
def test_the_schedules_read_tags_and_codes_with_any_dash(d: str) -> None:
    assert project_schedule.canon(f"xx{d}02") == "XX-2"
    assert project_schedule._finish_tag(f"ST{d}02") == "ST-02"
    assert project_schedule._item_number(f"XX{d}213") == "XX-213"
    assert schedule_index.codes(f"QUARTZ QZ{d}16") == frozenset({"QZ16"})
    assert schedule_index._tag_of(f"ST{d}02") == "ST-02"


@EVERY_DASH
def test_drawing_notes_list_tags_with_any_dash(d: str) -> None:
    phrases = [
        {"text": f"XX{d}213", "box": [0, 0, 10, 10]},
        {"text": f"TOEKICK{d} FN{d}04 PAINTED", "box": [0, 20, 50, 30]},
    ]
    notes = drawing_notes.page_notes(1, phrases, [])
    assert [t["text"] for t in notes["drawings"][0]["tags"]] == ["XX-213", "FN-04"]
    # The note keeps its words as printed.
    assert notes["drawings"][0]["notes"][0]["text"] == f"TOEKICK{d} FN{d}04 PAINTED"


@EVERY_DASH
def test_the_finding_readers_read_codes_with_any_dash(d: str) -> None:
    assert look_rules._tags(f"XX{d}21") == {"XX21"}
    assert decision_resolver._codes(f"XX{d}12") == [("XX", "XX-12")]
    assert product_scope._family_product(f"LA{d}002") == product_scope._family_product("LA-002")
    assert one_card._codes(f"SS{d}01") == one_card._codes("SS-01") == frozenset({"SS-1"})
    assert architect_items.tag_codes(f"(FN{d}01) LAMINATE") == [("FN-01", "FN", "FN-1")]
    assert tag_reader.tag_keys(f"XX{d}012{d}CUST") == ["XX-12-CUST"]
    assert claim_rules._vendor_units(f"AB{d}CD {d} 2 UNITS") == ({"AB-CD": 2}, None)
    assert filler_range.is_filler_label(f'3" F{d}1')


@EVERY_DASH
def test_cabinet_tags_read_with_any_dash(d: str) -> None:
    assert dim_ends._names_a_sink(f"VSB30{d}2")
    assert is_cabinet_code(f"B36{d}2") and not is_cabinet_code(f"QV{d}07")
    assert is_finish_code(f"QV{d}07")
    assert [kind for kind, _why in kinds._kinds_in(f"B36{d}2")] == [kinds.PieceKind.CABINET]


@EVERY_DASH
def test_sheet_numbers_read_with_any_dash(d: str) -> None:
    assert set_pack._sheet_numbers(f"SHEET NO. A{d}501") == ["A-501"]
    assert views.normal_sheet(f"IN{d}7.0") == "IN7.0"
    assert vendor_consistency._norm_code(f"A{d}501") == "A501"
    index = {
        ("set.pdf", 1): "SHEET INDEX " + " ".join(f"A{d}10{n}" for n in range(1, 5)),
        ("set.pdf", 2): f"A{d}101",
        ("set.pdf", 3): f"A{d}102",
    }
    missing, found = vendor_consistency.sheet_index(index, None)
    assert found["listed"] == 4 and sorted(found["missing"]) == ["A103", "A104"]
    assert len(missing) == 2
    phrase = PrintedPhrase(
        f"SHEET NO. A{d}401",
        Box(Decimal(0), Decimal(0), Decimal(80), Decimal(10)),
        Orientation.UPRIGHT,
        Decimal(10),
    )
    assert sheet_index._labels([phrase]).sheet_number == "A-401"


@EVERY_DASH
def test_view_references_read_with_any_dash(d: str) -> None:
    assert view_matching.find_references([f"REF 3/A{d}401"]) == ("3/A401",)
    assert view_matching.normalise_reference(f"3 / a{d}401.") == "3/A401"


@EVERY_DASH
def test_an_item_picture_label_is_a_tag_with_any_dash(d: str) -> None:
    from tests.workflow.test_item_pictures import SETTINGS, _text
    from workflow.item_pictures import LabelSort, label_sort

    assert label_sort(_text(0, 0, f"AB{d}12"), SETTINGS) is LabelSort.TAG


# ---- pairing: the architect prints the vendor's tag with another dash -------------------------------


def _swap(node: Any, old: str, new: str) -> Any:
    if isinstance(node, dict):
        return {key: _swap(value, old, new) for key, value in node.items()}
    if isinstance(node, list):
        return [_swap(value, old, new) for value in node]
    return node.replace(old, new) if isinstance(node, str) else node


def _tagged(found: dict[str, Any], vendor: str, architect: str) -> dict[str, Any]:
    """The synthetic set with the bench's tag made up as `XX-213`: the vendor's pages print it with
    the dash `vendor`, the architect's pages (its key plan and any elevation) with `architect`."""
    by_file = {SHOP: f"XX{vendor}213", ARCH: f"XX{architect}213"}
    for kind in ("dims", "notes"):
        found[kind]["pages"] = [
            _swap(page, "SB-202", by_file[page["file"]]) for page in found[kind]["pages"]
        ]
    return found


@VARIANTS
@pytest.mark.parametrize("side", ["architect", "vendor"])
def test_a_tag_printed_with_another_dash_pairs_with_the_architects_elevation(
    d: str, side: str
) -> None:
    """The bug: the architect prints `XX−213` (or any look-alike), the vendor `XX-213`. The
    architect's elevation printing the tag is the item's drawing, as when both print the hyphen
    (and the other way round)."""
    vendor, architect = ("-", d) if side == "architect" else (d, "-")
    found = _with_pages({4: [_phrase("SB-202", [820, 300, 860, 310])]})
    bench = _across(_work_with(_tagged(found, vendor, architect), _index()), 6)
    assert bench.chosen == ("p1004",) and bench.views == ("p1004.a1.view2",)
    assert bench.code_paired and bench.unsure == () and not bench.needed
    assert "XX-213" in bench.why


@VARIANTS
def test_a_tag_printed_with_another_dash_only_on_a_plan_names_the_plan(d: str) -> None:
    """The architect prints the tag, with the minus sign (or any look-alike), only on its key plan:
    the plan is the item's page ("only as a tag on a plan"), named with the hyphen."""
    found = _with_pages({})
    bench = _across(_work_with(_tagged(found, "-", d), _index()), 6)
    assert bench.needed and bench.chosen == () and bench.unsure == ()
    assert "only as a tag on a plan (XX-213 on its page 5" in bench.why


# ---- the guard ------------------------------------------------------------------------------------

#: Where code reads what a drawing prints.
PACKAGES = (
    "app",
    "extraction",
    "workflow",
    "vocabulary",
    "evidence",
    "retrieval",
    "reports",
    "eval",
)
#: A pattern's name that says it reads a code.
_CODE_NAME = re.compile(
    r"(?:^|_)(?:TAGS?|SHEETS?|CODES?|REFS?|REFERENCES?|PARTS|ITEM_NUMBER|TOKEN|MODEL)(?:_|$)"
)
#: A character range in a class (`A-Z`, `0-9`): not a hyphen the pattern spells.
_RANGE = re.compile(r"[A-Za-z0-9]-[A-Za-z0-9]")

#: Patterns whose text is read through `dashes_to_hyphen` before they match (each module imports it).
READ_THROUGH_THE_HELPER = {
    ("app/review/assistant/architect_items.py", "_CODE"),
    ("app/review/assistant/claim_rules.py", "_CODE"),
    ("app/review/assistant/decision_resolver.py", "_TAG_CODE"),
    ("app/review/assistant/dim_ends.py", "_TAG"),
    ("app/review/assistant/drawing_notes.py", "_TAG"),
    ("app/review/assistant/filler_range.py", "_FILLER_TAG"),
    ("app/review/assistant/look_rules.py", "_TAG"),
    ("app/review/assistant/one_card.py", "_TAG"),
    ("app/review/assistant/product_scope.py", "_TAG_CODE"),
    ("app/review/assistant/project_schedule.py", "_PARTS"),
    ("app/review/assistant/reviewer_context.py", "_MODEL"),
    ("app/review/assistant/reviewer_context.py", "_TAG_PARTS"),
    ("app/review/assistant/schedule_index.py", "_CODE"),
    ("app/review/assistant/set_context.py", "TAG"),
    ("app/review/assistant/set_context.py", "_PARTS"),
    ("app/review/assistant/set_pack.py", "_SHEET"),
    ("app/review/assistant/simple_check/pairing.py", "_VIEW_REF"),
    ("app/review/assistant/simple_check/pairing.py", "_SHEET"),
    ("app/review/assistant/simple_check/pairing.py", "_TAG"),
    ("app/review/assistant/simple_check/pairing.py", "_PADDED_TAG"),
    ("app/review/assistant/simple_check/views.py", "_SHEET"),
    ("app/review/assistant/tag_reader.py", "_TAG"),
    ("app/review/assistant/vendor_consistency.py", "_SHEET_CODE"),
    ("extraction/architect/sheet_index.py", "_SHEET"),
    ("extraction/architect/view_matching.py", "_REFERENCE"),
    ("workflow/item_pictures.py", "_TAG"),
}
#: Patterns that spell the hyphen but read no code a drawing prints, and why.
NOT_A_DRAWINGS_CODE = {
    ("app/models/runs.py", "READING_EVENT_CODE_PATTERN"): "our own event names",
    ("app/review/assistant/chat_corrections.py", "REF"): "our own correction ids",
    ("app/review/assistant/learned.py", "_TAG"): "the reviewer's and the AI's words, not a drawing",
    ("app/review/assistant/page_reviewer.py", "_FINISH_ITEM_NUMBER"): (
        "matches `set_context.tags_in`'s tags, already written with the hyphen"
    ),
    ("app/review/assistant/view_questions.py", "_FINISH_ITEM_NUMBER"): (
        "matches `set_context.tags_in`'s tags, already written with the hyphen"
    ),
    ("app/review/assistant/whole_look.py", "_PARTS"): "part words (toe-kick), not a code",
    ("extraction/models/invocations.py", "_TOKEN_LIKE"): "a secret's shape, for redaction",
    ("extraction/reader.py", "FEET_INCH_TOKEN_RE"): "a size, never a code (sizes keep their marks)",
    ("workflow/slot_reader.py", "_EVENT_CODE"): "our own event names",
}


def _strings(node: ast.AST) -> str:
    return "".join(
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def _code_patterns() -> dict[tuple[str, str], bool]:
    """Every module-level pattern named like a code reader whose pattern spells a hyphen, and
    whether it matches `DASH` itself."""
    found: dict[tuple[str, str], bool] = {}
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target, value = node.targets[0], node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    target, value = node.target, node.value
                else:
                    continue
                if not isinstance(target, ast.Name) or not _CODE_NAME.search(target.id):
                    continue
                if isinstance(value, ast.Call) and getattr(value.func, "attr", "") == "compile":
                    pattern = _strings(value.args[0]) if value.args else ""
                elif isinstance(value, ast.JoinedStr) or (
                    isinstance(value, ast.Constant) and isinstance(value.value, str)
                ):
                    pattern = _strings(value)
                    if "\\d" not in pattern and "[" not in pattern:
                        continue  # a plain string (a model id, a sentence), not a pattern
                else:
                    continue
                if "-" not in _RANGE.sub("", pattern):
                    continue
                uses_dash = any(isinstance(n, ast.Name) and n.id == "DASH" for n in ast.walk(value))
                found[(str(path.relative_to(ROOT)), target.id)] = uses_dash
    return found


def test_every_code_pattern_spelling_the_hyphen_reads_every_look_alike() -> None:
    """A new pattern that reads a tag, a sheet or a code and spells `-` must match `DASH`, or read
    its text through `dashes_to_hyphen` (add it to `READ_THROUGH_THE_HELPER`), or read no
    drawing's code (add it to `NOT_A_DRAWINGS_CODE` with the reason)."""
    found = _code_patterns()
    unread = sorted(
        key
        for key, uses_dash in found.items()
        if not uses_dash and key not in READ_THROUGH_THE_HELPER and key not in NOT_A_DRAWINGS_CODE
    )
    assert unread == [], (
        "these patterns spell the hyphen only, so a code printed with a look-alike dash "
        f"(`XX−213`, a minus sign) is not read: {unread}; use vocabulary.dashes"
    )
    # The lists hold no pattern that is gone (a stale entry would hide a new one of the same name).
    assert sorted((READ_THROUGH_THE_HELPER | set(NOT_A_DRAWINGS_CODE)) - set(found)) == []
    for file in {file for file, _name in READ_THROUGH_THE_HELPER}:
        source = (ROOT / file).read_text(encoding="utf-8")
        assert "dashes_to_hyphen(" in source, f"{file} never reads its text through the helper"
