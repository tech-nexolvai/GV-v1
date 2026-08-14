# DESIGN — evidence and extraction (Track B)

Companion to `DESIGN.md`, which owns the deterministic core and the evidence contracts (§3.14).
This document owns **B6–B11** and the matching lanes: pages, the drawing model, coordinate spaces,
geometry, revision identity and advisory retrieval.

Read `DESIGN.md` §5 first. It divides this track in two, and the division is load-bearing: the
**contracts** below are designable today because the architecture documents fix them, while four named
areas cannot be specified until real drawings exist. Those four are marked ⚠ and their stories carry
open decisions instead of an invented interface.

---

## 1. Package layout

```
extraction/
  manifest.py  page_type.py  sheet.py  revision.py  supersession.py
  model/       views.py items.py identifiers.py aliases.py assembly.py
  geometry/    deskew.py dimension_lines.py text_association.py containment.py
evidence/
  coordinates.py  polygon.py  crop.py          # candidate.py, canonical.py, gate.py: DESIGN.md §3.14
retrieval/
  candidate.py  approval.py  fusion.py
  lanes/       exact.py alias.py metadata.py geometry.py trigram.py lexical.py dense.py
```

## 2. Import rules

Extends `DESIGN.md` §2.

| Package | May import | Must never import |
|---|---|---|
| `extraction/` | `units/`, `evidence/`, `storage/` | `verdict/`, `rules/`, `retrieval/` |
| `evidence/` | `units/` | `extraction/`, `retrieval/`, `verdict/` |
| `retrieval/` | `units/`, `evidence/` (read), `extraction/model/` | `verdict/`, `rules/` |

`extraction/` must not import `rules/`. An extractor that knows which rule is coming is an extractor
that can be tuned to satisfy it, and the whole point of the evidence gate is that reading and judging
are separate acts.

---

## 3. B6 — pages

### 3.1 The manifest is the unit of work

```python
@dataclass(frozen=True, slots=True)
class PageRecord:
    index: int                    # 0-based internally; 1-based in anything a reviewer sees
    content_hash: str
    width_pt: Decimal
    height_pt: Decimal
    rotation: int                 # 0 | 90 | 180 | 270, from /Rotate
    has_vector_text: bool         # decides the extraction lane: B2.2 or B2.4
    render_failed: bool
    sheet_number: str | None
    page_type: PageType | None    # None means unknown, explicitly
    revision: RevisionLabel | None

@dataclass(frozen=True, slots=True)
class PageManifest:
    document_version_id: UUID
    pages: tuple[PageRecord, ...]
```

Built once per document version, never mutated. A page that cannot be rendered is recorded with
`render_failed=True` rather than omitted — the manifest is the only place that can know a page existed
and was not read.

### 3.2 Classification fails to `unknown`, not to a guess

```python
class PageType(StrEnum):
    PLAN = "plan"; ELEVATION = "elevation"; SECTION = "section"
    DETAIL = "detail"; SCHEDULE = "schedule"; TITLE = "title"

def classify(page: PageRecord, text: PageText) -> Classification:
    """Deterministic signals first. Records which signal decided."""
```

`None` is a real outcome. A countertop width found on a *cabinet elevation* is a plausible number
attached to the wrong drawing, and no tolerance check catches it — so an unclassifiable page must not be
rounded to the nearest plausible type. It is still extracted; it is only excluded from `scope: same_view`.

### 3.3 Scope resolution fails closed

`same_view` returns an explicit empty result when it cannot resolve. It never widens to "the whole
package" — silently widening scope is how a rule finds a number that satisfies it somewhere.

---

## 4. B7 — the drawing model

### 4.1 Identity rules

| Type | Identity | Why |
|---|---|---|
| `DrawingView` | `(page_id, tag)` | tags repeat across pages; tag alone merges two views |
| `DrawingItem` | surrogate id, one view | cross-view identity is B7.3's job, not the type's |
| `ItemIdentifier` | `(item, kind, value)` | a catalogue code and a unique ID are not interchangeable |
| `Alias` | `(spelling, rulebook_version)` | an alias is a small rule and is versioned like one |

