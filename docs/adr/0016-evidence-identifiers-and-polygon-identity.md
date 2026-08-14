# ADR-0016 — Identifier types, and a polygon's coordinate space is its identity

**Status:** Accepted
**Date:** 2026-08-14
**Decides:** D16 (#277)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Accepted by the admin on 2026-08-14.**

## Context

Three mismatches on one seam, all surfaced while answering #170.

### `document_version_id` has two types

`docs/DESIGN_EXTRACTION.md` §3.1 declares `PageManifest.document_version_id: UUID`.
`docs/DESIGN.md` §3.14 declares `CanonicalObservation.document_version_id: str`.

Two design documents disagree, so the usual tie-break — the design document wins over an
implementation plan — does not apply. Something has to decide which document is wrong.

The codebase already uses `str` for several identifiers, which makes this look settled when it is
not. Those identifiers are a different kind of thing:

| Identifier | Type | What it is |
|---|---|---|
| `snapshot_id` | `str` | `sha256:…` — derived from content |
| `rule.id` | `str` | `CT-WIDTH-001` — authored by a human, meaningful |
| `project_id` | `str` | `GV-2026-ABC` — authored, meaningful |
| `document_version_id` | ? | a surrogate key for a row nobody names by hand |

### `CanonicalObservation` carries an untyped polygon

§3.14 declares `polygon: tuple[tuple[int, int], ...]`, while #170 builds a `Polygon` that validates
itself at construction — rejecting zero-area, self-intersecting and out-of-bounds geometry, and
carrying the coordinate space it belongs to.

The canonical observation is where a polygon is *used*: it is what the evidence-localisation gate
reads and what a reviewer clicks to see the number on the drawing. A raw tuple at that boundary
discards every guarantee the `Polygon` type constructs.

This is the shape of the #64 problem exactly. `ProjectScope` stored parameters as bare
`Quantity`, which recorded what a number was and lost who set it; the fix was to store the type
that carries the record. Here a bare tuple records where a polygon is and loses which page, which
document version, and whether it is valid at all.

### A polygon's space is `(document version, page)`, not document version

#170 rejects a comparison between polygons from different document versions, on the grounds that
they are in different coordinate spaces and the question is meaningless. That reasoning is right
and it is incomplete: **two polygons on different pages of the same document are equally
unrelated**, and the interface as amended would compare them and return a confident geometric
answer.

Page 3's top-left and page 7's top-left have identical coordinates and nothing to do with each
other.

## Options considered — identifier type

1. **`str` everywhere, for consistency with `snapshot_id` and `project_id`.** Uniform, and wrong
   about what those identifiers are: both are meaningful strings a human reads and writes.
   `str` also admits `"6f1b…"`, `"6F1B…"` and `"6f1b-…"` as three different values for one
   document, and a mismatch on a join key fails silently by finding nothing.
2. **`UUID` everywhere.** Parses the hyphenated and unhyphenated forms to the same value, compares
   canonically, and rejects a malformed identifier at construction instead of at a join that
   quietly returns empty.
3. **A `DocumentVersionId` newtype wrapping `str`.** Strongest typing, but new machinery for a
   problem `UUID` already solves in the standard library.

## Options considered — polygon on the observation

1. **Keep `tuple[tuple[int, int], ...]`.** No change, and the validity guarantees stop at the
   boundary where they matter most.
2. **Keep the tuple, and validate on read.** Validation that runs at every read is validation
   somebody eventually skips for performance, and a raw tuple can still be constructed and stored
   by any code path that never reads it.
3. **Carry the `Polygon`.** The guarantee travels with the value.

## Decision

### 1. `document_version_id` is `UUID`. `docs/DESIGN.md` §3.14 is wrong and is corrected

The distinction to hold onto, so this does not get re-litigated at the next identifier:

> **Authored or content-derived identifiers stay `str`. System-generated surrogate keys are
> `UUID`.**

`snapshot_id` is a hash — its string form *is* the identity, and `sha256:` in front of it is
meaningful. `rule.id` and `project_id` are written by people and read by people. A document
version id is none of those: it is a row key nobody types, and the only thing anyone does with it
is compare it to another one. That is precisely the case where a permissive type hurts and a
canonicalising one helps.

### 2. `CanonicalObservation.polygon` is a `Polygon`

```python
@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    document_version_id: UUID
    document_role: DocumentRole
    page: int
    polygon: Polygon               # was tuple[tuple[int, int], ...]
    ...
```

The observation keeps its own `document_version_id` and `page`, because they identify the
observation and are used to filter it. The polygon carries them too, as part of its coordinate
space. **They must agree, and construction rejects an observation whose polygon belongs to a
different document version or a different page.**

That redundancy is deliberate and has a precedent in this codebase: `ProjectScope` refuses a
`ParameterSet` belonging to another project rather than trusting the caller to pair them
correctly. Serving one page's geometry as another's is the same class of error, and it would be
invisible in the resulting finding — a reviewer would be shown a highlighted region on the wrong
page and have no way to tell.

### 3. A polygon's coordinate space is `(document_version_id, page, space)`

```python
@dataclass(frozen=True, slots=True)
class Polygon:
    points: tuple[StoredPoint, ...]
    space: Literal["stored"]
    document_version_id: UUID
    page: int                      # NEW — part of the space, not a label
    page_extent: StoredExtent
```

`contains` and `overlaps` raise when any of `document_version_id`, `page` or `space` differ. The
existing reason for raising rather than returning `False` applies unchanged and is worth
restating: `False` is a factual claim about geometry, and there is no true geometric answer to a
question about two unrelated planes.

## Consequences

`docs/DESIGN.md` §3.14 is corrected on both counts. #170's interface gains `page`; the change is
small and it lands before implementation rather than after.

`evidence/` gains an import of `evidence/polygon.py` into `evidence/canonical.py`, which is within
the §2 import table. Anything constructing a `CanonicalObservation` must now construct a valid
`Polygon` first — that is the point, and it is the cost.

Serialisation of a `UUID` is `str(uuid)`, which is deterministic, so nothing that canonicalises or
hashes an identifier changes behaviour.

## Safety impact

Positive, and specifically on the metric that measures whether a reviewer can trust what they are
shown.

`AGENTS.md` §9 gates a release on evidence page and polygon meeting a threshold. That gate reads
the polygon on a canonical observation. If that polygon can be a raw tuple of unknown validity, in
an unknown space, on an unrecognised page, then the gate measures the presence of coordinates
rather than the correctness of localisation — and it would pass while pointing reviewers at the
wrong region.

The page-identity change closes a silent-wrong-answer path that the document-version check alone
left open. It is the same failure mode, one level finer, and it is more likely: a package has many
pages and few document versions.

## Unblocks

#170 (amended), #171, and the evidence plane generally.
