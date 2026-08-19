"""Short-lived capability tokens: one key, one purpose, one deadline (#205, C2.3).

A backend that has no gatekeeper of its own still has to be able to say *what was authorised*. S3
answers that with its own request signature; the local filesystem answers nothing at all. This module
is the primitive both cases are built on: an HMAC-SHA-256 token that carries the key it covers, the
single thing it permits, and the instant it stops being valid.

**Verification never trusts the token's own claims.** `verify_capability` takes the purpose and the
key the caller is *about to allow* and checks the token agrees. The alternative — returning the
claims and letting the caller act on them — would mean a token authorises whatever it says it does,
which is not a check, it is a formality. So the only two answers this module gives are "yes, for
exactly what you asked" and an exception.

**The MAC is verified before the payload is parsed**, over exactly the bytes that arrived. Parsing
first and re-serialising to compare would make the signature cover our re-rendering of the payload
rather than the payload, and any difference in JSON spacing or key order becomes a way to change the
covered text without changing the signature.

**What a token is not.** It is a statement, not an enforcement. Nothing here can stop a process that
can already write to the storage root from writing without a token — the filesystem has no
gatekeeper to consult one. What the token delivers is that whoever *does* accept a write can decide
correctly, and that a token cannot be stretched: not to another key, not to another purpose, not
past its deadline. Where the backend enforces its own signatures (S3, #221) this module is not in the
path at all.

**Timezone-aware instants only.** Exact-arithmetic rules are about measurements and do not apply to a
deadline, but a naive datetime does: comparing one against an aware one raises, and comparing two
naive ones silently compares different clocks. Both `expires_at` and `now` are required to be aware.

Source: `docs/DESIGN_PLATFORM.md` §7 · Verification: `tests/storage/test_signing.py`
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

__all__ = [
    "Capability",
    "CapabilityInvalid",
    "sign_capability",
    "verify_capability",
]

#: Separates the payload from its signature. `.` because both halves are base64url, which never
#: contains one, so splitting can never be ambiguous.
SEPARATOR: Final = "."

#: The token format version, inside the signed payload. A future format change becomes a refusal
#: rather than a payload read under the wrong rules.
FORMAT_VERSION: Final = 1

_DIGEST: Final = "sha256"


class CapabilityInvalid(Exception):
    """The token does not authorise what the caller asked about.

    One exception for every reason — malformed, wrong signature, wrong key, wrong purpose, expired.
    Distinguishing them in the type would let a caller handle "expired" and accidentally treat a
    forged token the same way, and it tells whoever presented it which part of their guess was
    closest.
    """


@dataclass(frozen=True, slots=True)
class Capability:
    """What a verified token turned out to permit.

    Returned *after* the caller's purpose and key have already been matched, so this is a record of
    what was checked rather than an input to the decision.
    """

    purpose: str
    key: str
    expires_at: datetime


def sign_capability(*, secret: bytes, purpose: str, key: str, expires_at: datetime) -> str:
    """Mint a token permitting `purpose` on `key` until `expires_at`.

    Args:
        secret: the signing key. Bytes, not text, because a signing key is not a string with an
            encoding to get wrong. Must not be empty.
        purpose: the single thing this token permits, e.g. `upload`. Compared exactly at
            verification, so an upload token can never be presented as a download token.
        key: the one artifact key this token covers.
        expires_at: when it stops being valid. Must be timezone-aware.

    Returns:
        `<base64url payload>.<base64url signature>`, safe to put in a URL query string unescaped.

    Raises:
        TypeError: the secret is not bytes, or a text field is not a string, or `expires_at` is not a
            datetime.
        ValueError: the secret, purpose or key is empty, or `expires_at` is naive.
    """
    _require_secret(secret)
    _require_text(purpose, "purpose")
    _require_text(key, "key")
    _require_aware(expires_at, "expires_at")

    payload = json.dumps(
        {
            "v": FORMAT_VERSION,
            "purpose": purpose,
            "key": key,
            "exp": expires_at.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    encoded = _b64(payload)
    return f"{encoded}{SEPARATOR}{_b64(_signature(secret, encoded))}"


def verify_capability(
    token: str, *, secret: bytes, purpose: str, key: str, now: datetime
) -> Capability:
    """Confirm this token permits `purpose` on `key` at `now`, or raise.

    The caller states what it intends to allow and this either agrees or refuses. It does not report
    what the token would like to be used for — see the module docstring for why that distinction is
    the whole mechanism.

    `now` is a required argument rather than a clock read here. A function that reads the clock cannot
    be tested at a boundary without moving the machine's time, and "is it expired?" is exactly a
    boundary question.

    Args:
        token: the string from `sign_capability`.
        secret: the same signing key it was minted with.
        purpose: what the caller is about to permit.
        key: the artifact key the caller is about to permit it on.
        now: the instant to judge expiry against. Must be timezone-aware.

    Returns:
        The `Capability` the token carried, all of it already checked against the arguments.

    Raises:
        CapabilityInvalid: for every reason a token might not authorise this.
        TypeError, ValueError: the arguments themselves are unusable (a caller bug, not a bad token).
    """
    _require_secret(secret)
    _require_text(purpose, "purpose")
    _require_text(key, "key")
    _require_aware(now, "now")
    if not isinstance(token, str):
        raise TypeError("token must be a string")

    encoded, separator, signature = token.partition(SEPARATOR)
    if not separator or not encoded or not signature:
        raise CapabilityInvalid("the token is not in the expected two-part form")

    # Signature first, over the bytes as received. Everything below this line is reading text an
    # untrusted caller supplied, and it is only safe to read because the MAC already covered it.
    if not hmac.compare_digest(_signature(secret, encoded), _unb64(signature)):
        raise CapabilityInvalid("the token signature does not match")

    claims = _claims(encoded)
    if claims.get("v") != FORMAT_VERSION:
        raise CapabilityInvalid("the token is a format version this code does not read")
    if claims.get("purpose") != purpose:
        raise CapabilityInvalid("the token does not permit this operation")
    if claims.get("key") != key:
        raise CapabilityInvalid("the token does not cover this artifact key")

    expires_at = _expiry(claims)
    if now >= expires_at:
        raise CapabilityInvalid("the token has expired")
    return Capability(purpose=purpose, key=key, expires_at=expires_at)


def _signature(secret: bytes, encoded_payload: str) -> bytes:
    """The MAC over the encoded payload text, which is what the token actually carries."""

    return hmac.new(secret, encoded_payload.encode("ascii"), _DIGEST).digest()


def _b64(raw: bytes) -> str:
    """base64url with the padding stripped, so the token survives a query string unescaped."""

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """Read back what `_b64` wrote, treating anything else as an invalid token rather than an error."""

    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise CapabilityInvalid("the token is not decodable") from error


def _claims(encoded_payload: str) -> dict[str, Any]:
    """Parse the signed payload. Reached only after the MAC matched."""

    try:
        claims = json.loads(_unb64(encoded_payload))
    except (UnicodeDecodeError, ValueError) as error:
        raise CapabilityInvalid("the token payload is not readable") from error
    if not isinstance(claims, dict):
        raise CapabilityInvalid("the token payload is not an object")
    return claims


def _expiry(claims: dict[str, Any]) -> datetime:
    """The deadline the token carries, refused rather than defaulted when it is unusable.

    A missing or unparseable expiry must never read as "no expiry". A token that cannot say when it
    stops being valid has already failed the one property it exists to carry.
    """

    raw = claims.get("exp")
    if not isinstance(raw, str):
        raise CapabilityInvalid("the token carries no expiry")
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError as error:
        raise CapabilityInvalid("the token expiry is not a readable instant") from error
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise CapabilityInvalid("the token expiry has no timezone")
    return expires_at


def _require_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes | bytearray):
        raise TypeError("secret must be bytes — a signing key is not text with an encoding")
    if not secret:
        raise ValueError("secret must not be empty")


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
