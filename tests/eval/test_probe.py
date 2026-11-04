"""The recall probe: page choice, price, scoring and the fact cache (harness rebuild, Phase 1).

Nothing here calls a model, opens a drawing, touches a database or reaches the network.
"""

from __future__ import annotations

import json
import re

import pytest

from eval.exam.truth import Miss
from eval.probe import cost, factcache, facts, pages, preflight, score
from eval.probe.config import QUESTION_LIST_ENV, Arm

#: The same shape test the exam uses: letters and digits in one token is what a drawing number,
#: item tag or sheet id looks like.
CODE_LIKE = re.compile(r"\b(?=[A-Za-z0-9.\-/]*[A-Za-z])(?=[A-Za-z0-9.\-/]*\d)[A-Za-z0-9.\-/]{3,}\b")
OURS = re.compile(r"^(set[A-Z](-(run\d+|flag|pick-\d+\w*|m\d+|probe))?|pack-harvest-\d+)$")


def _miss(miss_id: str, job: str, page_list: tuple[int, ...], kind: str, cues: tuple[str, ...]):
    return Miss(
        miss_id=miss_id,
        job=job,
        pages=page_list,
        kind=kind,
        severity="should-flag",
        description="",
        where="",
        cues=cues,
        source="",
    )


# --- the committed data files -------------------------------------------------------------------


@pytest.mark.parametrize("path", [pages.JOBS, cost.CALIBRATION], ids=lambda path: path.name)
def test_no_client_text_in_the_probe_data(path):
    """A job table and a price list must carry no drawing code and no client name."""
    text = path.read_text(encoding="utf-8")
    found = [token for token in CODE_LIKE.findall(text) if not OURS.match(token)]
    assert not found, f"{path.name} holds drawing-code-shaped tokens: {sorted(set(found))[:10]}"


def test_every_job_in_the_exam_has_a_page_count():
    """A probe cannot pick control pages for a job whose size it does not know."""
    from eval.exam.truth import load_misses

    known = pages.load_jobs()
    assert {miss.job for miss in load_misses()} <= set(known)
    assert all(count > 0 for count in known.values())


def test_the_calibration_is_only_runs_that_were_really_paid_for():
    raw = json.loads(cost.CALIBRATION.read_text(encoding="utf-8"))
    for row in raw["points"]:
        assert row["pages"] > 0
        assert row["cost_usd"] > 0
        assert row["seconds"] > 0


# --- picking the pages --------------------------------------------------------------------------


def test_a_cross_sheet_conflict_needs_every_one_of_its_sheets():
    """The problem is the disagreement between the sheets, so one of them tests nothing."""
    misses = [_miss("x-m1", "x", (4, 9), "cross-sheet conflict", ("a",))]
    units = pages.units_for("x", misses)
    assert units[0].relational
    assert not units[0].covered_by(frozenset({4}))
    assert units[0].partly_by(frozenset({4}))
    assert units[0].covered_by(frozenset({4, 9}))


def test_a_repeated_defect_needs_only_one_of_its_sheets():
    misses = [_miss("x-m1", "x", (4, 9, 12), "gv rule", ("a",))]
    chosen = pages.select("x", misses, vendor_pages=20, controls=0)
    assert len(chosen.problem_pages) == 1
    assert chosen.covered == ("x-m1",)


def test_the_cover_drops_a_page_it_does_not_need():
    """A greedy pass can pick a page a later pick makes redundant; it is pruned, not paid for."""
    misses = [
        _miss("x-m1", "x", (1, 2), "gv rule", ("a",)),
        _miss("x-m2", "x", (2, 3), "gv rule", ("b",)),
        _miss("x-m3", "x", (2,), "gv rule", ("c",)),
    ]
    chosen = pages.select("x", misses, vendor_pages=20, controls=0)
    assert chosen.problem_pages == (2,)
    assert len(chosen.covered) == 3


def test_capping_the_pages_says_which_problems_dropped_out():
    """A recall score whose denominator moved silently is not a score."""
    misses = [_miss(f"x-m{n}", "x", (n,), "gv rule", ("a",)) for n in range(1, 6)]
    chosen = pages.select("x", misses, vendor_pages=20, max_pages=2, controls=0)
    assert len(chosen.problem_pages) == 2
    assert len(chosen.covered) == 2
    assert len(chosen.uncovered) == 3
    assert set(chosen.covered) | set(chosen.uncovered) == set(chosen.asked)


def test_control_pages_never_carry_a_known_problem():
    misses = [_miss("x-m1", "x", (3,), "gv rule", ("a",)), _miss("x-m2", "x", (9,), "gv rule", ())]
    chosen = pages.select("x", misses, vendor_pages=12, controls=3)
    assert len(chosen.control_pages) == 3
    assert not set(chosen.control_pages) & {3, 9}
    assert chosen.pages == tuple(sorted(set(chosen.problem_pages) | set(chosen.control_pages)))


