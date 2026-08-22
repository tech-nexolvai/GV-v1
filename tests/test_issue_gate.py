"""The finish step, and the guard that stops it clearing live work.

`--start` and `--review` were a one-way trip: they set a `state:` label and nothing ever took it off.
By the time this was noticed, **111 closed issues** still carried one — 24 saying `in-progress`, 25
saying `in-review` — while no open issue carried either. The labels that answer "what is being worked
on right now?" answered it exactly wrongly, and the more work the project finished the wronger they
got.

So the tests here are about the two ways this step can be wrong, not about the happy path. It has to
clear a finished issue, and it has to refuse an open one — because clearing live work makes it look
unclaimed, and an unclaimed issue looks normal. A guard that fails towards "looks normal" is one
nobody catches.

Every test is offline. `gh` is never invoked: the issue is a dict and the write is a recorded call, so
this suite can never edit the real board while proving what the step does to it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "issue_gate", Path(__file__).resolve().parents[1] / "scripts" / "issue_gate.py"
)
assert _SPEC and _SPEC.loader
issue_gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(issue_gate)


def _issue(state: str = "closed", *labels: str) -> dict[str, Any]:
    """One issue as the GitHub API returns it — labels as objects, not strings."""
    return {"state": state, "labels": [{"name": name} for name in labels]}


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str | None, bool]]:
    """Every `set_state` call the step makes, so a no-op can be told from a write."""
    calls: list[tuple[int, str | None, bool]] = []
    monkeypatch.setattr(
        issue_gate,
        "set_state",
        lambda number, state, assign_self: calls.append((number, state, assign_self)),
    )
    return calls


# ---------------------------------------------------------------------------
# It clears a finished issue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["state:in-progress", "state:in-review", "state:todo"])
def test_a_closed_issue_has_its_execution_state_cleared(
    label: str, recorded: list[tuple[int, str | None, bool]]
) -> None:
    """Whichever of the three it is holding. The drift arrived as all three."""
    assert issue_gate.finish(207, _issue("closed", "type:story", label)) == issue_gate.READY
    assert recorded == [(207, None, False)]


def test_the_state_is_removed_rather_than_replaced_with_a_done_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no `state:done` in the label set, and adding one would answer a different question.
    `state:` answers "is this being worked?" — for closed work that is the absence of a label.

    Asserted on the payload that actually reaches GitHub, with the real `set_state` running, rather
    than on the argument handed to it. Passing `None` is only correct if `None` removes the label, and
    that is the half a mocked `set_state` cannot show.
    """
    sent: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        sent.append(kwargs.get("input", ""))

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(
        issue_gate,
        "gh_json",
        lambda path: [{"name": "status:ready"}, {"name": "state:in-review"}],
    )
    monkeypatch.setattr(issue_gate.subprocess, "run", fake_run)

    issue_gate.finish(1, _issue("closed", "state:in-review"))

    payload = json.loads(sent[0])["labels"]
    assert payload == ["status:ready"], "the state label must be dropped, not swapped for another"
    assert not any(label.startswith("state:") for label in payload)


def test_the_other_labels_are_not_touched(recorded: list[tuple[int, str | None, bool]]) -> None:
    """`set_state` keeps everything that is not a `state:` label, and this step asks it to keep them.

    `status:ready` in particular must survive: readiness records that the dependencies were settled,
    which stays true for ever. Conflating it with execution state is what the two label families exist
    to avoid.
    """
    issue = _issue("closed", "status:ready", "owner:dev", "P1", "state:in-progress")
    assert issue_gate.finish(207, issue) == issue_gate.READY
    assert recorded == [(207, None, False)], "the step must not pass a replacement label"


def test_the_assignee_is_left_alone(recorded: list[tuple[int, str | None, bool]]) -> None:
    """It records who did the work, and that stays true after the merge. `assign_self` is False."""
    issue_gate.finish(207, _issue("closed", "state:in-progress"))
    assert recorded[0][2] is False


# ---------------------------------------------------------------------------
# It refuses live work
# ---------------------------------------------------------------------------


