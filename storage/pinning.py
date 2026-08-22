"""Every extracted fact names the exact bytes it came from (#220, C5.3).

`docs/DESIGN_PLATFORM.md` §7: *"a document version pins the exact bytes every downstream fact was extracted
from. Without that pin, an observation is a claim about 'the drawing' — a file that may since have been
replaced."*

**All four of this story's acceptance criteria are already enforced by the schema, and that is worth stating
plainly rather than quietly re-implementing.** Checked against the models, not assumed:

- `ObservationCandidate.document_version_id` and `CanonicalObservation.document_version_id` are both
  `nullable=False` with a foreign key, so an observation cannot exist without a version.
- `EvidenceArtifact.document_version_id` is direct; a `Finding` reaches one through
  `FindingEvidence → CanonicalObservation`, every hop a foreign key.
- `uq_document_versions_document_id_sha256` makes new bytes a new version and identical bytes a duplicate
  the database refuses, and `DocumentVersion` is `Immutable`, so nothing overwrites a version in place.
- Nothing in `extraction/` resolves a document without a version.

So this module does not add the constraint. It makes the guarantee **callable** — one function that either
returns a pin or refuses, so a caller cannot accidentally proceed unpinned — and
`tests/storage/test_pinning.py` turns each of those schema facts into a test, because a property enforced
only by a column somebody could later make nullable is a property with no alarm on it.

**There is no "latest version" mode here, and that absence is the design.** A resolver that could answer
"the current document" is the thing §7 warns about: it turns a fact about specific bytes into a fact about
whatever is there now. The only way in is with an id.

Source: backend proposal §11; `AGENTS.md` §2.7 · Design: `docs/DESIGN_PLATFORM.md` §7 ·
Verification: `tests/storage/test_pinning.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import DocumentVersion, SourceArtifact
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing, sha256_stream

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from storage.store import ArtifactStore

__all__ = ["Pin", "require_pin"]

#: The digest form this project records for a document's bytes: 64 lowercase hex, no prefix.
#:
#: Matched to `ck_document_versions_document_version_sha256` in the schema rather than invented here — and
#: deliberately *not* the `sha256:`-prefixed form used by `model_invocations.node_invocation_key`. Two
#: formats exist in this codebase and confusing them produces a validator that passes everything.
DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Pin:
    """The bytes a fact was extracted from: which version, and the digest recorded for it.

    Frozen, because a pin that could be edited after the fact would answer "which bytes?" with whatever was
    most recently convenient — which is the failure this whole story exists to prevent.
    """

    document_version_id: UUID
    sha256: str


def require_pin(
    session: Session,
    document_version_id: UUID,
    *,
    store: ArtifactStore | None = None,
) -> Pin:
    """Resolve a document version into a pin, or refuse.

    Refuses in four ways, and each is a different thing being wrong:

    - **The version does not exist** — `IntegrityRecordMissing`. A fact pointing at a version that is not
      there is not pinned to anything, and the foreign keys make this unreachable through the ORM; it is
      checked because this function is also the path a raw id from an API request takes.
    - **The recorded digest is not a digest** — `ArtifactCorrupt`. **Unreachable while the schema holds**,
      and I confirmed that by failing to construct it: `sha256` is `VARCHAR(64)` with a `^[0-9a-f]{64}$`
      check, so the `sha256:`-prefixed form cannot even be stored. Kept for a row that arrived another way.
    - **The version and its source artifact disagree** — `ArtifactCorrupt`. Also unreachable, and for a
      better reason than I first wrote: `document_versions` carries a composite foreign key
      `(source_artifact_id, sha256) → (source_artifacts.id, source_artifacts.sha256)`, so PostgreSQL
      refuses the disagreement. I had described this as the case "visible in the database alone" as though
      it could occur; the database does not allow it to.
    - **The stored bytes no longer hash to what the *document version* recorded** — `ArtifactCorrupt`, and
      only when a `store` is given. Note what this adds and what it does not: `ArtifactStore.get` already
      verifies the bytes against the store's *own* integrity record and raises if they changed (#219), so
      passing a store first buys that. This then compares the same bytes against the digest the document
      version recorded — a different question, and the one that catches the store and the database
      disagreeing with each other rather than either being internally inconsistent.

    **No `store` argument means the record is trusted, not that nothing was checked.** The first three still
    run. Saying so because "verified" and "self-consistent" are different claims and the caller should know
    which one it bought.

    Deviation from the issue's sketch, recorded rather than hidden: it gives
    `require_pin(document_version_id) -> Pin`, which has nothing to resolve *from* — it would have to read a
    global or hand back its own argument. This takes the session.
    """
    row = session.execute(
        select(DocumentVersion, SourceArtifact)
        .join(SourceArtifact, DocumentVersion.source_artifact_id == SourceArtifact.id)
        .where(DocumentVersion.id == document_version_id)
    ).one_or_none()
    if row is None:
        raise IntegrityRecordMissing(
            f"there is no document version {document_version_id}, so nothing is pinned to it"
        )
    version, artifact = row

    if not DIGEST.fullmatch(version.sha256 or ""):
        raise ArtifactCorrupt(
            f"document version {document_version_id} records {version.sha256!r}, which is not a "
            "64-character lowercase hexadecimal digest"
        )

    if version.sha256 != artifact.sha256:
        raise ArtifactCorrupt(
            f"document version {document_version_id} records {version.sha256} but its source artifact "
            f"records {artifact.sha256}. Two rows describe the same bytes and they disagree, so one of "
            "them has been rewritten — the pin cannot be trusted either way."
        )

    if store is not None:
        with store.get(artifact.storage_key) as data:
            actual, _ = sha256_stream(data)
        if actual != version.sha256:
            raise ArtifactCorrupt(
                f"the bytes at {artifact.storage_key} hash to {actual}, but document version "
                f"{document_version_id} was pinned to {version.sha256}. Every fact extracted from this "
                "version is a claim about bytes that are no longer there."
            )

    return Pin(document_version_id=document_version_id, sha256=version.sha256)
