"""A made-up finding's record that each of its numbers was read by two different routes
(`reading_routes`, final plan #2): for tests about other steps, whose reds rest on numbers read
twice. The reading rule itself is tested in `test_reading_routes.py`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.review.assistant import reading_routes

#: The two routes a made-up red's numbers are read by: the PDF's text and the drawn length.
ROUTES = [reading_routes.TEXT, reading_routes.DRAWN]


def two_routes(finding: dict[str, Any]) -> dict[str, Any]:
    """The finding with every number it rests on recorded as read by the text and its drawn
    length (each side's label when it has one, its string's labels for a vendor string)."""
    values: list[dict[str, Any]] = []
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    refs = finding.get("ledger_ids")
    if finding.get("source") in ("vendor_arithmetic", "cross_view") and isinstance(refs, list):
        box = finding.get("vendor_box") or {}
        file = str((box or {}).get("file") or (dims or {}).get("vendor", {}).get("file") or "")
        for ref in refs:
            if re.match(r"^p\d+\.\S+$", str(ref)):
                values.append(_value("vendor", str(ref), file, ""))
    for side in ("vendor", "architect"):
        label = (dims or {}).get(side)
        if isinstance(label, Mapping) and label.get("id"):
            ref = f"p{label.get('page')}.{label.get('id')}"
            values.append(_value(side, ref, str(label.get("file") or ""), label.get("text")))
        else:
            values.append(_value(side, "", "", finding.get(side)))
    finding[reading_routes.FIELD] = {"values": values, "doubts": [], "enough": True}
    return finding


def _value(side: str, label: str, file: str, text: object) -> dict[str, Any]:
    return {
        "side": side,
        "label": label,
        "file": file,
        "text": str(text or ""),
        "routes": list(ROUTES),
        "doubts": [],
    }
