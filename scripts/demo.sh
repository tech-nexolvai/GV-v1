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
# **What each model call cost (#700).** Both processes record model calls — the worker's readers and
# the API's reviewer chat — and price them from this stated file, which names its own source and
# date. Without it every call's cost is recorded as unknown, never as a free zero.
GV_DATABASE_URL="$BARE_URL" \
GV_MODEL_RATES_FILE="deploy/model_rates.us-east-1.json" \
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
# passing. `GLYPH_MAXIMUM_PT` moved with it: it is the largest a path may be and still be part of a
# glyph, and leaving it at 10 would put every stroke between 6 and 10 in both categories at once.
#
# **It is 6, not 5, because the client's digits are 5.4-5.5 pt tall (#715).** At 5 no digit was a
# glyph, so a region formed from the inch mark or a tick beside the number, and the vision crop cut
# round it showed part of the label: `91"` of `191"`, `9 1/2"` of `39 1/2"`. A reader that read that
# crop correctly returned a wrong dimension that parses, and two vendors agreed on it. On the four
# such crops in the human-read key (#666), 7 readers were right 6 times in 28 with the crops cut at
# 5, and 26 times in 28 cut at 6; accepted-but-wrong fell from 19 to 1. At 6 it equals
# `LINE_MINIMUM_PT`, so no stroke is in both categories except a short diagonal, and on the 17-page
# set the line segments (23,069) and dimension lines (1,515) are unchanged while label-to-line
# associations rise from 1,193 to 1,236.
#
# These are still this demo's numbers rather than a deployment's, and still measured rather than
# chosen — the difference is that they are now measured against the drawings somebody actually
# uploads instead of one sheet.
# **The stacked-fraction detector (#735).** A stacked fraction always goes to a reviewer (#726), and
# these six say what one looks like: a flat stroke (thinner than 0.3 pt, longer than 1 pt) with a
# glyph-sized shape starting within 3 pt above and below it, each side at least 1 pt tall, nothing
# over 12 pt, the parts within 2.5x of each other in proportion. Measured on `AI_Set 2`'s 17 pages:
# it finds all 12 stacked fractions drawn there as paths, with 15 false alarms (hatching, a rotated
# word, a few outlet symbols) that cost a reviewer a look and never a wrong number. Fractions drawn as
# font text rather than paths — all of `AI_Set 1`'s, and some of `AI_Set 2`'s (pages 15 and 17) — are
# not seen by it at all (#738).
#
# **`CHARACTER_GAP_PT` lays each one out (#834)**: how far apart along the baseline the whole number's
# digits, the fraction and the inch mark may be. A reading whose digit counts the layout contradicts
# — `3 3/4"` on a `3/4"` — is refused. On those 12 fractions every value from 3.2 to 6 pt counts every
# part right; the widest gap inside a label is 3.12 pt, between the `1` and `3` of a `13 1/8"`.
#
# **`TURNED_ASPECT_MIN` finds the sideways ones (#869).** `AI_Set 2` draws its vertical dimensions
# turned a quarter inside stamps that read upright, so their bars run up the page and the six above
# never saw them. A bar across the stamp's baseline now counts where each character either side
# stands at least 1.1 times as tall as it is wide, read the way the label reads; the letters beside
# an upright `l` or `I` lie on their side and do not. Measured on all 17 pages: 26 sideways stacked
# fractions drawn as paths (9 on page 2, 8 on page 3, 3 on page 4, 6 on page 5), every one found,
# laid out reading up the page and with its inch mark, and 4 false alarms, all electrical-outlet
# symbols. Every value from 1.05 to 1.25 gives exactly that; 1 adds a fifth outlet; at 1.37 two
# real fractions are lost, because the client's widest digits stand 1.36 times as tall as they are
# wide. The 27 fractions found before are unchanged. On `AI_Set 1` its 22 are unchanged too, and it
# gains 5 false alarms, none of them a label: its labels are text the detector cannot see (#738).
#
# **The two Bedrock vision readers, and what turning them on costs (#651).** They were built in
# #622/#623 and PC.3 and this flag is the only thing that starts them; until it was set here the
# whole reading rebuild had never executed, and the demo ran the vector+OCR path alone. They are
# what makes the cross-route SECOND_READER lane possible: a number read by two *different*
# extractors that agree exactly is the only reading this system will seal without a human.
#
# It is a paid call per candidate region, so the cost is real and belongs next to the switch rather
# than in a runbook. `scripts/reading_funnel.py` reports `model_invocations` alongside the reading
# counts for exactly that reason — the benefit and the bill are read off the same table.
#
# **The reading agent (#757), on in the demo only** — the admin's decision of 2026-10-01, taken once
# the 51-crop human key showed it confirms no wrong value (#775 made agreement count only across
# vendors). Where a label is hard to read it widens the crop to the whole label, turns it upright or
# looks sharper, asks Nova 2 Lite, then mistral-large-3 (D-A2). What it reads is a candidate like any
# other: a person confirms it, and a disagreement goes to a reviewer. On that key it also proposed
# two wrong values for a person to catch, and read nothing the pair had missed — so it is on here to
# be tried on real drawings, and nowhere else. About 40% more model cost per crop. Every setting is
# required; `workflow/reading_agent.py` says what each one is.
# **Ministral reads first (#787).** Nova 2 Lite is held to 20 requests a minute on this account and
# AWS declined to raise it (#716), so Nova reads only the crops Ministral found a value in — about 83
# of 1,009 on AI_Set_2, which is minutes of Nova's quota instead of 50. Nothing that could be
# confirmed is lost: a confirmation is two readers' values agreeing (#775).
#
# **The phrase index is on (#849).** After extraction the worker joins each line's words into
# passages (#836), which is what `workflow/parameter_proposals.py` searches when it looks for the
# passage that states a setting. Words join when the space between them is at most 0.34 of the
# line's height. Measured on both client drawings in #840: the space between two words of one note
# is at most 0.333 line heights, and the narrowest space between two separate items is 0.372 on
# AI_Set_2 and 0.421 on AI_Set_1. The admin's decision of 2026-10-03, on #798. No model is called.
GV_VISION_GATE_READER=bedrock-ministral-3-3b \
GV_READING_AGENT=1 \
GV_AGENT_MAX_STEPS=6 \
GV_AGENT_MAX_ESCALATIONS=1 \
GV_AGENT_SHARPER_DPI=450 \
GV_AGENT_PRIMARY_READER=bedrock-nova-2-lite \
GV_AGENT_ESCALATION_READER=bedrock-mistral-large-3 \
GV_AGENT_LABEL_GAP_PT=4 \
GV_AGENT_MAX_LABEL_PT=40 \
GV_DATABASE_URL="$BARE_URL" \
GV_DEV_STORAGE=".dev-storage" \
GV_MODEL_RATES_FILE="deploy/model_rates.us-east-1.json" \
GV_LOCALIZED_OCR_ENABLED=1 \
GV_BEDROCK_VISION_ENABLED=1 \
GV_READER_LINE_MINIMUM_PT=6 \
GV_READER_GLYPH_MAXIMUM_PT=6 \
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
GV_READER_FRACTION_BAR_THICKNESS_MAX_PT=0.3 \
GV_READER_FRACTION_BAR_LENGTH_MIN_PT=1 \
GV_READER_FRACTION_REACH_PT=3 \
GV_READER_FRACTION_GLYPH_MIN_PT=1 \
GV_READER_FRACTION_GLYPH_MAX_PT=12 \
GV_READER_FRACTION_PROPORTION_MAX=2.5 \
GV_READER_FRACTION_CHARACTER_GAP_PT=4 \
GV_READER_FRACTION_TURNED_ASPECT_MIN=1.1 \
GV_PHRASE_GAP_LINE_HEIGHTS=0.34 \
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
