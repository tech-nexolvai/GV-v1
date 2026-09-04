"""`.env.example` and `Settings` are held to each other (#417, F3.3).

The template had never worked. Its first line said *"Copy to .env and fill"* and following it produced an
application that would not start: every name lacked the `GV_` prefix, `Settings` never read a `.env` file
at all, and the port was 5432 where the local stack uses 5433. **Nothing noticed**, because a template is
prose until something reads it — which is what this file is for.

The tests are deliberately mechanical. Each one is a claim the template makes that could quietly stop
being true: a renamed setting, a required field nobody documented, a port that drifted from
`docker-compose.yml`, or an unknown key that now stops the application booting rather than being ignored.

Source: `app/config.py` — `Settings` is the contract · Verification: this file
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

TEMPLATE = Path(".env.example")
COMPOSE = Path("docker-compose.yml")

#: The prefix every variable in the template must carry — read from `Settings`, not written out here, so
#: changing the prefix cannot leave this test asserting the old one.
PREFIX = Settings.model_config["env_prefix"]


def _active_keys() -> dict[str, str]:
    """The variables the template actually sets, ignoring comments.

    Active lines only, and that distinction is the whole point: a commented `#AWS_REGION=...` documents
    something, while an uncommented one would stop the application starting. A test that searched the
    text for a name could not tell those apart — which is a mistake I made in `tests/test_local_stack.py`
    and had to correct there.
    """
    found: dict[str, str] = {}
    for line in TEMPLATE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        found[key.strip()] = value.strip()
    return found


# ---------------------------------------------------------------------------
# Every name in the template is a real setting
# ---------------------------------------------------------------------------


def test_the_template_sets_something_at_all() -> None:
    """Guard for the guards: an empty template would satisfy every test below vacuously."""
    assert _active_keys(), "the template sets no variables, so the tests below check nothing"


def test_every_variable_resolves_to_a_real_setting() -> None:
    """The first acceptance criterion, and the defect that made the file useless.

    The template said `DATABASE_URL`; the application reads `GV_DATABASE_URL`. Setting the documented
    name failed with `database_url Field required` — the template's instruction was actively wrong.
    """
    fields = set(Settings.model_fields)
    for key in _active_keys():
        assert key.startswith(PREFIX), f"{key} lacks the {PREFIX} prefix the application reads"
        field = key[len(PREFIX) :].lower()
        assert field in fields, (
            f"{key} maps to no field on Settings. Either it was renamed, or the template documents a "
            f"setting that does not exist. Real fields: {sorted(fields)}"
        )


def test_every_required_setting_appears_in_the_template() -> None:
    """A required setting a developer cannot discover is exactly the defect this file exists to prevent.

    Asserted from `Settings` rather than from a list here, so adding a required field without documenting
    it fails the build instead of failing a newcomer's afternoon.
    """
    required = {
        f"{PREFIX}{name.upper()}"
        for name, field in Settings.model_fields.items()
        if field.is_required()
    }
    missing = required - set(_active_keys())
    assert not missing, f"required settings absent from .env.example: {sorted(missing)}"


def test_the_engine_token_is_present_and_says_where_it_comes_from() -> None:
    """`Settings.hatchet_token` defaults to empty, so nothing *requires* it — and a worker refuses
    without it.

    That combination is the trap: the application starts, the worker exits 78, and the template gave no
    hint. So the criterion asks for the key *and* for where the value comes from.
    """
    assert f"{PREFIX}HATCHET_TOKEN" in _active_keys(), "the token is not in the template"
    text = TEMPLATE.read_text()
    assert "make token" in text, "the template does not say how to get a token (#416)"


# ---------------------------------------------------------------------------
# The port matches the stack it is meant to point at
# ---------------------------------------------------------------------------


def test_the_database_port_matches_docker_compose() -> None:
    """5433, asserted against the compose file rather than written twice.

    The template said 5432. On a machine already running PostgreSQL that connects successfully to the
    *wrong database* — which is worse than a refused connection, because it writes.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose file")
    compose = yaml.safe_load(COMPOSE.read_text())
    published = [
        str(p).split(":")[0] for p in compose["services"]["db"]["ports"]
    ]  # host side of "host:container"

    url = _active_keys()[f"{PREFIX}DATABASE_URL"]
    port = re.search(r":(\d+)/", url)
    assert port, f"no port in {url!r}"
    assert (
        port.group(1) in published
    ), f".env.example points at port {port.group(1)} but docker-compose publishes {published}"


# ---------------------------------------------------------------------------
# The file is now actually read, and cannot override the environment
# ---------------------------------------------------------------------------


def test_settings_reads_a_dotenv_file() -> None:
    """The template's own first line — "Copy to .env and fill" — is only true because of this.

    It was `env_file=None`, so copying the template had no effect whatsoever. A file nobody reads is a
    worse form of documentation than no file, because it looks like configuration.
    """
    assert Settings.model_config["env_file"] == ".env"


