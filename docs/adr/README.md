# Architecture Decision Records

One file per decision, newest number highest. The D-series issues in GitHub each
correspond to an ADR here.

**The loop:**

1. `python scripts/issue_gate.py <decision-issue> --role admin` — prints the decision brief
2. Draft the ADR from `TEMPLATE.md` (an agent may write it)
3. The **admin** sets `Status: Accepted` — nobody and nothing else
4. `python scripts/ratify.py D<n> --adr docs/adr/<file>.md` — every dependent story is
   rewritten to `status: ready` and its blocked label cleared, automatically

Step 4 is what makes "architecture before implementation" real rather than aspirational:
a story blocked on a decision cannot pass the readiness gate until the ADR exists and is
accepted.
