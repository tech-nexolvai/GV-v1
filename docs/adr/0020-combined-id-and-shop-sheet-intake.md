# ADR-0020 — A sheet may carry both the ID set and the vendor shop drawing

**Status:** Accepted        <!-- Proposed | Accepted | Rejected | Superseded by ADR-NNNN -->
**Date:** 2026-09-28
**Decides:** how a drawing that carries both the architectural and the shop view is ingested, classified and matched (#705)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. Only the admin may set `Status: Accepted`.
> Accepted by the admin (AnantBisht07) on 2026-09-28, who directed the unblock after the finding in
> #705 showed the intake model cannot accept the client's own drawings.
> `scripts/ratify.py D20 --adr docs/adr/0020-combined-id-and-shop-sheet-intake.md` rewrites #705 to
> `status: ready`; the issue gate keeps it blocked until this status reads Accepted.

## Context

We believed the client had never supplied an architectural set, and `#274` has blocked ~12 stories
and every release gate on that belief since the beginning of the project.

It is not true. `data/drawings/aiset2/AI_Set_2.pdf` — in the repository since the start — carries an
`ID SET ELEVATION` panel on **13 of its 16 content pages**, directly above a
`VENDOR'S SHOP DRAWING ELEVATION` panel of the same room and the same elevation. Page 13, Board
Room 1:

| Panel | Contents |
|---|---|
| `ID SET ELEVATION` | `BOARD ROOM 1 ELEVATION`, `8/ID 5.1`, `1/4" = 1'-0"`, dimensions `6'-0"` `3'-1"` `9'-0"` `7'-0"` `1'-2"` `1'-9"`, tags `PT-19` `VB-01` |
| `VENDOR'S SHOP DRAWING ELEVATION` | `ELEVATION A`, cabinets `B15L` `B36` `B18R`, panels `WD-1`, dimensions `914 [36]` `457 [18]` `724 [28 1/2]` `102 [4]`, `74-1/2" (VIF)`, `80-1/2" (VIF)`, note *"Scribe to fit (5" filler to field cut as required on site)"* |

The two halves even use different conventions: the ID set is dimensioned in **feet-inches**, the
vendor half in **mm [inch]**. Both on one page.

**Our intake cannot accept this.** `app/api/documents.py:529` refuses the client's own file:

> *Upload exactly one confirmed architectural PDF and one confirmed shop PDF before AI reading can
> start. Missing: architectural.*

That refusal is correct against our data model and wrong about the world. It is why `match` reports
`items 0, candidates 0` on every run, and why the real set has never gone end to end.

This is our own data-model choice, not a value the client owes — which is why it is
`needs-architecture` rather than `blocked-client`.

## Options considered

**A — require the client to split each sheet into two PDFs.** Rejected. It asks the client to
re-author documents their whole business produces in this shape, makes every package begin with
manual surgery, and throws away the most valuable property the sheet has: the two views are
*already paired*, same room, same elevation, drawn one above the other.

**B — split one upload into two synthetic documents at ingest.** Rejected. It fabricates
`document_versions` that no uploaded bytes correspond to. ADR-0018 makes document identity the hash
of what was actually supplied, and the storage key *is* that hash; two derived documents from one
file would each claim a provenance they do not have. A reviewer clicking through to the source would
arrive at a document the client never sent.

**C — the arch/shop role belongs to the view, not to the document.** Chosen.

## Decision

**A document may carry both roles. The role is a property of a `DrawingView` — a titled region of a
page — and not of the `Document`.**

1. `DrawingView` gains a nullable `role` (`ARCH` | `SHOP`). `NULL` means *not yet established*, and
   is never guessed. A view with no confirmed role takes no part in matching.
2. `Document.kind` is unchanged and keeps its meaning: what the client said they sent. It stops
   being the thing that decides which side of a comparison a reading is on.
3. `MATCH_ROLES` resolves a role from the **view** first, falling back to `Document.kind` for a
   genuine two-PDF package. Both intake shapes keep working; neither is privileged.
4. **The panel role is proposed by a model and confirmed by a person — never auto-assigned.** The
   heading is plain English on the sheet (`ID SET ELEVATION`), but it is drawn as outlined vector
   paths with no text layer (`pdfplumber` reports 0 characters on page 13), so reading it is a model
   task. This reuses the machinery that already exists for exactly this shape of question:
   `BedrockClosedQuestionReader`, `layout_proposals` and `layout_confirmations` (migration 0047).
5. **The intake gate stops refusing at the door.** A package with one combined document is accepted.
   Whether both roles are actually present is a fact about the *views*, which are not known until
   extraction has run — so refusing at upload is refusing on a fact we have not yet measured. The
   `match` stage abstains, with a reason, when it cannot find both roles.

## What this does not change

- **The verdict path is untouched.** No operand, no arithmetic, no tolerance, and nothing in
  `verdict/` is affected. Exact `Fraction` arithmetic still decides, and a reviewer still signs off.
- **Nothing auto-types a value.** A panel role is not a semantic type; it says which half of a sheet
  a region is, and it still requires a human confirmation before it is usable.
- **Inches remain authoritative** (Q12). That the vendor half is dimensioned in mm [inch] changes
  nothing: mm may corroborate that an inch was read correctly and is never a verdict operand.
- **Document identity** stays the hash of the supplied bytes (ADR-0018).

## Consequences

- `#274` is substantially wrong as written and must be rescoped: the arch set is here, and what the
  client still owes is *more projects* and *a known-correct answer per check*, not this file.
- Arch↔shop matching was scoped in `docs/V1_TARGET_AND_GAP.md` §4 as the hard 2–3 week phase gated
  on client data. It is neither gated on the client nor as hard — the sheet pairs the views for us.
- A migration adds a nullable column to `drawing_views`, which is a mutable table; no append-only
  guarantee is touched.
- Some sheets have no `ID SET` panel (3 of 16). Those pages simply yield one role, and a check that
  needs both abstains — which is the existing behaviour and the correct one.
