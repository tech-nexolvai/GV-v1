"""Immutable, content-addressed rule snapshots.

`AGENTS.md` §2.7 requires every finding to store the exact rule snapshot that produced it, so
a verdict can be reproduced months later. If a rule can be edited without its identifier
changing, you can no longer tell which version of the rule judged a drawing — and an audit
becomes impossible. The snapshot is what makes a verdict re-checkable.

**Canonicalisation.** The identifier is a hash, so the same logical rule must always produce
the same bytes first. This module follows the approach of RFC 8785 (the JSON Canonicalization
Scheme, as used by WebAuthn and JWS): object keys sorted, no insignificant whitespace, UTF-8
output.

Hashing Pydantic's own ``model_dump_json()`` would have been simpler and wrong. Pydantic emits
keys in *field declaration order*, so reordering fields in ``rules/schema.py`` — a purely
cosmetic edit — would change every stored snapshot identifier and silently orphan every
finding that referenced one. Sorting decouples the identifier from Python's field order. The
same applies to ``inputs`` and ``operands``, which otherwise keep the order they were written
in, so two logically identical rules would hash differently.

RFC 8785's hardest requirement, normalising numbers, does not arise here: a ``Fraction``
serialises as the string ``"1/8"`` rather than a float, because ADR-0001 forbids floats in the
first place.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from rules.schema import Rule

#: Prefix on every snapshot identifier, so the algorithm is visible in stored data.
#: If the hash function is ever changed, existing identifiers stay unambiguous.
HASH_ALGORITHM = "sha256"


class SnapshotIntegrityError(Exception):
    """Raised when a snapshot's stored bytes do not match its identifier.

    This means the snapshot was altered after publication. A verdict produced by it can no
    longer be trusted to be reproducible, so this is an error rather than a warning.
    """


class SnapshotConflictError(Exception):
    """Raised when publishing different content under an identifier already in the store.

    Cannot happen while the identifier is honestly derived from the content — so if it does,
    something is generating identifiers by another route.
    """


class VersionConflictError(Exception):
    """Raised when a `(rule_id, version)` pair would map to a second content hash.

    ADR-0006 selects the effective rule by highest version, which is only well defined if a
    version identifies exactly one rule. Two snapshots sharing `CT-WIDTH-001 1.0.0` with
    different content would leave the resolver with no defined way to choose — and the check
    would still run, producing a confident verdict from a rule nobody could later identify.

    The fix is always the same: bump the version.
    """


def canonical_json(rule: Rule) -> str:
    """Return the rule as canonical JSON: sorted keys, no insignificant whitespace.

    The output is what gets hashed, and it is stored alongside the snapshot so the hash can be
    re-verified later without needing to re-serialise through the current model code. That
    matters: a future change to the model must not be able to alter the bytes of an
    already-published snapshot.
    """
    # Round-trip through Pydantic's JSON first so field serialisers apply (Fraction -> "1/8",
    # enums -> their values), then re-emit with sorted keys.
    payload: Any = json.loads(rule.model_dump_json())
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compute_snapshot_id(rule: Rule) -> str:
    """Return the deterministic content identifier for a rule, e.g. ``sha256:1a2b...``.

    Full-length hex, deliberately untruncated: this identifier ties a manufacturing decision
    to the rule that made it, and truncation trades away collision resistance for prettier
    logs.
    """
    digest = hashlib.sha256(canonical_json(rule).encode("utf-8")).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"


def _version_key(version: str) -> tuple[int, ...]:
    """Return a sortable key for a semantic version.

    Compared numerically per component, because string ordering puts "1.0.10" *below*
    "1.0.9" — a silently wrong answer to "which is newest". `Rule.version` is validated
    against a three-part numeric pattern on the way in, so the parse is safe here.
    """
    return tuple(int(part) for part in version.split("."))


@dataclass(frozen=True, slots=True)
class RuleSnapshot:
    """A published rule, frozen and identified by the hash of its own content.

    Immutability is structural rather than a convention. The dataclass is frozen, the ``Rule``
    inside it was already frozen at validation, and — most importantly — the identifier *is*
    the content hash, so there is no way to change the content while keeping the identifier.
    An edited rule is a different snapshot by construction.
    """

    snapshot_id: str
    rule: Rule
    canonical_json: str

    @property
    def rule_id(self) -> str:
        """The rule's own identifier, e.g. ``CT-WIDTH-001``."""
        return self.rule.id

    @property
    def version(self) -> str:
        """The rule's authored version, e.g. ``1.0.0``."""
        return self.rule.version

    @property
    def short_id(self) -> str:
        """First eight hex characters, for logs and reports.

        Display only. Never use this as an identity — see :func:`compute_snapshot_id`.
        """
        return self.snapshot_id.split(":", 1)[1][:8]

    @property
    def label(self) -> str:
        """A human-readable description for a report, e.g.
        ``CT-WIDTH-001 1.0.0 (1a2b3c4d)``."""
        return f"{self.rule_id} {self.version} ({self.short_id})"

    def verify(self) -> None:
        """Recompute the identifier from the stored bytes and confirm it still matches.

        Detects a snapshot altered at rest. Deliberately hashes ``canonical_json`` as stored
        rather than re-serialising the rule, so this stays a check on the stored bytes rather
        than on the current model code.
        """
        digest = hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()
        expected = f"{HASH_ALGORITHM}:{digest}"
        if expected != self.snapshot_id:
            raise SnapshotIntegrityError(
                f"snapshot {self.snapshot_id} does not match its own content "
                f"(recomputed {expected}). It was altered after publication, so any verdict "
                "it produced can no longer be reproduced."
            )