def test_a_real_environment_variable_beats_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence, asserted rather than trusted.

    CI and containers pass variables directly. A developer's stale `.env` overriding those would be a
    process configured differently from how its operator believes — and the failure would look like the
    application ignoring its own deployment.
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{PREFIX}DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/from_file\n")
    monkeypatch.setenv(
        f"{PREFIX}DATABASE_URL", "postgresql+psycopg://gv:gv@localhost:5433/from_env"
    )

    resolved = Settings(_env_file=str(dotenv))  # type: ignore[call-arg]

    assert resolved.database_url.endswith("/from_env"), (
        "the .env file overrode a real environment variable, so a stale local file would silently "
        "reconfigure a deployment"
    )


def test_the_dotenv_file_alone_is_enough_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: with nothing in the environment, the file supplies the value.

    Without this, the precedence test above would pass on a file that was never read at all.
    """
    monkeypatch.delenv(f"{PREFIX}DATABASE_URL", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{PREFIX}DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/from_file\n")

    resolved = Settings(_env_file=str(dotenv))  # type: ignore[call-arg]
    assert resolved.database_url.endswith("/from_file")


def test_an_unknown_key_in_the_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`extra="forbid"` applies to the file too, which is why the template's spare values are comments.

    This is the interaction that makes the template's structure necessary rather than tidy: the old file
    carried `AWS_REGION`, `S3_BUCKET`, `BEDROCK_MODEL_ID`, `OTEL_EXPORTER_OTLP_ENDPOINT` and
    `HATCHET_DATABASE_URL`, none of which are settings. Copying it to `.env` with `env_file` enabled
    would stop the application booting — so enabling the file without restructuring the template would
    have replaced a silent no-op with a hard failure.
    """
    monkeypatch.delenv(f"{PREFIX}DATABASE_URL", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"{PREFIX}DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv\nAWS_REGION=us-east-1\n"
    )

    with pytest.raises(ValidationError, match="aws_region"):
        Settings(_env_file=str(dotenv))  # type: ignore[call-arg]


def test_the_template_itself_loads_as_a_dotenv_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The test the whole story is about: does following the instruction actually work?**

    Every other test here checks a property. This one does what a developer does — points `Settings` at
    the template and sees whether an application would start. The old file failed this on three separate
    counts, and no test existed to say so.

    The environment is cleared first, or a `GV_` variable already exported would supply the value the
    file is supposed to prove it can supply.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(f"{PREFIX}{name.upper()}", raising=False)

    resolved = Settings(_env_file=str(TEMPLATE))  # type: ignore[call-arg]

    assert resolved.database_url, "the template does not supply a database URL"
    assert "5433" in resolved.database_url


# ---------------------------------------------------------------------------
# Still no secrets
# ---------------------------------------------------------------------------


def test_the_template_carries_no_credential() -> None:
    """`AGENTS.md` §2.8. The local database password is `gv/gv` by design — it is in
    `docker-compose.yml` in plain sight and grants nothing — but a token or key must never appear.

    The token line is deliberately empty: a template that ships a working credential is a template that
    ships a working credential into somebody's git history.
    """
    keys = _active_keys()
    assert keys[f"{PREFIX}HATCHET_TOKEN"] == "", "the template ships a token value"

    text = TEMPLATE.read_text()
    for marker in ("BEGIN RSA", "BEGIN PRIVATE", "AKIA", "eyJhbGciOi"):
        assert marker not in text, f"{marker} appears in .env.example"


def test_the_development_identity_variables_are_documented_by_their_real_names() -> None:
    """The two names come from `app/auth/development.py`, not from prose here.

    Without this the template could keep saying `GV_DEV_PRINCIPAL` long after the constant was
    renamed, and following the instructions would give you an API that refuses every request with no
    hint why — which is what happens today when nobody sets them at all.
    """
    from app.auth.development import PRINCIPAL_VARIABLE, PROJECTS_VARIABLE

    text = TEMPLATE.read_text()
    for variable in (PRINCIPAL_VARIABLE, PROJECTS_VARIABLE):
        assert variable in text, (
            f"{variable} is not mentioned in .env.example, so nothing tells a newcomer how to get an "
            "identity the API will accept"
        )


def test_the_development_identity_variables_are_not_live_keys() -> None:
    """**They must stay commented, because setting them here stops the application starting.**

    Neither is a field on `Settings`, and `extra="forbid"` means `Settings()` refuses an unknown key it
    finds in `.env`. Verified rather than reasoned about: with both written in, `Settings()` raises
    `extra_forbidden` for each and the API never boots.

    `test_every_variable_resolves_to_a_real_setting` would also catch this, but it would report "not a
    real setting", which reads as *add a field* — the opposite of what is needed. These belong in the
    environment, not in the file.
    """
    from app.auth.development import PRINCIPAL_VARIABLE, PROJECTS_VARIABLE

    active = _active_keys()
    for variable in (PRINCIPAL_VARIABLE, PROJECTS_VARIABLE):
        assert variable not in active, (
            f"{variable} is an active key in .env.example. It is not a Settings field, so a copied "
            "`.env` containing it makes Settings() raise extra_forbidden and the API cannot start. "
            "Export it for the process instead."
        )
