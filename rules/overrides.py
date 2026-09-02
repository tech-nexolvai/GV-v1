"""The summary of what a reviewer changed before a run, and what nobody supplied.

`CLIENT_FACTS` Q10. Every check carries a GLOBAL default — the company standard. Before a run the
reviewer may set PROJECT overrides for that job: *"the reviewer says I can manage with 3.5 inch"*.
Raj asked for two things to come out of that, and this module produces both:

* *"wherever the global and project-specific variables differ, you can send a report, a summary of
  the variable discrepancies"* — `override_report`.
* *"imagine filling a form, there are some mandatory entries"* — a required parameter nobody set is
  listed as outstanding, so the reviewer is prompted rather than the run proceeding without it.

**No override is ever read off a drawing.** This is the substance of Q10 and the easiest thing to get
wrong. A shop drawing that says `4" TYP U.N.O.` is *stating* a value, not authorising one; treating
the note as an override would let the drawing under review decide the standard it is judged against,
which is a vendor marking their own homework. Overrides come from the reviewer, through the parameter
layers, and from nowhere else. Nothing in this module accepts evidence, and `tests/rules/` asserts
that the module never reaches into `extraction/` or `evidence/` to get one.

**Where the drawing differs from the effective value, the flag is the finding.** That mechanism
already exists and is not duplicated here: the rule compares the drawn dimension against the resolved
parameter and returns FAIL when they differ, the reviewer accepts or rejects it through the review
ledger (`confirm` / `except`). Building a second discrepancy channel beside the verdict would give a
reviewer two places to look and eventually two different answers. What was missing was never the
flag — it was the summary of the parameter changes that produced it, which is this.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rules.parameters import (
    ParameterLayer,
    ParameterSet,
    ParameterValue,
    ResolvedParameter,
    ShadowedParameter,
    resolve_all,
)
from rules.schema import Rule
from units.imperial import format_inches


@dataclass(frozen=True, slots=True)
class Override:
    """One parameter whose effective value displaced something below it."""

    name: str
    effective: ParameterValue
    layer: ParameterLayer
    displaced: tuple[ShadowedParameter, ...]

    @property
    def displaces_a_company_standard(self) -> bool:
        """True when a GLOBAL value was set aside — the case Q10 asks to be reported."""
        return any(s.layer is ParameterLayer.GLOBAL for s in self.displaced)

    def explain(self) -> str:
        """One line of plain English, in the same shape `ResolvedParameter.explain` produces."""
        displaced = ", ".join(
            f"{format_inches(s.value.value.exact_value)} {s.value.value.unit.value} "
            f"({s.layer.value})"
            for s in self.displaced
        )
        return (
            f"{self.name} = {format_inches(self.effective.value.exact_value)} "
            f"{self.effective.value.unit.value} ({self.layer.value}, set by "
            f"{self.effective.set_by}); overrides {displaced}"
        )


@dataclass(frozen=True, slots=True)
class OverrideReport:
    """What the reviewer changed, and what they still have to supply.

    `outstanding` is not a lesser kind of `overrides`. One says a standard was deliberately set aside
    for this job; the other says a number the check cannot run without is missing, and the run will
    abstain until somebody types it. A reviewer needs to act on the second before the run and only
    verify the first.
    """

    overrides: tuple[Override, ...]
    outstanding: tuple[str, ...]

    @property
    def company_standards_displaced(self) -> tuple[Override, ...]:
        """Only the GLOBAL-versus-PROJECT differences — the literal ask in Q10."""
        return tuple(o for o in self.overrides if o.displaces_a_company_standard)

    def lines(self) -> tuple[str, ...]:
        """The report as text, for a reviewer to read before approving a run.

        Both sections always appear, including when empty. A report that omitted its empty half
        would make "nothing was overridden" and "overrides were not checked" look identical, and the
        reviewer is being asked to confirm the first.
        """
        rendered: list[str] = []

        if self.overrides:
            rendered.append(f"{len(self.overrides)} parameter(s) overridden for this run:")
            rendered.extend(f"  {o.explain()}" for o in self.overrides)
        else:
            rendered.append("No parameters overridden: every check uses the company standard.")

        if self.outstanding:
            rendered.append(
                f"{len(self.outstanding)} required parameter(s) not supplied — these checks "
                "cannot decide until they are:"
            )
            rendered.extend(f"  {name}" for name in self.outstanding)
        else:
            rendered.append("No required parameter is missing.")

        return tuple(rendered)


def override_report(rules: Iterable[Rule], *sets: ParameterSet) -> OverrideReport:
    """Summarise the effective parameters for a run against the layers that set them.

    `rules` is what makes `outstanding` meaningful: only a rule can say a parameter is required, so
    a report built from the parameter sets alone could list what was overridden but never what was
    missing — and missing is the half that stops a run.

    A parameter a rule declares with its own default is not outstanding when no layer sets it: the
    default is a real answer the author wrote down. Outstanding means *declared with no default and
    supplied by nobody*, which is exactly the mandatory blank form field Raj described.
    """
    resolved = resolve_all(*sets)

    overrides = tuple(
        Override(
            name=name,
            effective=parameter.value,
            layer=parameter.layer,
            displaced=parameter.shadowed,
        )
        for name, parameter in sorted(resolved.items())
        if parameter.shadowed
    )

    return OverrideReport(
        overrides=overrides,
        outstanding=_outstanding(rules, resolved),
    )


def _outstanding(rules: Iterable[Rule], resolved: dict[str, ResolvedParameter]) -> tuple[str, ...]:
    """Parameters a rule requires, declares no default for, and no layer supplied.

    Deduplicated and sorted because two rules sharing a parameter — `sink_cutout_clearance` is
    shared by the two cutout checks by design — must produce one line in the reviewer's form, not
    one per rule that happens to read it.
    """
    missing: set[str] = set()
    for rule in rules:
        for name, declared in rule.parameters.items():
            if declared.default is None and name not in resolved:
                missing.add(name)
    return tuple(sorted(missing))


def effective_values(*sets: ParameterSet) -> dict[str, ResolvedParameter]:
    """The values a run will actually use, whatever set them.

    A thin pass-through to `resolve_all`, exported so a caller assembling a report and a caller
    running the engine ask the same function the same question. Two paths to "the effective value"
    is how a report starts describing a run that did not happen.
    """
    return resolve_all(*sets)


def as_text(report: OverrideReport) -> str:
    """The report as one block, for a log line, an email, or a reviewer's screen."""
    return "\n".join(report.lines())


__all__: Sequence[str] = (
    "Override",
    "OverrideReport",
    "as_text",
    "effective_values",
    "override_report",
)
