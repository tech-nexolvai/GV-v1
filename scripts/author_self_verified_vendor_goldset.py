"""Author reading-only gold cases from independently agreeing vendor dual notation.

The input proposals are proprietary exploration data under ``data/``. This harness is tracked, but
its outputs are not: every case, PDF link, pending report, and scorecard stays under git-ignored
``data/`` paths.

This is intentionally narrower than a general gold-set author. A row is eligible only when its raw
``millimetres [inches]`` token parses and the repository's rounding-aware dual-unit policy independently
confirms the two authored values agree. The bracketed inch value becomes the reading answer because
inches govern (CLIENT_FACTS Q12). No finding/verdict is authored.

Semantic labels are required by GOLD_SET_FORMAT even for a reading-only case. Here they are explicitly
*not* human-confirmed. A proposal's existing positional label wins when present; otherwise a
deterministic bbox heuristic suggests filler width at the two ends of a horizontal row, cabinet width
in its interior, and the neutral field-dimension type when the position does not establish either. The
unconfirmed status and basis are recorded both in provenance and in an adjacent ``case_metadata.json``;
none of this enters the production typing path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from eval.gold_set.schema import GoldCase
from units.dual import DualDimensionParseError, parse_dual
from units.imperial import format_inches
from units.policy import Consistency, check_dual
from vocabulary.semantic_types import SemanticType

AGREE = "AGREE"
TYPE_STATUS = "heuristic-unconfirmed"
VALUE_PROVENANCE = "dual-notation self-verified (mm~=inch), NOT per-case human-read"
DEFAULT_PROPOSAL_ROOT = Path("data/exploration/all_projects_vendor_dimension_proposals")
DEFAULT_SOURCE = Path("data/drawings/aiset2_reviewed/AI_Set_2_reviewed.pdf")
DEFAULT_CASE_ROOT = Path("data/goldset/aiset2-self-verified-vendor-readings")
DEFAULT_PENDING_REPORT = DEFAULT_PROPOSAL_ROOT / "PENDING_FOR_TRUE_REPRESENTATIVE_NUMBER.md"
_CASE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True, slots=True)
class Proposal:
    """One validated proposal row with its owning project."""

    project: str
    row: dict[str, Any]

    @property
    def page(self) -> int:
        """One-based reviewed-set page number."""
        return int(self.row["page"])

    @property
    def proposal_id(self) -> str:
        """Source proposal identifier, validated before it becomes a path component."""
        return str(self.row["id"])

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        """Validated 300 dpi image-pixel location from the proposal."""
        values = tuple(self.row["bbox_300dpi"])
        if len(values) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError(f"{self.project}/{self.proposal_id} has a non-integer four-point bbox")
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            raise ValueError(f"{self.project}/{self.proposal_id} has an empty bbox {values!r}")
        return left, top, right, bottom


@dataclass(frozen=True, slots=True)
class TypeSuggestion:
    """A vocabulary value plus its unconfirmed input-label or bbox-selection basis."""

    semantic_type: SemanticType
    basis: str


def load_proposals(paths: Iterable[Path]) -> list[Proposal]:
    """Load proposal JSON without changing, completing, or visually interpreting any row."""
    proposals: list[Proposal] = []
    for path in sorted(paths):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise TypeError(f"{path} must contain a JSON list")
        for row in payload:
            if not isinstance(row, dict):
                raise TypeError(f"{path} contains a non-object proposal")
            proposal = Proposal(project=path.parent.name, row=row)
            _ = proposal.bbox
            proposals.append(proposal)
    return proposals


def self_verified(proposals: Sequence[Proposal]) -> list[Proposal]:
    """Return only rows whose raw dual token independently satisfies the dual-unit policy."""
    eligible: list[Proposal] = []
    for proposal in proposals:
        if proposal.row.get("agreement") != AGREE:
            continue
        raw = proposal.row.get("raw")
        if not isinstance(raw, str):
            raise TypeError(f"{proposal.project}/{proposal.proposal_id} has no raw dual token")
        try:
            dual = parse_dual(raw)
        except DualDimensionParseError as error:
            raise ValueError(
                f"{proposal.project}/{proposal.proposal_id} is labelled AGREE but is not dual notation"
            ) from error
        if dual.alternate is None or check_dual(dual) is not Consistency.CONSISTENT_WITHIN_ROUNDING:
            raise ValueError(
                f"{proposal.project}/{proposal.proposal_id} is labelled AGREE but its authored "
                "mm/in values do not independently corroborate"
            )
        eligible.append(proposal)
    return eligible


def _vertical_overlap(first: Proposal, second: Proposal) -> bool:
    _, first_top, _, first_bottom = first.bbox
    _, second_top, _, second_bottom = second.bbox
    return max(first_top, second_top) <= min(first_bottom, second_bottom)


def suggest_types(proposals: Sequence[Proposal]) -> dict[tuple[str, str], TypeSuggestion]:
    """Keep existing positional labels, then use bbox position with a neutral unclear fallback."""
    suggestions: dict[tuple[str, str], TypeSuggestion] = {}
    unresolved: list[Proposal] = []
    for proposal in proposals:
        existing = str(proposal.row.get("proposed_type", ""))
        key = (proposal.project, proposal.proposal_id)
        if existing.startswith(SemanticType.FILLER_WIDTH.value):
            suggestions[key] = TypeSuggestion(
                SemanticType.FILLER_WIDTH,
                "upstream proposal label records an end-of-run position; "
                "heuristic-unconfirmed for this case",
            )
        elif existing.startswith(SemanticType.CABINET_WIDTH.value):
            suggestions[key] = TypeSuggestion(
                SemanticType.CABINET_WIDTH,
                "upstream proposal label records a mid-run segment position; "
                "heuristic-unconfirmed for this case",
            )
        else:
            unresolved.append(proposal)

    by_page: dict[tuple[str, int], list[Proposal]] = defaultdict(list)
    for proposal in unresolved:
        by_page[(proposal.project, proposal.page)].append(proposal)
    for page_proposals in by_page.values():
        remaining = list(page_proposals)
        groups: list[list[Proposal]] = []
        while remaining:
            group = [remaining.pop(0)]
            changed = True
            while changed:
                changed = False
                for proposal in list(remaining):
                    if any(_vertical_overlap(proposal, member) for member in group):
                        group.append(proposal)
                        remaining.remove(proposal)
                        changed = True
            groups.append(group)
        for group in groups:
            ordered = sorted(group, key=lambda proposal: (proposal.bbox[0] + proposal.bbox[2]) / 2)
            for index, proposal in enumerate(ordered):
                key = (proposal.project, proposal.proposal_id)
                if len(ordered) >= 3 and index in {0, len(ordered) - 1}:
                    suggestions[key] = TypeSuggestion(
                        SemanticType.FILLER_WIDTH,
                        "outermost token in a horizontally aligned dimension row; endpoint/wall "
                        "relationship is heuristic-unconfirmed",
                    )
                elif len(ordered) >= 3:
                    suggestions[key] = TypeSuggestion(
                        SemanticType.CABINET_WIDTH,
                        "interior token in a horizontally aligned dimension row; cabinet relationship "
                        "is heuristic-unconfirmed",
                    )
                else:
                    suggestions[key] = TypeSuggestion(
                        SemanticType.FIELD_DIMENSION,
                        "position does not establish an end-of-run filler or mid-run cabinet segment; "
                        "neutral reading-only fallback is heuristic-unconfirmed",
                    )
    return suggestions


def _exact_text(value: Fraction) -> str:
    """Serialize an exact rational without rounding it through a float."""
    return (
        str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    )


def _case_id(proposal: Proposal) -> str:
    """Return one safe path component or refuse untrusted proposal identifiers."""
    case_id = f"aiset2-{proposal.project.replace('_', '-')}-{proposal.proposal_id}"
    if _CASE_ID_RE.fullmatch(case_id) is None:
        raise ValueError(
            f"unsafe case id {case_id!r} derived from {proposal.project!r}/{proposal.proposal_id!r}"
        )
    return case_id


def case_payload(
    proposal: Proposal,
    *,
    suggestion: TypeSuggestion,
    source_name: str,
    source_hash: str,
    annotated_on: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build schema-valid reading truth and adjacent status metadata from one corroborated token."""
    dual = parse_dual(str(proposal.row["raw"]))
    if dual.alternate is None or check_dual(dual) is not Consistency.CONSISTENT_WITHIN_ROUNDING:
        raise ValueError(
            f"{proposal.project}/{proposal.proposal_id} cannot be authored: its authored "
            "mm/in values do not independently corroborate"
        )
    case_id = _case_id(proposal)
    source_description = f"vendor dual notation, AI_Set_2_reviewed vendor-only p{proposal.page}"
    payload: dict[str, Any] = {
        "id": case_id,
        "product_type": "cabinet",
        "arch": source_name,
        "shop": source_name,
        "ground_truth": {
            "observations": [
                {
                    "semantic_type": suggestion.semantic_type.value,
                    "source": "SHOP",
                    "value": {
                        "exact": _exact_text(dual.alternate.exact),
                        "unit": "in",
                        "raw_text": str(proposal.row["raw"]),
                    },
                    "page": proposal.page,
                    "polygon": list(proposal.bbox),
                    "item_id": f"{case_id}-{TYPE_STATUS}",
                }
            ],
            "matches": [],
            "expected_findings": [],
        },
        "provenance": {
            "annotator": (
                f"{VALUE_PROVENANCE}; source={source_description}; "
                f"semantic_type positional heuristic-unconfirmed"
            ),
            "annotated_on": annotated_on.isoformat(),
            "documents": [
                {
                    "source": "SHOP",
                    "document_version_id": str(uuid5(NAMESPACE_URL, source_hash)),
                    "content_hash": source_hash,
                }
            ],
        },
        "disagreements": [],
    }
    metadata = {
        "case_id": case_id,
        "value_status": VALUE_PROVENANCE,
        "value_source": source_description,
        "millimetres": str(dual.primary.exact),
        "inches": format_inches(dual.alternate.exact),
        "semantic_type": suggestion.semantic_type.value,
        "semantic_type_status": TYPE_STATUS,
        "semantic_type_basis": suggestion.basis,
        "expected_finding": "ABSENT - reading accuracy only",
        "proposal_source": f"{proposal.project}/dimensions.json#{proposal.proposal_id}",
    }
    GoldCase.model_validate(payload)
    return payload, metadata


