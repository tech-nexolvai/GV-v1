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
- **A person always wins.** Code writes nothing on a suggestion anyone has decided, nor where a
  person's current decision stands on a countertop at the same place on the same drawing. A person
  undoes code's item on the Measure page ("Found by code: two checks agreed · Undo", a withdrawal),
  and a person's later decision replaces code's like any other.
- **Code takes back only its own item** (`release_contradicted_code_part`, as
  `release_contradicted_code_role` does for roles): when a newer slot-reader run replaces the row,
  or the newest read no longer gives both judgments.
- **Pieces stay a person's click** (decision 2): cabinets and fillers are never confirmed by code.
- **Off by default**, behind `GV_BALA_AUTO_ITEMS`.
- **Only two callers of `confirm_part`:** `confirm_part_by_code`, and the Measure page's own
  (`app/evidence/parts.py:confirm_listed_part` and `add_part`), which refuse any actor naming code
  (`code:`). A second guard in `tests/db/test_drawing_models.py` holds this.

## Consequences

- Easier: a countertop both AI readers and code agree on needs no click, so later steps (per-item
  plans, results grouped by countertop) have items to work with.
- Harder: an item's `confirmed_by` may now name code; anything that asks "did a person decide this?"
  must look at it (`workflow/parts.py:by_code`).
- Forbidden: code confirming a piece; code confirming over a person; code confirming through any
  route but `confirm_part_by_code`; a countertop run written by anything but a person's decision.

## Safety impact

None on the false-PASS rate. Code's items can never create a PASS: the countertop checks read
items only once a person has confirmed a countertop's run (`workflow/part_operands.py:
countertop_scopes` returns `None` until a person's `CountertopRunDecision` exists), and code never
writes a run or a reading link. CT-WIDTH and CAB-FILLER read exactly what they read before, and no
item sets `corroborated`. The $0 proof on the four stored sets (#1244) found no automatic countertop
on a page or row that is not a countertop, and findings byte-identical with the switch off and on.

## Unblocks

#1244 (this amendment), and phase 4 PR 3 (items used: per-item assembly that only withholds, a plan
per item, results grouped per countertop on screen).