def test_the_same_ask_gives_the_same_probe():
    """Run to run, a difference has to come from the finding stage, never from a new page."""
    misses = [_miss(f"x-m{n}", "x", (n * 2,), "gv rule", ("a",)) for n in range(1, 6)]
    first = pages.select("x", misses, vendor_pages=30, controls=4)
    again = pages.select("x", misses, vendor_pages=30, controls=4)
    assert first == again


def test_a_real_job_needs_far_fewer_pages_for_one_family_than_for_all_of_them():
    """The measured reason to probe one family at a time."""
    from eval.exam.truth import load_misses

    misses = load_misses()
    size = pages.load_jobs()["setA"]
    family = pages.select("setA", misses, vendor_pages=size, kinds=frozenset({"gv rule"}))
    everything = pages.select("setA", misses, vendor_pages=size)
    assert len(family.problem_pages) <= 5
    assert family.uncovered == ()
    assert len(everything.problem_pages) > 3 * len(family.problem_pages)


# --- the price ------------------------------------------------------------------------------------


def test_the_price_is_fitted_to_the_runs_that_were_given_a_page_list():
    points = cost.load_points()
    line = cost.fit(points)
    assert line.points == sum(1 for point in points if point.picked)
    assert line.per_page_usd > 0


def test_a_bigger_probe_costs_more_and_never_more_than_the_ceiling():
    small = cost.estimate(5)
    large = cost.estimate(15)
    assert large.cost_usd > small.cost_usd
    assert small.cost_usd <= small.ceiling_usd
    assert large.cost_usd <= large.ceiling_usd


def test_repeats_and_arms_multiply_the_bill_and_the_clock():
    one = cost.estimate(6)
    six = cost.estimate(6, repeats=3, arms=2)
    assert six.runs == 6
    assert six.total_cost_usd == pytest.approx(one.cost_usd * 6)
    assert six.total_seconds == pytest.approx(one.seconds * 6)


def test_a_page_count_past_the_evidence_is_marked_as_an_extrapolation():
    assert not cost.estimate(4).extrapolated
    assert cost.estimate(40).extrapolated


def test_the_ceiling_comes_from_the_repo_s_own_money_rules():
    ceiling, where = cost.ceiling_usd(10)
    assert ceiling > 0
    assert "money rules" in where or "written defaults" in where


def test_the_cost_of_a_run_is_read_from_the_run_s_own_usage():
    """Never re-priced from tokens: a rate-card change cannot make an old probe look cheaper."""
    events = [
        {"event": "usage", "data": {"cost_usd": 1.5}},
        {"event": "usage", "data": {"cost_usd": 4.25}},
        {"event": "scope", "data": {"pages": [1, 2, 3]}},
        {"event": "message_end", "data": {"cost_usd": 4.25}},
    ]
    point = cost.actual_from_events(events, seconds=600)
    assert point.cost_usd == pytest.approx(4.25)
    assert point.pages == 3
    assert point.picked


# --- the preflight --------------------------------------------------------------------------------


def test_a_full_disk_stops_a_paid_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "free_gb", lambda _path: 1.0)
    report = preflight.check(api=None, disk_path=tmp_path, want_docker=False)
    assert not report.ok
    assert any("GB free" in problem for problem in report.problems)


def test_a_healthy_disk_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "free_gb", lambda _path: 50.0)
    assert preflight.check(api=None, disk_path=tmp_path, want_docker=False).ok


def test_the_probe_never_starts_the_app_it_only_looks(tmp_path, monkeypatch):
    """A probe that started a server would run code nobody chose."""
    monkeypatch.setattr(preflight, "free_gb", lambda _path: 50.0)
    report = preflight.check(api="http://127.0.0.1:1", disk_path=tmp_path, want_docker=False)
    assert not report.ok
    assert any("start the app yourself" in problem for problem in report.problems)


# --- the arms -------------------------------------------------------------------------------------


def test_an_arm_naming_a_question_list_that_is_not_there_is_called_out(tmp_path):
    """Two identical arms must never be mistaken for a result."""
    arm = Arm(name="candidate", question_list=tmp_path / "nothing.yaml")
    assert any("no question list" in line for line in arm.warnings())
    assert QUESTION_LIST_ENV not in arm.environment()


def test_an_arm_with_no_question_list_asks_for_nothing_extra():
    assert Arm(name="today").environment() == {}


# --- scoring ----------------------------------------------------------------------------------------