def _link_or_verify(source: Path, destination: Path, source_hash: str) -> None:
    """Hard-link one immutable source, or verify an idempotent rerun's existing bytes."""
    if destination.exists():
        actual = "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest()
        if actual != source_hash:
            raise ValueError(f"refusing to replace changed case drawing {destination}")
        return
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _write_or_verify(path: Path, content: str) -> None:
    """Write a new case artifact, refusing to overwrite a different prior artifact."""
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError(f"refusing to overwrite changed authored artifact {path}")
        return
    path.write_text(content, encoding="utf-8")


def author_cases(
    proposals: Sequence[Proposal],
    *,
    source_pdf: Path,
    output_root: Path,
    annotated_on: date,
) -> list[Path]:
    """Write one independently loadable, reading-only package per eligible proposal."""
    case_ids = [_case_id(proposal) for proposal in proposals]
    if len(case_ids) != len(set(case_ids)):
        duplicate = next(case_id for case_id in case_ids if case_ids.count(case_id) > 1)
        raise ValueError(f"duplicate case id {duplicate!r}; refusing to author any packages")

    source_hash = "sha256:" + hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    suggestions = suggest_types(proposals)
    prepared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for proposal, case_id in zip(proposals, case_ids, strict=True):
        suggestion = suggestions[(proposal.project, proposal.proposal_id)]
        payload, metadata = case_payload(
            proposal,
            suggestion=suggestion,
            source_name=source_pdf.name,
            source_hash=source_hash,
            annotated_on=annotated_on,
        )
        if payload["id"] != case_id:
            raise AssertionError("validated case id changed while constructing the payload")
        prepared.append((case_id, payload, metadata))

    written: list[Path] = []
    for case_id, payload, metadata in prepared:
        case_dir = output_root / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        _link_or_verify(source_pdf, case_dir / source_pdf.name, source_hash)
        _write_or_verify(case_dir / "answer_key.json", json.dumps(payload, indent=2) + "\n")
        _write_or_verify(case_dir / "case_metadata.json", json.dumps(metadata, indent=2) + "\n")
        written.append(case_dir)
    return written


