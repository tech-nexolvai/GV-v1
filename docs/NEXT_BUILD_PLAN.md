# Next build plan

**Rewritten 2026-09-23** after four research threads. What we are, and what we ship next. Detail in
[`SYSTEM_STATE`](SYSTEM_STATE_2026-09-22.md) · [`V1_TARGET_AND_GAP`](V1_TARGET_AND_GAP.md) ·
[`AGENTIC_STACK_RESEARCH`](AGENTIC_STACK_RESEARCH.md) · [`AI_FILLS_THE_FORM_PLAN`](AI_FILLS_THE_FORM_PLAN.md).

## Where we are

Deterministic half built and tested (4,830 passing). Reading half reads **9%**, so the reviewer types
28 fields and the system has **never produced a PASS**. Vision readers built last week, switched off.

**The diagnosis changed.** The 9% is not OCR tuning. The vendor's numbers are *outlined glyphs* —
text converted to vector paths at plot time — so there is no text to find. `vector_first.py` says so.

## How the work divides

Not by pipeline stage. By **trust boundary** — who is allowed to decide what. Five roles, and they
cut *across* the six stages rather than along them:

> **The rulebook directs. The AI reads with tools and context. Code cross-checks. Exact arithmetic
> decides. A reviewer confirms.**

Only **three of six stages touch a model at all** — `extract_pages`, `match`, `generate_outputs`.
`ingest`, `validate_evidence` and `run_checks` have none, by design.

**On naming: keep the six stage keys, add a plain-English label, and mark which stages a model
touched.** No personas, no human names. Three reasons, in order of weight:

1. A persona on `run_checks` would tell the reviewer a *mind* judged their drawing, when the fact
   that makes this product defensible is that a deterministic function did. It would contradict the
   one rule the product is built on.
2. Anthropomorphism's one reliable effect in task settings is **trust that survives errors** — the
   exact failure mode for a tool whose purpose is that a human catches the machine's mistakes.
   Professional scepticism toward machines is a free safety property; a persona spends it.
3. EU AI Act Art. 50(1) (applicable since 2026-08-02) requires disclosure unless the AI is obvious.
   "Rule check" is obviously not a person. "Alex" is not obviously not a person.

The industry already retreated from this: ChatDev deleted its entire named cast in v2, Magentic-UI
collapsed its agents into one plus a registry, and MetaGPT's human names are decorative — `profile`
("Architect") enters the prompt, `name` ("Bob") does not, and the first names collide.

**The higher-yield job is a pronoun audit.** Anthropomorphism is carried by ordinary copy — a log
line reading *"I couldn't find the dimension"* does it with or without a name. Write "the reader
found", never "I found", everywhere.

## The verification loop, corrected

The earlier version of this plan kept the model out of the loop entirely. That was too binary — the
model supplies every operand, and excluding it wastes the context it has while reading. The loop is
five sub-tasks and the answer differs per sub-task:

| Sub-task | Who |
|---|---|
| Which numbers form a chain | **Model proposes**, geometry first, recorded as an evidenced hypothesis |
| The sum | Python `Fraction`, exact — **no model** |
| Which read is the suspect | Deterministic enumeration (`difference` already gives it), **model ranks** |
| Proposing a corrected value | **Model** — re-enters as a new read at the bottom of the evidence ladder |
| When to stop | Fixed budget calibrated on the gold set — **no model** |

Two findings fix the line where it is. Models validate arithmetic by *surface consistency* in middle
layers before the result is even computed, and collapse to **12.39% accuracy on consistent errors** —
which is exactly the case when the model produced the operands, so it cannot catch its own misread.
And self-correction alone *degrades* (GSM8K 75.9→75.1→74.7) while self-correction **given an oracle
label improves** (75.9→84.3). Closure *is* the oracle label: it is what lets the model into the loop
productively, not what keeps it out. This pattern is LLM-Modulo — the only one in the field with a
published soundness guarantee.

A grouping chosen because it makes the sum close is curve-fitting, not a check. Grouping is fixed and
logged — geometry or model — before closure tests it.

---

## The steps

**0 · Measure the baseline** — *half a day, unblocked*
Set `GV_BEDROCK_VISION_ENABLED`, run one real upload, record the funnel. Every estimate below is a
guess until this exists.

**1 · Fill the two layout dropdowns** — *1–2 days, unblocked*
`wall_config` (back_left_right / back_only / island) and `filler_symmetry` are the most visually
obvious facts on a sheet and are reviewer dropdowns today. Classify, show the crop, reviewer
confirms. `wall_config` also derives `field_cut` and supplies `field_cut_count` to step 4 — **one
answer fills three fields and unblocks two rule variants.**

**2 · Read the glyphs, don't guess them** — *half-day de-risk, then 1–2 weeks*
`OutlinedTextRegion` finds the glyph clusters and discards the path geometry. Keep it, normalise and
hash the outlines, cluster. One vendor, one plotter, one font ⇒ ~20 distinct shapes; a reviewer
labels each **once** and every occurrence then reads *exactly* — which is what exact-match verdicts
require. Stacked fractions become geometry; rotation comes from the path matrix.
**De-risk first:** cluster one real sheet, count distinct shapes. ~20 → go. ~500 → stop and reassess.

