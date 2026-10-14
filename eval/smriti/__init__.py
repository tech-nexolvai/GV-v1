"""Smriti, the learner: reviewer decisions become candidate Pariksha key cases (#1232, v2 #1267).

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

## Who decided (v2, `eval/smriti/actors.py`)

One set of rules for every table. **Code is never a candidate**: a `code:` actor, or a record whose
source is the machine (`code`, `code+ais`, `both-ais`, `none`, `automatic`, `carried`,
`deterministic`), is counted in the export's `skipped` and serves only as the machine's answer a
person may overturn. **A carried copy and a batch click are never ready**: the copy's original is
its own candidate, and a page answered in one "confirm these pages" click needs its own look.
**Only names on the local real-reviewer list count** (a file under `~/gv-local`, named by
`GV_SMRITI_REVIEWERS`; no default, and never in a repository): any other name is shown and never
convertible.

## Which table corrects which agent

| Table | Agent | What it corrects | Becomes |
|---|---|---|---|
| `correction_ledger` | Akshara (Vastu on an architect reading) | a value a reader read | a label case; the person states the printed text and says "AI misread" or "vendor drew it wrong" |
| `review_actions` confirm of a reading | Akshara (Vastu) | nothing: the reading was right | a **positive** label case; the answer is the stored reading's text, which the person checks |
| `slot_row_review_decisions` walls | Rekha | the walls at a row's ends | a `rekha-walls` case on the row's walls question: the reviewer's layout is the site answer, and the person states what the vendor drew at each end |
| `slot_row_review_decisions` typed widths | Akshara | a width the readers held | a label case; the person states the printed text, and misread or vendor |
| `architect_pairing_records` (reviewer) | Milan | which dimensions pair | kept and counted (no Milan key yet) |
| `architect_view_matches` (reviewer) | Milan | which architect view matches | kept and counted (no Milan key yet) |
| `no_countertop_answers` | Rekha, or Bala on a page code listed | a page listed with no countertop | "Confirm: no countertop": a row case `{"kind": "no_countertop"}`; "there is a countertop": a row case whose box the person picks; a batch click: its own look first |
| `review_actions` on a split or stopped page's result | Rekha (Bala, none) | the page's result | kept and counted: the product records no reviewer row pick |
| `review_actions` dismiss, except, a finding's confirm | Kautilya | a check's result | kept and counted for the report; it judges a finding, not a reading |
| `part_confirmations` (a person's) | Bala | what a part is | a piece's kind: a `bala-kinds` case (`p<page>:piece<n>`); a countertop yes/no: counted |
| `view_role_confirmations` (a person's) | Bala | which drawing a view is | kept and counted (no view-role key) |
| `item_classifications` | Bala | an older form's kinds | kept and counted |
| `layout_confirmations` | Rekha | an older form's whole-revision layout | kept and counted |
| `parameter_value_citations` | Khoj | where a setting is stated | kept and counted (Khoj stays off; no key) |
| `approved_matches` (human) | Milan | that two items correspond (older matcher) | kept and counted |

**Not read yet: countertop runs and reading links.** `countertop_run_decisions` (the run beneath a
countertop and its wall choice) and `reading_parts` (which part a reading measures) are person
decisions too, but `tests/db/test_drawing_models.py` holds which modules may read those tables
(only the confirmed-structure resolver, so rules never read them by another path). Adding Smriti
to that list is a separate decision; until then they are not exported.

A Vastu reading corrected in the ledger is Vastu's, and not convertible: a Vastu key lists every
architect width on a page, and one correction is not a whole page. An answer on a page the AI budget
stopped before anything read it corrects no agent (`none`).

## Overturned automatic results (report only)

A person's decision that replaced something code or both AIs decided by themselves is marked
`overturned`, and listed newest first: code's countertop item (`code:bala-two-judgments/v1`), a
view role code set, an automatic pairing (`code+ais`) or view match (`auto_matched`), an automatic
wall layout (`wall-source:code-ink-and-both-ais`), a page both AIs (or code) said has no
countertop, a reading both readers agreed on (sealed). In production that is the only proof of an
"agreed-wrong", which must stay 0. The listing never switches anything off: that stays a human
call, like switching on.

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
* **A walls row** (Rekha's walls): the row's wall record is the run's `wall-reader` candidate on
  the same page with the anchor's row rank; its question is the one `slot-walls` packet naming it,
  and its `row_box` is that record's box, as `eval/pariksha/walls.py` reads it.
* **A piece kind** (Bala): Bala's link (`part_proposal_rows`) names the row and the piece's place;
  the question is `p<page index>:piece<n>` with the number of pieces the run read on the page.

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
decision; how many are convertible (ready, or waiting for a person's answer) and how many are not;
and how many overturned an automatic result.
`scripts/pariksha_scorecard.py score --smriti <candidates file>` prints the agent's line in the
scorecard report.

Source: issues #1232, #1267 · Verification: `tests/eval/test_smriti_export.py`,
`tests/eval/test_smriti_actors.py`, `tests/eval/test_smriti_confirm.py`,
`tests/scripts/test_smriti_candidates.py`
"""
