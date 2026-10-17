"""The reviewer says what product a drawing set is for, through the API (#994).

- `POST /packages` requires `product_type`; a value outside the vocabulary, or one no published rule
  checks, is a 422 and writes nothing.
- `GET` returns it, and a package from before #994 reads back as `null`.
- `GET /product-types` lists only the active products the published rulebook has checks for.
- `GET /products` lists every product of the product registry (#1547), inactive ones too, with
  whether each can be picked and how many published checks it has: the screens build their product
  choices from it, never from a list of their own.
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
from app.api.packages import product_label
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import Package, Project
from app.models.rules import RuleDefinition, RuleSnapshot
from rules.schema import Rule
from rules.snapshot import publish
from tests.app.postgres_fixture import alembic_config
from tests.dummy_products import DUMMY, use_dummy_registry
from vocabulary.product_registry import registry
from vocabulary.semantic_types import ProductType

pytest_plugins = ("tests.app.postgres_fixture",)
__all__ = ["use_dummy_registry"]  # the fixture, imported so pytest finds it

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
    assert {member.value for member in ProductType} <= set(registry().ids())
    assert product_label("countertop") == "Countertop"
    assert all(product_label(product.id) == product.label for product in registry().products)


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
        {"vendor": "A vendor", "product_type": "spaceship"},
        {"vendor": "A vendor", "product_type": None},
        {"vendor": "A vendor", "product_type": ""},
    ],
    ids=["missing", "wrong-case", "inactive-product", "not-in-registry", "null", "empty"],
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
    assert "No checks are published for cabinet drawing sets" in response.json()["message"]
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
            "published_checks": 8,
        }
    ]

    _publish(session, ProductType.CABINET)
    choices = client.get("/api/v1/product-types").json()
    assert [choice["value"] for choice in choices] == ["countertop", "cabinet"]
    assert [choice["label"] for choice in choices] == ["Countertop", "Cabinet"]
    assert [choice["published_checks"] for choice in choices] == [8, 2]


def test_products_lists_the_whole_registry_with_what_can_be_picked(session: Session) -> None:
    """Every registry product, in its order: countertop and cabinet active, the six others
    listed but inactive (shown greyed, "coming later"), each with its checklist."""
    _publish(session, ProductType.COUNTERTOP)

    products = _client(session).get("/api/v1/products").json()

    assert [p["id"] for p in products] == [
        "countertop",
        "cabinet",
        "lighting",
        "seating",
        "case_goods",
        "mirrors",
        "accessories",
        "artwork",
    ]
    by_id = {p["id"]: p for p in products}
    assert [p["id"] for p in products if p["active"]] == ["countertop", "cabinet"]
    assert by_id["countertop"]["published_checks"] == 8
    assert by_id["cabinet"]["published_checks"] == 0
    assert by_id["countertop"]["plural"] == "Countertops"
    assert by_id["lighting"]["draft"] is True and by_id["countertop"]["draft"] is False
    assert all(p["checklist"] and p["description"] for p in products)
    assert set(by_id["lighting"]["check_kinds"]) == {"dimension_match", "global_rules"}


def test_a_product_added_only_to_the_registry_can_be_uploaded_once_a_rule_checks_it(
    session: Session, use_dummy_registry: object
) -> None:
    """#1547: a made-up product in a test registry, with one rule published for it, is listed by
    `GET /products` and accepted for a new drawing set, with no code change; the database (0110)
    stores it."""
    template = min(RULEBOOK.glob("*.yaml"))
    data = yaml.safe_load(template.read_text(encoding="utf-8"))
    data.update(id="DUMMY-001", product_type=DUMMY)
    rule = Rule.model_validate(data)
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
    session.commit()
    client = _client(session)

    products = {p["id"]: p for p in client.get("/api/v1/products").json()}
    assert products[DUMMY]["active"] is True and products[DUMMY]["published_checks"] == 1
    assert products[DUMMY]["plural"] == "Dummy widgets"
    choices = [choice["value"] for choice in client.get("/api/v1/product-types").json()]
    assert choices == [DUMMY]

    created = client.post(
        f"/api/v1/projects/{PROJECT}/packages", json={"vendor": "A vendor", "product_type": DUMMY}
    )

    assert created.status_code == 201, created.text
    assert created.json()["product_type"] == DUMMY
    assert session.get(Package, created.json()["id"]).product_type == DUMMY  # type: ignore[union-attr]
