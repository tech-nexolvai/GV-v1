"""What the packages and documents endpoints accept and return (#205, C2.3).

**Nothing here can carry a file.** Every field below is a string, a number, an enum or a UUID. That is
the point of the module, not an accident of what happened to be needed: backend §4.1 says uploads go
straight to storage and the control plane does short work only, so a request body that could hold
bytes would be the design failing at its narrowest point. `tests/api/test_packages.py` walks every
route in the whole application and asserts it, because a convention nobody enumerates is one somebody
eventually breaks.

**The client declares the hash; the server checks it.** Registration takes the SHA-256 the client
computed locally, and that hash is what the storage key is built from — so the ticket the client gets
back permits writing *those* bytes to *that* key and nothing else. Confirmation then reads what
actually landed and compares. A declaration that turns out to be wrong is rejected and writes nothing,
so the pin between a document version and its exact bytes (`AGENTS.md` §2.7) is a check rather than a
promise.

**`page_count` is a declaration, and it is labelled as one.** The API never opens the file, so it
cannot count pages, and there is no honest way for it to. The real page manifest is built during
ingestion (#160, `DESIGN_EXTRACTION.md` §3.1) from the actual PDF; a disagreement between what was
declared here and what the manifest finds is visible there. This field is not a measurement and this
docstring is the only place that can say so, because `document_versions.page_count` is just an integer.

Source: backend proposal §10.2, §11 · Design: `docs/DESIGN_PLATFORM.md` §4.1, §4.2 ·
Verification: `tests/api/test_packages.py`
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.document import DocumentKind, clean_file_name
from vocabulary.semantic_types import ProductType

#: A lowercase hex SHA-256, matched against the same shape the database checks
#: (`source_artifact_sha256`). Validated at the boundary as well as in the schema so a malformed hash
#: is a 422 naming the field rather than a 500 from a constraint violation three writes later.
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class PackageCreate(BaseModel):
    """A new reviewable drawing package."""

    model_config = ConfigDict(extra="forbid")

    vendor: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Who supplied the drawings, if it is known. Left null rather than guessed — an invented "
            "vendor name is worse than an absent one, because it reads as a fact."
        ),
    )
    product_type: ProductType = Field(
        description=(
            "What product this drawing set is for, chosen by the reviewer (#994). Only the published "
            "checks for this product are run, and the readers are told it. Must be one of the "
            "values `GET /product-types` lists: a product with no published check is refused, "
            "because a set checked against nothing would look exactly like a clean one."
        ),
    )


class ProductTypeChoice(BaseModel):
    """One product a drawing set may be for, as the upload screen offers it (#994).

    Listed from the published rulebook rather than typed into the UI, so a product appears only
    once there is at least one check for it.
    """

    value: ProductType
    label: str
    """Plain English for the dropdown, e.g. `Countertop`. Display only; `value` is what is sent."""
    published_checks: int
    """How many published rules check this product — what choosing it will run."""


class ReadingProgressOut(BaseModel):
    """Where the reading of the current revision is, while it runs (#1178).

    Present only while the revision is being read (`UPLOADED` to `VALIDATING_EVIDENCE`, or a retry
    after `FAILED_RETRYABLE` that has written progress since the failure). Everything
    here is recorded, never estimated: a page count is given only where the running part walks the
    pages, and the calls and cost are the model calls the ledger already holds for this reading.
    """

    stage: str = Field(
        description=(
            "Which part of the reading is running, as a code: waiting, ingest, pages, architect, "
            "preparing, rows, dimensions, chains, architect_pairing, whole_pages, saving, match, "
            "validate or form."
        )
    )
    label: str = Field(description="The same part in plain words, for the screen.")
    pages_done: int | None = Field(
        description="Pages this part has finished, or null where it does not go page by page."
    )
    pages_total: int | None = Field(
        description="Pages this part works on, or null where that is not known."
    )
    calls: int = Field(
        description=(
            "Model calls recorded for this revision so far, counted as the Usage page counts them "
            "(reused stored answers are not calls)."
        )
    )
    cost_usd: str = Field(
        description="What those calls cost so far, in US dollars, exact to the millionth."
    )
    unpriced_calls: int = Field(
        description="Calls with no published price; when above 0, `cost_usd` leaves them out."
    )
    updated_at: datetime | None = Field(
        description=(
            "When the worker last said where it is; null before its first line. A time far in the "
            "past while reading means the worker has stopped saying anything (it may have stopped)."
        )
    )
    retrying: bool = Field(
        description=(
            "True while a failed reading is being tried again: the package still says "
            "FAILED_RETRYABLE until the retried stage finishes, and this line is the retry's."
        )
    )


class PackageOut(BaseModel):
    """One package, and the revision documents currently attach to.

    `current_revision_number` is returned rather than left to be inferred. A package has several
    revisions and a client that assumed "the latest" would eventually register a document against a
    superseded one (`docs/DESIGN_PLATFORM.md` §5).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    vendor: str | None
    product_type: ProductType | None
    """What the drawing set is for (#994). `null` for a package created before the reviewer could
    say — its checks run for every product, as they always did."""
    created_at: datetime
    current_revision_id: UUID
    current_revision_number: int
    state: str
    """The current revision's lifecycle state, e.g. `CREATED`. Moving a package through the states is
    the state machine's job (#209, C3.1); this only reports where it is."""
    reading: ReadingProgressOut | None = None
    """Where the reading is while it runs (#1178). Filled by `GET .../packages/{id}` only, and only
    while the revision is being read; `null` otherwise and on the package list."""
    stop_reason: str | None = None
    """Why the current revision's reading stopped, in plain words a person can act on (#1206):
    today only "The AI provider account is out of credit. Top it up, then run the reading again."
    Filled by `GET .../packages/{id}` only, while the revision is stopped (`FAILED_RETRYABLE` or
    `FAILED_PERMANENT`) for that reason; `null` otherwise and on the package list."""


class ExtractionRequestOut(BaseModel):
    """The durable request to read a completed two-drawing package.

    Upload confirmation proves that one file landed. It must not start a package review while its
    counterpart may still be arriving; this handle exists only after the architectural and shop pair
    have both been confirmed.
    """

    accepted_id: UUID
    package_revision_id: UUID


class PackagePage(BaseModel):
    """One page of packages, plus how to ask for the next one.

    `next_cursor` is `None` on the last page, and that is the only reliable end-of-list signal — a page
    shorter than `limit` is not one in any cursor scheme.
    """

    items: list[PackageOut]
    next_cursor: str | None = None
    limit: int
    ordering: str


class DocumentRegistration(BaseModel):
    """A document about to be uploaded: what it is, and the exact bytes it will be.

    There is no size field. A declared size nothing compares against would read as a check and be
    none — the size recorded on the artifact is measured from the stored object at confirmation, which
    is a measurement rather than a claim. The hash is the one declaration, and it is the one that is
    checked.
    """

    model_config = ConfigDict(extra="forbid")

    kind: DocumentKind = Field(
        description="What kind of source document this is, from the fixed V1 vocabulary."
    )
    sha256: str = Field(
        pattern=SHA256_PATTERN,
        description=(
            "The SHA-256 of the file you are about to upload, lowercase hex. Hash it locally first. "
            "It is part of the storage key, so the upload ticket you get back permits writing exactly "
            "these bytes and nothing else."
        ),
    )


class UploadRequest(BaseModel):
    """Another upload of an existing document — a new version of the same logical document.

    Separate from `DocumentRegistration` because the document already exists and its `kind` is already
    settled. Sending `kind` again would let a caller appear to change it, and a document that silently
    changes kind is a document whose earlier versions were classified under something else.
    """

    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=SHA256_PATTERN, description="The SHA-256 of the new file.")


class PresignedUpload(BaseModel):
    """Where to send the bytes, how, and until when.

    Replay `method` and `required_headers` exactly. On a backend that signs headers, the same bytes
    sent with a different verb or a different `Content-Type` is a different request and is refused —
    which is the ticket being narrow, not the storage being awkward.
    """

    document_id: UUID
    storage_key: str
    """The key the bytes will live under. Derived from the document and the hash you declared, so it is
    stable: asking for a second ticket for the same bytes gives the same key."""

    upload_url: str
    method: str
    expires_at: datetime
    """Timezone-aware, and it is a deadline rather than a duration on purpose: a duration would have to
    be added to a clock the client and the server do not share."""

    required_headers: dict[str, str]


class UploadConfirmation(BaseModel):
    """Tell the API the bytes have landed, and what they were.

    The hash is required and re-checked against the stored object. That is the whole substance of this
    request: the API cannot know an upload finished, and it will not take "it finished" on trust.
    """

    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(
        pattern=SHA256_PATTERN,
        description=(
            "The SHA-256 you declared at registration. The stored object is read and hashed, and a "
            "mismatch is rejected without writing anything."
        ),
    )
    page_count: int = Field(
        ge=1,
        description=(
            "How many pages the file has. Your count, not ours — the API never opens the file. "
            "Ingestion builds the real page manifest and a disagreement shows up there."
        ),
    )
    file_name: str | None = Field(
        default=None,
        description=(
            "The file's name as the browser gave it, to name the file in results and reports. "
            "Display text only: never a key, never used by a check. Cleaned before it is stored "
            "(base name only, no control characters, at most 255 characters). Optional: without "
            "it the file is named by its kind."
        ),
    )

    @field_validator("file_name")
    @classmethod
    def _clean_file_name(cls, value: str | None) -> str | None:
        return clean_file_name(value)


class DocumentVersionOut(BaseModel):
    """One immutable upload, pinned to the exact bytes it represents."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    source_artifact_id: UUID
    sha256: str
    page_count: int
    storage_key: str
    size_bytes: int
    created_at: datetime
    file_name: str | None = None
    """The cleaned name the reviewer uploaded (#1174), or None (an older upload, or none sent)."""
