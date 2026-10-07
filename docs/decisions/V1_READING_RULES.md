# V1 reading rules — code-traced register

This register describes the reading path merged on `main` at `3db9543` for issue #990. It is a
description of enforced behavior, not permission for a reader to decide a verdict. The reader
produces proposals; evidence qualification and the published, deterministic rule decide what can
be checked; a person reviews and signs off. A missing or disputed input remains for the person.

| # | Frozen behavior | Enforced by | Synthetic verification |
|---|---|---|---|
| 1 | A package's product is an explicit choice. The form and slot questions record that choice; neither infers it from an answer. | `app/api/packages.py:create_package`; `workflow/stages.py:_run_form_reader_for_document`; `workflow/slot_reader.py:configured_slot_reader` | `tests/workflow/test_slot_reader.py`, `tests/extraction/slot_reader/test_bedrock.py` |
| 2 | A whole-page form value counts only on exact agreement by two different makers. An overall must be scoped to the countertop run, not a wall or appliance span. Multiple countertops on a page stay with the person. | `extraction/form_reader/agreement.py:compare_page_answers`; `extraction/form_reader/mapping.py:map_page_to_fields` | `tests/extraction/form_reader/test_form_reader.py`, `tests/eval/test_form_first_adversarial.py` |
| 3 | A model's location is for a review picture, never for setting a number. A missing location or reviewer-coloured/covered label cannot become an accepted form value. | `workflow/form_reader.py:guard_located_comparison`, `apply_ink`; `extraction/form_reader/locator.py` | `tests/extraction/form_reader/test_form_reader.py`, `tests/eval/test_form_first_adversarial.py` |
| 4 | The slot reader may propose from a row only when its choice and each label's owning slot are certain. A missing or competing label is not chosen by confidence. | `extraction/slot_reader/runs.py:choose_row`; `extraction/slot_reader/seal.py:owner_outcome`; `extraction/slot_reader/mapping.py:map_row` | `tests/extraction/slot_reader/test_runs.py`, `tests/extraction/slot_reader/test_seal.py`, `tests/extraction/slot_reader/test_kinds_and_mapping.py` |
| 5 | A slot label needs independent, identical printed text: the file's own text plus one reader, or two readers of different makers for glyphs. Code parses the agreed text into exact inches; model-supplied numeric components do not set it. | `extraction/slot_reader/seal.py:seal_label`, `normalise_text`; `extraction/slot_reader/labels.py:plain_dimension`; `units/notation.py` | `tests/extraction/slot_reader/test_seal.py`, `tests/extraction/slot_reader/test_labels.py` |
| 6 | Reviewer ink, unknown ink, a cut-off edge, crowded labels, an uncertain row or slot, and a stacked fraction without the stated permission hold the reading for review. A guard can withhold but cannot repair or create a value. | `extraction/slot_reader/seal.py:seal_label`; `extraction/ink.py:read_page_ink` | `tests/extraction/slot_reader/test_seal.py`, `tests/workflow/test_slot_reader.py` |
| 7 | Drawn length may veto an agreed reading when other sealed, non-stacked pieces establish a scale. It never changes the text or supplies the replacement width. | `extraction/slot_reader/veto.py:drawn_length_vetoes`; `workflow/slot_reader.py` | `tests/extraction/slot_reader/test_veto.py`, `tests/eval/test_form_first_adversarial.py` |
| 8 | Vendor-layer words for a tall appliance or range bay inside the row's own drawing box and slot span hold the whole row. Undercounter and microwave-oven phrases, words outside that span, and reviewer ink do not trigger this particular guard; other guards may still hold them. | `workflow/slot_reader.py:_counter_break_row_hold`; `extraction/slot_reader/labels.py:counter_break_hold` | `tests/workflow/test_slot_reader.py` |
| 9 | Only the two approved worded-label forms are expanded by exact arithmetic on agreed text. A width marked as already including field cut, or provisional for field verification, holds the whole row. | `extraction/slot_reader/labels.py:expand_label`, `row_hold`; `extraction/slot_reader/seal.py:seal_label` | `tests/extraction/slot_reader/test_labels.py`, `tests/extraction/slot_reader/test_seal.py`, `tests/eval/test_form_first_adversarial.py` |
| 10 | Wall layout can be proposed from two different makers' agreement only when the row is certain and the hatch check does not object. Every row in the newest run must seal the same layout before a revision-wide reader proposal is usable; a person's recorded choice wins. | `extraction/slot_reader/walls.py:seal_walls`; `workflow/layout_proposals.py:reader_sealed_wall_config`, `confirmed_discriminators` | `tests/extraction/slot_reader/test_walls.py`, `tests/app/test_layout_proposals.py` |
| 11 | A complete countertop row offers its overall and **all** piece widths in left-to-right order. CT-WIDTH-001 compares exact inch totals plus the published field-cut allowance selected by the explicit wall layout; conflicting or absent inputs abstain rather than pass. | `extraction/slot_reader/mapping.py:map_row`; `rules/rulebook/ct_width_001.yaml`; `verdict/operations/aggregate.py`, `verdict/engine.py` | `tests/extraction/slot_reader/test_kinds_and_mapping.py`, `tests/workflow/test_ct_width_pieces.py` |
| 12 | These readings are proposals, not check operands. The candidate and its private per-attempt raw answers are retained; the evidence/semantic-type boundary and the person's save or confirmation separate a reading from a checked input. The person signs the resulting findings. | `workflow/slot_reader.py:persist_slot_readings`; `workflow/stages.py:_record_reader_attempts`; `app/api/confirmations.py`; `workflow/evidence_operands.py` | `tests/workflow/test_slot_reader.py`, `tests/eval/test_form_first_product.py`, `tests/workflow/test_mixed_fraction_guard.py` |

## Two-way trace and limits found by #990

- Each row above points to a current enforcer and a synthetic test. The form reader's no-row
  fallback is covered by rules 2–3; label/crop holds and reviewer-only candidates are covered by
  rules 4–9 and 12. This is the safety register, not an inventory of every rendering or OCR
  heuristic.
- The two GV-marked sets produced no accepted width proposals in the fresh product run. The
  rules therefore showed abstention, but did **not** yield a measurable automatic CT-WIDTH-001
  comparison—even on the page with independently confirmed wall truth. Zero observed false-PASS
  over zero automatic verdicts is not a release-rate measurement.
- The first version of the evaluation bridge overlooked product-qualified slot prompt ids and
  counted all invocations of a run again for every page without a proposal. Evaluation-only
  fail-first tests caught both; the corrected audit counts each stored page attempt once. No
  product reader, rule, or verdict behavior was changed.
- Append-only invocations contain every stored wall, slot-label and whole-page attempt, including
  raw answers where a provider returned one. They do not contain a per-crop invocation key, so a
  saved answer can be checked against the multiset of candidate texts but cannot be assigned to
  one particular identical crop from the invocation alone. That provenance limit must stay visible
  in any claim of complete audit accounting.
- Coverage on clean as-sent vendor drawings remains unmeasured. The present keys are marked-up
  reviewer copies; they must not be used to promise a fill rate for clean files.
