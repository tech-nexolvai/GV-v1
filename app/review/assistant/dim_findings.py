"""Findings built from dimension labels (#1558): the agent picks a label, code fills the rest.

`report_finding` and `update_finding` take `architect_dim` / `vendor_dim` (a label id, `D17` or
`p23.D17`, from `dim_index`), and `cite` takes `dim`. Code then fills that side's value (the
label's text, as printed), its box (the label's own box, so the evidence crop and the cloud go
round the printed value) and records which label it came from (`dims`). The model may still type
the value: when it is not the label's value the call is refused (it read the picture one way and
picked a label that prints another, so one of them is wrong). Without a label, nothing changes.

**Witness.** A side built from a label was read by code (`dim_labels`): `label_checks` gives it
code's reading as its `verify_<side>` check (a text label also its `printed_<side>` match), so a
finding whose two sides are text labels has the `printed` witness, and a drawn-ink label the
`ocr_ink` witness under the rule that already governs it. Only while the side still claims the
label (the same box, the same inches): a value or box changed later without a label goes back to
the ordinary checks.

Pure code: `dim_index` reads `dims.json` (and loads the PDF reader by name only when a page's
labels must be read now).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.review.assistant.dim_index import DimLabels, parse_ref
from app.review.assistant.finding_checks import claim_value, to_inches

__all__ = [
    "DIM_FIELDS",
    "fill_from_labels",
    "label_checks",
    "label_for_cite",
]

#: A finding's label fields: (field, side, box field).
DIM_FIELDS: Final = (
    ("architect_dim", "architect", "arch_box"),
    ("vendor_dim", "vendor", "vendor_box"),
)
_EXPECTED: Final = {"architect": ("architect", "brand"), "vendor": ("vendor",)}


class LabelRefused(ValueError):
    """The label cannot be used as asked; the message says why and what to do instead."""


def _inches(label: Mapping[str, Any]) -> Decimal | None:
    try:
        return Decimal(str(label.get("inches")))
    except (InvalidOperation, ValueError):
        return None


def _same(text: object, label: Mapping[str, Any]) -> bool | None:
    """Whether a typed value is the label's value (`None`: it holds no number)."""
    claim = claim_value(text)
    if claim is None:
        return None
    return to_inches(claim) == _inches(label)


def _provenance(label: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": label.get("id"),
        "page": label.get("page"),
        "file": label.get("file"),
        "text": label.get("text"),
        "inches": label.get("inches"),
        "route": label.get("route"),
        "side": label.get("side"),
        "box": list(label.get("box") or []),
        **({"origin": label["origin"]} if label.get("origin") else {}),
    }


