"""A package's product comes from the product registry, not a list fixed in the database (#1547).

Revision ID: 0111_package_product_registry
Revises: 0110_assistant_set_notes

**Why.** 0073 limited `packages.product_type` to a written-out list, `'countertop', 'cabinet'`. GV
reviews more products than that (the client's category model has eight), and they are now data in
the product registry (`vocabulary/products.yaml`). With the fixed list, every new product would need
a migration before a drawing set for it could be stored, which is the code change the registry is
there to remove.

**What.** The fixed list is replaced by a check on the *shape* of the value: NULL, or an id the
registry could hold (lowercase letters, digits and `_`, starting with a letter, at most 32). So a
typo such as `Countertop` or an empty string still cannot be stored, and which ids are allowed is
decided in one place, by code reading the registry: `PackageCreate` accepts only an active product,
and `package_product` refuses a stored id the registry does not know. No row changes.

**Why not list all eight products in the constraint instead:** a ninth would again need a
migration, and the registry and the database would be two lists that can drift. The shape check
keeps the database's guard against junk while leaving the vocabulary to the registry.

**Downgrade** puts 0073's fixed list back. It refuses, plainly, while a package for another product
is stored, because the narrower check could not hold it.

Verification: `tests/db/test_package_product_type.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0111_package_product_registry"
down_revision: str | None = "0110_assistant_set_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PACKAGES = "packages"
NAME = "package_product_type"
#: 0073's check, written out as it was.
BEFORE = "product_type IS NULL OR product_type IN ('countertop', 'cabinet')"
#: A registry id's shape (`vocabulary/product_registry.py`).
AFTER = "product_type IS NULL OR product_type ~ '^[a-z][a-z0-9_]{1,31}$'"


def upgrade() -> None:
    op.drop_constraint(NAME, PACKAGES, type_="check")
    op.create_check_constraint(NAME, PACKAGES, AFTER)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM packages "
        "WHERE product_type IS NOT NULL AND product_type NOT IN ('countertop', 'cabinet')) "
        "THEN RAISE EXCEPTION 'packages for other products are stored; they cannot be "
        "downgraded away'; END IF; END $$"
    )
    op.drop_constraint(NAME, PACKAGES, type_="check")
    op.create_check_constraint(NAME, PACKAGES, BEFORE)
