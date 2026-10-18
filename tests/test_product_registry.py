"""The product registry: products are data, not code (#1547). No database, no model call, $0.

What is asserted:

- the repository's registry loads and holds together: unique ids that fit the database, every
  required field, countertop and cabinet active, the six other client products listed inactive
  and marked draft, every active product with a checklist and a skill file;
- a registry that does not hold together is refused, naming the problem;
- `ProductType` is the registry: its written-out members are registry ids, any other registry id is
  a `ProductType` too, and an id the registry does not list is refused;
- a made-up product added only to a test registry reaches the message check, the finding
  categories, the words the screens and the report use, and the full-check brief, with no code
  change; an inactive product is refused in a message; old values (countertop, cabinet) still work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.models.package import PRODUCT_TYPE_SHAPE
from app.review.assistant.chat_contract import ChatFindingOut, ChatMessageIn
from app.review.assistant.chat_findings import finding_categories, products_words
from app.review.assistant.full_check_brief import helper_brief, product_checklist
from tests.dummy_products import DUMMY, use_dummy_registry, write_dummy_registry
from vocabulary.product_registry import (
    DEFAULT_REGISTRY,
    RegistryError,
    load_registry,
    registry,
    reset_registry,
)
from vocabulary.semantic_types import ProductType

__all__ = ["use_dummy_registry"]  # the fixture, imported so pytest finds it

CLIENT_PRODUCTS = (
    "countertop",
    "cabinet",
    "lighting",
    "seating",
    "case_goods",
    "mirrors",
    "accessories",
    "artwork",
)


def _message(**fields: object) -> ChatMessageIn:
    body = {"text": "hello", "model": "claude-opus-5-5", "effort": "high", "thinking": True}
    return ChatMessageIn.model_validate({**body, **fields})


# ---- the repository's registry -------------------------------------------------------------------


def test_the_registry_holds_the_clients_products_two_active() -> None:
    known = load_registry(DEFAULT_REGISTRY)

    assert known.ids() == CLIENT_PRODUCTS
    assert known.active_ids() == ("countertop", "cabinet")
    assert len(set(known.ids())) == len(known.ids())
    for product in known.products:
        assert re.fullmatch(PRODUCT_TYPE_SHAPE, product.id), product.id
        assert product.label and product.plural and product.description and product.category_label
        assert product.check_kinds and product.checklist, product.id
        assert len({item.name for item in product.checklist}) == len(product.checklist)
        assert product.has_skill, f"{product.id}.md"
        assert product.draft is (not product.active), product.id
    for product in known.active():
        assert product.skill_file.read_text(encoding="utf-8").strip()
        assert product.global_rule_hints, product.id
        # The strict product scope reads these: specific words, then broad hints, and
        # no word two active products share.
        assert product.words and product.hints, product.id
    for one, two in ((a, b) for a in known.active() for b in known.active() if a.id < b.id):
        assert not set(one.words) & {*two.words, *two.hints}, (one.id, two.id)
        assert not set(two.words) & {*one.words, *one.hints}, (one.id, two.id)
    assert known.get("lighting") is not None and not known.get("lighting").active  # type: ignore[union-attr]


def test_product_type_is_the_registry() -> None:
    assert {member.value for member in ProductType} <= set(registry().ids())
    assert ProductType("countertop") is ProductType.COUNTERTOP
    lighting = ProductType("lighting")
    assert lighting.value == "lighting" and lighting is ProductType("lighting")
    assert lighting == "lighting"
    for wrong in ("Countertop", "spaceship", ""):
        with pytest.raises(ValueError):
            ProductType(wrong)


def _broken(tmp_path: Path, change: object) -> Path:
    data = yaml.safe_load(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
    data["skills_folder"] = str((DEFAULT_REGISTRY.parent / data["skills_folder"]).resolve())
    change(data)  # type: ignore[operator]
    path = tmp_path / "products.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda d: d["products"].append(dict(d["products"][0])), "listed twice"),
        (lambda d: d["products"][0].update(id="Counter Top"), "lowercase letters"),
        (lambda d: d["products"][0].update(id="other"), "not an id"),
        (lambda d: d["products"][0].pop("label"), "`label` must be non-empty"),
        (lambda d: d["products"][0].update(colour="red"), "unknown field"),
        (lambda d: d["products"][0].update(checklist=[]), "needs a checklist"),
        (lambda d: d["products"][0].update(check_kinds=["vibes"]), "check_kinds"),
        (lambda d: d["products"][2].update(active=True, id="lamp_post"), "skill file lamp_post"),
        (lambda d: [p.update(active=False) for p in d["products"]], "at least one product"),
        (lambda d: d.update(version=2), "version: 1"),
        (lambda d: d["products"][0].update(words="countertop"), "`words` must be a list"),
        (lambda d: d["products"][1].update(hints=[""]), "`hints` must be a list"),
    ],
    ids=[
        "repeated-id",
        "bad-id",
        "other-is-no-id",
        "missing-label",
        "unknown-field",
        "active-without-checklist",
        "unknown-check-kind",
        "active-without-skill",
        "none-active",
        "unknown-version",
        "words-not-a-list",
        "empty-hint",
    ],
)
def test_a_registry_that_does_not_hold_together_is_refused(
    tmp_path: Path, change: object, says: str
) -> None:
    with pytest.raises(RegistryError, match=says):
        load_registry(_broken(tmp_path, change))


# ---- a product added only to the registry --------------------------------------------------------


def test_a_dummy_product_reaches_messages_categories_words_and_the_brief(
    use_dummy_registry: Path,
) -> None:
    assert registry().source == use_dummy_registry.resolve()
    assert DUMMY in registry().active_ids()
    assert ProductType(DUMMY).value == DUMMY

    # The message check: the new product is accepted, alone or with the others.
    assert _message(products=[DUMMY]).products == [DUMMY]
    assert _message(mode="full_check", text="", products=["cabinet", DUMMY]).products == [
        "cabinet",
        DUMMY,
    ]
    # Findings can be filed under it, and the screens' words name it.
    assert DUMMY in finding_categories() and finding_categories()[-1] == "other"
    assert products_words(["countertop", DUMMY]) == "Countertops and Dummy widgets"
    # The full check's brief carries its checklist and GV standards, after the shared items.
    brief = helper_brief([2, 3], file_name="/mnt/user-data/uploads/v.pdf", products=[DUMMY])
    assert "WHAT TO COMPARE: " in brief
    assert brief.index("WHAT TO COMPARE: ") < brief.index("Dummy widgets (category dummy_widget")
    assert "- widget knob: Knob count and finish code." in brief
    assert "Knob finish matches GV's made-up standard" in brief
    assert "Countertops (category" not in brief


def test_an_inactive_or_unknown_product_is_refused_in_a_message() -> None:
    with pytest.raises(ValidationError, match="Lighting is not available yet"):
        _message(products=["lighting"])
    with pytest.raises(ValidationError, match="'spaceship' is not a product"):
        _message(products=["spaceship"])
    with pytest.raises(ValidationError, match="'dummy_widget' is not a product"):
        _message(products=[DUMMY])
    with pytest.raises(ValidationError, match="each product at most once"):
        _message(products=["cabinet", "cabinet"])


def test_old_values_still_work() -> None:
    """Answers and packages stored before the registry keep reading: countertop and cabinet are
    still products, and an old finding's category is still shown."""
    assert _message(products=["countertop", "cabinet"]).products == ["countertop", "cabinet"]
    assert products_words(["countertop", "cabinet"]) == "Countertops and Cabinets"
    assert products_words([]) == "All products"
    assert registry().category_label("cabinet") == "Cabinets"
    assert registry().category_label("other") == "Other"
    assert registry().category_label("retired_product") == "Other"
    assert set(finding_categories()) >= {"countertop", "cabinet", "other"}
    old = ChatFindingOut.model_fields["category"]
    assert old.annotation is str


def test_the_brief_with_no_products_named_covers_every_active_product() -> None:
    text = product_checklist(None)
    assert "Countertops (category countertop" in text and "Cabinets (category cabinet" in text
    assert "Lighting (category" not in text


def test_reset_forgets_a_test_registrys_products(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_dummy_registry(tmp_path)
    monkeypatch.setenv("GV_PRODUCT_REGISTRY", str(path))
    reset_registry()
    assert ProductType(DUMMY).value == DUMMY
    monkeypatch.delenv("GV_PRODUCT_REGISTRY")
    reset_registry()
    with pytest.raises(ValueError):
        ProductType(DUMMY)