def write_pending_report(proposals: Sequence[Proposal], output: Path) -> None:
    """List every excluded row so uncertainty remains visible rather than silently disappearing."""
    pending = [proposal for proposal in proposals if proposal.row.get("agreement") != AGREE]
    lines = [
        "# Pending dimensions for the representative reading-accuracy number",
        "",
        (
            f"These {len(pending)} proposals were deliberately excluded from answer-key authoring. "
            "No crop was read by Codex or the reader to invent an answer. A human must establish "
            "the value first."
        ),
        "",
        "| Project | Proposal | Page | OCR proposal | Location @300dpi | Why pending |",
        "|---|---|---:|---|---|---|",
    ]
    for proposal in pending:
        box = ", ".join(str(value) for value in proposal.bbox)
        raw = str(proposal.row.get("raw", "")).replace("|", "\\|")
        reason = str(proposal.row.get("agreement", "unknown")).replace("|", "\\|")
        lines.append(
            f"| {proposal.project} | {proposal.proposal_id} | {proposal.page} | `{raw}` | "
            f"[{box}] | {reason} |"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Validate the fixed selection, author cases, and preserve every excluded proposal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-root", type=Path, default=DEFAULT_PROPOSAL_ROOT)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_CASE_ROOT)
    parser.add_argument("--pending-report", type=Path, default=DEFAULT_PENDING_REPORT)
    parser.add_argument("--expected-count", type=int, default=49)
    parser.add_argument("--annotated-on", type=date.fromisoformat, default=datetime.now(UTC).date())
    arguments = parser.parse_args()

    all_proposals = load_proposals(arguments.proposal_root.glob("*/dimensions.json"))
    eligible = self_verified(all_proposals)
    if len(eligible) != arguments.expected_count:
        raise ValueError(
            f"expected {arguments.expected_count} self-verified proposals, found {len(eligible)}; "
            "refusing to author a silently changed selection"
        )
    written = author_cases(
        eligible,
        source_pdf=arguments.source,
        output_root=arguments.output,
        annotated_on=arguments.annotated_on,
    )
    write_pending_report(all_proposals, arguments.pending_report)
    print(f"authored {len(written)} reading-only self-verified case(s) under {arguments.output}")
    print(
        f"pending uncertain proposals: {len(all_proposals) - len(eligible)} in {arguments.pending_report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
