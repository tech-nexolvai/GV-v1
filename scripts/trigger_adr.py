"""Draft the ADR that a crossed upgrade trigger demands, for the admin to ratify or reject.

`docs/DESIGN_CONTROLS.md` §6 defers Temporal, Qdrant, OpenSearch, GraphRAG, MCP, managed Postgres
and a self-hosted VLM — each with a **measured trigger** rather than a "never". A crossed threshold
therefore drafts an ADR, not a ticket: adopting Temporal is an architecture decision and belongs in
the same record as every other one.

**The failure this exists to prevent is not adopting the wrong technology.** It is re-arguing the
same question every few months with nobody able to say what was measured last time. So the draft
carries the numbers, and a rejection is written down as firmly as an acceptance.

**Only the admin ratifies.** Everything here writes `Status: Proposed`, and `write_outcome` refuses
to write `Accepted` at all — `scripts/ratify.py` requires an ADR that already says `Accepted` before
it will unblock anything, so a drafter that could set it would defeat the whole gate. Rejection *is*
writable, because a rejection nobody recorded is the state this story exists to remove.

**Why this takes plain values rather than a `Trigger` object.** `#269`'s plan sketched
`draft_trigger_adr(trigger: Trigger, measurement: Measurement, ...)`. Both types belong to `#267`
(`app/telemetry/triggers.py`), which is not built. Rather than guess at them, this module takes the
handful of strings and numbers an ADR actually needs, and `#267`'s types adapt at the call site. An
ADR drafter writes markdown; coupling it to a telemetry type buys nothing and would have to be
unpicked when `#267` lands and disagrees.

(The plan's `Measurement` was also the wrong type in a way worth stating: `units/measurement.py`
defines one, but its `Unit` is `MM | INCH` — an exact *dimension*, load-bearing in the verdict path
under ADR-0001. A trigger measures concurrent packages and p95 latency, which are not lengths.)

Source: system design §15; `CONTRIBUTING.md` ADR process · Verification: `tests/test_trigger_adr.py`
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "docs" / "adr"
TEMPLATE = "TEMPLATE.md"

#: `0007-applicability-resolver-interface.md` — four digits, a hyphen, a slug.
ADR_FILENAME = re.compile(r"^(?P<number>\d{4})-(?P<slug>[a-z0-9-]+)\.md$")

#: A whole `**Status:** …` line, however the emphasis fell, **including any trailing HTML comment**.
#: Mirrors `scripts/ratify.py`, which strips markdown before matching because the status has been
#: written both `**Status:** X` and `**Status**: X`.
#:
#: Consuming to end of line matters: an earlier version stopped at `<`, so rewriting the status left
#: the template's original comment in place and appended a second one beside it. The tests passed
#: because they asked whether `**Status:** Rejected` appeared, not whether the line was intact.
STATUS_LINE = re.compile(r"^\*{0,2}Status\*{0,2}:?\*{0,2}\s*:?\s*.*$", re.MULTILINE)


class Outcome(StrEnum):
    """What the admin decided. `ACCEPTED` is deliberately absent — see `write_outcome`."""

    PROPOSED = "Proposed"
    REJECTED = "Rejected"


class AdrNumberingError(Exception):
    """The ADR sequence is not in a state a new number can safely be allocated from."""


class RatificationRefused(Exception):
    """Something tried to have this module accept a decision. Only the admin does that."""


@dataclass(frozen=True, slots=True)
class TriggerEvidence:
    """What was measured, in the terms `docs/DESIGN_CONTROLS.md` §6 uses.

    Every field is required. There are no defaults anywhere in here: a drafted ADR with an invented
    threshold is worse than no ADR, because it reads like a measurement.
    """

    trigger: str
    """The measured quantity, named as §6 names it — "workflow recovery interventions"."""

    upgrade: str
    """What crossing it gates — "Temporal". The thing the ADR is actually about."""

    threshold: str
    """The documented limit, as authored. A string because §6's thresholds are phrases as often as
    numbers, and reformatting somebody's stated threshold is a way to change it by accident."""

    measured: str
    """What was actually observed. The reason this is an ADR and not a ticket."""

    window: str
    """Over what period. A threshold about a sustained condition means nothing without it — one
    spike and a fortnight of pressure are different arguments."""

    source: str
    """Where the number came from — the story or module that measured it, so a reader can check."""


def next_adr_number(adr_dir: Path = ADR_DIR) -> int:
    """The next free number in the sequence, refusing to guess when the sequence is broken.

    Highest existing plus one, rather than "count the files": a gap would otherwise re-issue a number
    that a superseded ADR still holds in every document that cites it.

    Raises when two files claim one number. That has happened once already — `D11` appears on
    `ADR-0007` while an earlier draft also claimed it — and the whole point of an ADR is that a
    citation resolves to exactly one record.
    """
    seen: dict[int, list[str]] = {}
    for path in sorted(adr_dir.glob("*.md")):
        match = ADR_FILENAME.match(path.name)
        if match is None:
            continue  # TEMPLATE.md and README.md are not numbered records.
        seen.setdefault(int(match.group("number")), []).append(path.name)

    collisions = {number: names for number, names in seen.items() if len(names) > 1}
    if collisions:
        raise AdrNumberingError(
            f"two ADRs claim the same number: {collisions}. A citation has to resolve to one "
            "record, so nothing new is numbered until that is settled."
        )
    return max(seen, default=0) + 1


def slugify(text: str) -> str:
    """A filename fragment. Lossy on purpose — the title carries the meaning, not the path."""
    lowered = re.sub(r"[^a-z0-9]+", "-", text.lower())
    slug = lowered.strip("-")
    if not slug:
        raise ValueError(f"{text!r} contains nothing usable in a filename")
    return slug


