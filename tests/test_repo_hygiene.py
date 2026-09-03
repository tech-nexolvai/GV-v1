"""Proprietary client material must never enter version control.

`data/` holds Graniti Vicentia's drawings and checklists, and `eval/gold_set/cases/` will
hold the reviewed answer key. Both are commercially sensitive and contractually not ours
to publish. `.gitignore` covers them, but an ignore rule is a convention — this turns it
into a build failure.

The check is deliberately about what is *tracked*, not what is ignored: a file that has
been force-added with `git add -f` is tracked despite matching an ignore rule, and that
is exactly the mistake worth catching.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths that must never contain a tracked file.
FORBIDDEN_PREFIXES = (
    "data/",
    "eval/gold_set/cases/",
)

# Filenames/extensions that must never be tracked anywhere in the tree.
FORBIDDEN_PATTERNS = (
    ".env",
    ".pem",
    ".key",
    ".dwg",
    ".xlsx",
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_no_client_data_is_tracked() -> None:
    offenders = [f for f in _tracked_files() if any(f.startswith(p) for p in FORBIDDEN_PREFIXES)]
    assert not offenders, (
        "Proprietary client material is tracked by git:\n  "
        + "\n  ".join(offenders)
        + "\n\nRemove it with `git rm --cached <file>`. Client drawings, checklists and "
        "gold-set cases must never be committed."
    )


def test_no_secrets_or_source_documents_are_tracked() -> None:
    offenders = []
    for f in _tracked_files():
        name = Path(f).name
        if name == ".env" or any(
            f.endswith(ext) for ext in FORBIDDEN_PATTERNS if ext.startswith(".") and ext != ".env"
        ):
            offenders.append(f)
    assert not offenders, (
        "Secrets or proprietary source documents are tracked:\n  "
        + "\n  ".join(offenders)
        + "\n\n`.env.example` is fine; a real `.env` is not."
    )


def test_env_example_exists_but_env_does_not() -> None:
    """A template must exist so nobody invents their own variable names, and the real
    file must not be committed alongside it."""
    assert (REPO_ROOT / ".env.example").is_file(), ".env.example is missing"
    assert ".env" not in _tracked_files(), "a real .env is tracked — remove it immediately"


def test_gitignore_covers_the_sensitive_paths() -> None:
    """The ignore rules are the first line of defence; the tests above are the second.
    Both matter — the rules stop the mistake, the tests catch it if the rules are edited."""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for needed in ("data/", "eval/gold_set/cases/", ".env", ".claude/settings.local.json"):
        assert needed in text, f".gitignore no longer covers {needed!r}"


def _is_ignored(relative_path: str) -> bool:
    """Ask git, rather than reading the file and guessing how the pattern would match."""
    return (
        subprocess.run(
            ["git", "check-ignore", "-q", relative_path],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    "path",
    [
        "data/drawings/vanity.pdf",
        "data/checklists/ct.xlsx",
        "eval/gold_set/cases/case_001.json",
    ],
)
def test_client_material_is_actually_ignored(path: str) -> None:
    """The rule that matters, asked of git instead of inferred from the text.

    The check above is a substring search, and it passed happily throughout the bug this was added
    for: `data/` appears inside `/data/`, inside a comment, and inside prose. It cannot tell whether
    the pattern still *does* anything.
    """
    assert _is_ignored(path), f"{path} is no longer ignored — client material could be committed"


@pytest.mark.parametrize(
    "path",
    ["frontend/main/src/data/mock.ts", "frontend/main/src/data/fixtures.ts"],
)
def test_the_frontends_own_data_directory_is_not_swept_up(path: str) -> None:
    """**The bug this pair exists for.**

    `data/` was unanchored, so it matched a directory of that name anywhere in the tree. It silently
    excluded `frontend/main/src/data/` — the frontend's mock data and its type definitions — and
    eight modules on `frontend-init-dev` import a file that was therefore never committed. Nothing
    failed loudly; the directory simply was not there and the app could not build.

    Anchoring to `/data/` fixes it, and this asserts the fix in the direction the substring check
    cannot see. An edit that drops the leading slash to "tidy up" fails here.
    """
    assert not _is_ignored(path), (
        f"{path} is ignored again — the client-data rule has been un-anchored and is swallowing "
        "the frontend's source"
    )


#: CodeRabbit's published config schema, vendored so the check is offline and deterministic. A test that
#: fetched it would fail whenever the network did, for reasons having nothing to do with this repository —
#: and a review-config check that goes red for unrelated reasons is one people learn to ignore.
#:
#: Refresh it from the URL below; `test_the_config_still_satisfies_the_published_schema` (opt-in, needs
#: the network) is how you find out that it needs refreshing.
CODERABBIT_SCHEMA_URL = "https://storage.googleapis.com/coderabbit_public_assets/schema.v2.json"
CODERABBIT_SCHEMA = REPO_ROOT / "tests" / "fixtures" / "coderabbit_schema_v2.json"


def _keys_the_schema_does_not_define(config: object, schema: object, path: str = "") -> list[str]:
    """Keys that sit under a plain `properties` node the schema does not define.

    **Not "at any depth" — I wrote that first and it overstates what this does.** This is a stricter
    project policy layered on top of the schema, and it reaches exactly one kind of node: a mapping whose
    schema declares `properties` and does not set `additionalProperties`. It walks past nothing else. In
    particular it does not resolve `anyOf`, `oneOf` or `$ref`, so a key nested inside one of those is not
    checked, and it stays silent wherever extra keys are deliberately allowed.

    Conservative on purpose. A false positive would fail CI on a valid config, and a check people learn to
    disable is worth less than one that misses a case — the schema itself still rejects anything unknown
    at the root. What this adds is the nested typo the schema waves through: `reviews.profil`,
    `reviews.auto_review.enabld`, `reviews.tools.ruf`.
    """
    if not isinstance(config, dict) or not isinstance(schema, dict):
        return []

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    if schema.get("additionalProperties") not in (False, None):
        return []

    unknown: list[str] = []
    for key, value in config.items():
        where = f"{path}.{key}" if path else key
        if key not in properties:
            unknown.append(where)
            continue
        child = properties[key]
        unknown.extend(_keys_the_schema_does_not_define(value, child, where))
        if isinstance(value, list) and isinstance(child, dict):
            items = child.get("items")
            for index, element in enumerate(value):
                unknown.extend(
                    _keys_the_schema_does_not_define(element, items, f"{where}[{index}]")
                )
    return unknown


def test_the_unknown_key_check_finds_what_it_claims_and_nothing_more() -> None:
    """The helper's documented limits, asserted rather than described.

    Its docstring makes five claims about where it looks and where it does not. I had already written one
    wrong version of that docstring ("at any depth"), so the claims are pinned here — a future edit that
    widens or narrows the walk has to change a test that says what the behaviour is.
    """
    # A plain `properties` node with no `additionalProperties`: the case this exists for.
    assert _keys_the_schema_does_not_define(
        {"a": {"typo": 1}}, {"properties": {"a": {"properties": {"real": {}}}}}
    ) == ["a.typo"]

    # Inside a list, which is how `path_instructions` is shaped.
    assert _keys_the_schema_does_not_define(
        {"a": [{"typo": 1}]}, {"properties": {"a": {"items": {"properties": {"real": {}}}}}}
    ) == ["a[0].typo"]

    # Silent where extra keys are deliberately allowed.
    assert not _keys_the_schema_does_not_define(
        {"a": {"anything": 1}},
        {"properties": {"a": {"properties": {}, "additionalProperties": True}}},
    )

    # Silent behind `anyOf` and `$ref`, which it does not resolve. A real limitation, stated in the
    # docstring and asserted here so it stays a known limitation rather than becoming a surprise.
    assert not _keys_the_schema_does_not_define(
        {"a": {"typo": 1}}, {"properties": {"a": {"anyOf": [{"properties": {"real": {}}}]}}}
    )
    assert not _keys_the_schema_does_not_define(
        {"a": {"typo": 1}},
        {"properties": {"a": {"$ref": "#/$defs/x"}}, "$defs": {"x": {"properties": {"real": {}}}}},
    )


def test_the_coderabbit_config_is_actually_used() -> None:
    """The reviewer config must parse and fit its schema, or it is silently thrown away.

    This is a regression test for a real and quiet failure. `.coderabbit.yaml` was added on 2026-08-13
    (`4c37420`) with a 431-character `tone_instructions` against a 250-character cap, so CodeRabbit
    rejected **the whole file** and reviewed on defaults — profile CHILL, and none of the
    `path_instructions` for `verdict/`, `units/`, `rules/` or `evidence/` in force. Because it was over
    the cap from the day it was written, this config had never once applied to a review before it was
    fixed on 2026-08-21. Nothing failed: the only sign was one collapsed warning inside a PR comment.

    That is the worst shape a problem can have here — a safety control that is configured, believed to be
    working, and not running. So it is asserted, cheaply, with no network.
    """
    import yaml

    config_path = REPO_ROOT / ".coderabbit.yaml"
    assert config_path.is_file(), ".coderabbit.yaml is missing, so the reviewer runs on defaults"

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict), ".coderabbit.yaml did not parse into a mapping"

    # **Validate the whole contract, not the one field that happened to break.** The original defect was
    # a length; the next one will not be. A wrong type or a bad enum fails here instead of silently
    # downgrading every review to defaults.
    schema = json.loads(CODERABBIT_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(config),
        key=lambda error: list(error.path),
    )
    assert not errors, (
        ".coderabbit.yaml violates CodeRabbit's schema, so the whole file is discarded and reviews run "
        "on defaults:\n  "
        + "\n  ".join(
            f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors
        )
    )

    # **A typo'd key is the failure the schema cannot catch, so it is caught here.**
    #
    # Only the root sets `additionalProperties: false`, so `languag:` is rejected — but `reviews` and
    # `reviews.auto_review` do not, which means `reviews.profil:` and `reviews.auto_review.enabld:`
    # validate cleanly and are then ignored. Both verified. A setting that is present, believed and inert
    # is this whole file's original bug in miniature, so the recursive check below closes it everywhere
    # rather than asserting the two fields I happened to think of.
    unknown = sorted(_keys_the_schema_does_not_define(config, schema))
    assert not unknown, (
        "these keys are not in CodeRabbit's schema, so they are silently ignored — most likely a "
        f"misspelling of a real setting: {unknown}"
    )

    # The limit that actually bit, named explicitly with a message that says what happens — a bare schema
    # error does not tell the reader that CodeRabbit discards the file rather than truncating the field.
    # Read from the schema rather than hardcoded, so it cannot drift from the real constraint.
    tone_limit = schema["properties"]["tone_instructions"]["maxLength"]
    tone = config.get("tone_instructions", "")
    assert isinstance(tone, str) and len(tone) <= tone_limit, (
        f"tone_instructions is {len(tone)} characters, over the {tone_limit} the schema allows. "
        "CodeRabbit does not truncate it — it discards the entire config and reviews on defaults. "
        "Move the detail into a path_instructions entry, which allows "
        f"{schema['properties']['reviews']['properties']['path_instructions']['items']['properties']['instructions']['maxLength']}."
    )

    # The path rules are the substance of the file; losing them is what the cap above actually cost. The
    # schema cannot check this part, because an empty instruction block is valid YAML and valid schema —
    # it just reviews nothing, while reading as coverage.
    #
    # Reached through `get` with stated assertions rather than by indexing. The schema has no `required`
    # at its root, so a config with no `reviews:` section at all validates cleanly and then died here on
    # `KeyError: 'reviews'` — an incidental exception that tells the reader nothing about what is wrong.
    reviews = config.get("reviews")
    assert isinstance(reviews, dict), (
        "the config has no `reviews:` section, so none of the per-zone instructions exist. The schema "
        "permits this, which is why it is asserted here."
    )
    raw_entries = reviews.get("path_instructions")
    assert (
        isinstance(raw_entries, list) and raw_entries
    ), "`reviews.path_instructions` is missing or empty, so every zone reviews on defaults"
    for index, entry in enumerate(raw_entries):
        assert isinstance(entry, dict) and isinstance(
            entry.get("path"), str
        ), f"path_instructions[{index}] is not an entry with a path: {entry!r}"

    # Duplicates are rejected before the mapping is built, because building it hides them: a dict
    # comprehension keeps the last entry for a repeated path, so an empty `**` followed by a populated
    # one satisfies every assertion below while the file itself stays ambiguous. Which of two entries
    # CodeRabbit honours is not something this repository should be guessing about.
    paths = [entry["path"] for entry in raw_entries]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    assert (
        not duplicates
    ), f"path_instructions repeats {duplicates}, so which block applies is ambiguous"

    entries = {entry["path"]: entry.get("instructions") for entry in raw_entries}
    for zone in ("**", "verdict/**", "units/**", "rules/**", "evidence/**"):
        assert zone in entries, f"{zone} has no review instructions"
        body = entries[zone]
        assert isinstance(body, str) and body.strip(), f"{zone} has an empty instructions block"

    # The profile, asserted by name because the schema cannot help here: `reviews` allows unknown keys,
    # so `profil: assertive` would validate and then be ignored, leaving every review on the `chill`
    # default. That is this PR's own bug in miniature — a setting that is present, believed, and inert.
    allowed = schema["properties"]["reviews"]["properties"]["profile"]["enum"]
    profile = reviews.get("profile")
    assert profile in allowed, (
        f"reviews.profile is {profile!r}, not one of {allowed}. A misspelled key here is not an error, "
        "it is a silent fall back to the default profile."
    )

    # **No green tick for a review that did not happen.** `reviews.commit_status` turns on a legacy
    # commit-status mirror, and GitHub folds that status into the same rollup as the Actions checks. On
    # #484 it reported `success` with the description "Review skipped: manual review required for this
    # OSS repository" — a pass for a declined review, sitting in a list of five green ticks.
    #
    # Asserted as `is False` rather than falsy, because the default is `true` and the schema puts no
    # `required` at this level: an absent key reads as "nobody chose" and behaves as "mirror on". This
    # has to be a decision that stays made. Re-enabling it should mean arguing with this test, since the
    # cost is not a noisy check but a silent claim of review coverage that nobody has any reason to
    # doubt.
    assert reviews.get("commit_status") is False, (
        "reviews.commit_status is not explicitly false, so CodeRabbit mirrors its review state as a "
        "legacy commit status. On this account every review is skipped, and the mirror reports that "
        "skip as a green `CodeRabbit` tick in the checks list — a control that looks like it ran."
    )

    # **No claim of automatic review, because the plan declines it.** This repository is public, which
    # puts CodeRabbit on the free open-source plan, where a review is requested rather than automatic —
    # its status on #484 read "manual review required for this OSS repository". `enabled: true` was
    # therefore a setting that had never once caused a review, and the cost was #487: five findings
    # arrived on a requested review and were merged over, because nothing in the process expected a
    # review to appear at all.
    #
    # `is False` for the same reason as `commit_status` above — the default is `true` and an absent key
    # reads as "nobody chose". Flipping it back is only meaningful alongside a plan that honours it.
    auto_review = reviews.get("auto_review")
    assert isinstance(auto_review, dict), (
        "the config has no `reviews.auto_review:` section, so the setting defaults to on and the file "
        "implies an automatic review that this plan does not perform"
    )
    assert auto_review.get("enabled") is False, (
        "reviews.auto_review.enabled is not explicitly false. On this plan CodeRabbit does not review "
        "automatically, so `true` records a belief rather than a behaviour — and a PR nobody requests "
        "a review for is then indistinguishable from one that passed review. The request is sent by "
        "the `request review` job in .github/workflows/ci.yml."
    )

    # `**` carries the project-wide safety framing that used to live in `tone_instructions`. Asserted by
    # concept rather than by exact prose, so the wording can be improved but not quietly dropped.
    assert "false-PASS" in entries["**"], (
        "the project-wide instruction no longer mentions the false-PASS rate, which is the one thing "
        "every review of this repository is supposed to be looking for"
    )


#: Set this to check the vendored schema against the live one. Off by default, and via an env var rather
#: than a pytest marker so it cannot be selected by accident.
SCHEMA_DRIFT_ENV = "GV_CHECK_CODERABBIT_SCHEMA"


@pytest.mark.skipif(
    not os.environ.get(SCHEMA_DRIFT_ENV),
    reason=f"set {SCHEMA_DRIFT_ENV}=1 to check the vendored schema against the published one",
)
def test_the_config_still_satisfies_the_published_schema() -> None:
    """Is the config still valid against the *live* schema? Opt-in, because it needs the network.

    **This deliberately does not compare the two schemas for equality.** I wrote that version first and it
    failed immediately: CodeRabbit had reworded a `description` between two fetches minutes apart. An
    equality check would go red every time they touch any prose, which tells us nothing — a reworded
    description cannot invalidate our config. A check that cries wolf about upstream copywriting is one
    people learn to ignore, and then it is worse than absent.

    So this asks the question that matters: does `.coderabbit.yaml` still satisfy what CodeRabbit publishes
    *today*, and have the two limits we actually depend on moved?
    """
    import urllib.request

    import yaml

    with urllib.request.urlopen(CODERABBIT_SCHEMA_URL, timeout=30) as response:
        published = json.loads(response.read())

    config = yaml.safe_load((REPO_ROOT / ".coderabbit.yaml").read_text(encoding="utf-8"))
    errors = sorted(
        jsonschema.Draft202012Validator(published).iter_errors(config),
        key=lambda error: list(error.path),
    )
    assert not errors, (
        "the published schema has changed and .coderabbit.yaml no longer satisfies it, so CodeRabbit is "
        "reviewing on defaults right now:\n  "
        + "\n  ".join(
            f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors
        )
    )

    vendored = json.loads(CODERABBIT_SCHEMA.read_text(encoding="utf-8"))

    def limits(schema: dict[str, object]) -> dict[str, object]:
        props = schema["properties"]
        assert isinstance(props, dict)
        instructions = props["reviews"]["properties"]["path_instructions"]["items"]["properties"]
        return {
            "tone_instructions": props["tone_instructions"]["maxLength"],
            "path_instructions": instructions["instructions"]["maxLength"],
        }

    assert limits(vendored) == limits(published), (
        f"the limits moved upstream: vendored {limits(vendored)} vs published {limits(published)}. "
        f"Refresh {CODERABBIT_SCHEMA.relative_to(REPO_ROOT)} from {CODERABBIT_SCHEMA_URL}."
    )
