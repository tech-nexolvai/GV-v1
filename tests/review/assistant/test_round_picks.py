"""Layer 2 for the round-only leftovers (#1861, Anant 2026-10-22: "go with hybrid, with both, but
less cost"), $0.

An item compared only with the vendor's own earlier round that code could not link for free gets
one cheap blind pick of the architect's label; code does the rest. Made-up values only:

- one question per vendor label, the biggest items first; an item whose earlier round's own
  comparison was not proved, or already decided, is never asked again;
- equal values are Right; a different value is a red candidate that never goes red on the cheap
  pick alone (the devil's advocate's own pick is the stronger model's, and must agree on the
  label and the ends); none, or a label code refuses, stays Not compared with the reason;
- the guards hold (underside / top face, room end, a check code fails);
- the money: a hard cap, the biggest first, kept picks reused at $0.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster, pick_cache, proof, round_picks
from app.review.assistant.chat_trust import judged
from app.review.assistant.full_check_advocate import STRONG_PICK, Check, decide
from app.review.assistant.full_check_roster import ROUND_PHASE, PricePlan
from app.review.assistant.round_reference import ROUND_MATCH, compare_with_architect

FILE = "set.pdf"
#: Page 7: the architect's drawing (left) and the vendor's round 1 (right); page 8: round 2.
PAGE_MAP = {
    "pages": [
        {
            "file": FILE,
            "page": 7,
            "area": "Lounge cabinet",
            "halves": [
                {"role": "architect", "box": [0, 0, 400, 600], "round": None},
                {"role": "vendor", "box": [400, 0, 800, 600], "round": 1},
            ],
        },
        {
            "file": FILE,
            "page": 8,
            "area": "Lounge cabinet",
            "area_from_page": 7,
            "halves": [{"role": "vendor", "box": [0, 0, 800, 600], "round": 2}],
        },
    ]
}
_SPANS = {"from": "floor", "to": "top_of_counter"}


def _label(id_: str, page: int, side: str, inches: str, at: tuple[float, float], text: str):
    x, y = at
    return {
        "id": id_,
        "page": page,
        "file": FILE,
        "text": text,
        "inches": inches,
        "box": [x - 8, y - 4, x + 8, y + 4],
        "side": side,
        "orientation": "v",
        "route": "text",
    }


#: Round 2 and round 1 print 30 1/4" (p8.D4, p7.D12); the architect prints 2'-6 1/4" (p7.D3) for
#: the height and 2'-6" (p7.D2) for another one; p8.D5 is a second vendor value (12 1/2").
ROUND2 = _label("D4", 8, "vendor", "30.25", (300, 300), '30 1/4"')
OTHER2 = _label("D5", 8, "vendor", "12.5", (500, 300), '12 1/2"')
ROUND1 = _label("D12", 7, "vendor", "30.25", (600, 300), '30 1/4"')
OTHER1 = _label("D13", 7, "vendor", "12.5", (700, 300), '12 1/2"')
SAME = _label("D3", 7, "architect", "30.25", (200, 300), "2'-6 1/4\"")
DIFFERENT = _label("D2", 7, "architect", "30", (100, 300), "2'-6\"")
LABELS: dict[tuple[str, int], list[dict[str, Any]]] = {
    (FILE, 7): [ROUND1, OTHER1, SAME, DIFFERENT],
    (FILE, 8): [ROUND2, OTHER2],
}


def _box(label: Mapping[str, Any]) -> dict[str, Any]:
    x0, y0, x1, y1 = label["box"]
    return {"page": label["page"], "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": FILE}


def _dim(label: Mapping[str, Any]) -> dict[str, Any]:
    return {k: label[k] for k in ("id", "page", "file", "text", "inches", "route", "side", "box")}


def _item(
    id_: str = "F1",
    vendor: Mapping[str, Any] = ROUND2,
    earlier: Mapping[str, Any] = ROUND1,
    item: str = "Lounge cabinet: overall height",
    **changes: Any,
) -> dict[str, Any]:
    """A helper's "same": the vendor's round 2 against the vendor's own round 1."""
    finding: dict[str, Any] = {
        "id": id_,
        "page": 8,
        "vs_page": 7,
        "round": 2,
        "item": item,
        "category": "cabinet",
        "architect": earlier["text"],
        "vendor": vendor["text"],
        "status": "same",
        "source": "architect",
        "created_by": "helper:3",
        "why": "floor to top of the unit",
        "compared": True,
        "checks": {"printed_architect": "match", "printed_vendor": "match"},
        "inches": {"architect": earlier["inches"], "vendor": vendor["inches"], "difference": "0"},
        "vendor_box": _box(vendor),
        "arch_box": _box(earlier),
        "dims": {"vendor": _dim(vendor), "architect": _dim(earlier)},
    }
    finding.update(changes)
    return finding


