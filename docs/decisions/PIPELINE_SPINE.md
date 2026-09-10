# The mechanical spine: what runs today, and what it is waiting for

The pipeline reads a PDF, records what was on its pages, cuts a picture of every reading, runs the
rules and writes the results out as a workbook. It does not say what any of the readings mean. This
records where that boundary falls and why it is where it is, so the next person to work on
`workflow/stages.py` does not mistake a deliberate stop for an unfinished one.

Written 2026-09-06 alongside #517; extended for #519, which wired the last stage.

## What is wired

| Stage | What it does now |
|---|---|
| `ingest` | Fetches each document, checks its bytes against the SHA-256 recorded at upload, checks it still parses and still has the recorded page count. |
| `extract_pages` | Reads pages, persists the manifest, opens an extraction run, records observation candidates — vector text, or OCR for a scanned page. |
| `validate_evidence` | Renders each page that produced candidates and cuts a real crop per candidate through `evidence/crop.py`, persisting an `evidence_artifacts` row. |
| `match` | Runs the real exact lane over the revision's drawing items and persists `match_candidates`. |
| `run_checks` | Runs the rules and records findings. |
| `generate_outputs` | Builds a findings workbook for the revision — outcome, rule, comparison, operands, snapshot — stores it and records an `output_artifacts` row. |

All six stages now do work. The loop closes: a package is verified, read, evidenced, matched,
checked, and turned into a file somebody can be handed.

**This is drawing-agnostic on purpose, and it is tested that way.** The end-to-end test runs on a real
PDF committed to this repository — our own design document, not a drawing — because the client's
drawings are proprietary and #274 has not landed. Nothing in the mechanism asks what the document is
about, which is exactly the property that test demonstrates.

**The reader and rasteriser now share a measured 300 dpi setting.** The original 150 dpi starting
point missed all five PM-confirmed vendor dual-notation dimensions on Board Room 1 page 13. For a
stamp-only vendor drawing whose deployment has supplied the association geometry settings, OCR now
uses the line-selected outlined candidate regions as bounded vendor-only crops at the measured 600-DPI crop
resolution, then maps the reading back to the shared 300-DPI page frame for evidence and association.
It never rasterises reviewer markup into those crops. The localized route also requires its explicit
minimum-path, maximum-span, and crop-context settings; a page with no selected region abstains rather
than returning to a full-page nearest-text guess. Pages without localized settings retain the ordinary
full-page OCR route. The 40-megapixel ceiling still protects full-page rendering; crop
rendering keeps the high-resolution lane bounded. `CROP_CONTEXT_MARGIN_PT` remains a starting point
(an eighth of an inch, chosen to show a dimension line either side of its text) and still needs
broader drawing coverage.

## Where it stops, and why each stop is where it is

**Candidates remain raw and untyped.** Nothing writes `semantic_guess`.  A deployment may opt into
the narrow semantic-typing gate only with an explicit layout vocabulary: an exact vector tag and a
numeric reading must share the same already-resolved dimension line before a separate
`CORROBORATED` observation is minted. OCR tags, position heuristics, conflicting tags and all agent
suggestions are `REVIEW_REQUIRED`; they never become operands. Q20 is final only for the three-sided
countertop layout, so there is no global tag configuration. `docs/decisions/SEMANTIC_TYPING_GATE.md`
records the proof and the rollout metric. The drawing-agnostic default stays with zero canonical
observations, and `test_nothing_in_the_pipeline_gives_a_candidate_a_meaning` still guards that state.

**`match` finds nothing yet, and says so.** A `match_candidates` row needs two `drawing_items`; an
item needs a view and a type from the `CT0xx` vocabulary; and nothing detects a view or an item on a
page. `extraction/model/` reasons about items it is *given* — `view_containing`, `contains`,
`resolve_assembly` — and does not find them. Both missing pieces are semantic, so the stage is wired
to the real matcher and returns an honest zero with the reason. When item detection exists this runs
unchanged.

**Evidence-grounded redline output is wired.** The optional redline artifact is built only when a
live finding has a sealed `VerdictInput` pointing to a typed canonical observation with stored-space
geometry on a recorded page transform. It never draws from raw candidates, reviewer-entered literals
or trace display text. Findings without a qualifying location remain in the redline summary rather
than acquiring a plausible-looking box. The artifact is generated for internal review and, like the
other review outputs, remains download-blocked until package sign-off.

**The workbook now fills every column (#525).** `findings` stores the outcome, severity, trace and
parameter versions, and — since #525 — a decision's prose reason, its delta, its applicability variant
and its notes as well. Earlier this paragraph recorded those four as an unfilled gap: they lived only
on `verdict.finding.Finding`, the engine's value type, and `record_finding` had never persisted them.
Rebuilding the value type from display text was rejected (that path is how `984 mm` once became
984 inches — see the dual-unit lane, #529); instead the four fields were made storable directly, which
was the schema change this note said was worth doing deliberately. It is done.

**Page classification is not wired, though the classifier is built.** `extraction/page_type.py:classify`
takes a `PageText` whose `title_block` and `view_tags` are *separate* fields, and its precedence rule
depends on telling them apart — a title block describes this sheet, a view tag points at another.
Nothing locates a title block on a page. Passing every line as `title_block` would produce confident
classifications from a distinction that was never made, so `Page.page_type` stays null, which is what
`extraction/manifest.py` already says it does until B6.2 (#161).

**Text association is not wired, for a stated reason rather than an oversight.**
`extraction/geometry/text_association.py:associate` has real inputs — the reader returns both text
runs and line segments — but it requires `proximity_limit` and `ambiguity_margin`, and the module
deliberately gives neither a default because both are judgements about how real drawings are
dimensioned. Choosing numbers without a real sheet would be inventing exactly the drawing-specific
tuning this spine avoids. There is also nowhere to put the answer: no table records an association.

## The gap this made visible, since closed

`ingest` reported a failed digest and the pipeline read the document anyway — the check that would
catch a corrupted drawing had already run and already knew. #523 closed it in `extract_pages`, which
verifies the bytes against the recorded SHA-256 before reading them and records a
`document_digest_mismatch` failure instead. Checking there rather than trusting the earlier report
also closes the window between the two stages: the artifact could change in between, and the place
that reads a document is the right place to establish it is the right document.

Still recorded rather than raised, for the reason #491 gave: a corrupt artifact is not transient, so
raising would roll the claim back and retry the same broken file for ever. The row is what keeps a
refused document from looking like a document with nothing on it.

`ingest` still reports rather than gates, and that is now redundancy rather than a gap — it tells a
human early, and `extract_pages` is what actually refuses.