def _finding(ident: str, page: int, text: str, group: str = "likely_problem"):
    return {"id": ident, "page": page, "why": text, "proof": {"group": group}}


def _selection(**over):
    base = {
        "job": "x",
        "problem_pages": (3,),
        "control_pages": (8,),
        "covered": ("x-m1",),
        "partly": (),
        "uncovered": (),
        "asked": ("x-m1",),
    }
    base.update(over)
    return pages.Selection(**base)


def test_a_problem_found_in_some_repeats_and_not_others_is_flaky_not_a_pass():
    """The finding stage is not repeatable; a single pass/fail is not a measurement."""
    miss = _miss("x-m1", "x", (3,), "gv rule", ("sink", "cut-out", "narrow"))
    hit = [_finding("F1", 3, "the sink cut-out is narrow")]
    runs = [
        score.RepeatFindings(repeat=1, findings=hit),
        score.RepeatFindings(repeat=2, findings=[]),
        score.RepeatFindings(repeat=3, findings=hit),
    ]
    result = score.score_arm("today", _selection(), runs, [miss])
    assert result.problems[0].found == 2
    assert result.problems[0].verdict == "flaky"
    assert result.mean_recall == pytest.approx(2 / 3)
    assert result.best_recall == pytest.approx(1.0)
    assert result.problems[0].evidence == ["r1:F1", "r3:F1"]


def test_flagging_the_control_pages_shows_up_as_noise():
    """Recall bought by shouting has to be visible, and this is where."""
    miss = _miss("x-m1", "x", (3,), "gv rule", ("sink", "cut-out", "narrow"))
    loud = [
        _finding("F1", 3, "the sink cut-out is narrow"),
        _finding("F2", 8, "something"),
        _finding("F3", 8, "something else"),
    ]
    result = score.score_arm(
        "loud", _selection(), [score.RepeatFindings(repeat=1, findings=loud)], [miss]
    )
    assert result.mean_recall == pytest.approx(1.0)
    assert result.control_noise == pytest.approx(2.0)


def test_an_answer_that_never_reached_the_problems_list_is_not_noise():
    miss = _miss("x-m1", "x", (3,), "gv rule", ("sink",))
    quiet = [_finding("F2", 8, "nothing to compare", group="not_compared")]
    result = score.score_arm(
        "quiet", _selection(), [score.RepeatFindings(repeat=1, findings=quiet)], [miss]
    )
    assert result.control_noise == pytest.approx(0.0)


def test_more_recall_bought_with_more_noise_is_not_called_better():
    miss = _miss("x-m1", "x", (3,), "gv rule", ("sink", "cut-out", "narrow"))
    before = score.score_arm(
        "today", _selection(), [score.RepeatFindings(repeat=1, findings=[])], [miss]
    )
    after = score.score_arm(
        "candidate",
        _selection(),
        [
            score.RepeatFindings(
                repeat=1,
                findings=[
                    _finding("F1", 3, "the sink cut-out is narrow"),
                    _finding("F2", 8, "a thing"),
                    _finding("F3", 8, "another thing"),
                ],
            )
        ],
        [miss],
    )
    verdict = score.compare(before, after)
    assert verdict["gained"] == ["x-m1 (0->1 of 1)"]
    assert verdict["control_noise_delta"] == pytest.approx(2.0)
    assert "shouting" in verdict["verdict"]


def test_a_problem_whose_sheets_are_only_half_here_is_not_scored():
    chosen = _selection(covered=(), partly=("x-m1",), asked=("x-m1",))
    result = score.score_arm("today", chosen, [score.RepeatFindings(1, [])], [])
    assert result.scoreable == 0
    assert result.partly == ("x-m1",)


# --- the fact cache ---------------------------------------------------------------------------------


def test_the_key_changes_with_the_page_the_prompt_and_the_model():
    base = {"digest": "aa", "prompt_version": "v1", "model": "m"}
    same = factcache.key_for(**base)
    assert factcache.key_for(**base) == same
    assert factcache.key_for(**{**base, "digest": "bb"}) != same
    assert factcache.key_for(**{**base, "prompt_version": "v2"}) != same
    assert factcache.key_for(**{**base, "model": "other"}) != same


def test_facts_come_back_exactly_as_they_went_in(tmp_path):
    entry = factcache.store(
        {"vendor_numbers": [{"text": '24"'}]},
        digest="abc",
        prompt_version="v1",
        model="m",
        where=tmp_path,
        cost_usd=0.9,
    )
    back = factcache.load(entry.key, where=tmp_path)
    assert back is not None
    assert back.facts == {"vendor_numbers": [{"text": '24"'}]}
    assert back.cost_usd == pytest.approx(0.9)
    assert factcache.stats(tmp_path).entries == 1


