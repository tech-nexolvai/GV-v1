"""What the review gate must refuse.

The gate exists because a merge happened ten minutes after a review nobody read (#487), so the cases
that matter here are the ones that *look* fine: a review that exists but covers an older commit, and a
review with comments still open. A gate that only catches "no review at all" would have let #484
through, because #484 had a review.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from review_gate import REVIEWER, Verdict, explain, main

HEAD = "db3ac35b8e2052a4d802131575c7fdde17aec7f7"
OLDER = "ead0a021d317118ab78a886804e4876ce2192c7c"


def _verdict(
    *,
    reviewed_head: bool,
    reviews: tuple[tuple[str, str], ...] = (),
    unresolved: tuple[tuple[str, int | None], ...] = (),
) -> Verdict:
    return Verdict(head=HEAD, reviewed_head=reviewed_head, reviews=reviews, unresolved=unresolved)


def test_a_reviewed_head_with_nothing_open_passes() -> None:
    verdict = _verdict(reviewed_head=True, reviews=((HEAD, "2026-09-03T10:13:57Z"),))
    assert verdict.ok
    assert HEAD[:7] in explain(verdict)


def test_a_review_of_an_earlier_commit_does_not_count() -> None:
    """The failure this gate was written for, and the one a tick beside the PR hides.

    #484 was reviewed. The review was real, it found five things, and merging still lost them. A gate
    that asked only "has this been reviewed?" would answer yes here, which is why the question is
    "has *this commit* been reviewed?" — and why pushing a fix has to invalidate the answer.
    """
    verdict = _verdict(reviewed_head=False, reviews=((OLDER, "2026-09-03T10:13:57Z"),))
    assert not verdict.ok
    message = explain(verdict)
    assert OLDER[:7] in message and "earlier commit" in message


def test_unresolved_threads_block_even_when_the_head_was_reviewed() -> None:
    """Reviewed is not read. Five actionable comments arrived on #484 and were merged over."""
    verdict = _verdict(
        reviewed_head=True,
        reviews=((HEAD, "2026-09-03T10:13:57Z"),),
        unresolved=(("app/evidence/record.py", 97), ("workflow/stages.py", 438)),
    )
    assert not verdict.ok
    message = explain(verdict)
    assert "app/evidence/record.py:97" in message
    assert "workflow/stages.py:438" in message
    assert "2 unresolved" in message


def test_no_review_at_all_says_how_to_get_one() -> None:
    """The plan declines automatic review, so "none yet" is the normal state, not an error.

    The message has to say what to do about it, because the setting that used to imply this happened
    by itself was inert for the life of the file.
    """
    verdict = _verdict(reviewed_head=False)
    assert not verdict.ok
    message = explain(verdict)
    assert REVIEWER in message
    assert "@coderabbitai review" in message


def test_an_unresolved_thread_with_no_line_is_still_reported() -> None:
    """A file-level comment has `line: null`. Formatting it as `path:None` would be a lie about where
    it is, and dropping it would hide an open thread — so it is reported as the path alone."""
    verdict = _verdict(
        reviewed_head=True,
        reviews=((HEAD, "2026-09-03T10:13:57Z"),),
        unresolved=(("workflow/stages.py", None),),
    )
    message = explain(verdict)
    assert "workflow/stages.py" in message
    assert "None" not in message


@pytest.mark.parametrize("argv", [["review_gate.py"], ["review_gate.py", "not-a-number"]])
def test_a_bad_invocation_exits_two_rather_than_reporting_a_verdict(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2, not 1. A caller that cannot tell "gate failed" from "you typed it wrong" will read a
    usage error as a blocked merge, and the next step after a blocked merge is to go look at reviews
    that are not the problem."""
    assert main(argv) == 2
    assert "usage" in capsys.readouterr().err
