"""The recall probe: measure the AI *finding* stage cheaply (harness rebuild, Phase 1).

The exam (`eval/exam/`) replays a saved run's answers for nothing, which makes a sorting or
grading change free to test. It is blind to the one thing that bounds the product: whether the AI
**finds** a problem at all. A better question list or page matcher writes answers that exist in no
saved run, so replay can never show them.

This package is the cheap way to see that. It runs the real finding stage on **only the pages that
carry known problems**, plus a few control pages that carry none, and scores what came back
against the exam's miss tables. The settled rule it implements (Anant, 2026-10-31) is "test on the
affected pages only; one full run at the end".

- `pages` — which pages a probe needs, and which problems that page set can and cannot see.
- `cost` — what a probe will cost and how long it will take, fitted to runs already paid for.
- `preflight` — Docker and free disk, checked before a paid probe is allowed to start.
- `config` — the arms of an experiment (today's prompts versus a candidate question list).
- `runner` — the plan of calls, and the one paid call that starts a probe.
- `score` — recall, the noise rate on the control pages, and the per-problem hit rate over repeats.
- `facts` / `factcache` — "read this page into facts" separated from "ask questions of those
  facts", so the question side can be iterated for nothing.

Nothing here spends money unless `--spend` is passed to `scripts/recall_probe.py`.
"""

from __future__ import annotations

__all__ = ["DIR"]

from pathlib import Path

DIR = Path(__file__).resolve().parent
