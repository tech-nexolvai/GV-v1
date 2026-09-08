# The answer key: what a reviewed package looks like on disk

This is the format a reviewed drawing package is written in, so that reviewed material can be dropped
straight in and measured. **The format and the loader are tracked code; the answers never are.** A
package lives under `data/goldset/`, which is git-ignored, and `AGENTS.md` §9 is why: the answers are
the client's material and the repository must not hold them.

Nothing in this document asks anybody to invent an answer. An answer key is a record of what a person
concluded while looking at a drawing, and a case authored to make a metric look good would make every
number computed from it meaningless.

## The shape

One package is one directory:

```
data/goldset/<case-id>/
  answer_key.json     # the answers
  shop.pdf            # the drawings, exactly as reviewed
  arch.pdf
```

`answer_key.json`:

```json
{
  "id": "townplace-buffet-01",
  "product_type": "countertop",
  "arch": "arch.pdf",
  "shop": "shop.pdf",
  "ground_truth": {
    "observations": [
      {
        "semantic_type": "CT007",
        "source": "SHOP",
        "value": {"exact": "4", "unit": "in", "raw_text": "4\""},
        "page": 1,
        "polygon": [1140, 855, 1275, 895],
        "item_id": "S_CAB_7"
      }
    ],
    "matches": [{"arch_item": "A_CAB_7", "shop_item": "S_CAB_7"}],
    "expected_findings": [
      {
        "check": "CT-SINK-OFFSET-FRONT-001",
        "outcome": "PASS",
        "reason": "the front offset is the standard 4 inches"
      }
    ]
  },
  "provenance": {
    "annotator": "raj",
    "annotated_on": "2026-09-08",
    "documents": [
      {
        "source": "SHOP",
        "document_version_id": "…",
        "content_hash": "sha256:…"
      }
    ]
  }
}
```

Generate a working example and read it rather than transcribing this:

```
python scripts/evaluate_goldset.py --make-fixture data/goldset/synthetic-01
```

## The fields, and why each one is required

### One reviewed dimension — `ground_truth.observations[]`

| field | what it is |
|---|---|
| `semantic_type` | **The reviewer's label.** `CT007`, `CT010`, `CAB_SIDE_THK` — from `vocabulary/semantic_types.py`. |
| `source` | Which drawing it was read off: `SHOP`, `ARCH` or `PRODUCT_SPEC`. |
| `value` | The correct value, **exact**: `{"exact": "115/4", "unit": "in", "raw_text": "28 3/4\""}`. |
| `page` | Which sheet, counting from 1 the way a person does. |
| `polygon` | Where on the sheet: `[left, top, right, bottom]` in image pixels. |
| `item_id` | The annotator's name for the cabinet or countertop it belongs to. |

**`semantic_type` is how the system learns the words.** Nothing in this project types a value on its
own — that is a hard rule with a test behind it (`test_nothing_types_a_candidate_on_its_own`), and a
reading with no human label is untyped for as long as it takes a person to say what it is. The answer
key is where those labels are written down, and the grader uses them exactly as a reviewer's
confirmation would: to say *what a reading is*, never *what it says*.

**`value` is authored as exact text and a float is refused.** `"exact": "115/4"` and not `28.75`.
Q2 settled exact matching for V1, so a rounded answer is a wrong answer, and the schema rejects a
float before Pydantic can coerce one.

**`polygon` is what pairs an answer with a reading.** The grader matches each answer to the reading
whose box is nearest it — not by value, which would be scoring the key against itself, and not by
list order, which would hand the first answer whatever the reader happened to emit first.

### The reviewer's verdict — `ground_truth.expected_findings[]`

| field | what it is |
|---|---|
| `check` | The rule id, e.g. `CT-SINK-OFFSET-FRONT-001`. |
| `outcome` | What a reviewer concluded: `PASS`, `FAIL`, `REVIEW_REQUIRED`, `NOT_FOUND`, `NO_APPLICABLE_RULE`. |
| `reason` | Why, in plain English. Optional to read, required to write. |

**An answer key is silent, not negative.** A check the key says nothing about is not scored — it is
not counted as a failure and not counted as a pass. So a partially annotated case is useful rather
than misleading, and there is no pressure to state a verdict nobody formed.

**`FAIL` on a `CRITICAL` check is what the ship gate is measured against.** The primary safety metric
is the critical false-PASS rate: how often a drawing a reviewer failed was passed by the system. It
can only be computed from cases where a reviewer says a critical check should have failed, so those
are the most valuable entries in the whole file.

### Which drawing the answers are about — `provenance`

| field | what it is |
|---|---|
| `annotator` | Who wrote the answers. |
| `annotated_on` | When. |
| `documents[]` | One per drawing read: its source, its version id, and its `sha256:…` content hash. |

**The hash is not bookkeeping.** An annotation is a statement about specific bytes — *"the front
offset on page 3 of this PDF is 4 inches"*. Replace the PDF and the sentence is no longer about
anything: the polygon points at different ink and the value may be right for a drawing nobody is
reviewing. So a mismatch **refuses the case**, with no flag to continue past it, because a gold set
scored against stale annotations does not produce a noisy metric — it produces a confidently wrong
one that reads as a passing gate. `eval/gold_set/store.py` is where that check lives.

Get a hash with `python -c "from eval.gold_set.store import content_hash; \
from pathlib import Path; print(content_hash(Path('shop.pdf')))"`.

## Running it

```
DATABASE_URL=postgresql+psycopg://… python scripts/evaluate_goldset.py data/goldset/<case-id>
```

It runs the real pipeline over the package's drawing and prints a scorecard: critical false-PASS
first, then verdict accuracy, reading accuracy, reading coverage and the abstention rate. It exits
non-zero only on a critical false PASS — the one result that must stop something.

The grader is **offline**. It uses its own rows and cannot change a verdict in a live package: a
measurement that could steer the thing it measures is not a measurement.

## Two things the format deliberately will not do

**An abstention is never scored as a wrong reading.** `REVIEW_REQUIRED`, `NOT_FOUND` and
`NO_APPLICABLE_RULE` mean the system declined to answer, which under `AGENTS.md` §2 is correct when it
cannot be sure. A harness that scored those as errors would make the honest path look like the
failing path, and the fastest way to improve the score would be to guess more.

**No answer is ever filled in from the key.** Where the key has an entry and the system produced
nothing, the scorecard says `nothing read`. A grader that supplied the answer it was grading against
would report a perfect score for a pipeline that read nothing at all.

## Where this fits

- `eval/gold_set/schema.py` — the typed format and its parser
- `eval/gold_set/store.py` — the content-hash integrity check
- `eval/scorecard.py` — the comparison, reusing the nine release metrics in `eval/metrics.py`
- `scripts/evaluate_goldset.py` — the runner, and `--make-fixture` for a worked example
- `eval/promotion.py` — turning a reviewer's correction into an entry, one at a time and never automatically
- `eval/trainer.py` — the empty socket a trained reader would plug into

Open: #188 (annotate the first representative cases) and #274 (the drawings and reviewed answers).
