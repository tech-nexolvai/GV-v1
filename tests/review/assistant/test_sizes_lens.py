"""The sizes lens (`sizes_lens`): a sizes-only page reviewer after each page's reviewer, on what
the page's money left, with a scripted AI on made-up pages (no network, no paid call).

- Money: the page's reviewer runs first with the page's whole share, as before; the lens spends
  only what it left, so the page never passes its share; no money, no lens.
- Its look: its own short brief, the reviewer's tools; its first message gives code's size facts
  and the size issues already on the page; its coverage is the page's sizes.
- Its findings: sizes only, through the same gates (nothing new is red); marked as the lens's.
- One card: its items fold into the page reviewer's at the same spot.
- The log: lens_calls, lens_spent, lens_issues, lens_folded in the page's row, its spend apart.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import likely_problems, page_reviewer, proof, sizes_lens, view_questions
from app.review.assistant.chat_request import MODELS
from app.review.assistant.page_reviewer import Options, PageReview, Reviews
from tests.review.assistant.test_page_reviewer import (
    _FIXED,
    _PER_TOKEN,
    FILE,
    _Engine,
    _results,
    _Script,
    _string_issue,
    _text,
    _tool,
)

LENS_ON = Options(sizes_lens=True)


class _Both(_Script):
    """Answers the page's reviewer from `general` and the lens from `lens` (told apart by the
    lens's first message); each call is kept with who made it (`who`)."""

    def __init__(self, general: Sequence[Any], lens: Sequence[Any], cost: int = 100_000) -> None:
        super().__init__([], cost)
        self.general, self.lens = list(general), list(lens)
        self.who: list[str] = []

    def __call__(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        first = messages[0]["content"][0].get("text", "")
        mine = "for its SIZES only" in first
        self.who.append("lens" if mine else "page")
        self.turns = (
            [(self.lens if mine else self.general).pop(0)]
            if (self.lens if mine else self.general)
            else []
        )
        return super().__call__(messages)


def _page(
    tmp_path: Path,
    general: Sequence[Any],
    lens: Sequence[Any],
    *,
    cap: int = 5_000_000,
    cost: int = 100_000,
    options: Options = LENS_ON,
) -> tuple[_Engine, PageReview, Reviews, Any, tuple[str, bool, str | None]]:
    """One page's step as the roster runs it (`page_reviewer._loop`): its reviewer, then the
    lens when it is on."""
    script = _Both(general, lens, cost)
    engine = _Engine(tmp_path, script)
    review = PageReview(page=2, file=FILE, options=options)
    reviews = Reviews()
    reviews.pages["page-review-p2"] = review
    agent = SimpleNamespace(model=MODELS["claude-opus-5-5"], cap_micros=cap, spent=0, last_call=0)
    ended = page_reviewer._loop(review, reviews, engine, agent, 0, {"steps": []}, None)  # type: ignore[arg-type]
    return engine, review, reviews, agent, ended


def _overall_check(**more: Any) -> dict[str, Any]:
    """A size please-check on the made-up page's printed overall, boxed by its anchor."""
    given = {
        "kind": "A",
        "claim": "please_check",
        "item": "Elevation: overall width",
        "wrong": 'The overall 36 1/4" does not match the parts above it.',
        "read": '36 1/4"',
        "anchor": '36 1/4"',
        "box": {"x0": 240, "y0": 300, "x1": 320, "y1": 345},
        "confidence": "medium",
        **more,
    }
    return _tool("report_issue", **given)


def _lens_calls(engine: _Engine) -> list[Any]:
    script = engine.script
    assert isinstance(script, _Both)
    return [call for call, who in zip(script.calls, script.who, strict=True) if who == "lens"]


# ---- on and off, and money ------------------------------------------------------------------------


def test_off_by_default_the_page_has_its_reviewer_only(tmp_path: Path) -> None:
    from app.config import Settings

    assert Options().sizes_lens is False
    assert Settings.model_fields["assistant_sizes_lens"].default is False
    engine, _review, reviews, _agent, ended = _page(
        tmp_path, [[_text("Checked.")]], [[_text("never")]], options=Options()
    )
    assert engine.script.who == ["page"]  # type: ignore[attr-defined]
    assert reviews.lenses == {} and "lens_calls" not in reviews.block()["pages"][0]
    assert ended[1] is True and "Sizes lens" not in ended[0]


def test_the_lens_runs_after_the_reviewer_on_what_the_page_left(tmp_path: Path) -> None:
    engine, review, reviews, agent, ended = _page(
        tmp_path,
        [[_text("Checked the page.")]],
        [[_text("Checked the sizes.")]],
        cap=2_000_000,
        cost=300_000,
    )
    assert engine.script.who == ["page", "lens"]  # type: ignore[attr-defined]
    lens = reviews.lenses[2]
    assert lens.lens == "sizes" and lens.calls == 1 and review.calls == 1
    assert lens.knowledge["lens_money"] == 1.7  # the page's $2.00 less the reviewer's $0.30
    assert agent.spent == 600_000 == engine.spent  # the page's step carries both
    assert reviews.spent == {2: 300_000} and reviews.lens_spent == {2: 300_000}
    assert ended[1] is True and ended[2] is None
    assert "Page review of page 2" in ended[0] and "Sizes lens of page 2" in ended[0]


def test_the_page_never_passes_its_share_and_no_money_means_no_lens(tmp_path: Path) -> None:
    looking = [_tool("zoom", crops=[{"box": {"x0": 200, "y0": 280, "x1": 400, "y1": 350}}])]
    cost = _FIXED + 4_000 * _PER_TOKEN  # never more than a call's worst case
    engine, _review, reviews, agent, _ended = _page(
        tmp_path, [[_text("Checked.")]], [looking] * 60, cap=1_200_000, cost=cost
    )
    lens = reviews.lenses[2]
    assert lens.calls >= 2 and lens.stop == "cost_cap"
    assert agent.spent == engine.spent <= 1_200_000
    assert reviews.cut_by_money == []  # the lens's money stop is not the page's

    # The reviewer used the page's share: the lens makes no call.
    used, _review, reviews, agent, _ended = _page(
        tmp_path / "b", [[_text("Checked.")]], [looking], cap=300_000, cost=250_000
    )
    assert used.script.who == ["page"]  # type: ignore[attr-defined]
    assert reviews.lenses[2].calls == 0 and reviews.lenses[2].stop == "cost_cap"
    assert used.spent == agent.spent == 250_000


def test_no_lens_after_an_error_or_a_cancel(tmp_path: Path) -> None:
    engine = _Engine(tmp_path, _Script([]))
    review = PageReview(page=2, file=FILE, options=LENS_ON)
    reviews = Reviews()
    agent = SimpleNamespace(model=None, cap_micros=1_000_000, spent=0, last_call=0)
    for ended in (("x", False, "error"), ("x", False, "cancelled")):
        assert sizes_lens.after(ended, review, reviews, engine, agent, {}, 0) == ended  # type: ignore[arg-type]
    assert reviews.lenses == {} and not engine.script.calls


# ---- the look -------------------------------------------------------------------------------------


def test_the_lens_has_its_own_short_brief_the_same_tools_and_code_s_size_facts(
    tmp_path: Path,
) -> None:
    engine, *_ = _page(
        tmp_path, [[_overall_check()], [_text("Checked.")]], [[_text("Checked the sizes.")]]
    )
    general = engine.script.calls[0]
    (lens,) = _lens_calls(engine)
    assert lens.system == sizes_lens.SYSTEM != general.system
    assert "kind A only" in lens.system and len(lens.system) < len(page_reviewer._BRIEF) * 1.2
    assert lens.tools == general.tools and lens.effort == "high"
    first = lens.messages[0]["content"]
    assert re.search(r"reviewing page 2 of .* for its SIZES only", first[0]["text"])
    assert first[1]["type"] == "text" and first[2]["type"] == "image"  # the whole page
    assert sum(1 for b in first if b["type"] == "image") == 10  # and the nine tiles
    texts = "\n".join(b["text"] for b in first if b["type"] == "text")
    assert "Dimension values code found lined up" in texts  # the rows, added up by code
    # The reviewer's size issue is listed so the lens does not report it again.
    reported = texts.split("Size issues already reported on this page")[1]
    assert "Elevation: overall width" in reported
    assert first[-1]["text"].startswith("The nine tiles are above")


def test_the_lens_covers_the_pages_sizes_only(tmp_path: Path) -> None:
    _engine, _review, reviews, *_ = _page(tmp_path, [[_text("ok")]], [[_text("ok")]])
    shown = sorted(t["text"] for t in reviews.lenses[2].targets)
    assert shown == sorted(['12"', '12"', '11 3/4"', '36 1/4"'])  # no tag, title or item cell
    targets = [{"text": t, "box": [0, 0, 1, 1]} for t in ("ST-01", "TOE KICK", '5"', "[127]")]
    assert [t["text"] for t in sizes_lens.size_targets(targets)] == ['5"', "[127]"]


def test_code_lists_each_views_sizes_and_each_sections_depth_make_up() -> None:
    views = [
        view_questions.View("FRONT VIEW", "elevation", (0.0, 0.0, 400.0, 300.0)),
        view_questions.View("SECTION S1", "section", (500.0, 0.0, 700.0, 300.0)),
        view_questions.View("NOTES", "view", (800.0, 0.0, 900.0, 100.0)),
    ]

    def value(inches: str, view: int, turned: bool) -> view_questions.Value:
        return view_questions.Value(Decimal(inches), (0.0, 0.0, 1.0, 1.0), view, turned)

    ledger = view_questions.Ledger(
        views,
        values=[value("30", 0, False), value("15", 0, False), value("33.5", 0, True)]
        + [value("22", 1, False), value("17.5", 1, False), value("3", 1, False)],
    )
    assert sizes_lens.view_sizes(ledger).splitlines() == [
        "FRONT VIEW (elevation) at [0, 0, 400, 300]: across the page 30, 15; turned 33 1/2",
        "SECTION S1 (section) at [500, 0, 700, 300]: across the page 22, 17 1/2, 3; turned none",
    ]  # a view with no size is left out
    sister = view_questions.Makeup(
        "SECTION S2", Decimal(22), (Decimal("17.5"), Decimal(3), Decimal("1.5")),
        (Decimal("17.5"), Decimal(3), Decimal("1.5")), (0.0, 0.0, 1.0, 1.0), 5, "XX-101", 0,
    )  # fmt: skip
    lines = sizes_lens.makeup_lines(view_questions.makeups(ledger), [sister])
    assert lines[0].startswith("This page, SECTION S1 at [500, 0, 700, 300]: overall 22;")
    assert "no printed depth parts add up to 22" in lines[0]
    assert lines[1] == (
        "p5 SECTION S2 (XX-101), the same item's other drawing: overall 22; "
        "17 1/2 + 3 + 1 1/2 = 22 (depth sizes printed: 17 1/2, 3, 1 1/2)"
    )


def _two_elevations() -> view_questions.Ledger:
    """Two elevations: a tag in one and not the other (a finish question), and a taller height in
    one (a size question)."""
    views = [
        view_questions.View("ELEVATION A", "elevation", (0.0, 0.0, 300.0, 300.0)),
        view_questions.View("ELEVATION B", "elevation", (400.0, 0.0, 700.0, 300.0)),
    ]
    box = (0.0, 0.0, 1.0, 1.0)
    return view_questions.Ledger(
        views,
        callouts=[
            view_questions.Callout("ZZ-01 TOE KICK", box, 0, ("TOE KICK",), ("ZZ-01",), None),
            view_questions.Callout("TOE KICK", box, 1, ("TOE KICK",), (), None),
        ],
        values=[
            view_questions.Value(Decimal(40), box, 0, True),
            view_questions.Value(Decimal(30), box, 1, True),
        ],
    )


def test_the_lens_gets_code_s_size_questions_but_those_its_reviewer_reported() -> None:
    ledger = _two_elevations()
    every = view_questions.questions(ledger)
    sizes = view_questions.questions(ledger, kinds=sizes_lens.LENS_KINDS)
    assert {q.kind for q in every} >= {"A", "D"}
    assert sizes and {q.kind for q in sizes} == {"A"} and [q.n for q in sizes] == [1]

    lens = PageReview(page=2, file=FILE, options=LENS_ON, lens="sizes")
    run = sizes_lens._LensRun.__new__(sizes_lens._LensRun)
    run.review = lens
    run.beside = None
    (block,) = run._questions(ledger, [])
    assert "Q1 [A]" in block["text"] and [q.n for q in lens.questions] == [1]
    # The page's reviewer reported that question: the lens is not asked it again.
    beside = PageReview(page=2, file=FILE, options=LENS_ON)
    beside.questions = every
    asked = next(q for q in every if q.kind == "A")
    beside.answers = {asked.label: "issue"}
    run.beside = beside
    assert run._questions(ledger, []) == [] and lens.questions == []


def test_already_reported_lists_the_open_size_issues_of_the_page() -> None:
    mark = {likely_problems.FIELD: {"kind": "size", "why": "Overall missing."}}
    box = {"x0": 10, "y0": 20, "x1": 30, "y1": 40}
    findings = [
        {"page": 2, "status": "check", "item": "Front", "vendor_box": box, **mark},
        {"page": 2, "status": "different", "item": "String", "source": "vendor_arithmetic",
         "why": "Parts add to 35."},
        {"page": 2, "status": "check", "item": "Tag", likely_problems.FIELD: {"kind": "finish"}},
        {"page": 3, "status": "check", "item": "Other page", **mark},
        {"page": 2, "status": "same", "item": "Fine", **mark},
    ]  # fmt: skip
    assert sizes_lens.already_reported(findings, 2) == [
        "[10, 20, 30, 40] Front: Overall missing.",
        "String: Parts add to 35.",
    ]


# ---- findings -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["B", "D", "E", "F"])
def test_the_lens_refuses_what_is_not_a_size_with_the_way_on(tmp_path: Path, kind: str) -> None:
    engine, _review, reviews, *_ = _page(
        tmp_path, [[_text("ok")]], [[_overall_check(kind=kind)], [_text("ok")]]
    )
    lens = reviews.lenses[2]
    (refused,) = _results(_lens_calls(engine)[1].messages)
    assert refused.startswith("Refused: this reviewer reports sizes only (kind A)")
    assert lens.refusals == {"report_issue: lens: not a size": 1}
    assert not lens.drafts and not engine.book.live()


def test_a_missing_dimension_reported_as_completeness_is_filed_as_a_size(tmp_path: Path) -> None:
    wrong = "The section prints no overall depth dimension."
    engine, _review, reviews, *_ = _page(
        tmp_path, [[_text("ok")]], [[_overall_check(kind="C", wrong=wrong)], [_text("ok")]]
    )
    (draft,) = reviews.lenses[2].drafts
    assert draft.kind == "A" and draft.also_kind == "C"  # the reviewer's C kept (plan M10)
    (found,) = engine.book.live()
    mark = found[likely_problems.FIELD]
    assert mark["kind"] == "size" and mark["lens"] == "sizes" and mark["by"] == "page_reviewer"
    assert mark["also_kind"] == "completeness"
    assert (likely_problems.payload(found) or {})["by"] == "page_reviewer"
    assert proof.proof_group(found) == "likely_problem"  # a please-check, never red


def test_nothing_new_is_red_a_lens_string_takes_the_same_proof_path(tmp_path: Path) -> None:
    unquoted = _tool(
        "report_issue",
        kind="A", claim="wrong", item="Depth", wrong="The depth is wrong.", read="",
        box={"x0": 800, "y0": 600, "x1": 850, "y1": 640}, confidence="high",
    )  # fmt: skip
    engine, *_ = _page(tmp_path, [[_text("ok")]], [[_string_issue(), unquoted], [_text("ok")]])
    refused = [r for r in _results(_lens_calls(engine)[1].messages) if r.startswith("Refused")]
    assert len(refused) == 1 and "two printed numbers" in refused[0]  # the same red gate
    (found,) = engine.book.live()
    assert found["source"] == "vendor_arithmetic" and found["architect"] == '35 3/4"'
    assert found[likely_problems.FIELD]["lens"] == "sizes"
    assert not proof.code_proved(found) and proof.proof_group(found) == "likely_problem"


# ---- one card -------------------------------------------------------------------------------------


def test_the_lens_folds_into_the_reviewers_card_of_the_same_size_issue(tmp_path: Path) -> None:
    other = _overall_check(
        read='11 3/4"', anchor='11 3/4"', wrong='The 11 3/4" part has no matching section size.'
    )
    engine, _review, reviews, *_ = _page(
        tmp_path,
        [[_overall_check()], [_text("ok")]],
        [[_overall_check(item="Front view: overall"), other], [_text("ok")]],
    )
    lens = reviews.lenses[2]
    cards = engine.book.live()
    (card,) = [c for c in cards if c[likely_problems.FIELD].get("lens") is None]
    folded, new = lens.drafts
    assert folded.joined == card["id"] and folded.finding_id is None
    assert lens.folds and lens.folds[0]["into"] == card["id"]
    assert new.joined is None and len(cards) == 2  # another size at another spot is its own


def test_a_lens_string_joins_the_reviewers_card_of_the_same_string(tmp_path: Path) -> None:
    """The one-card test (`one_card.same_item`) sees the other reviewer of the page; a reviewer's
    own earlier items stay out of it, as before."""

    def checked(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # The engine's own checks read the reviewer's string's values (`ChatEngine._checked`).
        (card,) = engine_box[0].book.live()
        engine_box[0].book.replace({**card, "inches": {"vendor": "36.25", "architect": "35.75"}})
        return [_string_issue()]

    engine_box: list[_Engine] = []
    original = _Engine.__init__

    def keep(self: _Engine, *a: Any, **k: Any) -> None:
        original(self, *a, **k)
        engine_box.append(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_Engine, "__init__", keep)
        engine, review, reviews, *_ = _page(
            tmp_path, [[_string_issue()], [_text("ok")]], [checked, [_text("ok")]]
        )
    (card,) = engine.book.live()
    assert card[likely_problems.FIELD].get("lens") is None
    (draft,) = reviews.lenses[2].drafts
    assert draft.joined == card["id"]
    run = page_reviewer._Run.__new__(page_reviewer._Run)
    run.review = review
    lens_card = {likely_problems.FIELD: {"lens": "sizes"}}
    assert run._foreign(lens_card) and not run._foreign(card)
    lens_run = sizes_lens._LensRun.__new__(sizes_lens._LensRun)
    lens_run.review = reviews.lenses[2]
    assert lens_run._foreign(card) and not lens_run._foreign(lens_card)


# ---- the log --------------------------------------------------------------------------------------


def test_the_log_row_of_a_page_adds_the_lens_and_keeps_its_spend_apart(tmp_path: Path) -> None:
    _engine, _review, reviews, *_ = _page(
        tmp_path,
        [[_overall_check()], [_text("ok")]],
        [
            [
                _overall_check(item="Front: overall"),
                _overall_check(read='11 3/4"', anchor='11 3/4"'),
            ],
            [_text("ok")],
        ],
        cap=2_000_000,
        cost=120_000,
    )
    (row,) = reviews.block()["pages"]
    assert row["spent_usd"] == 0.24  # the reviewer's two calls; the lens's never overwrite it
    assert row["lens_money"] == 1.76 and row["lens_calls"] == 2 and row["lens_spent"] == 0.24
    assert row["lens_issues"] == 2 and row["lens_folded"] == 1 and row["lens_kept"] == 1
    assert row["lens_stop"] is None and row["issues"] == 1
    assert reviews.spent == {2: 240_000} and reviews.lens_spent == {2: 240_000}


# ---- in the full check ----------------------------------------------------------------------------


def _turn(params: Mapping[str, Any], sizes: bool) -> list[dict[str, Any]]:
    """The scripted reviewer or lens inside a real full check: one issue, then done."""
    from tests.review.assistant.chat_fakes import reply, text, tool

    messages = params["messages"]
    if any(m["role"] == "assistant" for m in messages):
        return reply(text("Checked the page."))
    first = messages[0]["content"][0]["text"]
    page = re.search(r"reviewing page (\d+) ", first).group(1)  # type: ignore[union-attr]
    issue = {
        "kind": "A" if sizes else "E",
        "claim": "please_check",
        "item": f"Page {page}: " + ("overall missing" if sizes else "who supplies it"),
        "wrong": "No overall width is printed." if sizes else "Not said who supplies it.",
        "confidence": "medium",
        "box": {"x0": 20, "y0": 20, "x1": 200, "y1": 120},
    }
    return reply(tool("c", "report_issue", issue), stop="tool_use")


def test_a_full_check_with_the_lens_on_runs_it_on_each_pages_leftover_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.review.assistant.test_chat_engine import DOLLARS
    from tests.review.assistant.test_full_check_roster import (
        _clean_ink,
        _findings,
        _Roster,
        _roster,
    )

    class _WithLens(_Roster):
        def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            system = params["system"][0]["text"]
            if system.startswith("You are reviewing one page"):
                self._seen("page_review", params)
                if params["max_tokens"] == page_reviewer.WARM_TOKENS:
                    from tests.review.assistant.chat_fakes import reply, text

                    return reply(text("OK"))
                return _turn(params, sizes=False)
            if system == sizes_lens.SYSTEM:
                self._seen("sizes_lens", params)
                return _turn(params, sizes=True)
            return super().__call__(params)

    _clean_ink(monkeypatch)
    script = _WithLens()
    harness = _roster(tmp_path, script, price=12 * DOLLARS)
    engine = harness.engine
    options = Options(per_page_micros=500_000, sizes_lens=True)
    engine.settings = dataclasses.replace(engine.settings, page_reviewer=options)

    outcome = harness.run()

    reviewed = {
        int(re.search(r"reviewing page (\d+) ", p["messages"][0]["content"][0]["text"]).group(1))  # type: ignore[union-attr]
        for p in script.params["page_review"]
        if p["max_tokens"] != page_reviewer.WARM_TOKENS
    }
    lensed = {
        int(re.search(r"reviewing page (\d+) ", p["messages"][0]["content"][0]["text"]).group(1))  # type: ignore[union-attr]
        for p in script.params["sizes_lens"]
    }
    assert reviewed and lensed == reviewed  # a lens after each page's reviewer
    found = [f for f in _findings(outcome) if f.get("created_by") == "page_reviewer"]
    lens_found = [f for f in found if f[likely_problems.FIELD].get("lens") == "sizes"]
    assert len(lens_found) == len(lensed) and all(
        proof.proof_group(f) == "likely_problem" for f in found
    )
    assert all((likely_problems.payload(f) or {})["kind"] == "size" for f in lens_found)
    # The page's money is unchanged: the lens spends inside it.
    assert engine.own_share_micros == len(reviewed) * 500_000
    assert 0 < engine._own_spent <= engine.own_share_micros
    assert engine.counted_spent == engine.spent - engine._own_spent
