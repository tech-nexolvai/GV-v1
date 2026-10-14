"""Smriti, the learner, v1: reviewer decisions become candidate Pariksha key cases (#1232).

**Why.** Reviewers correct and decide things every day, and each decision is recorded, but nothing
learned from them: `correction_ledger` is written and `eval/promotion.py` and
`eval/correction_rate.py` have no callers, and most decisions live in tables the ledger does not
cover. Pariksha (#1219) scores an agent against a key; Smriti turns what reviewers decided into
*candidate* cases for those keys, so the keys grow from real use.

**Nothing changes in the product, and nothing joins a key by itself.** The exporter only reads (the
transaction is made read-only first). Every candidate is marked `needs-confirmation`. A person
checks a candidate against the drawing and names it in a confirmations file; only then does
`eval/smriti/confirm.py` write it into a local key file (never in git). This is the rule
`eval/promotion.py` already follows: an answer key that grew by itself would become the thing every
metric is measured against without anybody having agreed to a single entry in it.

## Which table corrects which agent

| Table | Agent | What it corrects | Becomes |
|---|---|---|---|
| `correction_ledger` | Akshara | a value a reader read | a label case; the person states the printed text |
| `slot_row_review_decisions` walls | Rekha | the walls at a row's ends | kept and counted (no walls key yet) |
| `slot_row_review_decisions` typed widths | Akshara | a width the readers held | a label case; the person states the printed text |
| `architect_pairing_records` (reviewer) | Milan | which dimensions pair | kept and counted (no Milan key yet) |
| `architect_view_matches` (reviewer) | Milan | which architect view matches | kept and counted (no Milan key yet) |
| `no_countertop_answers` | Rekha, or Bala on a page code listed | a page listed with no countertop | "Confirm: no countertop" on a page the readers asked about: a row case `{"kind": "no_countertop"}` |
| `review_actions` on a split or stopped page's result | Rekha (Bala, none) | the page's result | kept and counted: the product records no reviewer row pick |

A Vastu reading corrected in the ledger is Vastu's, and not convertible: a Vastu key lists every
architect width on a page, and one correction is not a whole page. An answer on a page the AI budget
stopped before anything read it corrects no agent (`none`).

## The identity, the same as Pariksha's

A candidate names its question exactly as a `pariksha-key/1` case does (`eval/pariksha/__init__.py`):
the prompt family, the page's content hash and the question id, with the guard the key requires.

* **A label** (Akshara): the decision's reading is the slot reader's span; its question is the one
  stored `claude-slot-span` packet whose only candidate is that span, and its `slot_box` is the
  span's `slot-box:` flag. A typed width is placed on the span of its row (same run, page and
  `row-rank:`) at its slot (`piece_widths:<n>` → `slot:<n>`, `countertop_width` → `slot:overall`).
* **A row** (Rekha): the page's row-choice record names its candidate rows (`row-candidate:`); its
  question is the stored `slot-row-choice` packet on the same document and page that names exactly
  those rows, and its `picture_sha256` is that packet's one numbered picture.

Each is checked twice: the packet's page hash must equal the page row's content hash (and, on a
no-countertop answer, the answer's own page hash). No question, two different questions, two
different packets or pictures for one question, a missing slot box: each is "not convertible", with
the reason. Nothing is guessed.

**Why a label's answer comes from the person.** The ledger keeps a corrected *value* and a typed
width is what the reviewer *typed*; an Akshara case is the *printed text* (compared after
`seal.normalise_text`). `36 1/2"` and `36.5` are one value and two texts, so turning a value into a
text would be a guess. The candidate carries the identity and the reviewer's value; the person
states the printed text when confirming.

**Only current decisions count.** A decision a later one on the same row or page replaced is
listed as `superseded`; one copied from an earlier revision as `carried` (its original is its own
candidate). Both are listed, never counted as decisions and never convertible.

## Counts

`agent_counts` and `table_counts`: per agent (or table), the current decisions and how many
corrected the machine's answer, filled a value the readers held, confirmed it or are another kind of
decision; how many are convertible (ready, or waiting for the printed text) and how many are not.
`scripts/pariksha_scorecard.py score --smriti <candidates file>` prints the agent's line in the
scorecard report.

Source: issue #1232 · Verification: `tests/eval/test_smriti_export.py`,
`tests/eval/test_smriti_confirm.py`, `tests/scripts/test_smriti_candidates.py`
"""
