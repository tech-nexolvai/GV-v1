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
        "An architectural decision must be ratified first. Architecture comes before "
        "implementation — the admin owns that decision.",
    ),
    "blocked-client": (
        BLOCKED,
        "A client answer is required. The value or rule this issue depends on does not "
        "exist yet, and it must not be guessed.",
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
        "This is a decision or a client question. It is the admin's to resolve and must "
        "never be answered by the dev or by a coding agent.",
    ),
}

CONTRACT_RE = re.compile(
    r"##\s*Agent contract\s*\n+```yaml\n(.*?)\n```", re.S | re.I
)


def gh_json(path: str):
    p = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True
    )
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
        rf"^#{{2,3}}\s*{re.escape(heading)}\s*$\n(.*?)(?=^#{{2,3}}\s|^\*\*Source:|\Z)",
        body or "",
        re.S | re.M,
    )
    return m.group(1).strip() if m else ""


def post_comment(number: int, text: str) -> None:
    p = subprocess.run(
        ["gh", "api", f"repos/{REPO}/issues/{number}/comments", "-X", "POST", "--input", "-"],
        input=json.dumps({"body": text}),
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        sys.stderr.write(f"warning: could not post comment: {p.stderr}\n")
    else:
        sys.stderr.write(f"Posted a comment on #{number}.\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("issue", type=int)
    ap.add_argument("--comment", action="store_true",
                    help="post the stop reason to the issue")
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

    # ---------------- not ready ----------------
    if code != READY:
        req = ", ".join(f"#{r}" if str(r).isdigit() else str(r) for r in requires)
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
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    read = contract.get("read") or []
    if isinstance(read, str):
        read = [read]

    print(f"READY — #{args.issue} may be implemented")
    print("=" * 62)
    print(f"title       : {title}")
    print(f"owner       : {contract.get('owner', 'dev')}")
    print(f"branch      : {args.issue}-{slug}")
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
    print("  4. PR description must contain 'Closes #%d'." % args.issue)
    print("  5. Never edit AGENTS.md, CLAUDE.md, memory.md, docs/ or rules/rulebook/.")
    return READY


if __name__ == "__main__":
    sys.exit(main())
