"""The review assistant (#1128): grounded answers about one review, from that review's records only.

The parts, in the order a question meets them:

- `records`: a compact, id-keyed snapshot of one package's review records, built from the same
  projections the screens use (countertop results, sign-off readiness, findings and decisions).
  Nothing is re-derived: every outcome and number is the API's own.
- `intent`: a request to decide, mark, approve or sign off is answered by a fixed refusal that
  points to the queue, with no model call.
- `records_only`: deterministic answers built from the records, for the starter questions, for a
  deployment with the model off, and as the fallback whenever a model answer is dropped.
- `model`: Claude Sonnet 5.5 through OpenRouter, on the readers' keep-no-data route.
- `guard`: since the chat-assistant style (2026-10-16) the model talks freely about the drawings,
  numbers and counts included; the guard keeps the hard rules (no action claimed or advised, no
  pass or fail of its own, a recorded result quoted through its placeholder on its own record,
  evidence and actions that exist), or the answer is dropped for the records-only one.
- `service`: the steps above, streamed as events.

**The assistant never judges or changes an outcome** (the verdict wall): it reads records that
deterministic code and the reviewer wrote, and it can only point and open.
"""

from __future__ import annotations
