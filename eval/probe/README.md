# The recall probe: testing the AI *finding* stage cheaply

**What it is.** A runner that does the real full check on **only the pages that carry known
problems**, plus a few control pages that carry none, and scores what came back against the exam's
miss tables. `scripts/recall_probe.py`.

**Why it exists.** The $0 exam (`eval/exam/`) replays answers a paid run already wrote. That makes
a sorting or grading change free to test, and it is blind to the thing that bounds the product:
recall is 5–15%, and 48 of the 74 known problems were never raised by any run. Every finding here
is written by an AI; code only does arithmetic on what the AI already wrote down. A better question
list or page matcher writes answers that exist in no saved run, so replay can never show them. Up
to now, proving a finding improvement meant a whole job — about $25–37 and 30–90 minutes — which is
why nobody iterated on it.

## The two halves

| half | what it buys |
|---|---|
| **the probe** (`pages`, `cost`, `preflight`, `config`, `runner`, `score`) | a finding change measured on 5–15 pages instead of 52 |
| **the fact cache** (`facts`, `factcache`) | the question side iterated for nothing, once a page has been read |

## Pages, and why control pages are half the measurement

`pages.select` takes the job's known problems and returns the smallest page set that can see them.
A **cross-sheet conflict** is the disagreement between two sheets, so every one of its pages is
needed; every other kind is a defect printed on a page, so any one of the pages a miss names will
do. Problems the page set cannot see in full are reported, never quietly dropped from the score.

Control pages carry no known problem. Recall on its own is maximised by flagging everything, and
no miss table can tell you that happened — a control page can, because there is nothing there to
find. `score` reports flags per control page beside recall, and `compare` refuses to call an arm
better when both went up.

## What one costs

Fitted to runs already paid for (`calibration.json`), so the price is evidence, not a guess. One
job's whole miss list needs most of the job and is barely cheaper than running it; **one family of
problems needs three to five pages**, which is where the probe earns its keep.

| probe on setA | pages | problems scored | estimate |
|---|---|---|---|
| one GV rule family | 5 | 4 of 4 asked | ~$6, ~13 min |
| the vendor's own arithmetic | 6 | 5 of 5 | ~$7 |
| architect differences | 7 | 6 of 6 | ~$8 |
| the best twelve pages | 15 | 29 of 39 | ~$15 |
| every known problem | 34 | 39 of 39 | ~$32 |

`plan` prints the ceiling beside the estimate. The ceiling is arithmetic from the repo's own money
rules and is about twice the estimate; it is the number to check an account balance against.

## How a problem is judged raised

By the exam's own matcher (`replay.auto_status`): an answer must sit on one of the miss's pages and
carry at least three of its cue words. Its error is measured, against the 121 hand-scored
run/miss pairs: at three cues it finds 10 of the 13 problems the helpers found, and leaves alone 76
of the 95 they did not. So a probe's recall reads about a fifth high and a quarter low. **Good
enough to rank two arms; not a number to quote as the product's recall.** Every score carries the
finding id the matcher leant on, so a doubtful one takes a minute to settle rather than a re-audit.

## Money and safety

- Nothing spends anything without `--spend`.
- `--spend` is refused unless Docker answers with the `gv-assistant-sandbox` image, more than 5 GB
  is free, and the app is already answering. The probe **never starts the app**.
- One paid job at a time: repeats and arms run one after another.
- A probe's answers are the client's drawings turned into findings. `--out` must be outside any
  git tree, and the script refuses otherwise. The fact cache lives in `~/gv-local/exam/factcache/`
  for the same reason.

## The question list is an input, not something this package writes

`vocabulary/review_questions.yaml` and `app/review/assistant/question_list.py` belong to the
`phase6-question-list` workstream. The probe only points a run at one: an arm names a file and the
probe puts its path in `GV_ASSISTANT_QUESTION_LIST`. When that module is not on the branch, `plan`
says so and the arm is a copy of the one without it, so two identical arms can never be mistaken
for a result.
