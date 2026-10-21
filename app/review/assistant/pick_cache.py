"""The devil's advocate's decided blind picks, kept with the drawings they were made on (#1829).

A blind pick names which architect label measures the same points as a vendor label. Its answer
depends only on what the pick was shown: the two crops, the labels numbered on them, the model,
its effort and the prompt. So a pick already decided on the same drawings is reused, at $0,
instead of asking again:

- **Within one run**, two findings on the same vendor label and the same architect crop share one
  call, whichever architect label each claims.
- **Across runs**, the answer is kept in the set's pack folder (`PICKS_FILE`). The pack folder is
  named by the uploads' SHA-256 (`set_pack.pack_key`), so it already scopes the cache to the same
  drawing files. A re-run or a Continue on them gets the same pick: the same drawings give the
  same red, not a different one each run.

The key holds the marked vendor label (page, id, value, place), every label on the architect's page
(the marked crop numbers them), both crop areas, the model, the effort and the prompt's own text:
never the architect label a helper claimed, which the pick is blind to.
Anything different is a new key, so the pick is asked again. Only a decided reply is kept (one the
model answered in its schema); a reply that was not readable, a failed call or a pick the price
did not reach is never stored.

Code still compares the numbers and decides red (`full_check_advocate.decide`): the cache only
keeps which label the pick named, and where it said both dimensions' ends land.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from app.review.assistant.full_check_advocate import STRONG_PICK

__all__ = ["CACHED", "PICKS_FILE", "decided", "load", "pick_key", "store"]

_log = logging.getLogger(__name__)

#: The decided picks of one set pack: {key: {"answer": ..., "model": ..., "effort": ...}}.
PICKS_FILE: Final = "blind_picks.json"
#: Boxes are rounded to this many decimals of a point in the key (a label's place, a crop's area).
_BOX_DECIMALS: Final = 1
#: Set by code on a pick reused from `PICKS_FILE` or from the same run (never the model's).
CACHED: Final = "cached"
#: Keys a stored answer never carries: code's own marks, set again on every reuse.
_CODE_MARKS: Final = frozenset({STRONG_PICK, CACHED})
_lock = threading.Lock()


def _box(value: object) -> list[float] | None:
    if isinstance(value, Mapping):
        value = [value.get(name) for name in ("x0", "y0", "x1", "y1")]
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
        return None
    try:
        return [round(float(v), _BOX_DECIMALS) for v in value]
    except (TypeError, ValueError):
        return None


def _label(label: Mapping[str, Any]) -> list[Any]:
    return [
        str(label.get("file") or ""),
        label.get("page"),
        str(label.get("id") or ""),
        str(label.get("text") or ""),
        _box(label.get("box")),
    ]


def pick_key(
    *,
    vendor: Mapping[str, Any],
    architect_page: Sequence[Mapping[str, Any]],
    crops: Sequence[Sequence[float]],
    model: str,
    effort: str,
    prompt: str,
) -> str:
    """The key of one blind pick: what it is shown and asked, nothing else. `vendor`: the marked
    vendor label; `architect_page`: every label code numbered on the architect's page; `crops`:
    the vendor's and the architect's crop areas; `prompt`: the pick's fixed prompt text (its
    system prompt, schema and question form), so a changed prompt asks again.

    The architect label a helper claimed is not in the key: the pick is blind to it, so two
    claims on the same vendor label and the same architect crop are one question with one answer
    (a run asked one vendor label against seven architect labels: at most one can be the same
    points, and separate asks could have said yes to two)."""
    numbered = sorted((_label(label) for label in architect_page), key=json.dumps)
    text = json.dumps(
        {
            "vendor": _label(vendor),
            "architect_page": numbered,
            "crops": [_box(list(crop)) for crop in crops],
            "model": model,
            "effort": effort,
            "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decided(answer: object) -> bool:
    """A reply worth keeping: the model answered in the pick's schema (it named a label, or
    `none`). A reply that was not readable, or none at all, is asked again next time."""
    return (
        isinstance(answer, Mapping)
        and "architect_pick" in answer
        and isinstance(answer.get("pick"), str)
        and answer.get("note") != "the reply was not readable"
    )


def _read(folder: Path) -> dict[str, Any]:
    try:
        found = json.loads((folder / PICKS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _log.warning("assistant blind picks: the kept picks could not be read")
        return {}
    return found if isinstance(found, dict) else {}


def load(folder: Path | None, key: str) -> dict[str, Any] | None:
    """The decided pick kept for `key` in the set pack `folder`, or `None`."""
    if folder is None:
        return None
    with _lock:
        entry = _read(folder).get(key)
    if not isinstance(entry, Mapping):
        return None
    answer = entry.get("answer")
    if not isinstance(answer, Mapping) or not decided(answer):
        return None
    return dict(answer)


def store(
    folder: Path | None, key: str, answer: Mapping[str, Any], *, model: str, effort: str
) -> None:
    """Keep a decided pick for `key` in the set pack `folder` (never code's own marks). A pick
    that cannot be kept is only asked again next time: never an error."""
    if folder is None or not decided(answer):
        return
    kept = {name: value for name, value in answer.items() if name not in _CODE_MARKS}
    try:
        with _lock:
            found = _read(folder)
            found[key] = {"answer": kept, "model": model, "effort": effort}
            part = folder / f".{PICKS_FILE}.{os.getpid()}.{threading.get_ident()}"
            part.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
            os.replace(part, folder / PICKS_FILE)
    except OSError:
        _log.warning("assistant blind picks: a decided pick could not be kept")