def test_an_entry_of_an_older_shape_reads_as_a_miss(tmp_path):
    entry = factcache.store({"a": 1}, digest="abc", prompt_version="v1", model="m", where=tmp_path)
    path = tmp_path / entry.key[:2] / f"{entry.key}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["fact_version"] = factcache.FACT_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert factcache.load(entry.key, where=tmp_path) is None


def test_a_missing_or_damaged_entry_is_a_miss_not_a_crash(tmp_path):
    assert factcache.load("deadbeef", where=tmp_path) is None
    (tmp_path / "de").mkdir()
    (tmp_path / "de" / "deadbeef.json").write_text("{not json", encoding="utf-8")
    assert factcache.load("deadbeef", where=tmp_path) is None


def test_a_crop_set_hashes_differently_from_the_crops_joined_up():
    """Without a length in front of each part, two different splits hash alike."""
    assert factcache.page_digest_of([b"ab", b"c"]) != factcache.page_digest_of([b"a", b"bc"])


# --- harvesting a pack --------------------------------------------------------------------------------


def _pack(tmp_path):
    (tmp_path / "dims.json").write_text(
        json.dumps(
            {
                "version": 4,
                "files": ["v.pdf", "a.pdf"],
                "pages": [
                    {
                        "file": "v.pdf",
                        "page": 1,
                        "drawn": "done",
                        "labels": [
                            {"id": "D1", "text": '24"', "inches": "24", "side": "vendor"},
                            {"id": "D2", "text": '12"', "inches": "12", "side": "architect"},
                        ],
                        "phrases": [{"text": "PLAN VIEW", "box": [0, 0, 1, 1]}],
                        "scale_notes": [],
                    },
                    {"file": "v.pdf", "page": 2, "drawn": "done", "labels": []},
                    {
                        "file": "a.pdf",
                        "page": 1,
                        "drawn": "done",
                        "labels": [
                            {"id": "D9", "text": '36"', "inches": "36", "side": "architect"}
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "page_map.json").write_text(
        json.dumps(
            {
                "version": 26,
                "files": ["v.pdf", "a.pdf"],
                "upload_roles": {"v.pdf": "vendor", "a.pdf": "architect"},
                "pages": [
                    {"file": "v.pdf", "page": 1, "role": "vendor", "headings": ["PLAN"]},
                    {"file": "v.pdf", "page": 2, "role": "general", "headings": []},
                    {"file": "a.pdf", "page": 1, "role": "architect", "headings": []},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "page_texts.json").write_text(
        json.dumps({"pages": [{"file": "v.pdf", "page": 1, "text": "PLAN VIEW"}]}),
        encoding="utf-8",
    )
    (tmp_path / "annotations.json").write_text(
        json.dumps([{"file": "v.pdf", "page": 1, "kind": "Square", "text": "a reviewer's note"}]),
        encoding="utf-8",
    )
    return tmp_path


def test_a_page_s_facts_come_out_of_the_pack(tmp_path):
    harvested = facts.harvest_page(_pack(tmp_path), "v.pdf", 1)
    assert [number["text"] for number in harvested.facts["vendor_numbers"]] == ['24"']
    assert harvested.facts["page_text"] == "PLAN VIEW"
    assert "vendor_numbers" in harvested.kinds


def test_the_reviewer_s_own_marks_are_counted_and_never_read_as_a_fact(tmp_path):
    """Settled 2026-09-09: the markup is the answer, never the reading source."""
    harvested = facts.harvest_page(_pack(tmp_path), "v.pdf", 1)
    assert harvested.gv_markup == 1
    assert "a reviewer's note" not in json.dumps(harvested.as_dict())


def test_the_vendor_upload_keeps_its_title_sheets(tmp_path):
    """A schedule inside the vendor's file is marked `general`; a probe still runs on it."""
    harvested = facts.harvest_pack(_pack(tmp_path), role="vendor")
    assert [page.page for page in harvested] == [1, 2]


def test_coverage_names_the_pages_with_nothing_read_on_them(tmp_path):
    report = facts.coverage(facts.harvest_pack(_pack(tmp_path), role="vendor"))
    assert report.pages == 2
    assert report.per_kind["vendor_numbers"] == 1
    assert report.empty_pages == (2,)


def test_the_architect_side_is_reported_apart_from_the_vendor_side(tmp_path):
    """A question comparing with the architect needs the paired sheet known *and* read."""
    report = facts.architect_cover(_pack(tmp_path), [1, 2], {1: 1})
    assert report["architect_page_known"] == 1
    assert report["architect_page_read"] == 1
    assert report["no_architect_page"] == [2]
