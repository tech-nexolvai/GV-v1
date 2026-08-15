"""The coverage checker must report a gap as a gap.

A checker that says COVERED on a half-delivered set is worse than no checker, because it converts an
open question into a settled one. These tests exercise the partial cases specifically — a full set
and an empty set are the easy ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_drawing_coverage import (
    COVERED,
    GAP,
    REQUIREMENTS,
    Package,
    _parse_manifest,
    report,
)


def _complete() -> list[Package]:
    """The minimum set that satisfies all six requirements."""
    return [
        Package(
            "job-a",
            {
                "wall_config": "back_only",
                "page_origin": "scanned",
                "two_revisions": "true",
                "arch_and_shop": "true",
            },
        ),
        Package(
            "job-b",
            {
                "wall_config": "back_left_right",
                "page_origin": "vector",
                "known_defect": "true",
                "dual_unit": "true",
            },
        ),
        Package("job-c", {"wall_config": "island", "page_origin": "vector"}),
    ]


def test_a_complete_set_reports_covered() -> None:
    text, code = report(_complete())
    assert code == COVERED
    assert "COVERED" in text


def test_an_empty_set_is_a_gap_not_a_pass() -> None:
    text, code = report([])
    assert code == GAP
    assert "INCOMPLETE" in text


# ---------------------------------------------------------------------------
# The partial cases — one missing dimension at a time
# ---------------------------------------------------------------------------


def test_a_missing_wall_layout_is_caught() -> None:
    """Two of three layouts is not coverage. The third variant's tolerance would never be tested."""
    packages = _complete()
    packages[2].declarations["wall_config"] = "back_only"  # island now absent
    text, code = report(packages)
    assert code == GAP
    assert "island" in text
    assert "missing" in text


def test_an_all_vector_set_is_caught() -> None:
    """The realistic failure: a CAD-exported set with no scans, so OCR and deskew are never
    exercised and nobody notices until a scanned package arrives in production."""
    packages = _complete()
    packages[0].declarations["page_origin"] = "vector"
    text, code = report(packages)
    assert code == GAP
    assert "scanned" in text


def test_a_set_with_no_defective_package_is_caught() -> None:
    """The one most likely to be quietly dropped — it reads as asking for evidence of a mistake."""
    packages = _complete()
    del packages[1].declarations["known_defect"]
    text, code = report(packages)
    assert code == GAP
    assert "critical false-PASS" in text


def test_a_gap_explains_why_it_matters() -> None:
    """A bare GAP invites someone to decide it is fine. The reason is what makes that harder."""
    packages = _complete()
    del packages[0].declarations["two_revisions"]
    text, _ = report(packages)
    assert "B11 supersession cannot be tested" in text


def test_the_report_never_claims_covered_while_any_requirement_is_unmet() -> None:
    """Swept across every requirement rather than spot-checked, so a new one cannot be added
    without also being enforced."""
    for requirement in REQUIREMENTS:
        packages = _complete()
        for package in packages:
            package.declarations.pop(requirement.key, None)
        text, code = report(packages)
        assert code == GAP, f"{requirement.key} can be missing while the set reports COVERED"
        assert "INCOMPLETE" in text


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_the_manifest_format_is_hand_editable() -> None:
    """Whoever receives the drawings fills this in by hand, so it has to survive being typed."""
    parsed = _parse_manifest("""
        # a comment, and a blank line follow

        - package: 2024-0142-kitchen
          wall_config: island
          page_origin: scanned
          two_revisions: true

        - package: 2024-0187-pantry
          wall_config: back_only
          page_origin: vector
        """)
    assert [p.name for p in parsed] == ["2024-0142-kitchen", "2024-0187-pantry"]
    assert parsed[0].declarations["wall_config"] == "island"
    assert parsed[1].declarations["page_origin"] == "vector"


def test_comments_and_quotes_are_stripped() -> None:
    parsed = _parse_manifest("""
        - package: "job-1"   # trailing comment
          wall_config: 'island'
        """)
    assert parsed[0].name == '"job-1"'.strip('"') or parsed[0].name == '"job-1"'
    assert parsed[0].declarations["wall_config"] == "island"