Items obey the same discipline as observations: created as candidates, corroborated separately
(`AGENTS.md` §2.3). If an item could be created directly as a fact, the drawing model would be a second,
unguarded route into the verdict.

### 4.2 `same_assembly` — the resolver CT-1 depends on

```python
@dataclass(frozen=True, slots=True)
class Assembly:
    countertop: DrawingItem
    run: tuple[AssemblyMember, ...]     # ORDERED left-to-right
    signals: Mapping[UUID, str]         # why each member was included

def resolve_assembly(countertop: DrawingItem, ctx: DrawingContext) -> Assembly | CannotResolve:
    """Never returns a partial run. A missing cabinet is not a shorter assembly."""
```

Order matters: filler distribution (A6.4) is positional. **A partial run is never silently summed** — a
cabinet wrongly dropped changes the expected width by a full cabinet, and the engine will compute that
wrong number exactly and confidently.

---

## 5. B8 — coordinate spaces

Three spaces are in play, and a silent mix-up puts the highlight box on the wrong part of the page.
They are distinct **types**, not conventions:

```python
class PdfPoint(NamedTuple):    x: Decimal; y: Decimal     # origin bottom-left, points
class ImagePoint(NamedTuple):  x: int;     y: int         # origin top-left, pixels
class StoredPoint(NamedTuple): x: Decimal; y: Decimal     # normalised 0..1, rotation-applied

@dataclass(frozen=True, slots=True)
class PageTransform:
    dpi: int
    rotation: int
    media_box: tuple[Decimal, Decimal, Decimal, Decimal]

    def to_image(self, p: PdfPoint) -> ImagePoint: ...
    def to_pdf(self, p: ImagePoint) -> PdfPoint: ...
```

Y-axis flips and rotated pages are the classic silent failure: everything looks right on unrotated test
pages and every crop is wrong on the real ones. Hence 90/180/270 are required test cases, not optional.

**Crops carry context.** A crop tight to the digits shows a reviewer `984` and proves nothing; context is
what makes the check take two seconds instead of two minutes. Crops are content-addressed and keyed to
the document version, so a revision cannot reuse a stale one.

---

## 6. B10 — geometry ⚠ partly undesignable

Two capabilities the system design names separately: OpenCV for rotation, deskew and enhancement;
Shapely for spatial relationships and evidence polygons.

**Designable now:** the deskew contract (the applied rotation must be recorded and invertible, or a crop
cannot map back to the true page), and the Shapely-backed polygon operations in `evidence/polygon.py`.

⚠ **Not designable now — B10.2, B10.3, B10.4.** Which vector primitives real drawings use for dimension
lines, where this vendor places dimension text, and what endpoint-alignment tolerance separates "spans
this cabinet" from "spans the run" are all empirical. `data/drawings/` is empty.

What *is* fixed regardless of the drawings, because it follows from the safety argument rather than from
the geometry:

- ambiguous association returns **no** association, never the nearest guess
- an unassociated number is still retained — it is evidence of something
- cannot-resolve marks the association for human confirmation

This is where a correctly-read number gets attached to the wrong item. The dual-unit lane corroborates
that `984` was *read* correctly and says nothing about *what it measures*. A geometry error produces a
finding that is internally consistent, fully traced and completely wrong.

---

## 7. B11 — revision identity

`C5` pins the **bytes**; this decides which **sheet governs**. Both are required and neither substitutes
for the other.

```python
@dataclass(frozen=True, slots=True)
class RevisionLabel:
    as_printed: str               # "A", "01", "Rev C"
    date: RevisionDate | None     # ambiguity preserved: 03/04/26 is not silently assumed
    sequence_index: int | None

def governing_revision(sheets: Sequence[PageRecord]) -> PageRecord | Unresolved:
    """Sheet identity is the sheet number — never the filename or page order."""
```

