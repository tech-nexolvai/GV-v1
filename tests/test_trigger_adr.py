"""A crossed upgrade trigger drafts an ADR, and only the admin ratifies it (#269, F6.3).

The failure being guarded against is not adopting the wrong technology. It is re-arguing the same
question every few months with nobody able to say what was measured last time. So the tests that
matter are about the record: it carries the numbers, it is never accepted by automation, a rejection
is written down, and a citation resolves to exactly one file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.trigger_adr import (
    ADR_DIR,
    TEMPLATE,
    AdrNumberingError,
    Outcome,
    TriggerEvidence,
    draft_trigger_adr,
    main,
    next_adr_number,
    slugify,
    write_outcome,
)

EVIDENCE = TriggerEvidence(
    trigger="workflow recovery interventions",
    upgrade="Temporal",
    threshold="more than 2 manual recoveries per month",
    measured="7 manual recoveries",
    window="the 30 days to 2026-08-17",
    source="app/telemetry/triggers.py (#267)",
)


def _adr(directory: Path, number: int, slug: str = "something") -> Path:
    path = directory / f"{number:04d}-{slug}.md"
    path.write_text("**Status:** Accepted\n\n## Decision\n\nx\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Numbering — the D11 duplicate happened once already
# ---------------------------------------------------------------------------


def test_the_next_number_follows_the_highest_not_the_count(tmp_path: Path) -> None:
    """A gap must not be refilled. A superseded ADR still holds its number in every document that
    cites it, so re-issuing it makes one citation point at two records."""
    _adr(tmp_path, 1)
    _adr(tmp_path, 2)
    _adr(tmp_path, 7)
    assert next_adr_number(tmp_path) == 8


def test_an_empty_directory_starts_at_one(tmp_path: Path) -> None:
    assert next_adr_number(tmp_path) == 1


def test_unnumbered_files_are_not_counted(tmp_path: Path) -> None:
    """`TEMPLATE.md` and `README.md` live in the same directory and are not records."""
    (tmp_path / "TEMPLATE.md").write_text("# ADR-NNNN\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# index\n", encoding="utf-8")
    _adr(tmp_path, 3)
    assert next_adr_number(tmp_path) == 4


def test_two_adrs_claiming_one_number_is_refused(tmp_path: Path) -> None:
    """Rather than quietly allocating past it. A citation has to resolve to exactly one record, and
    this has gone wrong once already."""
    _adr(tmp_path, 4, "first")
    _adr(tmp_path, 4, "second")
    with pytest.raises(AdrNumberingError, match="same number"):
        next_adr_number(tmp_path)


def test_the_real_adr_directory_has_no_collisions() -> None:
    """Run against the repository, not a fixture: this is the state the guard exists to protect."""
    assert next_adr_number(ADR_DIR) > 0


# ---------------------------------------------------------------------------
# The draft is a draft
# ---------------------------------------------------------------------------


def test_the_draft_is_proposed_and_never_accepted(tmp_path: Path) -> None:
    """`scripts/ratify.py` unblocks work on an ADR that reads Accepted. A drafter able to write that
    would satisfy the gate with the same automation that raised the question."""
    path = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "**Status:** Proposed" in text
    assert "Status:** Accepted" not in text


def test_the_draft_carries_the_measurement_not_just_the_breach(tmp_path: Path) -> None:
    """The whole reason this is an ADR rather than a ticket. In six months somebody has to see what
    was true when the question was asked, without the dashboard still existing."""
    text = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path).read_text("utf-8")
    for expected in (
        EVIDENCE.trigger,
        EVIDENCE.threshold,
        EVIDENCE.measured,
        EVIDENCE.window,
        EVIDENCE.source,
    ):
        assert expected in text


def test_the_draft_names_the_upgrade_being_decided(tmp_path: Path) -> None:
    path = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path)
    assert "temporal" in path.name
    assert "Adopt Temporal?" in path.read_text(encoding="utf-8")


def test_keeping_the_deferral_is_one_of_the_options(tmp_path: Path) -> None:
    """A drafted ADR that only offers "adopt" is a recommendation wearing a decision's clothes. The
    third option matters too: moving a limit because it was reached turns a measured trigger into
    decoration."""
    text = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path).read_text("utf-8")
    assert "Do not adopt" in text
    assert "Change the threshold" in text


def test_the_decision_section_says_nothing_was_decided(tmp_path: Path) -> None:
    """A pre-filled Decision would be an automated system stating an architectural position."""
    text = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path).read_text("utf-8")
    assert "Nothing here has been decided" in text


def test_the_date_is_supplied_not_read_from_the_clock(tmp_path: Path) -> None:
    """A drafter that stamps "now" produces a different file every run — untestable, and easy to
    regenerate on top of itself."""
    text = draft_trigger_adr(EVIDENCE, date="2026-01-02", adr_dir=tmp_path).read_text("utf-8")
    assert "**Date:** 2026-01-02" in text


def test_the_number_allocated_continues_the_sequence(tmp_path: Path) -> None:
    _adr(tmp_path, 17)
    assert draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path).name.startswith("0018-")


def test_a_second_draft_does_not_overwrite_the_first(tmp_path: Path) -> None:
    """Two crossings of the same trigger are two arguments, and the first one's record survives."""
    first = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path)
    second = draft_trigger_adr(EVIDENCE, date="2026-09-01", adr_dir=tmp_path)
    assert first != second
    assert first.exists() and second.exists()


