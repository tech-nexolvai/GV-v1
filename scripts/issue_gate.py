#!/usr/bin/env python3
"""Deterministic readiness gate for working a GitHub issue.

Usage:
    python scripts/issue_gate.py <issue-number> [--comment]

A coding agent MUST run this before writing any code for an issue. The gate — not
the agent — decides whether work may start. Exit code is the contract:

    0   READY      implement it; the brief is printed on stdout
    2   BLOCKED    stop; do not write code; the reason is printed on stderr
    3   ADMIN ONLY  a decision or client question; never the dev's to answer
    4   MALFORMED  the issue has no valid agent contract; ask the admin to fix it

`--comment` posts the stop reason to the issue so the block is visible and chaseable.

No third-party dependencies: standard library plus the `gh` CLI.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

REPO = "tech-nexolvai/GV-v1"

READY = 0
BLOCKED = 2
ADMIN_ONLY = 3
MALFORMED = 4

# status -> (exit code, plain-English explanation)
STATUS = {
    "ready": (READY, "All dependencies are settled. Implement it."),
    "needs-architecture": (
        BLOCKED,
        (
            "An architectural decision must be ratified first. Architecture comes before "
            "implementation — the admin owns that decision."
        ),
    ),
    "blocked-client": (
        BLOCKED,
        (
            "A client answer is required. The value or rule this issue depends on does not "
            "exist yet, and it must not be guessed."
        ),
    ),
    "blocked-data": (
        BLOCKED,
        "Real drawings or the gold set are required and have not arrived.",
    ),
    "deferred": (
        BLOCKED,
        "Deferred by the build order. Earlier phases must be proven first.",
    ),
    "epic": (
        BLOCKED,
        "This is an epic — a container, not a unit of work. Work one of its sub-issues.",
    ),
    "admin-only": (
        ADMIN_ONLY,
        (
            "This is a decision or a client question. It is the admin's to resolve and must "
            "never be answered by the dev or by a coding agent."
        ),
    ),
}

CONTRACT_RE = re.compile(r"##\s*Agent contract\s*\n+```yaml\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def gh_json(path: str):
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if p.returncode != 0:
        sys.stderr.write(f"gh api failed for {path}:\n{p.stderr}\n")
        sys.exit(MALFORMED)
    return json.loads(p.stdout)


def parse_contract(body: str) -> dict | None:
    """Parse the fenced contract block. Deliberately a tiny hand-rolled parser so the
    gate has no third-party dependency and cannot execute anything from the issue."""
    m = CONTRACT_RE.search(body or "")
    if not m:
        return None
    out: dict[str, object] = {}
    key = None
    for raw in m.group(1).splitlines():
        if raw.strip().startswith("#"):
            continue
        # A YAML comment starts at whitespace-'#'. A bare '#' inside a value is data —
        # issue references like 'D1 (#1)' must survive.
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and key:
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(line.lstrip()[2:].strip())
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            key = k.strip()
            v = v.strip()
            # An empty or '[]' value means "a list follows (or is empty)" — it must
            # become a list, not "", or the '- item' lines below have nowhere to go.
            out[key] = [] if v in ("", "[]") else v
    return out


def section(body: str, heading: str) -> str:
    """Extract one '## <heading>' section verbatim.

    The lookahead must stop at h2 *and* h3 ('### Definition of Done') and at the
    bold '**Source:**' trailer, or a section swallows everything after it.
    """
    m = re.search(
        rf"^#{{2,3}}\s*{re.escape(heading)}\s*$\n(.*?)(?=^#{{2,3}}\s|^\*\*Source:|^---\s*$|\Z)",
        body or "",
        re.DOTALL | re.MULTILINE,
    )
    return m.group(1).strip() if m else ""


def post_comment(number: int, text: str) -> None:
    p = subprocess.run(
        ["gh", "api", f"repos/{REPO}/issues/{number}/comments", "-X", "POST", "--input", "-"],
        input=json.dumps({"body": text}),
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        sys.stderr.write(f"warning: could not post comment: {p.stderr}\n")
    else:
        sys.stderr.write(f"Posted a comment on #{number}.\n")


def set_state(number: int, state: str | None, assign_self: bool) -> None:
    """Execution state, kept separate from readiness. Readiness answers 'may work start?';
    execution state answers 'is it being worked?'. Conflating them hides both."""
    cur = [l["name"] for l in gh_json(f"repos/{REPO}/issues/{number}/labels")]
    keep = [l for l in cur if not l.startswith("state:")]
    want = keep + ([f"state:{state}"] if state else [])
    subprocess.run(
        ["gh", "api", f"repos/{REPO}/issues/{number}", "-X", "PATCH", "--input", "-"],
        input=json.dumps({"labels": sorted(set(want))}),
        capture_output=True,
        text=True,
        check=False,
    )
    if assign_self:
        me = gh_json("user")["login"]
        subprocess.run(
            ["gh", "api", f"repos/{REPO}/issues/{number}/assignees", "-X", "POST", "--input", "-"],
            input=json.dumps({"assignees": [me]}),
            capture_output=True,
            text=True,
            check=False,
        )
        sys.stderr.write(f"Assigned #{number} to {me}, state -> {state}.\n")


def decision_brief(number: int, title: str, body: str) -> None:
    """Printed when the ADMIN works a decision or client question.

    The agent may *draft* the architecture; the admin *ratifies* it. That split keeps
    the judgement with the person who holds the plan context while still letting an
    agent do the writing.
    """
    print(f"DECISION WORK — #{number} (admin)")
    print("=" * 62)
    print(f"title: {title}\n")
    for h in (
        "Decision required",
        "Evidence (verified, `docs/V1_RESEARCH_AND_PLAN.md` §F1)",
        "Recommendation",
        "Consequence if rejected",
        "Question for Raj",
        "What we need",
        "Why it blocks",
        "Why it matters",
    ):
        s = section(body, h)
        if s:
            print(f"--- {h} ---\n{s}\n")
    ref = title.split()[0].rstrip(":—-").strip()
    print("WHAT TO DO")
    if ref.upper().startswith("D"):
        print("  1. Draft an ADR in docs/adr/ using docs/adr/TEMPLATE.md.")
        print("     Record context, the options considered, the decision and its consequences.")
        print("  2. The ADR is a PROPOSAL until the admin sets 'Status: Accepted'.")
        print("     A coding agent must NOT set it to Accepted on its own.")
        print(f"  3. Once accepted:  python scripts/ratify.py {ref} --adr docs/adr/<file>.md")
        print("     That rewrites every dependent story to 'status: ready' automatically.")
    else:
        print("  1. This needs an answer from the client. It cannot be derived from the repo.")
        print("  2. Do not assume a value. An assumed tolerance becomes a false PASS.")
        print(f'  3. When the client answers:  python scripts/ratify.py {ref} --answer "..."')
    print()
    print(
        f"  Preview what this unblocks:  python scripts/ratify.py {ref} --dry-run "
        + ("--adr <file>" if ref.upper().startswith("D") else "--answer x")
    )


def client_fact_verdict(requires: list[object]) -> tuple[list[str], list[str]]:
    """Resolve every `Qn` in `requires` against `docs/CLIENT_FACTS.md`.

    Returns (stopping, provisional) as printable lines. This is what stops a dependency being an
    argument: the file says whether an open question changes the *formula* or only supplies a
    *value*, and those are not the same kind of blocked. Three stories were held up by the second
    kind before this existed.
    """
    try:
        from client_facts import ClientFactsError, load, lookup
    except ImportError:  # pragma: no cover - the gate must work even if this module moves
        return [], []

    try:
        facts = load()
    except (ClientFactsError, OSError) as error:
        return [f"could not read the client facts: {error}"], []

    from client_facts import Blocks, Status

    stopping: list[str] = []
    provisional: list[str] = []
    for item in requires:
        text = str(item)
        fact = lookup(text, facts)
        if fact is None:
            # An unknown Qn fails CLOSED. Ignoring it would mean a typo silently removes a
            # dependency, which is the failure mode this file exists to end.
            if re.search(r"\bQ\d+\b", text):
                stopping.append(
                    f"{text} is not in docs/CLIENT_FACTS.md. Either the reference is wrong or the "
                    "question was never recorded — both mean nobody has checked whether it blocks."
                )
            continue
        if fact.stops_work:
            stopping.append(fact.gate_line())
        elif fact.status is Status.OPEN and fact.blocks is Blocks.VALUE:
            # Only these ship provisional. An ANSWERED fact blocks nothing, and a `blocks: nothing`
            # one never did — announcing either as "an unconfirmed value prevents release" would be
            # false.
            provisional.append(fact.gate_line())
    return stopping, provisional


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("issue", type=int)
    ap.add_argument("--comment", action="store_true", help="post the stop reason to the issue")
    ap.add_argument(
        "--role", choices=("dev", "admin"), default="dev", help="who is working this (default: dev)"
    )
    ap.add_argument(
        "--start",
        action="store_true",
        help="mark in-progress and assign to yourself (only if READY)",
    )
    ap.add_argument("--review", action="store_true", help="mark as in-review (PR opened)")
    args = ap.parse_args()

    iss = gh_json(f"repos/{REPO}/issues/{args.issue}")
    if "pull_request" in iss:
        sys.stderr.write(f"#{args.issue} is a pull request, not an issue.\n")
        return MALFORMED

    title = iss["title"]
    body = iss.get("body") or ""
    contract = parse_contract(body)

    if not contract or "status" not in contract:
        sys.stderr.write(
            f"MALFORMED  #{args.issue} {title}\n\n"
            "This issue has no '## Agent contract' block, so readiness cannot be\n"
            "determined. Do not guess. Ask the admin to add the contract.\n"
        )
        return MALFORMED

    status = str(contract["status"]).strip()
    if status not in STATUS:
        sys.stderr.write(
            f"MALFORMED  #{args.issue}: unknown status {status!r}.\n"
            f"Valid: {', '.join(sorted(STATUS))}\n"
        )
        return MALFORMED

    code, why = STATUS[status]
    requires = contract.get("requires") or []
    if isinstance(requires, str):
        requires = [requires]

    # ---------------- admin working a decision or client question ----------------
    # The dev is stopped here (exit 3). The admin is not: this is precisely their work,
    # and their agent may draft the ADR that unblocks everything downstream.
    if status == "admin-only" and args.role == "admin":
        decision_brief(args.issue, title, body)
        if args.start:
            set_state(args.issue, "in-progress", True)
        return READY

    # An admin hitting a needs-architecture story should be pointed at the decision,
    # not just told "blocked" — the decision is the actual next action.
    if status == "needs-architecture" and args.role == "admin":
        req = ", ".join(str(r) for r in requires)
        sys.stderr.write(
            f"NOT YET — #{args.issue} {title}\n{'=' * 62}\n"
            f"This story is waiting on architecture: {req or 'an unrecorded decision'}\n\n"
            "Work the decision first. For each one:\n"
            "  python scripts/issue_gate.py <decision-issue> --role admin\n\n"
            "Ratifying it will flip this story to 'status: ready' automatically.\n"
        )
        return BLOCKED

    # ---------------- client facts, before any path can report READY ----------------
    # This ran only on the already-blocked path, so a contract saying `status: ready` with
    # `requires: Q5` sailed through — Q5 being an open question that changes the calculation. The
    # whole point of docs/CLIENT_FACTS.md is that the status field stops being the last word, so it
    # has to be consulted before readiness is granted, not after it is denied.
    fact_stopping, fact_provisional = client_fact_verdict(requires)
    if fact_stopping:
        sys.stderr.write(
            f"STOP — #{args.issue} depends on a question that changes the calculation\n"
            f"{'=' * 62}\n"
            + "".join(f"  - {line}\n" for line in fact_stopping)
            + "\nSource: docs/CLIENT_FACTS.md, which is the authority. Do not infer the answer "
            "from the\nchecklist — it contradicts itself in several places, which is why that "
            "file exists.\n"
        )
        if args.comment:
            post_comment(
                args.issue,
                "🚫 **Blocked — an unanswered question changes the calculation**\n\n"
                + "\n".join(f"- {line}" for line in fact_stopping)
                + "\n\n_Source: `docs/CLIENT_FACTS.md`. Posted by `scripts/issue_gate.py`._",
            )
        return BLOCKED

    # ---------------- design must exist before implementation ----------------
    # 'ready' only ever meant "no decision or client answer outstanding". It said nothing
    # about whether the design exists — so an agent would invent the architecture per
    # issue and the pieces would not fit. Readiness now requires a design contract.
    design = str(contract.get("design") or "").strip()
    if code == READY and (not design or design.upper() == "MISSING"):
        # No architecture yet. The dev stops. The admin is instead told to draft it —
        # an agent may write the design; only the admin ratifies it.
        if args.role == "admin":
            print(f"DESIGN WORK NEEDED — #{args.issue} (admin)")
            print("=" * 62)
            print(f"title : {title}")
            print(f"implements: {contract.get('implements', '(not stated)')}\n")
            for h in ("Context", "Scope", "Acceptance criteria"):
                s = section(body, h)
                if s:
                    print(f"--- {h} ---\n{s}\n")
            print("DRAFT THE ARCHITECTURE FIRST — then implementation becomes possible.")
            print("  1. Add a section to docs/DESIGN.md covering, at minimum:")
            print("       - which module owns the code (exact file path)")
            print("       - the public interface: types and function signatures")
            print("       - what it may and may not import (see DESIGN.md §2)")
            print("       - how it connects to the types already defined there")
            print("  2. An agent MAY draft this. Only the admin ratifies it.")
            print("     If it changes a boundary or a golden rule, write an ADR too.")
            print("  3. Set the issue's contract fields:")
            print("       design: docs/DESIGN.md §<n>")
            print("       implements: <module path>")
            print("  4. Re-run this gate. It will then report READY.")
            print()
            print("Do NOT write implementation code before step 3. Designing while")
            print("implementing is how the interface ends up shaped by the first caller.")
            return READY
        sys.stderr.write(
            f"STOP — #{args.issue} has no design contract\n{'=' * 62}\n"
            f"  title : {title}\n\n"
            "This issue's dependencies are settled, but nothing states which module owns\n"
            "the code or what its interface is. Implementing it would mean inventing the\n"
            "architecture, and architecture is the admin's to define.\n\n"
            "WHAT TO DO INSTEAD:\n"
            "  - Do not start coding, and do not choose a module or interface yourself.\n"
            "  - Comment on the issue asking the admin for a design contract, or propose\n"
            "    one for review — clearly labelled as a proposal, not a decision.\n"
            "  - Admin path:  python scripts/issue_gate.py "
            f"{args.issue} --role admin\n"
            "    That prints the design brief and the steps to draft docs/DESIGN.md.\n"
        )
        if args.comment:
            post_comment(
                args.issue,
                "🚫 **Blocked — no design contract**\n\nDependencies are settled, but this issue does "
                "not state which module owns the code or what its interface is. Implementing it would "
                "mean inventing architecture.\n\nAdmin: add the section to `docs/DESIGN.md` and set the "
                "`design:` field.\n\n_Posted automatically by `scripts/issue_gate.py`._",
            )
        return BLOCKED

    # ---------------- not ready ----------------
    if code != READY:
        req = ", ".join(f"#{r}" if str(r).isdigit() else str(r) for r in requires)
        provisional = fact_provisional
        if provisional:
            # Every outstanding question supplies a value, not a formula. The rule can be authored
            # with an UNCONFIRMED tolerance, which cannot reach production (ADR-0011). Say so
            # rather than reporting a flat stop that somebody then has to argue about.
            print(f"READY (PROVISIONAL) — #{args.issue} may be implemented")
            print("=" * 62)
            print(f"title  : {title}")
            for line in provisional:
                print(f"  - {line}")
            print("\nAuthor it with the value left UNCONFIRMED. The check will return REVIEW")
            print("REQUIRED for every drawing and cannot be released (ADR-0011, D6.4).")
            return READY
        msg = (
            f"STOP — #{args.issue} is not ready to implement\n"
            f"{'=' * 62}\n"
            f"  title    : {title}\n"
            f"  status   : {status}\n"
            f"  owner    : {contract.get('owner', 'unknown')}\n"
            f"  requires : {req or '—'}\n\n"
            f"{why}\n\n"
            "Do not write code. Do not choose a value to unblock yourself.\n"
        )
        sys.stderr.write(msg)
        if args.comment:
            post_comment(
                args.issue,
                f"🚫 **Blocked — not ready to implement** (`status: {status}`)\n\n"
                f"{why}\n\n"
                + (f"**Requires:** {req}\n\n" if req else "")
                + "_Posted automatically by `scripts/issue_gate.py`. "
                "Implementation must not begin until this is cleared by the admin._",
            )
        return code

    # ---------------- ready: print the brief ----------------
    # Branch naming: <issue-number>-<what-the-issue-is-about>.
    # Strip the internal story code ("A5.1 — ") first: it tells a reader nothing, and
    # '53-a5-1-rule-models...' is noisier than '53-rule-models...'.
    subject = re.sub(r"^[A-Za-z]\d+(?:\.\d+)*\s*[—–-]\s*", "", title)
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
    # Truncate on a word boundary. A hard character cut produced names like
    # '...-explicit-no-applicabl', which reads as a typo rather than a shortening.
    if len(slug) > 48:
        slug = slug[:48].rsplit("-", 1)[0]
    read = contract.get("read") or []
    if isinstance(read, str):
        read = [read]

    print(f"READY — #{args.issue} may be implemented")
    print("=" * 62)
    print(f"title       : {title}")
    print(f"owner       : {contract.get('owner', 'dev')}")
    print(f"branch      : {args.issue}-{slug}")
    print(f"implements  : {contract.get('implements', '(not stated)')}")
    print(f"design      : {design}")
    print(f"verification: {contract.get('verification', '(none stated)')}")
    print()
    print("READ THESE FIRST, IN ORDER:")
    for r in read:
        print(f"  - {r}")
    print()
    for h in ("Context", "Scope", "Acceptance criteria", "Out of scope"):
        s = section(body, h)
        if s:
            print(f"--- {h} ---")
            print(s)
            print()
    dod = section(body, "Definition of Done") or section(body, "### Definition of Done")
    if dod:
        print("--- Definition of Done ---")
        print(dod)
        print()
    print("RULES WHILE IMPLEMENTING")
    print("  1. Implement only what Scope states. Anything else -> comment, do not do it.")
    print("  2. If you need a value, tolerance or decision that is not in this issue,")
    print("     STOP and comment. Never choose one yourself.")
    print("  3. Every Definition of Done item must be met before opening the PR.")
    print(f"  4. PR description must contain 'Closes #{args.issue}'.")
    print("  5. Never edit AGENTS.md, CLAUDE.md, memory.md, docs/ or rules/rulebook/.")
    print()
    if args.start:
        set_state(args.issue, "in-progress", True)
    elif args.review:
        set_state(args.issue, "in-review", False)
    else:
        print(
            "NEXT: re-run with --start to claim it "
            "(sets state:in-progress and assigns it to you)."
        )
    return READY


if __name__ == "__main__":
    sys.exit(main())
