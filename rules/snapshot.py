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
        self._by_id[snapshot.snapshot_id] = snapshot
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

    def versions_of(self, rule_id: str) -> tuple[RuleSnapshot, ...]:
        """Every stored snapshot for one rule, oldest identifier order not implied.

        Useful for showing a reviewer that a rule has been republished, and which snapshot a
        given finding used.
        """
        return tuple(s for s in self._by_id.values() if s.rule_id == rule_id)