def test_an_open_issue_is_refused(
    recorded: list[tuple[int, str | None, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    """**The failure this guard exists for.** Clearing the state of live work makes it look unclaimed,
    and an unclaimed issue looks entirely normal — so the mistake would sit there unnoticed while two
    people picked up the same story."""
    assert issue_gate.finish(207, _issue("open", "state:in-progress")) == issue_gate.BLOCKED
    assert recorded == [], "an open issue must not be written to at all"
    assert "still open" in capsys.readouterr().err


def test_the_refusal_says_what_to_do_next(capsys: pytest.CaptureFixture[str]) -> None:
    """A refusal that does not name the next action gets worked around rather than followed."""
    issue_gate.finish(207, _issue("open", "state:in-review"))
    message = capsys.readouterr().err
    assert "Close the" in message and "Closes #N" in message


def test_the_refusal_exit_code_is_the_blocked_one() -> None:
    """The script's exit code is its contract, and callers already treat 2 as "stop"."""
    assert issue_gate.finish(207, _issue("open", "state:in-progress")) == issue_gate.BLOCKED
    assert issue_gate.BLOCKED != issue_gate.READY


# ---------------------------------------------------------------------------
# It does nothing when there is nothing to do
# ---------------------------------------------------------------------------


def test_an_already_clean_issue_is_a_no_op(
    recorded: list[tuple[int, str | None, bool]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Running it twice, or on an issue that was never claimed, must not spend an API call writing the
    labels back unchanged — and must not read as a failure either."""
    assert issue_gate.finish(207, _issue("closed", "type:story", "P1")) == issue_gate.READY
    assert recorded == []
    assert "Nothing to clear" in capsys.readouterr().out


def test_an_issue_with_no_labels_at_all_is_handled(
    recorded: list[tuple[int, str | None, bool]],
) -> None:
    """The API omits the key rather than sending an empty list in some responses."""
    assert issue_gate.finish(207, {"state": "closed"}) == issue_gate.READY
    assert recorded == []


# ---------------------------------------------------------------------------
# It does not depend on the contract
# ---------------------------------------------------------------------------


def test_a_finished_issue_is_cleared_even_with_no_agent_contract(
    monkeypatch: pytest.MonkeyPatch, recorded: list[tuple[int, str | None, bool]]
) -> None:
    """**Why this runs before the contract is parsed.** The other state flags apply at the end of the
    READY path, so they need a well-formed contract and a ready status. An issue that has already
    landed has nothing to prove on either count, and requiring them would mean an issue whose contract
    was edited after the merge could never be tidied — which is exactly the issue most likely to need
    it.

    Driven through `main`, because the ordering inside `main` is the thing under test.
    """
    monkeypatch.setattr(
        issue_gate,
        "gh_json",
        lambda path: _issue("closed", "state:in-progress") | {"title": "x", "body": "no contract"},
    )
    monkeypatch.setattr("sys.argv", ["issue_gate.py", "207", "--done"])

    assert issue_gate.main() == issue_gate.READY
    assert recorded == [(207, None, False)]


def test_a_blocked_issue_can_still_be_finished(
    monkeypatch: pytest.MonkeyPatch, recorded: list[tuple[int, str | None, bool]]
) -> None:
    """A story closed as superseded may well still say `blocked-client`. Readiness answers "may work
    start?" and is the wrong question to ask about work that has stopped."""
    body = "## Agent contract\n\n```yaml\nstatus: blocked-client\n```\n"
    monkeypatch.setattr(
        issue_gate,
        "gh_json",
        lambda path: _issue("closed", "state:in-progress") | {"title": "x", "body": body},
    )
    monkeypatch.setattr("sys.argv", ["issue_gate.py", "207", "--done"])

    assert issue_gate.main() == issue_gate.READY
    assert recorded == [(207, None, False)]


def test_a_pull_request_number_is_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The early `--done` dispatch must not jump the check that the number names an issue."""
    monkeypatch.setattr(
        issue_gate, "gh_json", lambda path: {"pull_request": {}, "state": "closed", "labels": []}
    )
    monkeypatch.setattr("sys.argv", ["issue_gate.py", "357", "--done"])

    assert issue_gate.main() == issue_gate.MALFORMED


# ---------------------------------------------------------------------------
# The lifecycle is documented where somebody will read it
# ---------------------------------------------------------------------------


def test_contributing_documents_all_three_steps() -> None:
    """The drift happened because the documented lifecycle stopped at two steps, so a third that only
    exists in the code would leave the same gap for the next person."""
    text = (Path(__file__).resolve().parents[1] / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for flag in ("--start", "--review", "--done"):
        assert f"issue_gate.py 40 {flag}" in text, f"{flag} is not in the worked example"


# ---------------------------------------------------------------------------
# It refuses an issue whose work is already done (#219)
# ---------------------------------------------------------------------------

#: A contract complete enough to reach READY. The design fields are not padding — the gate blocks without
#: them, and rightly, because it refuses to let anyone invent a module or an interface. My first version of
#: this fixture omitted them and the open-issue test below blocked: the gate was right and the test wrong.
_READY_CONTRACT = """## Agent contract

```yaml
status: ready
owner: dev
requires: []
implements: storage/hashing.py
design: docs/DESIGN_PLATFORM.md §7
verification: tests/storage/test_hashing.py
```

## Scope

Something to build.
"""


def _gate_on(monkeypatch: pytest.MonkeyPatch, state: str) -> int:
    """Run the gate against one issue in `state`, with no network and no writes."""
    monkeypatch.setattr(
        issue_gate,
        "gh_json",
        lambda _path: {
            "state": state,
            "title": "C5.2 — Content hashing and versioned keys",
            "body": _READY_CONTRACT,
            "labels": [],
        },
    )
    monkeypatch.setattr(issue_gate.sys, "argv", ["issue_gate.py", "219"])
    return int(issue_gate.main())


def test_a_closed_issue_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The case that happened.** Asked about #219, the gate said "READY — #219 may be implemented".

    The story was finished: `storage/hashing.py` was written, tested and merged. Nothing in the contract
    recorded that, because `status:` says whether a story was *ready to pick up*, not whether it is done —
    so the gate read `status: ready`, agreed, and would have had a second implementation of a
    safety-critical file written over a working one.
    """
    assert _gate_on(monkeypatch, "closed") == issue_gate.CLOSED_ALREADY


def test_closed_is_its_own_answer_rather_than_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Already done" and "not yet allowed" must not share an exit code.

    Reporting BLOCKED would send somebody hunting for a dependency to wait on when there is nothing left
    to do at all.

    Both halves are asserted, because the negative alone is vacuous: before the fix this returned READY,
    which is also "not BLOCKED", so a test with only the second assertion passed while the bug was live.
    """
    code = _gate_on(monkeypatch, "closed")
    assert code == issue_gate.CLOSED_ALREADY
    assert code not in {issue_gate.BLOCKED, issue_gate.MALFORMED, issue_gate.ADMIN_ONLY}


def test_an_open_ready_issue_is_still_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not turn the gate into a wall: the ordinary path still opens."""
    assert _gate_on(monkeypatch, "open") == issue_gate.READY


def test_a_closed_issue_is_refused_even_with_no_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The closed check must come *before* contract parsing, and this is what pins the order.

    The tests above use a valid contract, so if a later change parsed the contract first they would still
    pass — review's point. An issue with no contract at all can only reach CLOSED_ALREADY if the state is
    read first; if parsing moved ahead of it, this returns MALFORMED instead.
    """
    monkeypatch.setattr(
        issue_gate,
        "gh_json",
        lambda _path: {"state": "closed", "title": "Done long ago", "body": "", "labels": []},
    )
    monkeypatch.setattr(issue_gate.sys, "argv", ["issue_gate.py", "219"])
    assert int(issue_gate.main()) == issue_gate.CLOSED_ALREADY


@pytest.mark.parametrize("state", [None, "", "merged", "locked", 42, [], {}, ["open"]])
def test_an_unrecognised_state_is_malformed_not_ready(
    state: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state the gate does not understand must never reach READY.

    `== "closed"` let everything else fall through to the readiness path, so a missing or renamed state
    would have been answered with "implement it". A gate that fails toward "go ahead" on an input it did not
    understand is the wrong way round — the same shape as the bug this check was added to fix.
    """
    monkeypatch.setattr(
        issue_gate,
        "gh_json",
        lambda _path: {
            "state": state,
            "title": "Odd",
            "body": _READY_CONTRACT,
            "labels": [],
        },
    )
    monkeypatch.setattr(issue_gate.sys, "argv", ["issue_gate.py", "219"])
    code = int(issue_gate.main())
    assert code == issue_gate.MALFORMED
    assert code != issue_gate.READY


@pytest.mark.parametrize("state", [None, "", "merged", 42, [], {}])
def test_done_also_refuses_an_unrecognised_state(
    state: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--done` reads the state too, so it must not be reached with one nobody can read.

    It refuses anything that is not closed, so an unrecognised value would have been reported as "this
    issue is still open" — a specific claim about something unknown. Validating above the dispatch rather
    than inside each path is what makes that impossible.
    """
    monkeypatch.setattr(
        issue_gate,
        "gh_json",
        lambda _path: {"state": state, "title": "Odd", "body": _READY_CONTRACT, "labels": []},
    )
    monkeypatch.setattr(issue_gate.sys, "argv", ["issue_gate.py", "219", "--done"])
    assert int(issue_gate.main()) == issue_gate.MALFORMED


@pytest.mark.parametrize("title", [None, "", "   ", 42, []])
def test_a_payload_with_no_usable_title_is_malformed(
    title: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`iss["title"]` raised `KeyError` on an absent field — a traceback where an exit code was needed.

    A gate that crashes has not answered the question it was asked. "It blew up" tells the caller nothing
    about whether to start work, so every unusable payload becomes MALFORMED, which does.
    """
    payload: dict[str, object] = {"state": "open", "body": _READY_CONTRACT, "labels": []}
    if title is not None:
        payload["title"] = title
    monkeypatch.setattr(issue_gate, "gh_json", lambda _path: payload)
    monkeypatch.setattr(issue_gate.sys, "argv", ["issue_gate.py", "219"])
    assert int(issue_gate.main()) == issue_gate.MALFORMED


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"state": "open", "title": "T", "body": [], "labels": []}, id="body-is-a-list"
        ),
        pytest.param(
            {"state": "open", "title": "T", "body": 42, "labels": []}, id="body-is-a-number"
        ),
        pytest.param(
            {"state": "open", "title": "T", "body": "x", "labels": "ready"}, id="labels-is-a-string"
        ),
        pytest.param(
            {"state": "open", "title": "T", "body": "x", "labels": [{"nom": "ready"}]},
            id="label-without-a-name",
        ),
        pytest.param(
            {"state": "open", "title": "T", "body": "x", "labels": ["ready"]},
            id="bare-string-label",
        ),
    ],
)
def test_a_malformed_payload_is_malformed_not_a_traceback(
    payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every field this gate reads, in a shape it cannot use.

    These are the paths that used to be dereferenced before anything could return MALFORMED: a non-string
    `body` reached the contract parser, and a malformed `labels` reached the label reads. Each was a
    traceback, and a gate that crashes has not answered the question it was asked.

    The model is what makes this a list of parameters rather than a list of hand-written guards — the point
    of declaring the shape once is that the cases nobody thought of are covered too.
    """
    monkeypatch.setattr(issue_gate, "gh_json", lambda _path: payload)
    monkeypatch.setattr(issue_gate.sys, "argv", ["issue_gate.py", "219"])
    assert int(issue_gate.main()) == issue_gate.MALFORMED


@pytest.mark.parametrize("payload", [None, [], "closed", 42])
def test_a_payload_that_is_not_an_object_is_malformed(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not just the fields — the payload itself. `iss.get(...)` on a list raises `AttributeError`."""
    monkeypatch.setattr(issue_gate, "gh_json", lambda _path: payload)
    monkeypatch.setattr(issue_gate.sys, "argv", ["issue_gate.py", "219"])
    assert int(issue_gate.main()) == issue_gate.MALFORMED


@pytest.mark.parametrize("stdout", ["", "   ", "unknown\tSomething", "garbage"])
def test_a_dependency_whose_state_cannot_be_read_blocks(
    stdout: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unverifiable dependency is not a satisfied one.

    `partition("\t")` on unparseable output leaves the state as something that is simply not "open", so it
    used to read as landed and work was allowed to start — the same fail-toward-"go ahead" direction as the
    closed-issue bug this change is about. The module already states the rule for a failed lookup; this
    applies it to a successful lookup nobody can interpret.
    """

    class _Result:
        returncode = 0

        def __init__(self, out: str) -> None:
            self.stdout = out

    monkeypatch.setattr(issue_gate.subprocess, "run", lambda *a, **k: _Result(stdout))
    blocking = issue_gate.open_issue_dependencies(["#42"])
    assert blocking, f"{stdout!r} must block, not read as landed"
    assert "unknown" in blocking[0] or "cannot read" in blocking[0]


def test_a_dependency_that_is_closed_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: a genuinely landed dependency must still pass, or the guard above would just
    block everything and look correct doing it."""

    class _Result:
        returncode = 0
        stdout = "closed\tAlready done"

    monkeypatch.setattr(issue_gate.subprocess, "run", lambda *a, **k: _Result())
    assert issue_gate.open_issue_dependencies(["#42"]) == []