*Unknown revision* and *the first revision* are different facts. Treating the first as the second is
exactly how a superseded sheet gets used with confidence.

Unresolved supersession produces REVIEW REQUIRED for every finding drawn from that sheet. It never
resolves to "the last page wins" or "the highest letter wins". Every other guard in the system assumes
the source page was the right page; this is the only place that assumption is checked, so it fails closed.

---

## 8. Matching — the eight lanes

| # | Lane | Module | Authority |
|---|---|---|---|
| 1 | exact identifier | `lanes/exact.py` | may auto-approve (deterministic) |
| 2 | alias | `lanes/alias.py` | may auto-approve (deterministic) |
| 3 | metadata | `lanes/metadata.py` | filter **and** ranker |
| 4 | geometry | `lanes/geometry.py` | candidate only |
| 5 | trigram | `lanes/trigram.py` | candidate only |
| 6 | lexical | `lanes/lexical.py` | candidate only |
| 7 | dense | `lanes/dense.py` | candidate only, **never on bare identifiers** |
| 8 | fusion | `fusion.py` | reorders 5–7; exact stays pinned |

```python
@dataclass(frozen=True, slots=True)
class MatchCandidate:
    left_item_id: UUID
    right_item_id: UUID
    lane: Lane
    score: Decimal | None         # diagnostic metadata, never authority
    # no approved field — approval is a different type

@dataclass(frozen=True, slots=True)
class ApprovedMatch:
    candidate: MatchCandidate
    source: Literal["deterministic", "human"]     # there is no third way in
    approved_by: str
```

Dense retrieval is deliberately near-last: `X-223` and `X-233` embed almost identically. The restriction
against applying it to bare identifiers is enforced by a test, not documented in a comment.

Every mechanism guarding the verdict boundary works because the unsafe thing is a *different type* from
the safe thing. A score field on an approved fact would be enough to lose that.

---

## 9. Testing convention

`tests/<package>/test_<module>.py` per `DESIGN.md` §4. Track B additions:

- **Rotation matrix**: every coordinate test runs against 0/90/180/270.
- **Refusal tests**: for each resolver that can fail — scope, assembly, association, supersession —
  assert the *cannot-resolve* path, not only the success path. These are the safety-critical cases.
- **Lane ordering**: fusion tests assert an exact-identifier match is never displaced.
- ⚠ Stories marked undesignable carry characterisation tests written **against real drawings when they
  arrive**, not fixtures invented now. A fixture invented today encodes today's guess as ground truth.

---

## 10. The names the shipped code already uses

`verdict/operands.py` is built and its types are fixed. Anything in `evidence/` or `extraction/` must
match them exactly rather than introduce a parallel vocabulary:

```python
class EvidenceStatus(StrEnum):          # FIVE members, not four
    RAW_CANDIDATE      # one unverified route produced it. Not evidence yet
    CORROBORATED       # may enter a verdict
    HUMAN_CONFIRMED    # may enter a verdict
    CONFLICTING        # never resolved by confidence or by preferring a reader
    REJECTED           # found invalid

QUALIFIED_STATUSES = {CORROBORATED, HUMAN_CONFIRMED}     # the only two that reach arithmetic

@dataclass(frozen=True, slots=True)
class VerdictOperand:                    # the engine's input type. Not "SealedOperand"
    name: str
    value: Measurement | Fraction | str | None
    status: EvidenceStatus
    source: str                          # SHOP | ARCH | PRODUCT_SPEC | LITERAL | USER_INPUT
    evidence_ref: str | None = None
```

`evidence/gate.py` returns `VerdictOperand | GateRefusal` (`DESIGN.md` §3.14). It **imports** these
from `verdict/operands.py` — they are contract types, and §2 permits exactly that while forbidding the
engine. The dependency points one way on purpose: the gate builds what the verdict accepts, and the
verdict never reaches back.

Do not redefine `EvidenceStatus` in `evidence/`. Two enums with overlapping members is how a
`CONFLICTING` reading quietly becomes a qualified one.