**3 · Improve association with the model** — *3–5 days, needs step 2*
**52% of associations are refused today** by a proximity threshold — "none of the 12 dimension lines
is within the stated limit". Deciding which line a number labels is spatial-semantic judgment.
Geometry scores first; the model is invoked **only on escalation**; the reviewer sees one review.
The closest published analogue on this exact problem reaches 86% F1 that way.

**4 · Wire the closure check** — *4–6 days, needs steps 1 and 3*
`extraction/chains.py:validate_closure` is built and tested; the chain-finding geometry is built;
**neither is ever called.** Connect them to `extract_pages`. It already returns three states —
`CLOSED` / `NOT_CLOSED` / `NOT_VERIFIABLE` — and carries `difference`, which is exactly what
enumerates candidate repairs. **Add reference/NTS-dimension handling in the same step**: nothing in
the codebase distinguishes a chain that legitimately does not close, and without it the first demo
produces confident false REVIEWs.

**5 · Confirm, don't author** — *3–5 days, needs 1–4*
Revive `ConfirmReadingsPage` (built, never routed). Three buckets: auto-accept where readers agree
and the chain closes; pre-filled awaiting confirm; **do not pre-fill when unsure** — a wrong guess
anchors the reviewer. Bulk confirm, keyboard-driven, crop beside every value. Never show a
confidence percentage; route on it, show the bucket and the action.

**6 · Output the reviewer already knows** — *4–6 days, unblocked*
Write real PDF `/Annot` objects, not flattened ink — that is what puts our markups in Bluebeam's
Markups List, where these reviewers already work. Report follows the audit convention:
conformed / exception / **could not check**, all three in one table, plus a vendor-response column.
Headline "n of m checks conformed". Use EJCDC's existing vocabulary ("Reviewed with No Exceptions").

**7 · Give a package its product** — *1–2 days, unblocked*
`Package` has no product field, so `run_checks` loops every `ProductType` and runs cabinet rules on
countertop jobs. Survivable at two products, not at five. Add the field, filter the rulebook, do it
before the next product lands.

**8 · Close the measurement loop** — *blocked on client*
A human-read answer key (~40–60 crops, ours to schedule) turns on the bake-off and the existing
`eval/` metrics — `critical_false_pass_rate`, `numeric_exact_match_accuracy`, `automation_coverage`.
The real drawing set (#274) and Q20 tags turn on matching and the release gate.

---

## The vision readers, and where their coordinate modes came from

Measured 2026-09-26 on four crops from `demo_pair/shop.pdf` p1, by sending each model the
four-scalar schema and comparing what came back against the crop's own pixel dimensions. Of 25
vision models this account can invoke, 20 were testable and **7 conform on every crop**. Three are
configured (#668):

| Reader | Coordinates | Why |
|---|---|---|
| `amazon.nova-pro-v1:0` | 0–1000 | incumbent, largest Amazon |
| `mistral.ministral-3-3b-instruct` | pixels | **different vendor** — 3.0s and 1,430 tokens, the cheapest and fastest that conform |
| `amazon.nova-2-lite-v1:0` | pixels | newest Amazon generation; answers in a *different* space from Nova Pro, which is the clearest evidence available that the two were trained separately |

`claude-haiku-4-5` is defined and disabled until #665; enabling it is a change to
`GV_BEDROCK_VISION_READERS`, not to code.

**Only two vendors answer at all.** Google, Meta, Moonshot, Qwen, xAI, Writer and Nvidia all refuse
forced tool use with an image, so the independence available to the agreement lane is narrower than
we would like. Widening it is what #665 buys.

**These modes are measurements with a date, not properties of a name.** Inferring one from whether
`"nova"` appeared in a model id is what #668 removed: Nova 2 Lite carries the word and answers in
pixels, and a pixel value read as a grid value is divided by a thousand, lands near the origin, and
*passes* the bounds check #664 added. If a reader is later seen answering in the other space, that is
a re-measurement, never a guess.

**What this does not claim.** Seven models returned seven different readings for the same crops.
This chooses readers that *function*; ranking them for accuracy is #666.

---

## What does not change

The verdict stays exact arithmetic — not because models are weak, but because a wrong PASS gets
built, and our own crop of `46"` read as `60` and `4` by two models, both confidently. Two readers
agreeing routes to review, never auto-passes: unanimous agreement across four frontier models is
still 14.2% wrong. Nothing seals evidence without a page and a polygon. A reviewer signs off.

## Order

Steps 0, 1, 6, 7 are unblocked and independent — start there. Step 2 is the real fix for the 9% and
carries the most unknown, hence the de-risk gate. Steps 3 and 4 build on it. Step 8 is the only thing
waiting on anyone else. **Four to six weeks of unblocked work**, and the first three days visibly
shorten the form.

---

*Figures cited above: 17× error amplification without an orchestrator and 39–70% degradation of
sequential tasks under multi-agent decomposition are from* Towards a Science of Scaling Agent Systems
*(Google Research + MIT, arXiv:2512.08296). The 12.39% consistent-error validation collapse is from*
The Validation Gap *(arXiv:2502.11771). Oracle-label self-correction figures from arXiv:2310.01798.
Full sourcing in the research documents linked at the top.*
