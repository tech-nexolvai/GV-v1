# What the reading layer actually did — measured, 2026-09-29

**This is `V1_TARGET_AND_GAP.md` §3 Phase A3: the funnel, recorded.** Every figure below was read off
a database after one complete run, not estimated. It exists so the next run has something to be
compared against, and so nobody has to re-derive it from issue comments.

**No client dimension appears in this file.** The drawings stay under `data/` and the values stay in
the database; what is recorded here is counts, rates and costs.

## The run

| | |
|---|---|
| Package | the 17-page client shop set, uploaded through the API as a single document |
| Code | `main` at `cd4d6e9` (before #719, #721, #723) |
| Readers | Nova Pro, Nova 2 Lite, Ministral 3B — all three enabled |
| Settings | `scripts/demo.sh` values: vision on, `LINE_MINIMUM_PT=6`, the #600 thresholds |
| Wall clock | **92.4 minutes**, single worker, calls serialized |

It is the first time the real client drawing has been through this pipeline end to end. Before
#705 the API refused it, because the intake wanted two PDFs and the client's sheets carry both the
architectural and the vendor view on one page.

## The funnel

```
  text fragments found                  1579
  ...that parse to a number              124   7.9%
  ...attached to a dimension line          25   1.6%
  ...sealed as usable evidence              1   0.1%

  findings                                 18
  ...of which PASS                          0
```

**No package has ever produced a PASS**, real or synthetic. That is the headline and it has not moved.

`observation_associations` holds 166 rows, of which 25 accepted; the rest carry a named refusal —
*"no dimension lines were found on this page"*, *"none of the N dimension lines on the page is
within…"*, *"the two nearest of N candidate dimension lines are conflicting"*. Before #698 that table
was empty and the rate was 0.0%, with no way to tell a threshold problem from a structural one.

## What each route contributed

| Route | Candidates | With a value | Carries a confidence |
|---|---|---|---|
| `rapidocr` | 903 | **0** | **903** |
| `bedrock-nova-2-lite` | 268 | 42 | 0 |
| `bedrock-nova-pro` | 152 | **6** | 0 |
| `extraction.annotations` | 146 | 16 | 0 |
| `bedrock-ministral-3-3b` | 70 | **59** | 0 |
| `pdfplumber` | 40 | 1 | 0 |

Two things to read off this table:

**RapidOCR produced 57% of all candidates and not one usable number.** It is reading line-work as
glyphs, including CJK characters (#703). It also inflates every rate above: the 7.9% parse rate has a
denominator that is mostly noise.

**Confidence is written by exactly one route — the one that reads nothing.** Every vision reader
records `confidence=None`. Two reviewer screens used to display that score, so the readings least
worth trusting wore a number that looked like authority and the best reader's showed none. Removed in
#721.

## What the model calls cost

| Outcome | Calls | Share | Input tokens | Model time |
|---|---|---|---|---|
| `ok` | 490 | 13% | 606,757 | 11.2 min |
| `rejected` | **1,743** | **47%** | 1,572,213 | **42.2 min** |
| `failed` | 1,451 | 39% | 0 | 25.6 min |
| **total** | **3,684** | | **2,178,970** | **79.0 min** |

**86% of all model time bought nothing.** The 490 calls that returned a usable answer cost 11.2
minutes between them.

79 minutes of summed latency inside a 92.4-minute wall span means the calls are **essentially fully
serialized** — one connection, one at a time. The remaining ~13 minutes is page rendering, OCR and
database work.

**`cost_micros` is 0 on all 3,684 rows.** Tokens are recorded correctly; the bill is not (#700). No
one can answer what a package costs to run, and Epic F5's budget ceiling has nothing to read.

### Per reader

| Reader | Calls | Model time | Answers accepted | Values |
|---|---|---|---|---|
| `nova-2-lite` | 1,842 | 29.1 min | 268 | 42 |
| `nova-pro` | 921 | **26.4 min** | 152 | **6** |
| `ministral` | 921 | 23.4 min | 70 | **59** |

Nova 2 Lite makes twice as many calls as the others because the bare model id never works on this
account and every call is retried with the inference-profile prefix (#702).

## The account is throttled to 1% of default

Read from Service Quotas on 2026-09-29, `us-east-1`:

| Quota | Applied | AWS default |
|---|---|---|
| Global cross-region requests/min — Nova 2 Lite | **20** | **2,000** |
| Cross-region requests/min — Nova Pro | 25 | 500 |
| On-demand requests/min — Nova Pro | **13** | — |
| On-demand requests/min — Ministral 3B | 100 | — |
| Cross-region tokens/min — Nova 2 Lite | 8,000,000 | — |
| On-demand tokens/min — Nova Pro | 1,000,000 | — |

Against the run:

| Reader | Calls/min | Quota | % of quota |
|---|---|---|---|
| nova-pro | 10.0 | 13 | 77% |
| **nova-2-lite** | **19.9** | **20** | **100%** |
| ministral | 10.0 | 100 | 10% |

**Nova 2 Lite ran at 19.9 calls per minute against a 20-per-minute ceiling, for 92 minutes.** The
runtime is a throttling artifact, not a property of this software. Token quotas are generous; the
request rate is the whole constraint. Requesting the increase is #716.

## What this measurement is not

- **It is not an accuracy measurement.** Nothing here says a reading is *correct*. The 59 values from
  Ministral have no ground truth behind them. Of four crops checked by hand against the drawing's own
  annotation text, it read one correctly — the only legible one. That is what #666 exists to fix, and
  until it lands every accuracy claim would be the pipeline grading itself.
- **It is not a fair baseline for throughput.** Every timing figure describes a throttled account.
  After #716 lands, re-run before comparing, or engineering work will be credited for a quota change.
- **It predates four fixes.** #698, #701, #719 and #723 all landed against or after this run. A re-run
  should show the rejection count fall and the remainder name themselves.

## Reproducing it

```bash
scripts/demo.sh                      # brings up the stack with the measured reader settings
# upload the package through the API, then:
python scripts/drain_outbox.py       # with the demo.sh environment
GV_DATABASE_URL=… python scripts/reading_funnel.py
```

`scripts/reading_funnel.py` prints the funnel, the per-route breakdown, the association refusals and
the model-invocation table. It is the tool this document was written from.