def _leftovers(*findings: dict[str, Any]) -> list[dict[str, Any]]:
    """The findings after the free round pass (`round_reference`): their round match recorded."""
    result = compare_with_architect(
        findings, labels=LABELS, page_map=PAGE_MAP, pairs={}, main_file=FILE
    )
    changed = {str(f["id"]): f for f in result.changed}
    return [judged(changed.get(str(f["id"]), f)) for f in findings]


def _group(finding: Mapping[str, Any]) -> str:
    return str(proof.proof_of(judged(dict(finding)))["group"])


def _passing(vendor: Mapping[str, Any], architect: Mapping[str, Any]) -> list[Check]:
    return [Check(name, "pass") for name in ("ink", "annotation", "roles", "axis", "round")]


def _answer(pick: str, **changes: Any) -> dict[str, Any]:
    reply = {
        "architect_pick": pick.split(".")[-1],
        "pick": pick,
        "vendor_spans": _SPANS,
        "architect_spans": _SPANS,
        "vendor_ink_black": "yes",
        "note": "",
    }
    reply.update(changes)
    return reply


def _same_ends(vendor: Mapping[str, Any], architect: Mapping[str, Any]) -> tuple[str, str]:
    return "same_extent", "both ends land on the same drawn lines"


def _cannot_tell(vendor: Mapping[str, Any], architect: Mapping[str, Any]) -> tuple[str, str]:
    return "cannot_tell", "a dimension line's ends were not found"


def _link(
    finding: dict[str, Any],
    answer: Mapping[str, Any] | None,
    checks: Any = _passing,
    ends: Any = _same_ends,
):
    return round_picks.link(
        finding, answer, vendor=ROUND2, labels=LABELS, main_file=FILE, checks=checks, ends=ends
    )


# ---- which items are asked ----------------------------------------------------------------------


def test_the_leftovers_get_one_question_per_vendor_label_on_the_architects_drawing() -> None:
    found = _leftovers(_item("F1"), _item("F2", item="Lounge cabinet: height (side)"))
    assert all(round_picks.waiting(f) for f in found)
    asked = round_picks.questions(found, labels=LABELS, page_map=PAGE_MAP, main_file=FILE)
    assert len(asked) == 1 and asked[0].ref == "p8.D4"
    assert asked[0].findings == ["F1", "F2"]
    # Shown the architect's drawing of the area (page 7's left half), not the vendor's round 1.
    assert asked[0].architect["page"] == 7 and asked[0].architect["box"] == [0, 0, 400, 600]


def test_the_biggest_items_are_asked_first() -> None:
    small = _item("F1", item="Lounge cabinet: shelf height")
    big = _item(
        "F2",
        vendor=OTHER2,
        earlier=OTHER1,
        item="Lounge cabinet: sink cut-out width",
        status="different",
    )
    asked = round_picks.questions(
        _leftovers(small, big), labels=LABELS, page_map=PAGE_MAP, main_file=FILE
    )
    assert [q.ref for q in asked] == ["p8.D5", "p8.D4"]


def test_an_item_code_linked_or_already_decided_is_never_asked_again() -> None:
    (found,) = _leftovers(_item())
    unproved = dict(found)
    unproved[ROUND_MATCH] = {**found[ROUND_MATCH], "earlier_item": "F9"}
    decided = dict(found)
    decided[ROUND_MATCH] = {**found[ROUND_MATCH], round_picks.ROUND_PICK: {"outcome": "none"}}
    missed = dict(found)
    missed[ROUND_MATCH] = {**found[ROUND_MATCH], round_picks.ROUND_PICK: {"outcome": "not_asked"}}
    assert not round_picks.waiting(unproved)  # an unproved round-1 pairing: no second chance
    assert not round_picks.waiting(decided)
    assert round_picks.waiting(missed)  # a Continue asks what the cap did not reach


# ---- what code does with the pick ---------------------------------------------------------------


