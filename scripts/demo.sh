#!/usr/bin/env bash
#
# One command to a working demo, and re-runnable without cleaning anything up first.
#
# **The two spellings of the database URL are the trap this script exists to stop re-teaching.**
# Alembic reads a bare `DATABASE_URL`: it is a separate tool with its own configuration and knows
# nothing of this application's `GV_` prefix. The application reads `GV_DATABASE_URL` through
# `Settings`. Following one instruction with the other variable set gives a server that refuses to
# start and blames a setting you have just made (#505). Both are exported explicitly below, next to
# each other, so the difference is visible rather than folklore.
#
# Every step is idempotent. `docker compose up` on a running stack is a no-op, `CREATE DATABASE` is
# guarded, `alembic upgrade head` on a current schema does nothing, and the rulebook publisher skips
# rules already published. Run it twice and the second run is fast rather than broken.
set -euo pipefail

cd "$(dirname "$0")/.."

# The local stack is a Python application, and relying on a shell's bare ``python`` makes the demo
# depend on whichever global interpreter happens to be first on PATH.  Use this checkout's declared
# runtime consistently; the early refusal is actionable when a new clone has not been installed yet.
PYTHON=".venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "  missing $PYTHON — create the project virtual environment before running the demo" >&2
  exit 1
fi

# Read from the Makefile rather than recomputed here. Two implementations of "which database is this
# checkout's" would disagree the first time either changed, and the disagreement would be a demo
# pointing at another worktree's data.
eval "$(make --no-print-directory demo-env)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/6  the stack"
docker compose up -d --wait

say "2/6  the database — $DEMO_DB"
if ! docker compose exec -T db psql -U gv -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$DEMO_DB'" | grep -q 1; then
  docker compose exec -T db psql -U gv -d postgres -c "CREATE DATABASE \"$DEMO_DB\" OWNER gv"
fi

say "3/6  the schema"
# BARE, for alembic.
DATABASE_URL="$BARE_URL" "$PYTHON" -m alembic upgrade head

say "4/6  the rulebook"
# `run_checks.py --publish` needs a revision to check, and publishing is the part we want; the seed
# below creates the package. Published first so the seed's own run has rules to run.
GV_DATABASE_URL="$BARE_URL" "$PYTHON" - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.config import Settings
sys.path.insert(0, str(pathlib.Path.cwd() / "scripts"))
from run_checks import _publish_rulebook

with Session(create_engine(Settings().database_url)) as session:
    published = _publish_rulebook(session)
    session.commit()
    print(f"  published {published} rule(s) (already-published rules are skipped)")
PY

say "5/6  a synthetic uploaded drawing with inspectable evidence"
PROJECT_ID="$(GV_DATABASE_URL="$BARE_URL" "$PYTHON" scripts/seed_demo.py --with-evidence 2>/dev/null \
  | awk '/^package /{print $2}')"
if [ -z "$PROJECT_ID" ]; then
  echo "  seed produced no package — run '$PYTHON scripts/seed_demo.py' to see why" >&2
  exit 1
fi
PROJECT_UUID="$(GV_DATABASE_URL="$BARE_URL" "$PYTHON" - "$PROJECT_ID" <<'PY'
import sys
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.config import Settings
from app.models import Package
with Session(create_engine(Settings().database_url)) as s:
    print(s.execute(select(Package.project_id).where(Package.id == sys.argv[1])).scalar_one())
PY
)"

say "6/6  the API, review worker, and UI"
# `GV_DEV_PRINCIPAL` and `GV_DEV_PROJECTS` are exported rather than written to `.env`: neither is a
# field on `Settings`, and `extra="forbid"` means a `.env` containing them stops the API starting
# (#504). `VITE_PROJECT_ID` is exported for the same shape of reason — writing `.env.local` is what
# two sessions used to fight over.
GV_DATABASE_URL="$BARE_URL" \
GV_DEV_PRINCIPAL="demo reviewer" \
GV_DEV_PROJECTS="$PROJECT_UUID" \
GV_DEV_PORT="$API_PORT" \
  "$PYTHON" scripts/dev_server.py &
