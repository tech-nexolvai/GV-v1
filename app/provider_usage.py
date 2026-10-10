"""What OpenRouter itself says has been spent, beside the recorded total (#1165).

The Usage page's "Spend so far" adds up the calls this system recorded. Where a provider reports an
account total, it is shown beside that sum as a cross-check: a gap means calls were made that were
never recorded here (another machine, another tool), or the other way round.

**Free and read-only.** `GET /api/v1/credits` (credits bought and used by the account) costs
nothing and calls no model. Some keys may not read the account's credits; then `GET /api/v1/key`
(what this key has used) is read instead, and the answer says which it is.

**The key never leaves this module.** It is sent only in the `Authorization` header to OpenRouter;
an error keeps only its kind, never the reply's words or the key. Off when no key is set, or when
`GV_USAGE_PROVIDER_CHECK=false`. A reply is kept for five minutes, so opening the page repeatedly
does not ask again each time.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CREDITS_URL: Final = "https://openrouter.ai/api/v1/credits"
KEY_URL: Final = "https://openrouter.ai/api/v1/key"
CACHE_SECONDS: Final = 300.0
TIMEOUT_SECONDS: Final = 5.0

#: Reads one URL with the given headers and returns (HTTP status, body). Tests pass a fake.
Fetch = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def _urlopen_fetch(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), bytes(response.read(65536))
    except HTTPError as error:
        status = int(error.code)
        error.close()
        return status, b""


@dataclass(frozen=True, slots=True)
class ProviderCheck:
    """One answer from the provider, or why there is none."""

    status: Literal["ok", "unavailable"]
    scope: Literal["account", "key"] | None
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


def _data(body: bytes) -> Mapping[str, object] | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    return data if isinstance(data, Mapping) else None


class OpenRouterUsageCheck:
    """Reads OpenRouter's own usage total; at most once every five minutes."""

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
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached[0] < self._cache_seconds:
                return self._cached[1]
            result = self._ask()
            self._cached = (now, result)
            return result

    def _ask(self) -> ProviderCheck:
        checked_at = datetime.now(UTC)
        try:
            status, body = self._fetch(CREDITS_URL, self._headers, TIMEOUT_SECONDS)
            data = _data(body) if status == 200 else None
            used = None if data is None else _usd(data.get("total_usage"))
            if used is not None:
                return ProviderCheck("ok", "account", used, checked_at)
            if status in (401, 403):
                status, body = self._fetch(KEY_URL, self._headers, TIMEOUT_SECONDS)
                data = _data(body) if status == 200 else None
                used = None if data is None else _usd(data.get("usage"))
                if used is not None:
                    return ProviderCheck("ok", "key", used, checked_at)
        except (URLError, TimeoutError, OSError, ValueError):
            pass
        return ProviderCheck("unavailable", None, None, checked_at)


__all__ = ["CREDITS_URL", "KEY_URL", "Fetch", "OpenRouterUsageCheck", "ProviderCheck"]