# ---------------------------------------------------------------------------
# Rejection is an outcome, recorded
# ---------------------------------------------------------------------------


def test_a_rejection_is_written_into_the_record(tmp_path: Path) -> None:
    """The acceptance criterion that stops the same argument running again in six months."""
    path = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path)
    write_outcome(path, Outcome.REJECTED, reason="Recoveries traced to one migration, since fixed.")
    text = path.read_text(encoding="utf-8")
    assert "**Status:** Rejected" in text
    assert "one migration, since fixed" in text


def test_a_rejection_keeps_the_measurement_that_prompted_it(tmp_path: Path) -> None:
    """Otherwise the record says a thing was declined without saying what was true at the time, which
    is the argument-with-no-evidence this replaces."""
    path = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path)
    write_outcome(path, Outcome.REJECTED, reason="Not yet worth the operational cost.")
    assert EVIDENCE.measured in path.read_text(encoding="utf-8")


def test_this_module_cannot_write_accepted(tmp_path: Path) -> None:
    """`Outcome` has no ACCEPTED member, so there is no value to pass. Asserted rather than left to
    inspection, because "we just never call it with Accepted" is a convention and this is a gate."""
    assert not hasattr(Outcome, "ACCEPTED")
    assert {member.value for member in Outcome} == {"Proposed", "Rejected"}


def test_amending_something_that_is_not_an_adr_is_refused(tmp_path: Path) -> None:
    stray = tmp_path / "notes.md"
    stray.write_text("just some notes\n", encoding="utf-8")
    with pytest.raises(AdrNumberingError, match="no Status line"):
        write_outcome(stray, Outcome.REJECTED, reason="x")


def test_an_adr_with_no_decision_section_is_refused(tmp_path: Path) -> None:
    """The reason has to land somewhere a reader will look."""
    odd = tmp_path / "0001-odd.md"
    odd.write_text("**Status:** Proposed\n\n## Context\n\nx\n", encoding="utf-8")
    with pytest.raises(AdrNumberingError, match="Decision"):
        write_outcome(odd, Outcome.REJECTED, reason="x")


# ---------------------------------------------------------------------------
# Slugs and the command line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Temporal", "temporal"), ("managed Postgres (RDS)", "managed-postgres-rds")],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_a_name_with_nothing_usable_is_refused() -> None:
    with pytest.raises(ValueError, match="nothing usable"):
        slugify("***")


def test_the_command_line_requires_every_field() -> None:
    """No defaults. A drafted ADR with an invented threshold is worse than no ADR, because it reads
    like a measurement."""
    with pytest.raises(SystemExit):
        main(["--trigger", "x"])


def test_the_draft_carries_every_section_the_template_requires(tmp_path: Path) -> None:
    """The draft is written inline rather than filled from `TEMPLATE.md`, because the template's
    placeholder prose does not fit a trigger ADR. That leaves the two able to drift: a section added
    to the template would be quietly missing from every ADR this drafts. This ties them together
    without inheriting the placeholders.
    """
    required = [
        line
        for line in (ADR_DIR / TEMPLATE).read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    drafted = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path).read_text("utf-8")
    missing = [heading for heading in required if heading not in drafted]
    assert not missing, f"the template requires sections this draft omits: {missing}"


def test_recording_an_outcome_leaves_one_well_formed_status_line(tmp_path: Path) -> None:
    """Found by running the thing rather than asserting about it. The substitution used to stop at
    `<`, so the template's comment survived and a second was appended beside it — and every test
    above still passed, because they asked whether the status appeared, not whether the line was
    intact."""
    path = draft_trigger_adr(EVIDENCE, date="2026-08-17", adr_dir=tmp_path)
    write_outcome(path, Outcome.REJECTED, reason="x")

    # Only the real status line: the blockquote above it also says "Status: Accepted", when
    # explaining that the admin alone may write it.
    status_lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("**Status:")
    ]
    assert len(status_lines) == 1
    assert status_lines[0].count("<!--") == 1
    assert "Proposed" not in status_lines[0].split("<!--")[0]
