# The exam: a $0 gate on real jobs

**What it is.** Every hand-audited answer from the real-job runs, turned into rows a machine can
score, plus the real problems on those drawings that no run raised. `scripts/exam_replay.py` scores
the code on a branch against all of it in under a second, with no AI call, no network and no
database.

**Why it exists.** For a month this project fixed each run's failures by hand and merged about
1,100 pull requests with no end-to-end test on a real job, so known false patterns came back: "no
overall height" was patched after the first run and is in the flag run's audit again, three times.
The rule now (Anant, 2026-11-04) is that a change is scored here before anything is paid for.

## The files

| file | one row is |
|---|---|
| `cases.tsv` | one audited answer: the run, the finding id, the vendor page, who raised it, the group the run put it in, and the helper's verdict |
| `pairing.tsv` | one vendor page: the architect page the run used, and the one that was right |
| `misses.tsv` | one real problem on the drawings, whether or not any run raised it |
| `miss_status.tsv` | what one run did about one miss: found, partly, or missed |
| `runs.json` | the run registry: neutral tags, which audits covered which run |

`truth.py` says what a verdict means for a score, `audit_parse.py` reads the helpers' markdown,
`labels.py` gives the neutral word for what an answer is about, and `replay.py` does the scoring.
`scripts/build_exam.py` rebuilds `cases.tsv` when new audits arrive.

## The stage exams

`scripts/exam_replay.py --stage pairing` and `--stage diff` print one stage alone (rebuild plan
Step 1A), so a pairing failure is never read as a finding failure.

- **Pairing**: each audited vendor page is `exact` (the right architect page, not a guess),
  `best_guess` (the right page, but the run marked it a best guess or code was unsure of it),
  `nothing_to_compare` (rightly no architect page), `wrong_view` (the right sheet, the wrong
  view), `wrong_page` or `unpaired` (no page, where the audit names one). The guess signs are
  read from the saved run: the coverage block's `pairing_unsure`, a finding's `pairing_guess`,
  or flag mode's words "best guess: architect page N" in a stored reason.
- **Diff**: per run and side, red precision, please-check precision and recall against the miss
  list (found, partly, missed, unscored), on every audited answer and again on the pages the
  pairing exam calls right.

The reading exam (`reading.py`, `reading_score.py`) is the third stage. Its key holds the printed
values, so it lives under `~/gv-local/exam/reading/`, never here.

## No client text

A case is a run tag, a finding id, a page number, who raised it and a label from a fixed list
(`labels.py`). The answer's own item text is **not** stored: it names the client's items, finish
codes, sink models and rooms. The miss descriptions were written by hand in plain English with
every item tag, sheet number, room number and model code taken out — sizes are written in words
("a quarter inch", "an eighth of an inch") rather than as figures. `tests/eval/test_exam.py`
asserts it, and `~/gv-local/client-values/check.sh` is run before every pull request.

The saved runs themselves, and the audits, are client data and are **not** in this repository.
`scripts/exam_replay.py --runs` takes a local manifest that says where they are.

## What the exam can and cannot see

It can see: where each audited answer was put against where it belonged; the leaks (a real
difference filed as Not compared, an agreement filed the same way); which vendor pages went to the
wrong architect page; how many known real problems each run raised; and which problems one run
found and another missed on the same drawings.

It cannot see:

- **Reading accuracy per number.** Whether a dimension was read correctly needs the set packs and
  a per-number key, not the findings. The exam only sees whether an answer was raised.
- **The true recall.** The helpers read the pages they were given, not all of them, so `misses.tsv`
  is a floor. Vendor pages with no answers at all were only partly checked; the real recall is
  lower than any number the exam prints.
- **Cost, speed, caching or the agent roster.** None of it is in a saved finding.
- **What a new run would do.** Replay re-groups answers that already exist; it cannot invent the
  answer an improved finding stage would write. Only a paid run can show that.
- **The wording of the letter, the cloud drawing or the register.**

## The two groups

A saved finding carries the `proof.group` the live server wrote while the run was going. Re-grouping
the same finding with the code on a branch can disagree — on the flag run it moves 143 answers out
of the problems list and 22 out of the reds — because grouping code merged since then behaves
differently. The exam scores the **stored** group, which is what the person saw and what the helpers
judged, and prints the branch's group beside it as "what today's code would do". Closing that gap
is Phase 2's job.
