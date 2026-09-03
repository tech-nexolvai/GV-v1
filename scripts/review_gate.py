"""Refuse to merge a pull request no reviewer has read.

**Written because it happened.** CodeRabbit reviewed #484 at 10:13:57Z with five actionable findings,
three of them real. The pull request was merged at 10:23:58Z, ten minutes later, by somebody whose
merge gate was "CI is green" — and CI cannot see a review. It finishes before the review arrives and
knows nothing about it either way. Three defects went onto `main` past a wall of green ticks (#487).

On this plan the review has to be asked for and lands a minute or two after the request, which is the
window that swallowed it. `.github/workflows/ci.yml` now sends the request; this decides whether the
answer came back before anything gets merged.

**The head SHA is the whole point.** A review of an earlier commit is not a review of what is about to
land, and GitHub shows both the same way — a tick beside the PR. So a review is only counted when it
was submitted against the exact commit being merged, and pushing a fix invalidates it, because it
should: nobody has read the fix.

Usage: `python scripts/review_gate.py <pr-number>` — needs `gh` authenticated. Exit 0 means a review
covering this head commit exists and every actionable comment on it is resolved.

Verification: `tests/test_review_gate.py`
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

#: The reviewer whose verdict this gate is looking for. Named rather than "any review", because a
#: self-approval is not the second pair of eyes this is protecting.
REVIEWER = "coderabbitai[bot]"


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether the head commit has been reviewed, and what is outstanding if so."""

    head: str
    reviewed_head: bool
    #: Reviews by `REVIEWER` against any commit, newest first, as (sha, submitted_at).
    reviews: tuple[tuple[str, str], ...]
    #: Unresolved review threads, as (path, line).
    unresolved: tuple[tuple[str, int | None], ...]

    @property
    def ok(self) -> bool:
        return self.reviewed_head and not self.unresolved


def _gh(*args: str) -> str:
    """One `gh` call, with a failure that says which call failed rather than a bare CalledProcessError."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def assess(pull_request: int) -> Verdict:
    """Read the pull request's review state without deciding anything about it."""
    head = json.loads(_gh("pr", "view", str(pull_request), "--json", "headRefOid"))["headRefOid"]

    reviews = [
        (entry["commit_id"], entry["submitted_at"])
        for entry in json.loads(
            _gh("api", f"repos/{{owner}}/{{repo}}/pulls/{pull_request}/reviews")
        )
        if entry.get("user", {}).get("login") == REVIEWER
    ]
    reviews.sort(key=lambda entry: entry[1], reverse=True)

    # Unresolved threads are read through GraphQL, because the REST comment list has no resolution
    # state at all — every comment looks open there, including the ones already dealt with.
    query = """
      query($owner:String!, $name:String!, $number:Int!) {
        repository(owner:$owner, name:$name) {
          pullRequest(number:$number) {
            reviewThreads(first:100) {
              nodes { isResolved isOutdated comments(first:1) { nodes { path line } } }
            }
          }
        }
      }
    """
    threads = json.loads(
        _gh(
            "api",
            "graphql",
            "-F",
            "owner={owner}",
            "-F",
            "name={repo}",
            "-F",
            f"number={pull_request}",
            "-f",
            f"query={query}",
        )
    )["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

    unresolved: list[tuple[str, int | None]] = []
    for thread in threads:
        if thread["isResolved"]:
            continue
        comments = thread["comments"]["nodes"]
        if not comments:
            continue
        unresolved.append((comments[0]["path"], comments[0]["line"]))

    return Verdict(
        head=head,
        reviewed_head=any(sha == head for sha, _ in reviews),
        reviews=tuple(reviews),
        unresolved=tuple(unresolved),
    )


def explain(verdict: Verdict) -> str:
    """What is wrong, in the terms somebody about to merge needs — never a bare boolean."""
    if verdict.ok:
        return f"{REVIEWER} reviewed {verdict.head[:7]} and nothing is unresolved."

    lines: list[str] = []
    if not verdict.reviewed_head:
        if verdict.reviews:
            reviewed = ", ".join(sha[:7] for sha, _ in verdict.reviews[:3])
            lines.append(
                f"No review of the head commit {verdict.head[:7]}. {REVIEWER} has reviewed "
                f"{reviewed} — an earlier commit, so the change about to land is unread."
            )
        else:
            lines.append(
                f"{REVIEWER} has not reviewed this pull request at all. On this plan the review is "
                'requested, not automatic — comment "@coderabbitai review" and wait for it.'
            )
    if verdict.unresolved:
        where = "\n  ".join(
            f"{path}:{line}" if line is not None else path for path, line in verdict.unresolved
        )
        lines.append(f"{len(verdict.unresolved)} unresolved review thread(s):\n  {where}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("usage: python scripts/review_gate.py <pr-number>", file=sys.stderr)
        return 2
    verdict = assess(int(argv[1]))
    print(explain(verdict))
    return 0 if verdict.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main(sys.argv))