def fill_from_labels(
    raw: Mapping[str, Any],
    labels: DimLabels,
    *,
    vendor_file: str | None,
    architect_file: str | None,
    current: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    """The finding's fields with each labelled side filled in (`architect_dim` / `vendor_dim`
    taken out), the finding's `dims` (which label each side came from; `None` when no side has
    one) and notes for the model. `current` is the finding being updated (an update keeps its
    other side's label, and drops a side's label when its value or box changes without one).
    Raises `LabelRefused`."""
    out = {k: v for k, v in raw.items() if k not in ("architect_dim", "vendor_dim")}
    before = (current or {}).get("dims")
    dims: dict[str, Any] = dict(before) if isinstance(before, Mapping) else {}
    notes: list[str] = []
    page = out.get("page", (current or {}).get("page"))
    vs_page = out.get("vs_page", (current or {}).get("vs_page"))
    for field, side, box_field in DIM_FIELDS:
        ref = raw.get(field)
        if ref is None:
            if side in out or box_field in out:
                dims.pop(side, None)
            continue
        file = vendor_file if side == "vendor" else (architect_file or vendor_file)
        where = page if side == "vendor" else (vs_page or page)
        if file is None or not isinstance(where, int):
            raise LabelRefused(f"{field}: give the page first (the label is looked up on it)")
        try:
            label = labels.resolve(ref, file, where)
        except LookupError as missing:
            raise LabelRefused(f"{field}: {missing}") from None
        typed = out.get(side)
        same = _same(typed, label)
        if same is False:
            raise LabelRefused(
                f"{side} {typed!r} is not what {label['id']} prints ({label['text']}): check "
                f"which label you mean, or leave {side} out and code fills it from the label"
            )
        if same is None:
            out[side] = str(label["text"])
        x0, y0, x1, y1 = (float(v) for v in label["box"])
        out[box_field] = {
            "page": label["page"],
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "file": file,
        }
        if side == "architect" and label["page"] != page and vs_page is None:
            out["vs_page"] = label["page"]
            vs_page = label["page"]
        found_side = str(label.get("side") or "unknown")
        if found_side != "unknown" and found_side not in _EXPECTED[side]:
            notes.append(
                f"{label['id']} is on the drawing the page map calls the {found_side}'s: check "
                f"it is the {side}'s"
            )
        if label.get("origin") == "picture":
            notes.append(f"{label['id']} was read from a pasted picture by OCR: look at it once")
        elif label.get("route") == "drawn":
            notes.append(f"{label['id']} was read from drawn ink by OCR: look at it once")
        dims[side] = _provenance(label)
    return out, (dims or None), notes


def _claims(
    finding: Mapping[str, Any], side: str, box_field: str, label: Mapping[str, Any]
) -> bool:
    box = finding.get(box_field)
    if not isinstance(box, Mapping) or box.get("page") != label.get("page"):
        return False
    try:
        same_box = all(
            abs(float(box[key]) - float(value)) < 0.05
            for key, value in zip(("x0", "y0", "x1", "y1"), label["box"], strict=True)
        )
    except (KeyError, TypeError, ValueError):
        return False
    return same_box and bool(_same(finding.get(side), label))


def label_checks(finding: Mapping[str, Any], checks: Mapping[str, Any]) -> dict[str, Any]:
    """`checks` with code's reading of each side the finding still claims from a label."""
    out = dict(checks)
    dims = finding.get("dims")
    if not isinstance(dims, Mapping):
        return out
    for _field, side, box_field in DIM_FIELDS:
        label = dims.get(side)
        if not isinstance(label, Mapping) or not _claims(finding, side, box_field, label):
            continue
        key = f"verify_{side}"
        existing = out.get(key)
        if isinstance(existing, Mapping) and existing.get("result") == "match":
            continue
        route = "drawn" if label.get("route") == "drawn" else "text"
        how = (
            f"the {side}'s pasted picture (two OCR engines)"
            if label.get("origin") == "picture"
            else (
                f"the {side}'s drawn ink (two OCR engines)"
                if route == "drawn"
                else "the drawing's text"
            )
        )
        out[key] = {
            "result": "match",
            "route": route,
            "read": label.get("text"),
            "reads": [f"label {label.get('id')}: {label.get('text')}"],
            "reason": f"code listed this dimension (label {label.get('id')}) from {how}",
            "value": claim_value(finding.get(side)),
            "pending": False,
            "words": f"Code listed this dimension (label {label.get('id')}): {label.get('text')}",
            "label": label.get("id"),
        }
        if route == "text":
            out[f"printed_{side}"] = "match"
    return out


def label_for_cite(
    raw: Mapping[str, Any], labels: DimLabels, *, file: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A cite's `dim`: its value (when not typed) and box filled from the label. Raises
    `LabelRefused`."""
    ref = raw.get("dim")
    page = raw.get("page")
    if isinstance(raw.get("box"), Mapping):
        page = raw["box"].get("page", page)
    if file is None:
        raise LabelRefused("dim: the set has no PDF to look the label up in")
    named = parse_ref(ref)
    if named is None:
        raise LabelRefused(f"dim: {ref!r} is not a label id (D17, or p23.D17 for another page)")
    if not isinstance(page, int) or isinstance(page, bool):
        if named[0] is None:
            raise LabelRefused("dim: give the page too (or name it, p23.D17)")
        page = named[0]
    try:
        label = labels.resolve(ref, file, page)
    except LookupError as missing:
        raise LabelRefused(f"dim: {missing}") from None
    out = {k: v for k, v in raw.items() if k != "dim"}
    same = _same(out.get("value"), label)
    if same is False:
        raise LabelRefused(
            f"value {out.get('value')!r} is not what {label['id']} prints ({label['text']})"
        )
    if same is None:
        out["value"] = str(label["text"])
    x0, y0, x1, y1 = (float(v) for v in label["box"])
    out["box"] = {"page": label["page"], "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": file}
    out.pop("page", None)
    return out, _provenance(label)
