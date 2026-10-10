"""What OpenRouter itself says this deployment's key has used (#1165).

The Usage page's "Spend so far" adds up the calls this system recorded. When switched on
(`GV_USAGE_PROVIDER_CHECK=true`, off by default) and `OPENROUTER_API_KEY` is set, the page also
shows what OpenRouter reports for that key, as a cross-check.

**The key's own usage only** (`GET /api/v1/key`), never the account's credits: an account total
spans every project and person on it. Even the key's usage covers everything that uses the key, so
the page labels it "all projects".

**Free, read-only, and never in the way.** The request calls no model. It is served by its own
endpoint, so the recorded totals never wait for it; it has a short timeout; the network call runs
outside the lock; and an answer (or a failure) is kept for five minutes.

**The key goes only to OpenRouter.** It is sent only in the `Authorization` header of a request to
`openrouter.ai`; a redirect is refused rather than followed, so the header can never reach another
host. Any error keeps only that it failed, never the reply's words or the key.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Literal
from urllib.error import HTTPError

KEY_URL: Final = "https://openrouter.ai/api/v1/key"
CACHE_SECONDS: Final = 300.0
TIMEOUT_SECONDS: Final = 3.0

#: Reads one URL with the given headers and returns (HTTP status, body). Tests pass a fake.
Fetch = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as the 3xx it is: the Authorization header never follows it."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_RefuseRedirects())


def _urlopen_fetch(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return int(response.status), bytes(response.read(65536))
    except HTTPError as error:
        status = int(error.code)
        error.close()
        return status, b""


@dataclass(frozen=True, slots=True)
class ProviderCheck:
    """One answer from the provider, or that there is none."""

    status: Literal["ok", "unavailable"]
    used_usd: str | None
    checked_at: datetime


def _usd(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return format(amount.quantize(Decimal("0.000001")), "f")


class OpenRouterUsageCheck:
    """Reads what OpenRouter reports this key has used; at most once every five minutes."""

    def __init__(
        self,
        key: str,
        *,
        fetch: Fetch = _urlopen_fetch,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._headers: Final = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        self._fetch = fetch
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, ProviderCheck] | None = None

    def __repr__(self) -> str:
        return "OpenRouterUsageCheck(key=<hidden>)"

    def check(self) -> ProviderCheck:
        # The lock guards only the memory; the request itself runs outside it, so a slow reply
        # never holds up another request (two at once may both ask, which is harmless).
        with self._lock:
            cached = self._cached
            if cached is not None and self._clock() - cached[0] < self._cache_seconds:
                return cached[1]
        result = self._ask()
        with self._lock:
            self._cached = (self._clock(), result)
        return result

    def _ask(self) -> ProviderCheck:
        checked_at = datetime.now(UTC)
        try:
            status, body = self._fetch(KEY_URL, self._headers, TIMEOUT_SECONDS)
            payload = json.loads(body) if status == 200 else None
        except Exception:  # noqa: BLE001 - only that it failed is kept, never its words
            payload = None
        data = payload.get("data") if isinstance(payload, Mapping) else None
        used = _usd(data.get("usage")) if isinstance(data, Mapping) else None
        if used is None:
            return ProviderCheck("unavailable", None, checked_at)
        return ProviderCheck("ok", used, checked_at)


__all__ = ["KEY_URL", "Fetch", "OpenRouterUsageCheck", "ProviderCheck"]
