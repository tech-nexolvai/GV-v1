# ADR-0021 — The parts of a drawing: suggested by code, confirmed before they count

**Status:** Accepted        <!-- Proposed | Accepted | Rejected | Superseded by ADR-NNNN -->
**Date:** 2026-10-03 (as built); amended 2026-10-14
**Decides:** what a cabinet, a filler or a countertop on a vendor's drawing is in the data, and who
may make one (#748, #852, #868, #882, #893, #897, #913; amendment #1244)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent, recording what was built. Adopted as built by the admin on 2026-10-03
> (decision log: "ADR-0021 adopted as built"; the fifth table, `countertop_run_decisions`, the same
> day, #893). The amendment below was decided by the admin on 2026-10-14 (phase 4, decisions 1, 2
> and 4: "yes to all 5"). Until #1244 the record lived only in the decision log and in the
> docstrings of `app/models/drawing.py` and `workflow/parts.py`; this file is it, written down.

## Context

A countertop rule is about *this countertop*, *that cabinet*, *this filler*: the drawing's items.
`AGENTS.md` §2.3 says AI creates candidates, not facts, and `app/models/drawing.py` says why: an
item made as a fact would be a second, unguarded route into the verdict, because whatever writes
`drawing_items` decides what a check is about. On the client's drawings, code can *suggest* parts
from the dimensions drawn end to end and the cabinet codes printed (#868), and from the countertop
rows the AI readers read (Bala, #1236), but neither is right often enough to stand alone.

## Options considered

1. **Code writes items directly.** Rejected: one wrong suggestion becomes a fact a check reads,
   with nobody having looked.
2. **A person marks every part by hand.** Rejected: dozens of parts per drawing, and the computer
   can already find most of them.
3. **Code suggests, a person confirms each one.** Chosen (2026-10-03), and amended on 2026-10-14
   for one narrow case (below).

## Decision (as built)

- **A suggestion is not a part.** `part_proposals` holds what code suggests (a cabinet, a filler or
  a countertop), with its outline in stored page space, the code read on it as printed, the line
  that defined it, what suggested it and why. Writing one writes no item.
- **A confirmation makes the part.** `part_confirmations` holds each decision on a suggestion:
  `confirmed` (it names the one `drawing_items` row it made, and a `catalogue` identifier when a
  code was kept) or `withdrawn` (it makes nothing). Each decision names the one it replaces; the
  schema keeps at most one current decision per suggestion.
- **One writer.** `workflow/parts.py:confirm_part` is the only code that writes `drawing_items` or
  `item_identifiers`; a guard in `tests/db/test_drawing_models.py` fails if anything else does.
- **Item types** are `cabinet_width`, `filler_width` and `countertop_overall_width`
  (`vocabulary/part_kinds.py`). An item is created with `corroborated = False`: the confirmation is
  the authority, as `view_role_confirmations` is for a drawing's role.
- **A corrected or withdrawn part stays on record but is never read**: readers keep to
  `live_part_item_ids` (#882).
- **A countertop's outline is its own**, never the union of the parts beneath it; otherwise "the
  run reaches both ends" would be true by construction.
- **Further tables, all append-only, none granted to the verdict role:** `countertop_runs` and
  `countertop_run_decisions` (a person's decision on the run beneath each countertop, #893),
  `reading_parts` (which confirmed reading is a part's width, #913), `part_pictures` (every
  suggestion's own picture, #897) and `part_proposal_rows` (which countertop row and piece each of
  Bala's suggestions came from, #1236).
- **Parts are confirmed only on a drawing confirmed as the vendor's**; withdrawing is allowed on
  any drawing. A width is never read from a code's digits or a box's size.

## Amendment (2026-10-14, #1244): code may confirm a countertop on two judgments

This replaces the 2026-10-03 rule "a person always confirms the parts" **for this narrow case
only**, following the precedent of code's view roles (decisions D2/D3 of 2026-10-09,
`workflow/view_roles.py:confirm_view_role_by_code`).

- **What:** code may confirm one of Bala's *countertop* suggestions with no click, through
  `workflow/parts.py:confirm_part_by_code`, which delegates to `confirm_part` (still the only
  writer). It is recorded under `confirmed_by = "code:bala-two-judgments/v1"`, audited as
  `EVIDENCE_QUALIFICATION`, with no code and `corroborated` left `False`.
- **When: two independent judgments agree** (`workflow/bala_items.py`):
  1. **the AI readers:** both row readers named this very row (so code did not pick it); the page
     is not one they split on; the row carries no hold of any kind (row hold, ambiguity, missing
     widths, a check hold, GV's markup over it); no reader said no stone is drawn over it; Rekha's
     gate did not stop it; no reviewer row decision typed widths into it;
  2. **code:** the row's printed overall exists and its pieces tile it; there is one reading per
     piece, as many as the row's `row-slot-count`, their boxes meeting end to end, every one and the
     overall sealed by both readers; and the overall and every piece lie wholly inside exactly one
     drawing (`app/evidence/sides.py:drawings_holding`) whose role a confirmation says is the
     vendor's.

  Either judgment missing leaves a suggestion for a person's one click, and the stage records why.
- **A person always wins, before and after code confirms.** Before: code writes nothing on a
  suggestion anyone has decided, nor where a person's current decision (confirmed or withdrawn)
  stands on a countertop at the same place on the same drawing (`why_code_may_not_confirm`).
  After: on every match, code takes its own item back where a person has since decided a
  countertop at the same place, so a person adding a countertop over code's item of **this**
  revision, or deciding an older suggestion while match confirmed a newer overlapping one, ends
  with the person's alone. Where code's item came from another revision's reading, code does not
  take it back (below): both stay live until a person undoes code's. **The take-back waits for
  the next match**, which runs only in the full pipeline (a new reading, or a re-run), so the two
  overlapping countertops can stand until then, possibly up to sign-off; while they do, the drawing
  only holds back (no item is a check scope unless a person stands by it, and two countertops on a
  drawing make its checks abstain). A person can end it at once with Undo. A person undoes code's
  item on the Measure page ("Found by code: two checks agreed · Undo", a withdrawal), and a
  person's later decision on it replaces code's like any other. "The same place" is outlines
  overlapping from left to right on one drawing.
- **A person's run beneath code's item is a person's decision about it** (re-review of #1248).
  A person may confirm (or withdraw) the run beneath code's countertop, which then becomes a
  checked countertop. **Code never takes that item back**, whatever a later read says: a newer
  reading, a row that stops agreeing, a person's countertop over it. The match result lists it
  under `kept` ("kept: a person confirmed a run beneath it", with why it would otherwise have
  gone), and `withdraw_part` in code's name refuses it. Taking it back would leave the person's run
  on a dead item and turn its check into "No live confirmed countertop run remains", hiding a real
  result. A person removes it with Undo.
- **No two overlapping countertops by code.** Code also writes nothing where a live countertop's
  outline overlaps on the same drawing, whoever made it, and says why: "a person confirmed a run
  beneath the countertop here" for code's item kept under a person's run, otherwise "an earlier
  revision's countertop stands here". Side-by-side countertops on one drawing do not block each
  other.
- **Code takes back only its own item, and only one this revision's reading made**
  (`release_contradicted_code_part`, as `release_contradicted_code_role` does for roles): when a
  newer slot-reader run of this revision replaces the row, when the newest read no longer gives
  both judgments, or when a person has since decided a countertop at the same place. A new
  revision carries every unchanged document at the version it had (`app/lifecycle/supersede.py`),
  so two revisions share drawings, and the older one may be signed off: code never writes onto
  another revision's records (it finds its items by the reading's run, `package_revision_id`).
  Such an item stays live and is listed on the new revision's Measure page with Undo, which is a
  person's decision and so allowed there. `withdraw_part` in code's name refuses unless the current
  decision is code's own confirmation, with no person's run beneath it.
  **The other way round is allowed, as it is for a person's decisions:** a new revision's match can
  still add code's item on a drawing it shares with a signed-off revision. That item is visible on
  the signed-off revision's Measure page, is not counted against its sign-off (code's decisions are
  not reviewer input, below), and changes none of its findings, which are frozen with its sign-off.
- **Pieces stay a person's click** (decision 2): cabinets and fillers are never confirmed by code.
- **Off by default**, behind `GV_BALA_AUTO_ITEMS`. **Off is not a rollback:** switching it off stops
  code making new items, but code's items whose two judgments still agree stay live. Code takes one
  back only for the reasons above; a person removes one with Undo.
- **Not reviewer input.** Code's part decisions (making or taking back its item) are not counted as
  inputs newer than the checks for sign-off (`app/review/approval.py`), as code's drawing roles are
  not.
- **Only two callers of `confirm_part`:** `confirm_part_by_code`, and the Measure page's own
  (`app/evidence/parts.py:confirm_listed_part` and `add_part`), which refuse any actor naming code
  (`code:`). A second guard in `tests/db/test_drawing_models.py` holds this, by the function's
  name wherever it is read: an import from any module (re-exported or relative, renamed or not),
  any `.confirm_part` attribute, and the text `"confirm_part"` (`getattr`) outside `__all__`.

## Consequences

- Easier: a countertop both AI readers and code agree on needs no click, so later steps (per-item
  plans, results grouped by countertop) have items to work with.
- Harder: an item's `confirmed_by` may now name code; anything that asks "did a person decide this?"
  must look at it (`workflow/parts.py:by_code`).
- Forbidden: code confirming a piece; code confirming over a person or over any live countertop;
  code confirming through any route but `confirm_part_by_code`; code taking back anything but its
  own item, or an item another revision's reading made; a countertop run written by anything but a
  person's decision.

## Safety impact

None on the false-PASS rate, and no change to any finding. Code's items can never create a PASS,
and change no finding, because the countertop checks are scoped only to a countertop a person
stands by: `workflow/part_operands.py:countertop_scopes` returns `None` until a person's
`CountertopRunDecision` exists, and after that it lists only countertops a person confirmed, or
that a person confirmed (or withdrew) a run beneath. Code's item alone is never a scope, so it
never takes a page from its countertop row (review of #1248: before this, one person-confirmed run
anywhere in the revision turned code's page into a "Confirm this countertop's run" question that
hid the row's real result). Code never writes a run or a reading link, and no item sets
`corroborated`. CT-WIDTH and CAB-FILLER read exactly what they read before, with or without
code's items, until a person decides a run beneath one of them; from then on code never takes
that item back, so a later read cannot hide the run's result. The $0 proof on the four stored
sets (#1244) found no automatic countertop on a page or row that is not a countertop, and findings
byte-identical with the switch off and on; the re-runs after the reviews add a person's run beside
code's item, and a person's run beneath it followed by a newer reading.

**Caveat: "identical with the switch on or off" holds until #1247 (items used) merges.** After
it, a drawing holding more than one live countertop withholds its readings, so code's item beside
a person's countertop on the same drawing (until the next match takes it back, or for ever when it
came from another revision) can turn that drawing's PASS into REVIEW_REQUIRED ("drawing that holds
more than one confirmed countertop"). That is the safe direction: code's items can then only
withhold, never move a result toward PASS.

## Unblocks

#1244 (this amendment), and phase 4 PR 3 (items used: per-item assembly that only withholds, a plan
per item, results grouped per countertop on screen).