def test_a_pick_naming_the_architects_same_value_is_right() -> None:
    (found,) = _leftovers(_item())
    linked, outcome = _link(found, _answer("p7.D3"))
    assert outcome == "linked" and linked["status"] == "same"
    assert linked["dims"]["architect"]["id"] == "D3" and linked["dims"]["architect"]["side"] == (
        "architect"
    )
    assert linked[ROUND_MATCH]["compared"] == round_picks.CHEAP_LINK
    assert _group(linked) == "right"


def test_a_pick_writing_the_same_two_ends_in_the_other_order_still_links() -> None:
    # The ends are an unordered pair (2026-10-27): floor -> top and top -> floor are the same ends.
    (found,) = _leftovers(_item())
    reversed_ends = {"from": _SPANS["to"], "to": _SPANS["from"]}
    linked, outcome = _link(found, _answer("p7.D3", architect_spans=reversed_ends))
    assert outcome == "linked" and _group(linked) == "right"


def test_a_cheap_pick_alone_never_makes_a_red() -> None:
    (found,) = _leftovers(_item())
    linked, outcome = _link(found, _answer("p7.D2"))
    assert outcome == "linked" and linked["status"] == "different"
    assert round_picks.cheap_linked(linked)
    # No devil's advocate verdict yet: the candidate is not red.
    assert _group(linked) != "wrong"
    assert round_picks.first_spans(linked) == (_SPANS, _SPANS)


@pytest.mark.parametrize(
    ("answer", "checks", "words"),
    [
        (None, _passing, "not asked within the round-pick cap"),
        (_answer("none"), _passing, "found no architect dimension"),
        (_answer("p7.D12"), _passing, "not a dimension on the architect's drawing"),
        (_answer("p7.D3", vendor_ink_black="no"), _passing, "in colour"),
        (
            _answer("p7.D3", architect_spans={"from": "floor", "to": "box_above_unit"}),
            _passing,
            "their ends land in different places",
        ),
        (
            _answer("p7.D3"),
            lambda v, a: [Check("axis", "fail", "one dimension is a width and the other a height")],
            "one dimension is a width and the other a height",
        ),
    ],
)
def test_anything_else_stays_not_compared_with_the_reason(
    answer: Mapping[str, Any] | None, checks: Any, words: str
) -> None:
    (found,) = _leftovers(_item())
    kept, outcome = _link(found, answer, checks)
    assert outcome != "linked"
    said = proof.proof_of(judged(kept))
    assert said["group"] == "not_compared" and said["not_compared"] == "round_only"
    assert words in said["reason"], said["reason"]


# ---- the stronger pick, and the guards -----------------------------------------------------------

_CODE_PASS = [Check(name, "pass") for name in ("ink", "annotation", "roles", "axis", "round")]
_COMMON: dict[str, Any] = {
    "mode": "ai_pair_plus_code",
    "checks": _CODE_PASS,
    "printed": True,
    "helper_pick": "p7.D2",
    "code_paired": False,
}
_STRONG = {**_answer("p7.D2"), STRONG_PICK: True}
_CANNOT = ("cannot_tell", "a dimension line's ends were not found")


def test_a_cheap_linked_red_needs_the_strong_pick_to_agree_on_the_ends_too() -> None:
    passed = decide(advocate=_STRONG, extent=_CANNOT, first_spans=(_SPANS, _SPANS), **_COMMON)
    assert passed.result == "passed"
    other = {"from": "floor", "to": "underside_of_counter"}
    refused = decide(advocate=_STRONG, extent=_CANNOT, first_spans=(_SPANS, other), **_COMMON)
    assert refused.result == "objects" and "two blind picks" in refused.reason


@pytest.mark.parametrize(
    ("advocate", "item", "words"),
    [
        ({**_STRONG, STRONG_PICK: False}, "Lounge cabinet: overall height", "off its drawing"),
        (None, "Lounge cabinet: overall height", "Not verified within the fixed price"),
        ({**_STRONG, "pick": "none"}, "Lounge cabinet: overall height", "chose no architect"),
        (_STRONG, "Lounge cabinet: height to the underside of the top", "different faces"),
        (_STRONG, "Lounge cabinet: height to the bulkhead", "bulkhead"),
    ],
)
def test_the_guards_hold_for_a_cheap_linked_candidate(
    advocate: Mapping[str, Any] | None, item: str, words: str
) -> None:
    verdict = decide(
        advocate=advocate,
        extent=("cannot_tell", "off its drawing's scale"),
        item=item,
        first_spans=(_SPANS, _SPANS),
        **_COMMON,
    )
    assert verdict.result == "objects" and words in verdict.reason, verdict.reason


