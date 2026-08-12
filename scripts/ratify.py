#!/usr/bin/env python3
"""Ratify a decision or a client answer, then unblock every story that waited on it.

Usage:
    python scripts/ratify.py D1 --adr docs/adr/0001-unit-policy.md
    python scripts/ratify.py Q2 --answer "Countertop width tolerance is 1/8 inch for three-wall"
    python scripts/ratify.py D1 --adr docs/adr/0001-unit-policy.md --dry-run

This is the step that turns "architecture first" into a closed loop. A story marked
`needs-architecture` cannot be implemented until its decision is ratified here; once it
is, the story's contract is rewritten to `status: ready` automatically and the dev can
pick it up with nothing more than the issue number.

A decision requires an ADR whose Status line reads Accepted. A client question requires
a recorded answer. Neither may be ratified silently — both leave an audit trail on the
issue, which is what `AGENTS.md` §2.6 and §2.7 require of rule and tolerance changes.

Only the admin runs this.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "tech-nexolvai/GV-v1"
CONTRACT_RE = re.compile(r"(##\s*Agent contract\s*\n+```yaml\n)(.*?)(\n```)", re.S | re.I)


def gh(args, payload=None):
    cmd = ["gh", "api"] + args
    if payload is not None:
        cmd += ["--input", "-"]
        p = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True)
    else:
        p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"gh api failed ({' '.join(args)}):\n{p.stderr.strip()[:300]}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def all_issues():
    return [i for i in gh([f"repos/{REPO}/issues?state=all&per_page=100", "--paginate"])
            if "pull_request" not in i]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ref", help="decision or question id, e.g. D1 or Q2")
    ap.add_argument("--adr", help="path to the ADR recording the decision (required for D-refs)")
    ap.add_argument("--answer", help="the client's answer (required for Q-refs)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ref = args.ref.strip().upper()
    if not re.fullmatch(r"[DQ]\d+", ref):
        raise SystemExit(f"ref must look like D1 or Q2, got {ref!r}")

    # ---- evidence that the decision was actually made ----
    if ref.startswith("D"):
        if not args.adr:
            raise SystemExit(
                f"{ref} is an architectural decision: --adr is required.\n"
                "Write the ADR first (docs/adr/), set 'Status: Accepted', then ratify."
            )
        adr = Path(args.adr)
        if not adr.is_file():
            raise SystemExit(f"ADR not found: {adr}")
        # Strip markdown emphasis first: the status line is written '**Status:** Accepted',
        # with the colon inside the bold, and may also appear as '**Status**: Accepted'.
        text = re.sub(r"[*_]", "", adr.read_text(encoding="utf-8"))
        if not re.search(r"^\s*Status\s*:\s*Accepted\b", text, re.M | re.I):
            raise SystemExit(
                f"{adr} does not say 'Status: Accepted'.\n"
                "A decision is not ratified until it is recorded as accepted."
            )
        evidence = f"ADR [`{adr.as_posix()}`](../blob/main/{adr.as_posix()})"
    else:
        if not args.answer:
            raise SystemExit(
                f"{ref} is a client question: --answer is required.\n"
                "Record the client's actual answer — never a assumed one."
            )
        evidence = f"Client answer recorded: {args.answer.strip()}"

    issues = all_issues()
    by_num = {i["number"]: i for i in issues}

    # ---- locate the D/Q issue itself ----
    target = next((i for i in issues if i["title"].upper().startswith(ref + " ")), None)

    # ---- find dependents by parsing their contracts ----
    dependents = []
    for i in issues:
        m = CONTRACT_RE.search(i.get("body") or "")
        if not m:
            continue
        block = m.group(2)
        req_lines = re.findall(r"^\s*-\s*(.+)$", block, re.M)
        # only the requires list matters; 'read:' items are file paths, never D#/Q#
        reqs = [r.strip() for r in req_lines if re.match(r"^[DQ]\d+\b", r.strip())]
        if any(r.split()[0] == ref for r in reqs):
            dependents.append((i, reqs))

    print(f"Ratifying {ref}")
    print(f"  evidence : {evidence}")
    print(f"  D/Q issue: {'#' + str(target['number']) if target else 'not found'}")
    print(f"  dependents: {len(dependents)}")
    if not dependents:
        print("  (nothing was waiting on this)")

    unblocked, still = [], []
    for i, reqs in dependents:
        remaining = [r for r in reqs if r.split()[0] != ref]
        if remaining:
            still.append((i, remaining))
        else:
            unblocked.append(i)

    for i in unblocked:
        print(f"  -> UNBLOCK  #{i['number']} {i['title'][:56]}")
    for i, rem in still:
        print(f"  -> partial  #{i['number']} {i['title'][:44]} (still needs {', '.join(r.split()[0] for r in rem)})")

    if args.dry_run:
        print("\ndry run — nothing changed")
        return 0

    # ---- apply ----
    for i, reqs in dependents:
        remaining = [r for r in reqs if r.split()[0] != ref]
        body = i["body"]
        m = CONTRACT_RE.search(body)
        block = m.group(2)

        if remaining:
            new_req = "requires:\n" + "\n".join(f"  - {r}" for r in remaining)
        else:
            new_req = "requires: []"
        block = re.sub(r"requires:(?:\s*\[\])?(?:\n\s*-\s*.+)*", new_req, block, count=1)

        if not remaining:
            block = re.sub(r"^status:\s*\S+", "status: ready", block, count=1, flags=re.M)
            block = re.sub(r"^owner:\s*\S+", "owner: dev", block, count=1, flags=re.M)

        new_body = body[:m.start(2)] + block + body[m.end(2):]
        gh([f"repos/{REPO}/issues/{i['number']}", "-X", "PATCH"], {"body": new_body})

        cur = {l["name"] for l in i["labels"]}
        if not remaining:
            keep = {l for l in cur if not l.startswith(("status:", "owner:"))}
            gh([f"repos/{REPO}/issues/{i['number']}", "-X", "PATCH"],
               {"labels": sorted(keep | {"status:ready", "owner:dev"})})
            note = (f"✅ **Unblocked — now `status: ready`**\n\n{ref} is ratified. {evidence}\n\n"
                    f"The dev can start this with `python scripts/issue_gate.py {i['number']}`.")
        else:
            note = (f"☑️ **{ref} ratified**, but this issue is still blocked.\n\n{evidence}\n\n"
                    f"**Still requires:** {', '.join(remaining)}")
        gh([f"repos/{REPO}/issues/{i['number']}/comments", "-X", "POST"], {"body": note})

    # ---- close the D/Q issue with its audit trail ----
    if target:
        gh([f"repos/{REPO}/issues/{target['number']}/comments", "-X", "POST"],
           {"body": (f"**Ratified.**\n\n{evidence}\n\n"
                     + (f"Unblocked: {', '.join('#' + str(i['number']) for i in unblocked)}\n"
                        if unblocked else "No issues were waiting on this.\n")
                     + "\n_Recorded by `scripts/ratify.py`._")})
        gh([f"repos/{REPO}/issues/{target['number']}", "-X", "PATCH"], {"state": "closed"})
        print(f"  closed #{target['number']}")

    print(f"\nDone. unblocked={len(unblocked)} still-blocked={len(still)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
