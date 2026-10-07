"""The reviewer says what product a drawing set is for, through the API (#994).

- `POST /packages` requires `product_type`; a value outside the vocabulary, or one no published rule
  checks, is a 422 and writes nothing.
- `GET` returns it, and a package from before #994 reads back as `null`.
- `GET /product-types` lists only the products the published rulebook has checks for, so the upload
  screen never hard-codes them.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.api.packages import PRODUCT_LABELS
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import Package, Project
from app.models.rules import RuleDefinition, RuleSnapshot
from rules.schema import Rule
from rules.snapshot import publish
from tests.app.postgres_fixture import alembic_config
from vocabulary.semantic_types import ProductType

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()
RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT}))


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        opened.add(Project(id=PROJECT, name="product type"))
        opened.commit()
        yield opened
    finally:
        opened.close()


def _publish(session: Session, *products: ProductType) -> dict[ProductType, int]:
    """Publish the authored rules for these products only, the way `_publish_rulebook` does."""
    published: dict[ProductType, int] = {}
    for path in sorted(RULEBOOK.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if rule.product_type not in products:
            continue
        snapshot = publish(rule)
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)
        session.flush()
        session.add(
            RuleSnapshot(
                rule_definition_id=definition.id,
                snapshot_id=snapshot.snapshot_id,
                version=rule.version,
                canonical_json=snapshot.canonical_json,
                product_type=rule.product_type.value,
                check_type=rule.check_type.value,
                unconfirmed_tolerance_count=0,
            )
        )
        published[rule.product_type] = published.get(rule.product_type, 0) + 1
    session.commit()
    return published


def _client(session: Session) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(
        Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
        )
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


def _packages(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Package)) or 0


def test_every_product_type_has_a_plain_label() -> None:
    assert set(PRODUCT_LABELS) == set(ProductType)
    assert PRODUCT_LABELS[ProductType.COUNTERTOP] == "Countertop"


def test_a_package_is_created_for_a_product_and_reads_it_back(session: Session) -> None:
    _publish(session, ProductType.COUNTERTOP, ProductType.CABINET)
    client = _client(session)

    created = client.post(
        f"/api/v1/projects/{PROJECT}/packages",
        json={"vendor": "A vendor", "product_type": "countertop"},
    )

    assert created.status_code == 201, created.text
    assert created.json()["product_type"] == "countertop"
    package_id = created.json()["id"]
    read = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")
    assert read.status_code == 200
    assert read.json()["product_type"] == "countertop"
    listed = client.get(f"/api/v1/projects/{PROJECT}/packages")
    assert [item["product_type"] for item in listed.json()["items"]] == ["countertop"]
    assert session.get(Package, package_id).product_type == "countertop"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "body",
    [
        {"vendor": "A vendor"},
        {"vendor": "A vendor", "product_type": "Countertop"},
        {"vendor": "A vendor", "product_type": "lighting"},
        {"vendor": "A vendor", "product_type": None},
        {"vendor": "A vendor", "product_type": ""},
    ],
    ids=["missing", "wrong-case", "not-in-vocabulary", "null", "empty"],
)
def test_a_product_outside_the_vocabulary_is_refused_and_writes_nothing(
    session: Session, body: dict[str, object]
) -> None:
    _publish(session, ProductType.COUNTERTOP, ProductType.CABINET)

    response = _client(session).post(f"/api/v1/projects/{PROJECT}/packages", json=body)

    assert response.status_code == 422, response.text
    assert _packages(session) == 0


def test_a_product_no_published_rule_checks_is_refused(session: Session) -> None:
    """Cabinet is in the vocabulary, but with only countertop rules published a cabinet set would
    be checked against nothing — and an empty findings list reads as a clean one."""
    _publish(session, ProductType.COUNTERTOP)

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages",
        json={"vendor": "A vendor", "product_type": "cabinet"},
    )

    assert response.status_code == 422, response.text
    assert "No checks are published for cabinets" in response.json()["message"]
    assert _packages(session) == 0


def test_a_package_from_before_994_reads_back_with_no_product(session: Session) -> None:
    package = Package(project_id=PROJECT, vendor="Older")
    session.add(package)
    session.flush()
    from app.models import PackageRevision, PackageState

    session.add(
        PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    )
    session.commit()

    read = _client(session).get(f"/api/v1/projects/{PROJECT}/packages/{package.id}")

    assert read.status_code == 200, read.text
    assert read.json()["product_type"] is None


def test_the_choices_are_the_products_the_published_rulebook_checks(session: Session) -> None:
    client = _client(session)
    assert client.get("/api/v1/product-types").json() == []

    _publish(session, ProductType.COUNTERTOP)
    assert client.get("/api/v1/product-types").json() == [
        {
            "value": "countertop",
            "label": "Countertop",
            "published_checks": 7,
        }
    ]

    _publish(session, ProductType.CABINET)
    choices = client.get("/api/v1/product-types").json()
    assert [choice["value"] for choice in choices] == ["countertop", "cabinet"]
    assert [choice["label"] for choice in choices] == ["Countertop", "Cabinets"]
    assert [choice["published_checks"] for choice in choices] == [7, 2]
