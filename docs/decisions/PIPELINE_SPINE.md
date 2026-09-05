# The mechanical spine: what runs today, and what it is waiting for

The pipeline reads a PDF, records what was on its pages, and cuts a picture of every reading. It does
not say what any of it means. This records where that boundary falls and why it is where it is, so the
next person to work on `workflow/stages.py` does not mistake a deliberate stop for an unfinished one.

Written 2026-09-06 alongside #517.

## What is wired

| Stage | What it does now |
|---|---|
| `ingest` | Fetches each document, checks its bytes against the SHA-256 recorded at upload, checks it still parses and still has the recorded page count. |
| `extract_pages` | Reads pages, persists the manifest, opens an extraction run, records observation candidates — vector text, or OCR for a scanned page. |
| `validate_evidence` | Renders each page that produced candidates and cuts a real crop per candidate through `evidence/crop.py`, persisting an `evidence_artifacts` row. |
| `match` | Runs the real exact lane over the revision's drawing items and persists `match_candidates`. |
| `run_checks` | Runs the rules and records findings. |
| `generate_outputs` | Still a stub. |

**This is drawing-agnostic on purpose, and it is tested that way.** The end-to-end test runs on a real
PDF committed to this repository — our own design document, not a drawing — because the client's
drawings are proprietary and #274 has not landed. Nothing in the mechanism asks what the document is
about, which is exactly the property that test demonstrates.

**Expect to tune it against the real GV drawings when #274 lands.** Two things in particular are
starting points rather than measured values: `CROP_CONTEXT_MARGIN_PT` (how much page a crop keeps
around a reading — an eighth of an inch, chosen to show a dimension line either side of its text) and
the 150 dpi the reader and the rasteriser share. Neither is wrong; neither has been checked against a
sheet a fabricator actually sent.

## Where it stops, and why each stop is where it is

**Candidates stay untyped.** Nothing assigns a `semantic_guess`, so nothing mints a canonical
observation, so nothing becomes eligible as a verdict operand — `evidence/gate.py` takes a canonical
observation and there are none. The value-to-meaning association needs the real drawings (#274) and
the vocabulary Q20 defers, and `CLIENT_FACTS` Q20 records Raj's own words: the tags are provisional
and final ones come after the layouts are settled. A heuristic here would look like progress and be a
fabricated fact in a review. `test_nothing_in_the_pipeline_gives_a_candidate_a_meaning` is the guard.

**`match` finds nothing yet, and says so.** A `match_candidates` row needs two `drawing_items`; an
item needs a view and a type from the `CT0xx` vocabulary; and nothing detects a view or an item on a
page. `extraction/model/` reasons about items it is *given* — `view_containing`, `contains`,
`resolve_assembly` — and does not find them. Both missing pieces are semantic, so the stage is wired
to the real matcher and returns an honest zero with the reason. When item detection exists this runs
unchanged.

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

## The one thing this made visible

`ingest` reports a failed digest; it does not stop the pipeline. A revision whose document no longer
matches its recorded hash should never reach `extract_pages`, and that belongs in an entry condition
on the next stage — which does not exist. Raising instead was rejected for the reason #491 gave: a
corrupt artifact is not transient, so raising would roll the claim back and retry the same broken file
for ever. Today the failure is visible and a human acts on it. That is a real gap, named here rather
than papered over.