def test_the_devils_advocate_asks_the_stronger_model_for_a_cheap_linked_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (found,) = _leftovers(_item())
    linked, _ = _link(found, _answer("p7.D2"))
    picked: list[bool] = []

    def pick(self: Any, finding: Any, vendor: Any, architect: Any, page_map: Any, *,
             strong: bool = False, kind: str = "advocate") -> dict[str, Any]:  # fmt: skip
        picked.append(strong)
        return {**_answer("p7.D2"), STRONG_PICK: strong}

    monkeypatch.setattr(full_check_roster._Run, "_blind_pick", pick)

    def label(self: Any, provenance: Any) -> dict[str, Any] | None:
        if not isinstance(provenance, Mapping):
            return None
        page = LABELS[(FILE, provenance["page"])]
        return next(dict(item) for item in page if item["id"] == provenance["id"])

    monkeypatch.setattr(full_check_roster._Run, "_label", label)
    monkeypatch.setattr(full_check_roster._Run, "_ink", lambda self, label: (100, 0.0))
    monkeypatch.setattr(full_check_roster._Run, "_newer_same", lambda self, v, pm: None)
    monkeypatch.setattr(full_check_roster._Run, "_twins", lambda self, r, a, pm: ())
    monkeypatch.setattr(
        full_check_roster._Run, "_extent", lambda self, v, a, pm=None: ("same_extent", "fit")
    )
    engine = SimpleNamespace(settings=SimpleNamespace(pairing_proof="ai_pair_plus_code"))
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    linked["checks"] = {**linked["checks"], "printed_architect": "match"}
    verdict = run._judge(linked, PAGE_MAP, [])
    assert picked == [True]  # geometry fits, and still the stronger model's pick
    assert verdict is not None and verdict[0]["result"] == "passed"


# ---- the money: the cap, the biggest first, kept picks at $0 ------------------------------------


class _Book:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.findings = {str(f["id"]): f for f in findings}
        self.main_file = FILE
        self.dim_labels = SimpleNamespace(
            on_page=lambda file, page, read_now=False: LABELS.get((file, page), [])
        )

    def live(self) -> list[dict[str, Any]]:
        return list(self.findings.values())

    def replace(self, finding: dict[str, Any]) -> bool:
        self.findings[str(finding["id"])] = finding
        return True

    def absorb(self, finding_id: str) -> bool:
        return self.findings.pop(finding_id, None) is not None


def _pack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    pack.mkdir(exist_ok=True)
    pages = [{"file": f, "page": p, "labels": labels} for (f, p), labels in LABELS.items()]
    (pack / "dims.json").write_text(json.dumps({"pages": pages}))
    (pack / "page_map.json").write_text(json.dumps(PAGE_MAP))
    return pack


def _run(pack: Path, findings: list[dict[str, Any]], cap: int) -> full_check_roster._Run:
    engine = SimpleNamespace(
        book=_Book(findings),
        tools=SimpleNamespace(workspace=SimpleNamespace(set_pack=pack, uploads=pack)),
        settings=SimpleNamespace(
            round_pick_cap_micros=cap, message_cap_micros=10_000_000, pairing_proof="x"
        ),
        spent=0,
        cancel=threading.Event(),
        finding_lock=threading.Lock(),
        _price=lambda model, prompt, out: 100,
        _fits=lambda estimate: True,
        _show_finding=lambda finding: None,
        _show_dropped=lambda finding_id, reason: None,
        _plan=None,
    )
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    run.plan = PricePlan.balanced(5_000_000, 1, 1)
    return run


def _fake_picks(
    monkeypatch: pytest.MonkeyPatch, answers: Mapping[str, str], extent: str = "same_extent"
) -> list[str]:
    asked: list[str] = []

    def held(self: Any, finding: Any, vendor: Any, architect: Any, page_map: Any, model: Any,
             effort: str = "medium", kind: str = "advocate") -> dict[str, Any]:  # fmt: skip
        asked.append(f"p{vendor['page']}.{vendor['id']}")
        self._charge(self.pick_phase, 100)
        return _answer(answers[f"p{vendor['page']}.{vendor['id']}"])

    monkeypatch.setattr(full_check_roster._Run, "_held_pick", held)
    monkeypatch.setattr(full_check_roster._Run, "_ink", lambda self, label: (100, 0.0))
    monkeypatch.setattr(
        full_check_roster._Run, "_extent", lambda self, v, a, pm=None: (extent, "the drawn run")
    )
    monkeypatch.setattr(full_check_roster, "_annotations", lambda engine: [])
    return asked


