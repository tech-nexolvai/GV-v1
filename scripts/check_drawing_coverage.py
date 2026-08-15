"""Report whether the drawing set covers the variation the checks turn on (#274).

`#274` asks the client for a *spread*, not a count — three wall layouts, scanned and vector pages, a
sheet at two revisions, matched architectural and shop drawings, dual-unit dimensions, and at least
one package with a real defect.

A list like that is easy to agree to and easy to half-deliver. Twelve packages arrive, work starts,
and three weeks later somebody notices every one was vector-exported and the OCR lane has never seen
a real page. This makes the gap visible on day one instead.

**What is declared and what is checked.** Nothing here reads a PDF — extraction is `B2`, and it is
blocked on this very issue. Whoever receives the set records what each package contains in
`data/drawings/MANIFEST.yaml`, and this script checks that the *declarations* cover the six
requirements. Human declares, machine checks completeness. The same division as
`docs/RISK_CONTROLS.md`.

    python scripts/check_drawing_coverage.py

Exit 0 when every requirement is covered, 1 when the set has a gap, 2 when there is no manifest yet.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DRAWINGS = REPO_ROOT / "data" / "drawings"
MANIFEST = DRAWINGS / "MANIFEST.yaml"

COVERED = 0
GAP = 1
NO_MANIFEST = 2


@dataclass(frozen=True, slots=True)
class Requirement:
    """One dimension the gold set has to span, and why the project needs it."""

    key: str
    title: str
    why: str
    #: Declared values that satisfy it. A requirement satisfied by any one value uses a single
    #: entry; `wall_config` needs all three, which `minimum` expresses.
    values: tuple[str, ...] = ()
    minimum: int = 1

    def satisfied_by(self, seen: Sequence[str]) -> bool:
        if not self.values:
            return len([s for s in seen if s == "true"]) >= self.minimum
        return len({v for v in self.values if v in seen}) >= self.minimum


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        key="wall_config",
        title="All three wall layouts",
        why=(
            "tolerance varies by layout (RULE_ENGINE_SPEC §3a), so a missing layout leaves that "
            "applicability variant permanently untested"
        ),
        values=("back_only", "back_left_right", "island"),
        minimum=3,
    ),
    Requirement(
        key="page_origin",
        title="Scanned and vector-exported pages",
        why=(
            "they take different extraction lanes (B2.2 vs B2.4), and only scans exercise deskew "
            "and OCR at all"
        ),
        values=("scanned", "vector"),
        minimum=2,
    ),
    Requirement(
        key="two_revisions",
        title="One package carrying two revisions of the same sheet",
        why="B11 supersession cannot be tested without a real one — a synthetic pair proves nothing",
    ),
    Requirement(
        key="arch_and_shop",
        title="Architectural and shop drawings for the same job",
        why="B5 arch-to-shop matching needs both halves of at least one job",
    ),
    Requirement(
        key="known_defect",
        title="At least one package with a real, reviewed defect",
        why=(
            "the primary metric is critical false-PASS rate, and it is only measurable on cases "
            "where the correct answer is FAIL. A set of clean drawings measures the easy half"
        ),
    ),
    Requirement(
        key="dual_unit",
        title="Dual-unit dimensions",
        why="984 [38 3/4] style, to exercise the corroboration lane that F1 was measured on",
    ),
)


@dataclass
class Package:
    name: str
    declarations: dict[str, str] = field(default_factory=dict)


def _parse_manifest(text: str) -> list[Package]:
    """Read the manifest without a YAML dependency.

    Same reasoning as the risk-control guard: PyYAML is in the `rules` extra, and a script that
    needs an optional dependency is a script that will fail for whoever runs it first.

    Format, deliberately plain enough to hand-edit:

        - package: 2024-0142-kitchen
          wall_config: island
          page_origin: scanned
          two_revisions: true
    """
    packages: list[Package] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            key, _, value = stripped[2:].partition(":")
            packages.append(Package(name=value.strip() or "(unnamed)"))
            if key.strip() != "package":
                packages[-1].declarations[key.strip()] = value.strip()
            continue
        if not packages:
            continue
        key, _, value = stripped.partition(":")
        if value.strip():
            packages[-1].declarations[key.strip()] = value.strip().strip("\"'")
    return packages


def report(packages: Sequence[Package]) -> tuple[str, int]:
    """Render the coverage report and the exit code that goes with it."""
    lines = [
        "Drawing-set coverage (#274)",
        "=" * 60,
        f"packages declared: {len(packages)}",
        "",
    ]
    gaps: list[Requirement] = []
    for requirement in REQUIREMENTS:
        seen = [p.declarations.get(requirement.key, "") for p in packages]
        ok = requirement.satisfied_by(seen)
        if not ok:
            gaps.append(requirement)
        mark = "OK  " if ok else "GAP "
        lines.append(f"  [{mark}] {requirement.title}")
        if requirement.values:
            present = sorted({v for v in requirement.values if v in seen})
            missing = sorted(set(requirement.values) - set(present))
            lines.append(f"         have: {', '.join(present) or 'none'}")
            if missing:
                lines.append(f"         missing: {', '.join(missing)}")
        if not ok:
            lines.append(f"         why it matters: {requirement.why}")
        lines.append("")

    if gaps:
        lines.append(f"INCOMPLETE — {len(gaps)} of {len(REQUIREMENTS)} requirements not covered.")
        lines.append(
            "The set can still be annotated and worked with. What it cannot do is measure the "
            "checks the missing cases exercise, and a gold set that silently omits a lane reports "
            "a passing gate for something nobody tested."
        )
        return "\n".join(lines), GAP

    lines.append(f"COVERED — all {len(REQUIREMENTS)} requirements are represented.")
    lines.append("This set can support the release gates in AGENTS.md §9.")
    return "\n".join(lines), COVERED


def main() -> int:
    if not MANIFEST.exists():
        sys.stderr.write(
            f"No manifest at {MANIFEST.relative_to(REPO_ROOT)}.\n"
            f"{'=' * 60}\n"
            "data/drawings/ is empty and #274 is open: the client has not sent the set yet.\n\n"
            "When it arrives, record what each package contains — nothing here reads a PDF, and\n"
            "extraction (B2) is blocked on this very issue. See the template in the issue.\n\n"
            "Client drawings are proprietary: data/ is gitignored and tests/test_repo_hygiene.py\n"
            "fails the build if anything under it is ever committed.\n"
        )
        return NO_MANIFEST

    text, code = report(_parse_manifest(MANIFEST.read_text(encoding="utf-8")))
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
