"""Write the API's OpenAPI schema to a file, so the frontend can generate types from it.

The frontend's `src/api/schema.d.ts` is generated from this and never hand-edited: a field renamed on
the server then breaks the frontend build instead of surfacing as a blank panel during a review.

Deliberately does not need a running server or a database. `create_app` builds the schema from the
route table alone, so this is safe in CI and cannot go stale against whatever happens to be deployed.

Usage: `python scripts/export_openapi.py [path]`
Verification: `tests/test_openapi_export.py`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Running a script puts `scripts/` on the path, not the repository root, so `app` and `vocabulary`
# would not import. Every other script here does the same.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT = Path(__file__).resolve().parent.parent / "frontend" / "main" / "openapi.json"
#: Any URL will do — the schema comes from the route table, not from a connection.
PLACEHOLDER_DATABASE_URL = "postgresql+psycopg://schema:schema@localhost:1/schema"


def schema() -> dict[str, object]:
    from app.config import Settings
    from app.main import create_app

    settings = Settings(database_url=PLACEHOLDER_DATABASE_URL)  # type: ignore[call-arg]
    return dict(create_app(settings).openapi())


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    target.parent.mkdir(parents=True, exist_ok=True)
    # A trailing newline and sorted keys so regenerating is a no-op in git unless the API changed.
    target.write_text(json.dumps(schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
