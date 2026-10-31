"""The learned list and the top-1 past wording (`app/review/assistant/learned.py`). Made-up data."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.review.assistant import learned
from app.review.assistant.learned import (
    DecidedCard,
    Example,
    Learned,
    LearnedRefused,
)

NEVER = learned.load_never()
REPO = Path(__file__).resolve().parents[3]


def card(
    item: str,
    why: str,
    *,
    letter: str = "D",
    fid: str = "f1",
    status: str = "check",
    proof: str = "likely_only",
    claim: str = "please_check",
    by: str = "page_reviewer",
    words: str | None = None,
) -> dict[str, Any]:
    return {
        "id": fid,
        "item": item,
        "status": status,
        "verified": False,
        "created_by": by,
        "whole_look": {
            "by": by,
            "proof": proof,
            "letter": letter,
            "kind": {"B": "construction", "D": "finish", "E": "scope"}.get(letter, "size"),
            "claim": claim,
            "why": why,
            "words": words or f"Please check and confirm: {item}: {why}",
        },
    }


def decided(
    key: str,
    decision: str,
    project: str,
    *,
    by: str = "Gina",
    words: str = "words",
    item_kind: str = "cabinet",
    view_kind: str = "elevation",
    at: str = "2026-10-01",
    maker: str = "page_reviewer",
) -> DecidedCard:
    return DecidedCard(
        type=key,
        letter=key[0],
        decision=decision,
        project=project,
        by=by,
        at=at,
        words=words,
        item_kind=item_kind,
        view_kind=view_kind,
        maker=maker,
    )


# ---- the type ---------------------------------------------------------------------------------


def test_type_key_is_stable_across_tags_places_and_numbers() -> None:
    one = card("Metal trim at sink wall: finish tag missing", "No MT-01 tag on the trim.")
    two = card("Metal trim, island: finish tag missing", 'The trim has no MT-002.C tag at 36".')
    assert learned.type_key(one) == "D: metal finish tag missing"
    assert learned.type_key(one) == learned.type_key(two)


@pytest.mark.parametrize(
    ("item", "why", "letter", "key"),
    [
        (
            "Toe kick, section S1: no finish tag",
            "The section's toe kick is untagged.",
            "D",
            "D: toe kick finish tag missing",
        ),
        (
            "Island top: overhang support",
            'No leg under the 14" overhang.',
            "B",
            "B: support missing",
        ),
        (
            "Fronts: grain arrows on a plain finish",
            "Remove grain direction.",
            "D",
            "D: grain not wanted",
        ),
        (
            "Trash bin in the front view",
            "Not said who supplies it.",
            "E",
            "E: bin who supplies missing",
        ),
    ],
)
def test_type_keys_read_like_gv_comments(item: str, why: str, letter: str, key: str) -> None:
    assert learned.type_key(card(item, why, letter=letter)) == key


def test_a_card_without_a_please_check_mark_has_no_type() -> None:
    assert learned.type_key({"id": "x", "item": "Width", "status": "different"}) is None


def test_reds_and_code_proved_items_are_never_please_checks() -> None:
    assert learned.is_please_check(card("Toe kick", "no tag"))
    assert not learned.is_please_check(card("Toe kick", "no tag", status="different"))
    assert not learned.is_please_check(card("Toe kick", "no tag", proof="labels"))
    assert not learned.is_please_check(card("Toe kick", "no tag", claim="wrong"))
    assert not learned.is_please_check({**card("Toe kick", "no tag"), "verified": True})


# ---- counts and proposals ------------------------------------------------------------------------

KEY = "D: metal finish tag missing"


def test_three_dismissals_never_accepted_is_proposed() -> None:
    two = [decided(KEY, "not_a_problem", p) for p in ("P1", "P2")]
    counted, _ = learned.tally(two, NEVER)
    assert counted[0].status == "watch"
    three = [*two, decided(KEY, "not_a_problem", "P3")]
    counted, _ = learned.tally(three, NEVER)
    assert (counted[0].status, counted[0].dismissed, counted[0].projects) == ("proposed", 3, 3)


def test_one_accept_keeps_a_type_off_the_proposals() -> None:
    cards = [decided(KEY, "not_a_problem", f"P{n}") for n in range(5)]
    cards.append(decided(KEY, "agree", "P9"))
    counted, _ = learned.tally(cards, NEVER)
    assert counted[0].status == "watch" and counted[0].accepted == 1


def test_only_real_reviewers_count() -> None:
    cards = [decided(KEY, "not_a_problem", f"P{n}", by="ui reviewer") for n in range(3)]
    counted, skipped = learned.tally(cards, NEVER, counts=lambda by: by == "Gina")
    assert counted == [] and skipped == 3


# ---- never suppress ------------------------------------------------------------------------------


def test_every_rulebook_rule_is_on_the_never_suppress_list() -> None:
    ids = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["id"]
        for path in (REPO / "rules" / "rulebook").glob("*.yaml")
    }
    assert ids and ids <= {rule.id for rule in NEVER.rules}


@pytest.mark.parametrize(
    ("key", "maker", "why"),
    [
        ("A: dimension differs", None, "kind A"),
        ("B: filler missing", None, "CAB-FILLER-001"),
        ("C: cut-out missing", None, "CT-SINK-CUTOUT"),
        ("D: toe kick finish tag missing", "sink_offset", "GV's sink rules"),
    ],
)
def test_protected_types(key: str, maker: str | None, why: str) -> None:
    assert why in (NEVER.why(key, maker) or "")


def test_a_protected_type_is_never_proposed() -> None:
    cards = [decided("B: filler missing", "not_a_problem", f"P{n}") for n in range(4)]
    counted, _ = learned.tally(cards, NEVER)
    assert counted[0].status == "protected"
    rows = learned.proposals(counted)
    with pytest.raises(LearnedRefused):
        learned.approve([], "B: filler missing", by="Anant", proposed=rows, never=NEVER)


def test_an_ordinary_type_is_not_protected() -> None:
    assert NEVER.why(KEY, "page_reviewer") is None


# ---- approvals -----------------------------------------------------------------------------------


def _proposed() -> list[dict[str, Any]]:
    cards = [decided(KEY, "not_a_problem", f"P{n}") for n in range(3)]
    return learned.proposals(learned.tally(cards, NEVER)[0])


def test_only_a_proposed_type_can_be_approved_and_the_newest_event_counts() -> None:
    with pytest.raises(LearnedRefused):
        learned.approve(
            [], "D: toe kick finish tag missing", by="Anant", proposed=_proposed(), never=NEVER
        )
    at = datetime(2026, 11, 1, tzinfo=UTC)
    events = learned.approve([], KEY, by="Anant", proposed=_proposed(), never=NEVER, at=at)
    assert learned.approved_types(events) == {KEY}
    events = learned.approve(events, KEY, by="Anant", proposed=[], never=NEVER, action="revoke")
    assert learned.approved_types(events) == frozenset()


def test_proposed_but_not_approved_changes_nothing_and_approved_drops_only_please_checks() -> None:
    please = card("Metal trim: finish tag missing", "No MT-01 tag.")
    proposed_only = Learned(approved=frozenset(), never=NEVER)
    assert proposed_only.suppressed(please) is None
    approved = Learned(approved=frozenset({KEY}), never=NEVER)
    assert "Anant approved" in (approved.suppressed(please) or "")
    red = card(
        "Metal trim: finish tag missing", "No MT-01 tag.", status="different", proof="labels"
    )
    assert approved.suppressed(red) is None
    # A damaged never-suppress list turns suppression off.
    assert Learned(approved=frozenset({KEY}), never=None).suppressed(please) is None


class _Book:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.findings = {f["id"]: f for f in findings}
        self.dropped: dict[str, str] = {}

    def live(self) -> list[dict[str, Any]]:
        return list(self.findings.values())

    def drop(self, raw: dict[str, Any]) -> tuple[str, str]:
        self.findings.pop(raw["finding_id"])
        self.dropped[raw["finding_id"]] = raw["reason"]
        return raw["finding_id"], raw["reason"]


def _engine(findings: list[dict[str, Any]], mode: str = "full_check") -> Any:
    shown: list[tuple[str, str]] = []
    engine = SimpleNamespace(
        settings=SimpleNamespace(mode=mode, identity=SimpleNamespace(project="Job One")),
        finding_lock=threading.Lock(),
        book=_Book(findings),
        shown=shown,
        _show_dropped=lambda fid, reason: shown.append((fid, reason)),
    )
    return engine


def test_drop_suppressed_drops_approved_please_checks_with_a_reason() -> None:
    keep = card("End panel: no finish tag", "untagged", fid="keep")
    gone = card("Metal trim: finish tag missing", "No MT-01 tag.", fid="gone", by="set_check")
    red = card(
        "Metal trim: finish tag missing", "No tag.", fid="red", status="different", proof="labels"
    )
    engine = _engine([keep, gone, red])
    rules = Learned(approved=frozenset({KEY}), never=NEVER)
    assert learned.drop_suppressed(engine, rules) == 1
    assert set(engine.book.findings) == {"keep", "red"}
    assert "learned list" in engine.book.dropped["gone"]
    assert engine.shown[0][0] == "gone"
    # Not a full check: nothing is dropped.
    assert learned.drop_suppressed(_engine([gone], mode="chat"), rules) == 0


# ---- decided cards from a stored answer ----------------------------------------------------------


def test_cards_of_answer_keeps_decided_please_checks_with_the_reviewers_value() -> None:
    findings = [
        card(
            "Toe kick: no finish tag",
            "untagged",
            fid="a",
            words="Please check and confirm: Toe kick: no finish tag. It should be: WD-1.",
        ),
        card("End panel: no finish tag", "untagged", fid="b"),
        card("Width", "differs", fid="c", status="different", proof="labels"),
    ]
    decisions: dict[str, dict[str, Any]] = {
        "a": {
            "decision": "agree",
            "by": "Gina",
            "at": datetime(2026, 10, 2, tzinfo=UTC),
            "should_be": "WD-04 TOE KICK",
        },
        "c": {"decision": "agree", "by": "Gina", "at": "2026-10-02"},
    }
    cards = learned.cards_of_answer("Job One", findings, decisions)
    assert len(cards) == 1
    assert cards[0].words == "Toe kick: no finish tag. It should be: WD-04 TOE KICK."
    assert cards[0].type == "D: toe kick finish tag missing"
    assert cards[0].item_kind == "cabinet" and cards[0].at.startswith("2026-10-02")


# ---- top-1 past wording --------------------------------------------------------------------------

TOE = "D: toe kick finish tag missing"


def _ex(
    project: str,
    words: str,
    *,
    item: str = "cabinet",
    view: str = "section",
    at: str = "2026-10-01",
) -> Example:
    return Example(
        type=TOE,
        part="toe kick",
        item_kind=item,
        view_kind=view,
        words=words,
        project=project,
        at=at,
    )


def test_top_one_never_comes_from_the_same_project() -> None:
    pool = [_ex("Job One", "own answer", at="2026-10-30"), _ex("Job Two", "other job")]
    best = learned.top_example(pool, TOE, project="Job One")
    assert best is not None and best.words == "other job"
    assert learned.top_example(pool[:1], TOE, project="job one") is None


def test_a_project_named_in_an_upload_is_excluded_and_unnamed_projects_never_count() -> None:
    pool = [_ex("Job Two", "two"), _ex("", "nameless")]
    assert learned.top_example(pool, TOE, project="Test run", uploads=["Job Two p449.pdf"]) is None


def test_top_one_prefers_item_kind_then_view_then_the_most_used_wording() -> None:
    pool = [
        _ex("P2", "WD-0x TOE KICK", item="countertop", at="2026-10-30"),
        _ex("P3", "Add WD-0x to the toe kick", view="plan"),
        _ex("P4", "Add WD-0x to the toe kick", view="plan"),
        _ex("P5", "Toe kick tag", view="section", at="2026-09-01"),
    ]
    best = learned.top_example(pool, TOE, project="Job One", item="cabinet", views={"plan"})
    assert best is not None and best.words == "Add WD-0x to the toe kick"
    best = learned.top_example(pool, TOE, project="Job One", item="cabinet", views={"section"})
    assert best is not None and best.words == "Toe kick tag"


def test_page_examples_one_per_type_on_the_page_never_an_approved_type() -> None:
    support = Example(
        type="B: support missing",
        part="support",
        item_kind="countertop",
        view_kind="plan",
        words="Provide Support",
        project="P2",
        at="2026-10-01",
    )
    rules = Learned(examples=(_ex("P2", "a"), _ex("P3", "b"), support), never=NEVER)
    found = rules.page_examples(["TOE KICK", "WD-04"], project="Job One")
    assert [e.type for e in found] == [TOE]
    found = rules.page_examples(["TOE KICK", "LEG"], project="Job One")
    assert {e.type for e in found} == {TOE, "B: support missing"}
    approved = Learned(examples=rules.examples, approved=frozenset({TOE}), never=NEVER)
    assert [e.type for e in approved.page_examples(["TOE KICK"], project="Job One")] == []


def test_reviewer_blocks_fence_the_wording_and_list_approved_types() -> None:
    rules = Learned(
        examples=(_ex("P2", "Add WD-0x to the toe kick"),), approved=frozenset({KEY}), never=NEVER
    )
    run = SimpleNamespace(
        review=SimpleNamespace(found=[{"text": "TOE KICK"}], questions=[]),
        engine=_engine([]),
    )
    blocks = learned.reviewer_blocks(run, rules)
    text = "\n".join(b["text"] for b in blocks)
    assert "Add WD-0x to the toe kick" in text and 'source="learned_examples"' in text
    assert KEY in text and 'source="learned_suppressed"' in text
    # The page is from the example's own project: no example.
    run.engine.settings.identity.project = "P2"
    assert all("learned_examples" not in b["text"] for b in learned.reviewer_blocks(run, rules))


def test_learning_is_off_without_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(learned.ENV_DIR, raising=False)
    assert learned.current() is None
    run = SimpleNamespace(review=SimpleNamespace(found=[], questions=[]), engine=_engine([]))
    assert learned.reviewer_blocks(run) == []
    assert learned.drop_suppressed(_engine([card("Metal trim", "no MT-1 tag")])) == 0


# ---- the files -----------------------------------------------------------------------------------


def test_files_round_trip_through_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = [decided(KEY, "not_a_problem", f"P{n}") for n in range(3)]
    cards.append(decided(TOE, "agree", "P7", words="Add WD-0x to the toe kick"))
    proposals, examples = learned.files_of(
        cards, NEVER, lambda _by: True, approved=frozenset({TOE})
    )
    assert [r["type"] for r in proposals["types"] if r["status"] == "proposed"] == [KEY]
    assert proposals["stale_approvals"] == [TOE]
    events = learned.approve([], KEY, by="Anant", proposed=proposals["types"], never=NEVER)
    (tmp_path / learned.EXAMPLES_FILE).write_text(json.dumps(examples))
    (tmp_path / learned.APPROVED_FILE).write_text(
        json.dumps({"format": learned.APPROVED_FORMAT, "events": events})
    )
    monkeypatch.setenv(learned.ENV_DIR, str(tmp_path))
    read = learned.current()
    assert read is not None
    assert read.approved == {KEY}
    assert [e.words for e in read.examples] == ["Add WD-0x to the toe kick"]
    assert read.examples[0].part == "toe kick"


def test_a_damaged_approvals_file_suppresses_nothing(tmp_path: Path) -> None:
    (tmp_path / learned.APPROVED_FILE).write_text("{not json")
    assert learned.read_folder(tmp_path).approved == frozenset()


def test_a_part_card_is_one_type_whatever_its_material() -> None:
    wood = card("Toe kick: no finish tag", "Needs WD-04 like the fronts.")
    laminate = card("Toe kick: no finish tag", "Needs PL-2 laminate like the fronts.")
    assert learned.type_key(wood) == learned.type_key(laminate) == "D: toe kick finish tag missing"


def test_a_finish_type_named_by_its_material_shows_where_the_page_prints_that_material() -> None:
    metal = Example(
        type=KEY,
        part="",
        item_kind="cabinet",
        view_kind="elevation",
        words="Add MT-0x to the trim",
        project="P2",
        at="2026-10-01",
    )
    rules = Learned(examples=(metal,), never=NEVER)
    assert rules.page_examples(["WD-04", "TOE KICK"], project="Job One") == []
    assert rules.page_examples(["MT-01 BRUSHED", "TRIM"], project="Job One") == [metal]
