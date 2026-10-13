"""Pariksha, the evaluator: per-agent scorecards from stored runs, stored, and compared (#1219).

**Why.** No reading agent had a stored score: on 2026-10-13 a weak row choice was found only by
digging through logs. Every agent's quality has to be measurable and comparable, so a new prompt,
model, picture or code version ships only when it is **better and gives 0 new wrong answers**.

**What it does, at $0.** It scores an agent from a run that already happened: the answers stored in
`model_invocations` (the readers' raw replies, read again by the repository's own reader code) or in
`observation_candidates` (the architect reader's spans), against an answer-key file. No model is
called and the run's database is only read, inside a read-only transaction.

The three phase-1 agents:

* **Akshara**, the number reader (label questions, `claude-slot-span-*`): per reader right, wrong
  (a confident wrong text), unsure and invalid; per pair agreed-right, **agreed-wrong** and held;
  cost per question from `model_invocations.cost_micros`.
* **Rekha**, the row choice (`slot-row-choice-*`): per pair agreed-right, **agreed-wrong** and
  split; per reader right, wrong, unsure and invalid.
* **Vastu**, the architect reader: `eval/architect_reader.py`'s found, held, missing and extra
  values, from the spans a run stored.

## The key file (`pariksha-key/1`)

One JSON object. The keys themselves hold client values and stay local (`~/gv-local/`, never git);
the tests use invented ones.

    {"format": "pariksha-key/1", "agent": "akshara" | "rekha", "name": "...", "version": "...",
     "cases": [{"family": "claude-slot-span", "page_sha256": "<64 hex>",
                "question_id": "p2:slot0:0", "slot_box": "462,1968,1597,2102",
                "picture_sha256": "<64 hex, optional>",
                "answer": {"texts": ["3/4\\""]} | {"no_label": true}, "source": "..."}]}

* **A case names its question by what the question is, never by a database id**: the prompt
  family (the prompt id without its `-vN`), the page's content hash (`source_page_sha256` in the
  question packet, the same in every database that read the same PDF), and the question id the
  code gives it (`p<page>:<row><slot>:<label>` for a label, `p<page>:row-choice` for a row). The
  same question asked again, by a new prompt version or in another database, matches the same
  case; that is what lets two versions be compared.
* **`slot_box` (optional, labels)**: the span's box as the run recorded it (`slot-box:` on the
  question's candidate). Slot numbers follow the chosen row, so on another row `slot2` is another
  span. A key case with a box only scores a question about that same box; any other is reported
  "not asked (a different span)", never scored against the wrong answer.
* **`picture_sha256` (optional)**: a picture the question must have shown. A row answer is a box
  number, and box numbers belong to one numbered picture: a row case should name it, so a run
  whose code numbered the boxes differently is reported "not asked (a different picture)", never
  scored against numbers that now mean other lines.
* **Label answers**: the accepted printed texts (compared after `seal.normalise_text`), or
  `{"no_label": true}`: the span has no printed label of its own.
* **Row answers**: `{"box": n}` (the numbered box that is the countertop row), or
  `{"kind": "no_countertop"}` / `{"kind": "not_among_boxes"}`.
* **Vastu** reuses the architect key's own page shape (`eval/architect_reader.key_pages`):
  `{"format": "pariksha-key/1", "agent": "vastu", "name", "version", "document_sha256": optional,
  "pages": [{"page": 1, "arch_widths": [{"text": "...", "in": 24}]}]}`. The document is named by
  its file's hash (`document_versions.sha256`), the same in every database.

Unknown fields are refused, so a typo cannot silently drop a guard.

## The scorecard (`pariksha-scorecard/1`) and why it is a file, not a table row

A scorecard is one immutable JSON file: the agent, the key (name, version, hash), the stored run's
code version, the prompt and model ids (`extractor_versions`, the shape `evaluation_runs` uses),
where the answers came from, every metric as an exact numerator and denominator (`None` = not
measured, never 0), and one line per case. It is written once (never overwritten) and carries the
hash of its contents, so an edited file is refused.

The `evaluation_runs` / `metric_results` / `case_results` tables do not fit without a migration:
`case_results` must point at a `gold_cases` row, which must point at a `document_versions` row of
the same database (`RESTRICT`); a stored run lives in its own database, so scoring it there would
write into the run being measured, and a central evaluation database would need a made-up project,
package, document and file for every drawing. `record_run` also requires rule snapshots and
verdict outcomes (PASS / FAIL / REVIEW), which an agent's answers are not. So the scorecards are
versioned JSON, and they reuse what fits: `eval.metrics.MetricResult` for exact rates,
`eval.regression.MetricDelta` for before → after per metric, and `eval.runs.MissingProvenanceError`
for a run with no code version.

## Compare

Per metric (before → after) and per case (fixed, broke, unchanged). The verdict is
`ACCEPT (better, 0 new wrong)`, `REJECT (n new wrong: …)` or `REJECT (not better: …)`. A new wrong
answer is a case whose after-outcome is a confident wrong one (Akshara and Rekha: agreed-wrong;
Vastu: a usable value the key does not list) and whose before-outcome was not, including a case the
older run never asked.

Source: issue #1219 · Verification: `tests/eval/test_pariksha_*.py`,
`tests/scripts/test_pariksha_scorecard.py`
"""
