"""GV's filler range on the vendor's own fillers (`filler_range`): Wrong, Right or a GV note.

Made-up labels only (no client data): page 1 holds an architect plan and page 2 a vendor plan of
one made-up area, or the vendor's page stands alone (no architect drawing at all).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import classification, escalation, proof
from app.review.assistant import filler_range as fr
from app.review.assistant.chat_trust import judged
from app.review.assistant.decision_resolver import ResolverContext, resolve, settings_in_force
from app.review.assistant.product_scope import CODE_MADE, ProductScope, refiled

FILE = "Made-up set.pdf"
RULES_MD = """| Setting | In force | Where it comes from | Used by |
|---|---|---|---|
| Filler width, maximum (`filler_max`) | 2" | the rulebook's default | CAB-FILLER-001 |
| Filler width, minimum (`filler_min`) | 1" | the rulebook's default | CAB-FILLER-001 |
"""
LIMITS = fr.limits_from(settings_in_force(RULES_MD))


def _label(
    ident: str, text: str, inches: str, side: str, x: float = 100.0, route: str = "text"
) -> dict[str, Any]:
    return {
        "id": ident,
        "text": text,
        "inches": inches,
        "side": side,
        "route": route,
        "orientation": "h",
        "box": [x, 300.0, x + 30, 312.0],
    }


def _dims(
    vendor: list[dict[str, Any]], architect: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    pages = [{"file": FILE, "page": 2, "labels": vendor}]
    if architect is not None:
        pages.insert(0, {"file": FILE, "page": 1, "labels": architect})
    return {"version": 8, "files": [FILE], "pages": pages}


PAGE_MAP = {
    "version": 8,
    "files": [FILE],
    "pages": [
        {"file": FILE, "page": 1, "area": "4. Pantry"},
        {"file": FILE, "page": 2, "area": "4. Pantry"},
    ],
}


def _one(
    vendor: list[dict[str, Any]], architect: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    return fr.findings_from(_dims(vendor, architect), PAGE_MAP, LIMITS)


def _recorded(made: dict[str, Any]) -> dict[str, Any]:
    """The finding as the book records it, GV's limits laid over it (the hook's own step)."""
    return judged({**made, "id": "F1", "checks": {"printed_vendor": "match", **made["checks"]}})


# ---- which labels are fillers --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '3/4" FILLER',
        'FILLER 2"',
        '4" Filler',
        '5" FILLER TYP.',
        '1 1/2" FILLER (L)',
        '4" F1',
        '4" F',
    ],
)
def test_a_size_with_a_filler_word_or_tag_is_a_filler(text: str) -> None:
    assert fr.is_filler_label(text)


@pytest.mark.parametrize(
    "text",
    [
        '4"',
        '4" PANEL',
        '34 1/2" FILLER HEIGHT',
        'OVERALL 96" W/ FILLERS',
        '3/4" CABINET SIDE',
        "FILLER",
        '2" + 2" FILLERS',
        '4" SHELF',
    ],
)
def test_any_other_label_is_not_a_filler(text: str) -> None:
    assert not fr.is_filler_label(text)


# ---- the vendor's filler with no architect size: GV's range decides -------------------------------


def test_a_two_inch_filler_is_within_the_range_and_adds_nothing() -> None:
    """In range: no Wrong (and no Right of its own: a GV rule has no architect side to look at)."""
    assert _one([_label("D3", '2" FILLER', "2", "vendor")]) == []
    assert _one([_label("D3", '1" FILLER', "1", "vendor")]) == []


@pytest.mark.parametrize(("text", "inches"), [('4" FILLER', "4"), ('5" FILLER', "5")])
def test_a_filler_wider_than_the_maximum_is_wrong(text: str, inches: str) -> None:
    (made,) = _one([_label("D3", text, inches, "vendor")])
    assert made["status"] == "different" and made["vendor"] == f'{inches}"'
    recorded = _recorded(made)
    assert proof.proof_group(recorded) == "clouded"
    assert proof.should_be(recorded) == '1" to 2" (GV filler rule)'
    assert proof.vendor_value(recorded) == f'{inches}"'
    assert classification.classify(recorded, proved=True).rule == "R6"


def test_a_filler_narrower_than_the_minimum_is_wrong() -> None:
    (made,) = _one([_label("D3", '3/4" FILLER', "0.75", "vendor")])
    assert made["status"] == "different"
    assert proof.proof_group(_recorded(made)) == "clouded"


def test_a_four_inch_dimension_that_is_not_a_filler_is_ignored() -> None:
    assert _one([_label("D3", '4"', "4", "vendor"), _label("D4", '4" PANEL', "4", "vendor")]) == []


def _phrase(text: str, x: float, width: float = 60.0) -> dict[str, Any]:
    return {"text": text, "box": [x, 300.0, x + width, 312.0], "side": "vendor"}


def test_a_dimension_whose_printed_row_says_filler_is_a_filler() -> None:
    """A dimension's value with FILLER printed after it on the same row (the reader keeps the
    value as the label and the whole row as a phrase)."""
    dims = _dims([_label("D5", '4"', "4", "vendor", x=100)])
    dims["pages"][0]["phrases"] = [_phrase('4" FILLER', 100), _phrase('36"', 200, 20)]
    (made,) = fr.findings_from(dims, PAGE_MAP, LIMITS)
    assert made["status"] == "different" and made["vendor"] == '4"'


@pytest.mark.parametrize(
    "phrases",
    [
        [_phrase('4" SHELF', 100)],
        [_phrase('4" FILLER', 300)],  # the phrase is elsewhere on the page
        [_phrase('4" FILLER', 100), _phrase('4" PANEL', 90, 80)],  # two rows: not sure
    ],
)
def test_a_dimension_is_a_filler_only_when_its_own_row_says_so(
    phrases: list[dict[str, Any]],
) -> None:
    dims = _dims([_label("D5", '4"', "4", "vendor", x=100)])
    dims["pages"][0]["phrases"] = phrases
    assert fr.findings_from(dims, PAGE_MAP, LIMITS) == []


def test_only_vendor_labels_are_judged_and_never_from_drawn_ink_alone() -> None:
    assert _one([_label("D3", '4" FILLER', "4", "unknown")]) == []
    assert _one([_label("D3", '4" FILLER', "4", "vendor", route="drawn")]) == []


def test_the_limits_in_force_decide_and_none_means_no_check() -> None:
    project = RULES_MD.replace('| 2" | the rulebook', '| 4" | the project')
    limits = fr.limits_from(settings_in_force(project))
    assert (
        fr.findings_from(_dims([_label("D3", '4" FILLER', "4", "vendor")]), PAGE_MAP, limits) == []
    )
    assert fr.limits_from({}) is None
    assert fr.findings_from(_dims([_label("D3", '4" FILLER', "4", "vendor")]), PAGE_MAP, None) == []


# ---- the architect governs ----------------------------------------------------------------------


def test_the_vendor_following_the_architects_out_of_range_filler_is_not_compared() -> None:
    (made,) = _one(
        [_label("D3", '3/4" FILLER', "0.75", "vendor")],
        [_label("D7", '3/4" FILLER', "0.75", "architect")],
    )
    assert made["status"] == "check"
    assert made["gv_note"] == 'the architect specifies 3/4", outside GV\'s 1"-2" filler range'
    shown = classification.classify(made, proved=False)
    assert (shown.outcome, shown.reason) == ("not_compared", fr.ARCHITECT_OUTSIDE)
    assert shown.why == made["gv_note"]
    assert proof.proof_group(judged({**made, "id": "F1"})) == "not_compared"


def test_the_vendor_differing_from_the_architect_is_left_to_the_architect_comparison() -> None:
    assert (
        _one(
            [_label("D3", '4" FILLER', "4", "vendor")],
            [_label("D7", '3/4" FILLER', "0.75", "architect")],
        )
        == []
    )


def test_an_in_range_filler_the_architect_also_gives_adds_nothing() -> None:
    both = _one(
        [_label("D3", '2" FILLER', "2", "vendor")], [_label("D7", '2" FILLER', "2", "architect")]
    )
    assert both == []


def test_an_architect_filler_of_another_area_does_not_count() -> None:
    page_map = {
        "pages": [
            {"file": FILE, "page": 1, "area": "1. Lobby"},
            {"file": FILE, "page": 2, "area": "4. Pantry"},
        ]
    }
    (made,) = fr.findings_from(
        _dims(
            [_label("D3", '4" FILLER', "4", "vendor")],
            [_label("D7", '4" FILLER', "4", "architect")],
        ),
        page_map,
        LIMITS,
    )
    assert made["status"] == "different"


# ---- the rest of the check leaves it alone -------------------------------------------------------


def test_the_resolver_and_the_escalation_leave_the_note_alone() -> None:
    (made,) = _one(
        [_label("D3", '3/4" FILLER', "0.75", "vendor")],
        [_label("D7", '3/4" FILLER', "0.75", "architect")],
    )
    note = {**made, "id": "F1"}
    result = resolve([note], ResolverContext(settings=settings_in_force(RULES_MD)))
    assert result.changed == [] and result.absorbed == {}
    assert not escalation.undecided(note)


def test_it_is_filed_under_cabinets_for_the_product_scope() -> None:
    assert fr.CREATED_BY in CODE_MADE
    (made,) = _one([_label("D3", '4" FILLER', "4", "vendor")])
    assert refiled(made, ProductScope.of(["cabinet"])) == "cabinet"
    assert refiled(made, ProductScope.of(["countertop"])) is None


# ---- the hook ------------------------------------------------------------------------------------


class _Book:
    main_file = FILE

    def __init__(self, live: list[dict[str, Any]]) -> None:
        self._live = {str(f["id"]): f for f in live}
        self.added: list[dict[str, Any]] = []
        self.dropped: list[str] = []

    def live(self) -> list[dict[str, Any]]:
        return list(self._live.values())

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        kept = {k: v for k, v in given.items() if k not in ("settled", "checks", "keep_reason")}
        made = {**kept, "id": f"P{len(self.added) + 1}", "created_by": created_by, "checks": {}}
        self.added.append(made)
        self._live[made["id"]] = made
        return dict(made)

    def replace(self, finding: dict[str, Any]) -> bool:
        self._live[str(finding["id"])] = finding
        return True

    def absorb(self, finding_id: str) -> bool:
        self.dropped.append(finding_id)
        return self._live.pop(finding_id, None) is not None


class _Engine:
    def __init__(self, folder: Path, live: list[dict[str, Any]], mode: str = "full_check") -> None:
        self.settings = type("S", (), {"mode": mode})()
        workspace = type("W", (), {"set_pack": folder, "skills": folder})()
        self.tools = type("T", (), {"workspace": workspace})()
        self.book = _Book(live)
        self.finding_lock = threading.RLock()
        self.shown: list[dict[str, Any]] = []
        self.dropped: list[tuple[str, str]] = []

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append((finding_id, reason))


def _pack(tmp_path: Path, dims: dict[str, Any]) -> Path:
    import json

    (tmp_path / "dims.json").write_text(json.dumps(dims))
    (tmp_path / "page_map.json").write_text(json.dumps(PAGE_MAP))
    (tmp_path / "rules.md").write_text(RULES_MD)
    return tmp_path


def test_the_hook_records_a_wrong_with_gv_limits_and_its_rule_witness(tmp_path: Path) -> None:
    folder = _pack(tmp_path, _dims([_label("D3", '5" FILLER', "5", "vendor")]))
    engine = _Engine(folder, [])
    assert fr.add_to_answer(engine) == {"added": 1, "replaced": 0}
    (recorded,) = engine.book.live()
    assert recorded["settled"]["method"] == "gv_limits"
    assert recorded["checks"]["rule"] == "agrees" and "rule" in recorded["witnesses"]
    assert fr.covered_labels(engine.book.live()) == {(FILE, 2, "D3")}
    assert fr.add_to_answer(_Engine(folder, [], mode="chat")) is None


def test_the_hook_turns_a_same_item_into_the_architect_note(tmp_path: Path) -> None:
    folder = _pack(
        tmp_path,
        _dims(
            [_label("D3", '3/4" FILLER', "0.75", "vendor")],
            [_label("D7", '3/4" FILLER', "0.75", "architect")],
        ),
    )
    same = {"id": "F4", "page": 2, "status": "same", "dims": {"vendor": {"id": "D3", "page": 2}}}
    engine = _Engine(folder, [same])
    assert fr.add_to_answer(engine) == {"added": 1, "replaced": 1}
    assert engine.dropped[0][0] == "F4" and "outside GV's" in engine.dropped[0][1]
    (note,) = engine.book.live()
    assert note["keep_reason"] == fr.ARCHITECT_OUTSIDE and note["status"] == "check"


def test_the_hook_leaves_a_label_the_answer_already_judged(tmp_path: Path) -> None:
    folder = _pack(tmp_path, _dims([_label("D3", '4" FILLER', "4", "vendor")]))
    other = {
        "id": "F2",
        "page": 2,
        "status": "different",
        "dims": {"vendor": {"id": "D3", "page": 2}},
    }
    engine = _Engine(folder, [other])
    assert fr.add_to_answer(engine) == {"added": 0, "replaced": 0}
    assert [f["id"] for f in engine.book.live()] == ["F2"]


def test_what_it_lays_over_a_finding_fits_the_answers_contract() -> None:
    """The stored answer is checked against the contract: GV's limits and the note's reason must be
    values it accepts (found on a $0 run through the app: a `settled.was` of None was refused)."""
    from typing import get_args

    from app.review.assistant.chat_contract import ChatFindingSettledOut, ChatKeepReason

    made = _one(
        [_label("D3", '4" FILLER', "4", "vendor"), _label("D4", '2" FILLER', "2", "vendor", x=200)]
    )
    for finding in made:
        ChatFindingSettledOut.model_validate(finding["settled"])
    (note,) = _one(
        [_label("D3", '3/4" FILLER', "0.75", "vendor")],
        [_label("D7", '3/4" FILLER', "0.75", "architect")],
    )
    assert note["keep_reason"] in get_args(ChatKeepReason.__value__)
    assert note["keep_reason"] in classification.REASONS