def _two_leftovers() -> list[dict[str, Any]]:
    small = _item("F1", item="Lounge cabinet: shelf height")
    big = _item(
        "F2",
        vendor=OTHER2,
        earlier=OTHER1,
        item="Lounge cabinet: sink cut-out width",
        status="different",
    )
    return _leftovers(small, big)


def test_the_cap_is_a_hard_limit_and_the_biggest_item_is_asked_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _fake_picks(monkeypatch, {"p8.D4": "p7.D3", "p8.D5": "none"})
    # Room for one pick's worst case (100) only: the cap is never passed.
    run = _run(_pack(tmp_path), _two_leftovers(), cap=150)
    run._round_picks(PAGE_MAP)
    assert asked == ["p8.D5"]  # the bigger item (a cut-out), never the smaller one
    assert run.spent[ROUND_PHASE] <= 150
    book = run.engine.book
    left = book.findings["F1"][ROUND_MATCH][round_picks.ROUND_PICK]
    assert left["outcome"] == "not_asked"
    assert any("not asked within their cap" in line for line in run.not_done)
    assert run.round_block["asked"] == 1 and run.round_block["not_asked"] == 1


def test_a_cap_of_nothing_turns_the_step_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _fake_picks(monkeypatch, {"p8.D4": "p7.D3", "p8.D5": "none"})
    run = _run(_pack(tmp_path), _two_leftovers(), cap=0)
    run._round_picks(PAGE_MAP)
    assert asked == [] and run.round_block == {}


def test_kept_picks_are_reused_at_no_cost_on_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _fake_picks(monkeypatch, {"p8.D4": "p7.D3", "p8.D5": "none"})
    pack = _pack(tmp_path)
    first = _run(pack, _two_leftovers(), cap=2_000_000)
    first._round_picks(PAGE_MAP)
    assert sorted(asked) == ["p8.D4", "p8.D5"]
    assert (pack / pick_cache.PICKS_FILE).is_file()
    right = first.engine.book.findings["F1"]
    assert right["status"] == "same" and _group(right) == "right"

    again = _run(pack, _two_leftovers(), cap=2_000_000)
    again._round_picks(PAGE_MAP)
    assert sorted(asked) == ["p8.D4", "p8.D5"]  # nothing asked again: $0
    assert again.round_block["kept"] == 2 and again.spent.get(ROUND_PHASE, 0) == 0
    assert again.engine.book.findings["F1"]["status"] == "same"


def test_the_cheap_pick_has_its_own_key_never_the_advocates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(_pack(tmp_path), _two_leftovers(), cap=2_000_000)
    drawing = {"file": FILE, "page": 7, "id": "", "text": "", "box": [0, 0, 400, 600]}
    cheap = run._pick_key(ROUND2, drawing, PAGE_MAP, "m", "medium", round_picks.CHEAP_LINK)
    advocate = run._pick_key(ROUND2, drawing, PAGE_MAP, "m", "medium")
    assert cheap is not None and advocate is not None and cheap != advocate


def test_the_round_phase_never_passes_its_cap(tmp_path: Path) -> None:
    run = _run(_pack(tmp_path), [], cap=1_000)
    assert run._phase_room(ROUND_PHASE, 1_000)
    run._charge(ROUND_PHASE, 600)
    assert not run._phase_room(ROUND_PHASE, 500)
    assert run._phase_room(ROUND_PHASE, 400)


def test_money_is_kept_for_the_picks_only_when_the_set_has_a_later_round(tmp_path: Path) -> None:
    run = _run(_pack(tmp_path), [], cap=2_000_000)
    assert run._round_need() == 0
    run.later_rounds = True
    assert run._round_need() == int(5_000_000 * full_check_roster.ROUND_PICK_SHARE)
    run.round_unpaid = 0
    assert run._round_need() == 0
    run.round_done = True
    assert run._round_need() == 0
    assert full_check_roster._later_rounds(PAGE_MAP) is True
    assert full_check_roster._later_rounds({"pages": [PAGE_MAP["pages"][0]]}) is False