def draft_trigger_adr(
    evidence: TriggerEvidence,
    *,
    date: str,
    adr_dir: Path = ADR_DIR,
    issue: str | None = None,
) -> Path:
    """Write a `Proposed` ADR carrying the measurement, and return where it went.

    `date` is passed in rather than read from the clock. A drafter that stamps "now" produces a
    different file every run, which makes the record impossible to test and easy to regenerate on
    top of itself.

    Refuses to overwrite. An ADR is a record; silently replacing one loses the argument it held.
    """
    number = next_adr_number(adr_dir)
    path = adr_dir / f"{number:04d}-{slugify(f'adopt-{evidence.upgrade}')}.md"
    if path.exists():  # pragma: no cover - `next_adr_number` makes this unreachable today
        raise AdrNumberingError(f"{path.name} already exists; refusing to overwrite a record")

    decides = f"trigger: {evidence.trigger}" + (f" ({issue})" if issue else "")
    path.write_text(
        f"""# ADR-{number:04d} — Adopt {evidence.upgrade}? A measured trigger has been crossed

**Status:** {Outcome.PROPOSED.value}        <!-- Proposed | Accepted | Rejected | Superseded by ADR-NNNN -->
**Date:** {date}
**Decides:** {decides}
**Deciders:** admin

> Drafted automatically because a measured upgrade trigger crossed its threshold. A coding agent may
> draft this record. Only the admin may set `Status: Accepted`, and `scripts/ratify.py` refuses to
> unblock anything until it reads Accepted.

## Context

`docs/DESIGN_CONTROLS.md` §6 defers **{evidence.upgrade}** rather than ruling it out, on a measured
trigger. That trigger has been crossed.

| | |
|---|---|
| Measured quantity | {evidence.trigger} |
| Documented threshold | {evidence.threshold} |
| **Measured** | **{evidence.measured}** |
| Over | {evidence.window} |
| Source | {evidence.source} |

The numbers are here rather than in a linked dashboard because the point of this record is that in
six months somebody can see what was true when the question was asked, without needing the dashboard
to still exist.

## Options considered

1. **Adopt {evidence.upgrade}** — the documented upgrade path for this trigger. Takes on the
   operational cost the deferral was avoiding.
2. **Do not adopt, and record why** — the deferral stands. Write the reason down, so this is not
   re-argued from scratch the next time the threshold is crossed.
3. **Change the threshold** — only if the threshold itself was wrong. Say what makes it wrong;
   moving a limit because it was reached is how a measured trigger becomes decoration.

## Decision

_The admin's ruling. Nothing here has been decided — this file was written by a script that measured
something._

## Consequences

_What becomes easier, what becomes harder, what this forbids._

## Safety impact

_Effect on the critical false-PASS rate. If none, say so and why. Adopting infrastructure usually has
none directly; the honest answer is often "no direct effect, and here is the indirect one"._

## Unblocks

_Which issues become implementable if this is accepted._
""",
        encoding="utf-8",
    )
    return path


def write_outcome(path: Path, outcome: Outcome, *, reason: str) -> None:
    """Record the admin's ruling on a drafted ADR. Never `Accepted`.

    Acceptance is `scripts/ratify.py`'s business and the admin's alone. If this could write it, the
    gate that requires an accepted ADR before unblocking work could be satisfied by the same
    automation that asked the question.

    Rejection is writable, and that is the point of the function: an upgrade argued and declined has
    to leave a record, or it gets re-argued from nothing in six months.
    """
    if outcome is not Outcome.PROPOSED and outcome is not Outcome.REJECTED:  # pragma: no cover
        raise RatificationRefused(f"{outcome} is not a status this module may write")

    text = path.read_text(encoding="utf-8")
    if not STATUS_LINE.search(text):
        raise AdrNumberingError(f"{path.name} has no Status line; it is not an ADR this can amend")

    updated = STATUS_LINE.sub(
        f"**Status:** {outcome.value}        <!-- Proposed | Accepted | Rejected -->", text, count=1
    )
    marker = "## Decision"
    if marker not in updated:
        raise AdrNumberingError(f"{path.name} has no '## Decision' section to record the reason in")
    updated = updated.replace(
        marker,
        f"{marker}\n\n**{outcome.value}.** {reason}\n\n<!-- original placeholder follows -->",
        1,
    )
    path.write_text(updated, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--trigger", required=True, help="the measured quantity, as §6 names it")
    parser.add_argument("--upgrade", required=True, help="what crossing it gates, e.g. Temporal")
    parser.add_argument("--threshold", required=True, help="the documented limit, as authored")
    parser.add_argument("--measured", required=True, help="what was actually observed")
    parser.add_argument("--window", required=True, help="over what period")
    parser.add_argument("--source", required=True, help="what measured it")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD; not read from the clock")
    parser.add_argument("--issue", help="the issue that observed the crossing")
    args = parser.parse_args(argv)

    evidence = TriggerEvidence(
        trigger=args.trigger,
        upgrade=args.upgrade,
        threshold=args.threshold,
        measured=args.measured,
        window=args.window,
        source=args.source,
    )
    try:
        path = draft_trigger_adr(evidence, date=args.date, issue=args.issue)
    except (AdrNumberingError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return 1

    sys.stdout.write(
        f"Drafted {path.relative_to(REPO_ROOT)} with Status: Proposed.\n\n"
        "Nothing is decided. Fill in Decision, Consequences, Safety impact and Unblocks, then "
        "either set Status: Accepted yourself and run scripts/ratify.py, or record the rejection — "
        "a declined upgrade needs a record too, or the same argument runs again in six months.\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