API_PID=$!

# The demo's crop-local reader uses the explicit geometry settings that were measured for its safe
# synthetic fixture.  They are process configuration, not application defaults: a deployment must
# state its own values before it can turn this route on for a different drawing family.
#
# The four dimension-line values were measured against `AI_Set 2` p13 (#179) and are this demo's,
# not anybody else's. `CROSSING_MARGIN` is the one that separates a dimension from the box it
# measures; raise it toward `WITNESS_TOLERANCE` and every real dimension on that sheet is rejected.
#
# **`LINE_MINIMUM_PT` was 50 and that discarded every dimension line on every drawing we have.**
# It is the shortest stroke the reader will consider line-work at all, applied before the detector
# ever runs. The client's dimension lines are shorter than that, so the detector was being handed a
# page with none of them on it. Swept across the whole set at dpi 150 (#600):
#
#     drawing                     min=50   min=10   min=6    min=4
#     demo_pair/shop.pdf p1            0        6        7        8
#     aiset2/AI_Set_2.pdf p13         11       29       37       44
#     aiset1/AI_Set_1.pdf p2          10      123      158      202
#
# At 6, `demo_pair` goes from filling nothing to filling its fields with the placement check
# passing. `GLYPH_MAXIMUM_PT` moves to 5 with it: it is the longest stroke that may still be part of
# a glyph, so leaving it at 10 would put every stroke between 6 and 10 in both categories at once.
#
# These are still this demo's numbers rather than a deployment's, and still measured rather than
# chosen — the difference is that they are now measured against the drawings somebody actually
# uploads instead of one sheet.
GV_DATABASE_URL="$BARE_URL" \
GV_DEV_STORAGE=".dev-storage" \
GV_LOCALIZED_OCR_ENABLED=1 \
GV_READER_LINE_MINIMUM_PT=6 \
GV_READER_GLYPH_MAXIMUM_PT=5 \
GV_READER_GLYPH_GAP_PT=4 \
GV_READER_PROXIMITY_LIMIT=0.05 \
GV_READER_AMBIGUITY_MARGIN=0.005 \
GV_READER_WITNESS_TOLERANCE=0.004 \
GV_READER_MINIMUM_SPAN=0.01 \
GV_READER_STRAIGHTNESS=0.0005 \
GV_READER_CROSSING_MARGIN=0.0005 \
GV_READER_LOCALIZED_MINIMUM_PATHS=1 \
GV_READER_LOCALIZED_MAXIMUM_SPAN=0.5 \
GV_READER_LOCALIZED_CROP_MARGIN_PT=2 \
  "$PYTHON" scripts/drain_outbox.py --watch &
WORKER_PID=$!

VITE_PROJECT_ID="$PROJECT_UUID" \
VITE_API_TARGET="http://127.0.0.1:$API_PORT" \
  npm --prefix frontend/main run dev -- --port "$VITE_PORT" --strictPort &
VITE_PID=$!

# Both die with this script, however it ends. Without the trap a Ctrl-C leaves two servers holding
# the ports the next run wants — which is the mess this whole change is about.
trap 'kill $API_PID $WORKER_PID $VITE_PID 2>/dev/null || true' EXIT INT TERM

sleep 4
cat <<EOF

  ────────────────────────────────────────────────────────────────
   Demo ready

     UI          http://localhost:$VITE_PORT
     API         http://localhost:$API_PORT
     project     $PROJECT_UUID
     database    $DEMO_DB

   The seeded package is a clearly labelled synthetic fixture. It has
   one deliberate 1/4-in countertop-depth mismatch and a real
   mechanical crop generated from its uploaded PDF. Open the Review
   page, choose CT-DEPTH-001, then Evidence & facts to inspect it.
   No client drawing, reviewer annotation, or model-derived type is
   used by this demo fixture.

   Ctrl-C stops both servers.
  ────────────────────────────────────────────────────────────────

EOF
wait