def test_a_pick_code_geometry_shows_ending_elsewhere_is_never_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code's drawn run shows the two dimensions end in different places: no Right, no red."""
    _fake_picks(monkeypatch, {"p8.D4": "p7.D3", "p8.D5": "p7.D2"}, extent="different_extent")
    run = _run(_pack(tmp_path), _two_leftovers(), cap=2_000_000)
    run._round_picks(PAGE_MAP)
    for finding in run.engine.book.live():
        said = proof.proof_of(judged(finding))
        assert said["group"] == "not_compared" and "the drawn run" in said["reason"]


# ---- option C (Anant, 2026-10-22): off by default; only code-proved ends compare an item ---------


def test_the_step_is_off_by_default_no_pick_no_money() -> None:
    from app.config import Settings
    from app.review.assistant.chat_engine import EngineSettings
    from app.review.assistant.chat_service import MessageJob

    assert Settings.model_fields["assistant_round_pick_cap_usd"].default == 0
    assert EngineSettings.__dataclass_fields__["round_pick_cap_micros"].default == 0
    assert MessageJob.__dataclass_fields__["round_pick_cap_micros"].default == 0


def test_with_the_cap_at_nothing_no_money_is_kept_even_with_later_rounds(tmp_path: Path) -> None:
    run = _run(_pack(tmp_path), _two_leftovers(), cap=0)
    run.later_rounds = True
    assert run._round_need() == 0


@pytest.mark.parametrize("pick", ["p7.D3", "p7.D2"])
def test_a_number_that_only_matches_is_no_right_and_no_red(pick: str) -> None:
    """Code cannot prove the two dimensions end at the same places: neither a Right (an equal
    number) nor a red candidate (a different one); Not compared, with the reason."""
    (found,) = _leftovers(_item())
    kept, outcome = _link(found, _answer(pick), ends=_cannot_tell)
    assert outcome == "refused" and kept["status"] == "same"
    said = proof.proof_of(judged(kept))
    assert said["group"] == "not_compared" and said["not_compared"] == "round_only"
    assert "could not prove both dimensions end at the same places" in said["reason"]


def test_agreeing_picks_alone_never_prove_a_cheap_pairing() -> None:
    """The stronger pick agrees on the label and the ends, but code's geometry cannot tell: not
    red. With code's proof of the same ends (and the pick agreeing) it may be."""
    refused = decide(
        advocate=_STRONG, extent=_CANNOT, first_spans=(_SPANS, _SPANS), code_ends=True, **_COMMON
    )
    assert refused.result == "objects" and "only when code proves" in refused.reason
    proved = decide(
        advocate=_STRONG,
        extent=("same_extent", "the anchors line up"),
        first_spans=(_SPANS, _SPANS),
        code_ends=True,
        **_COMMON,
    )
    assert proved.result == "passed" and proved.answers["pairing"] == "proved"


def test_no_pick_is_asked_for_a_cheap_pairing_code_cannot_prove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (found,) = _leftovers(_item())
    linked, _ = _link(found, _answer("p7.D2"))
    picked: list[bool] = []

    def pick(self: Any, *given: Any, strong: bool = False, kind: str = "advocate") -> Any:
        picked.append(strong)
        return {**_answer("p7.D2"), STRONG_PICK: strong}

    def label(self: Any, provenance: Any) -> dict[str, Any] | None:
        page = LABELS[(FILE, provenance["page"])]
        return next(dict(item) for item in page if item["id"] == provenance["id"])

    monkeypatch.setattr(full_check_roster._Run, "_blind_pick", pick)
    monkeypatch.setattr(full_check_roster._Run, "_label", label)
    monkeypatch.setattr(full_check_roster._Run, "_ink", lambda self, label: (100, 0.0))
    monkeypatch.setattr(full_check_roster._Run, "_newer_same", lambda self, v, pm: None)
    monkeypatch.setattr(full_check_roster._Run, "_twins", lambda self, r, a, pm: ())
    monkeypatch.setattr(full_check_roster._Run, "_extent", lambda self, v, a, pm=None: _CANNOT)
    engine = SimpleNamespace(settings=SimpleNamespace(pairing_proof="ai_pair_plus_code"))
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    verdict = run._judge(linked, PAGE_MAP, [])
    assert picked == []  # no money spent on a pairing code cannot prove
    assert verdict is not None and verdict[0]["result"] == "objects"