def publish(rule: Rule) -> RuleSnapshot:
    """Freeze a validated rule into a snapshot.

    Publishing is a pure function of the rule: the same rule always yields the same snapshot,
    which is what makes republishing idempotent and makes a change necessarily a new
    identifier. Nothing time-dependent enters the hash — a timestamp would break
    "byte-identical input yields an identical identifier". Record publication time alongside
    the snapshot instead.
    """
    body = canonical_json(rule)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return RuleSnapshot(
        snapshot_id=f"{HASH_ALGORITHM}:{digest}",
        rule=rule,
        canonical_json=body,
    )


class SnapshotStore:
    """An append-only collection of published snapshots.

    Republishing identical content is idempotent — same content, same identifier, nothing to
    do. Publishing *different* content simply produces a different identifier, so an in-place
    edit is not something the store has to prevent; it cannot be expressed.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, RuleSnapshot] = {}
        # (rule_id, version) -> snapshot_id. The index that makes "highest version" a
        # well-defined question: one version, one rule.
        self._by_rule_version: dict[tuple[str, str], str] = {}

    def add(self, snapshot: RuleSnapshot) -> RuleSnapshot:
        """Store a snapshot, or return the existing one when it is already present.

        Verifies integrity on the way in: a snapshot whose bytes do not match its identifier
        never enters the store.
        """
        snapshot.verify()
        existing = self._by_id.get(snapshot.snapshot_id)
        if existing is not None:
            if existing.canonical_json != snapshot.canonical_json:
                raise SnapshotConflictError(
                    f"{snapshot.snapshot_id} already stores different content. An identifier "
                    "derived from content cannot legitimately collide, so the identifier was "
                    "produced some other way."
                )
            return existing

        # ADR-0006: one version, one rule. Republishing identical content returned above,
        # so reaching here with a known (rule_id, version) means the content changed.
        key = (snapshot.rule_id, snapshot.version)
        clash = self._by_rule_version.get(key)
        if clash is not None:
            raise VersionConflictError(
                f"{snapshot.rule_id} {snapshot.version} is already published as {clash[:15]}..., "
                f"and this is different content ({snapshot.snapshot_id[:15]}...). "
                "A published rule cannot be changed in place — bump the version instead. "
                "Otherwise 'the highest version' no longer identifies a single rule, and a "
                "verdict could not be traced to the rule that produced it."
            )

        self._by_id[snapshot.snapshot_id] = snapshot
        self._by_rule_version[key] = snapshot.snapshot_id
        return snapshot

    def get(self, snapshot_id: str) -> RuleSnapshot:
        """Return the snapshot with this identifier.

        Raises ``KeyError`` when absent. A finding referencing an unknown snapshot is a
        integrity problem, not a cache miss, so there is deliberately no default.
        """
        return self._by_id[snapshot_id]

    def __contains__(self, snapshot_id: object) -> bool:
        return snapshot_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def latest(self, rule_id: str) -> RuleSnapshot | None:
        """Return the effective snapshot for a rule: the one with the highest version.

        Returns ``None`` when the rule has never been published. That is a normal state — a
        rule that does not exist yet is not an integrity failure — and the caller turns it
        into NO_APPLICABLE_RULE rather than an exception.

        Safe to call precisely because :meth:`add` guarantees one content hash per version;
        without that, "the highest version" could name two different rules.
        """
        candidates = self.versions_of(rule_id)
        if not candidates:
            return None
        return max(candidates, key=lambda s: _version_key(s.version))

    def rule_ids(self) -> tuple[str, ...]:
        """Every distinct rule id in the store, sorted.

        The applicability resolver needs the candidate set and lives in another module, so it
        cannot read ``_by_id``. Sorted rather than insertion-ordered because resolution must
        not depend on the order snapshots were added — the same store rebuilt in a different
        order has to resolve identically (ADR-0006).
        """
        return tuple(sorted({s.rule_id for s in self._by_id.values()}))

    def versions_of(self, rule_id: str) -> tuple[RuleSnapshot, ...]:
        """Every stored snapshot for one rule, oldest identifier order not implied.

        Useful for showing a reviewer that a rule has been republished, and which snapshot a
        given finding used.
        """
        return tuple(s for s in self._by_id.values() if s.rule_id == rule_id)
