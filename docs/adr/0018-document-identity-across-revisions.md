# ADR-0018 — A document belongs to the package; a revision names the versions it includes

**Status:** Proposed
**Date:** 2026-08-21
**Decides:** D17 (#367)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Only the admin may set `Status: Accepted`.**
> `scripts/ratify.py` refuses to unblock #366 or #211 until this status reads Accepted.

## Context

`Document` states one thing and is modelled as another. Its own docstring:

> *"Logical document identity shared by all uploads of that document."*

It carries `package_revision_id`, so it belongs to exactly one revision. A logical identity *shared
across uploads* cannot be scoped to one revision, and both readings cannot hold at once. The
contradiction has been harmless so far because nothing has needed a drawing to exist in two revisions.

Supersede needs exactly that. `DESIGN_PLATFORM.md` §5:

> *"A new document revision never overwrites an old version; it supersedes the prior package revision
> and starts a new workflow run."*

The behaviour agreed for #211 is that the superseding revision carries every drawing forward, with the
new upload replacing the ones that changed. That is not a convenience: a countertop width check reads
the cabinet elevation as well as the countertop sheet, so a revision holding only the changed drawing
would run its checks against a partial set — and the drawings that were absent would produce no
failures, which reads as no problems. `AGENTS.md` §2.2 is explicit that silence must never read as
completion.

**The schema refuses it.** `document_versions` carries `UniqueConstraint("source_artifact_id")`, so the
same bytes cannot back a version under a second revision's document. Demonstrated against PostgreSQL at
head rather than reasoned about:

```
first version on revision 1: ok
carrying the SAME artifact to revision 2: REFUSED
    duplicate key value violates unique constraint "uq_document_versions_source_artifact_id"
```

That constraint has no recorded rationale — `0004_document_aggregate.py` declares it, and neither the
model nor §3 says why. But it is not really the obstruction. The obstruction is that a drawing has no
identity across revisions at all: revision 2's "same sheet" is an unrelated `documents` row with
nothing linking it to revision 1's. Removing the constraint would let the bytes be shared while leaving
the two rows strangers to each other.

## Options considered

1. **Drop `uq_document_versions_source_artifact_id`.** Smallest change; unblocks #211 immediately. The
   docstring still contradicts the schema, a drawing still has no cross-revision identity, and two
   versions may then share one artifact with nothing recording which of them a finding meant. Reverses
   an undocumented decision without replacing the reasoning.
2. **Move document identity to the package.** `documents.package_id` replaces
   `documents.package_revision_id`; a revision's contents become an explicit set of the document
   versions it includes. Larger migration and it touches every query joining documents to revisions.
   Makes the docstring true, gives a drawing one identity, and lets a revision share bytes with its
   predecessor without copying anything.
3. **Change the #211 answer instead.** Supersede performs the transition only; composing the new
   revision is deferred. Cheapest, and it leaves the contradiction in place for the next story to hit.

## Decision

**Option 2.** A document belongs to its package. A package revision names the document versions it
includes, through an explicit membership table.

```python
# app/models/document.py
class Document(Base, TimestampedUUID):
    package_id: Mapped[UUID]        # was package_revision_id
    kind: Mapped[str]

class PackageRevisionDocument(Base, TimestampedUUID, Immutable):
    """Which document versions one revision is composed of."""
    package_revision_id: Mapped[UUID]
    package_id: Mapped[UUID]
    document_id: Mapped[UUID]
    document_version_id: Mapped[UUID]
```

Three properties are load-bearing, and each is a constraint rather than a convention:

**A revision includes at most one version of any drawing.**
`UniqueConstraint("package_revision_id", "document_id")`. Without it a revision could hold v1 and v2 of
the same sheet, its checks would run against both, and no reader could say which version a finding
meant.

**A revision cannot include a version from another package.** Composite foreign keys resolving both
sides, following `ApprovedFinding` in `app/models/review.py` — which exists for the same reason, so an
approval cannot list a finding from a different package.

**The membership record is append-only.** `PackageRevisionDocument` carries `Immutable`. What a
revision was composed of is precisely the record somebody would want to adjust after a dispute, and
`AGENTS.md` §2.7 does not allow that. Composition is corrected by superseding the revision, which is
what #211 is for.

**`uq_document_versions_source_artifact_id` is kept.** Under this model, carrying a drawing forward
inserts a membership row and creates no new version, so the constraint never obstructs — and it gains
a meaning it did not have: a set of bytes is registered as a document version exactly once. Option 1
would have removed it and permitted two versions of identical bytes with nothing distinguishing them.

## Consequences

**Easier.** A revision's document set is a query rather than an inference. Carrying a drawing forward
costs one row and stores no bytes twice. "Which revisions contained this drawing?" becomes answerable,
and it is not answerable today.

**Harder.** Every read that joined `documents.package_revision_id` now joins through the membership
table — `app/api/documents.py`, `app/api/packages.py`, and their tests. The migration must backfill,
and the backfill has to resolve an ambiguity the old model permitted: `confirm_upload` adds a new
version to an existing document on the *same* revision, so existing data can hold two versions of one
document per revision, which the new constraint forbids. The backfill takes the latest version per
`(revision, document)`. Nothing is lost — superseded versions remain in `document_versions` as history,
and a finding cites a version directly rather than through a revision's set.

On any database that exists today this backfill is a no-op: there is no deployment, and every database
is built from the migrations. It is written correctly for when that stops being true.

**What this forbids.** A revision's composition can no longer be edited — not the set, not the choice
of version within it. A drawing can no longer be attached to a revision without existing in the
package first. And a document version cannot be moved between packages, because the membership row
resolves both sides.

**It amends no golden rule.** §2.7 append-only is strengthened rather than relaxed: a fact that was
previously implicit in a mutable foreign key becomes an append-only row.

## Safety impact

**Neutral on the critical false-PASS rate, and it removes one route to a false PASS.**

Nothing here is in the verdict path. `verdict/` cannot reach `app/` at all — the isolation guard
enforces it — so no operand, tolerance or comparison changes.

What improves is the completeness of what gets checked. Under the current model, the only way to
compose a superseding revision is to attach the changed drawing alone, and checks would then run
against a partial drawing set. A countertop width check that cannot see the cabinet elevation does not
fail loudly; it produces fewer findings, and fewer findings look like a cleaner package. That is a
false PASS arriving through absence rather than through arithmetic, which is the failure mode
`AGENTS.md` §2.2 names and the hardest kind to notice.

What must not weaken, and does not: a finding cites a `document_version`, and a version pins the exact
artifact bytes it was extracted from. Both are untouched. A six-month-old review reconstructs through
the same two links it does today.

## Unblocks

- **#366** — the implementation: the model change, the migration, the backfill, the readers.
- **#211** — C3.3 supersede, blocked behind #366.

Ratify with:

```bash
python scripts/ratify.py D17 --adr docs/adr/0018-document-identity-across-revisions.md
```
