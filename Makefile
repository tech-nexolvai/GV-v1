# Shortcuts for the checks CI runs, and for running the stack locally (#416, F3.2).
#
# `make check` is the one that matters for a change — it is the same chain CI runs, in the same order,
# and it says plainly what it could not verify. The rest of this file is about getting the application
# to actually run, which until this story was not possible: the API and the durable workflow seam were
# both built and there was no documented way to start either.
#
# CONTRIBUTING.md has the runbook in order, from a clone to a package the API has accepted.

# ---------------------------------------------------------------------------
# Where this checkout runs: its own database and its own ports.
#
# **The problem this solves cost six discarded test runs in one day.** Two sessions sharing a checkout
# fought over ports, over `frontend/main/.env.local`, and — the expensive one — over the `gvtest`
# database. The database contention is not what it looks like: `tests/app/postgres_fixture.py` already
# gives every test its own schema, so schemas were never the issue. `alembic/versions/0020_pg_trgm.py`
# runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` **unqualified**, so it lands in whatever schema is
# first on `search_path` — the per-test schema — while an extension is a *database-wide* object. The
# second concurrent run's `IF NOT EXISTS` therefore sees it already present, skips, and its schema
# cannot see `gin_trgm_ops`. Every failure looked like a regression and was a race.
#
# So the isolation has to be per *database*, not per schema. A linked worktree gets its own.
#
# The main checkout keeps the plain `gv` and `gvtest` and the plain ports, because README,
# CONTRIBUTING and `.env.example` all name them and a rename would make those instructions wrong.
IS_WORKTREE := $(shell [ "$$(git rev-parse --git-dir 2>/dev/null)" != "$$(git rev-parse --git-common-dir 2>/dev/null)" ] && echo yes)
SLUG        := $(shell printf '%s' "$(notdir $(CURDIR))" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '_' | cut -c1-24)

ifeq ($(IS_WORKTREE),yes)
# A stable offset from the name, so the same worktree always gets the same ports — an offset that
# moved between runs would leave a stale server on the port you are about to use.
OFFSET   := $(shell printf '%s' "$(SLUG)" | cksum | cut -d' ' -f1 | awk '{print ($$1 % 40) + 1}')
TEST_DB  := gvtest_$(SLUG)
DEMO_DB  := gv_$(SLUG)
else
OFFSET   := 0
TEST_DB  := gvtest
DEMO_DB  := gv
endif

API_PORT  := $(shell echo $$(( 8000 + $(OFFSET) )))
VITE_PORT := $(shell echo $$(( 5173 + $(OFFSET) )))

# **One stack, whichever checkout drives it.** Compose names a project after the directory by default,
# so a worktree would start a *second* PostgreSQL and collide on 5433. Pinning the name means every
# worktree talks to the same server and isolates itself by database instead.
#
# `gv-v1` and not `gv`, because that is the name compose already derived from this directory and the
# containers running on any existing machine carry it. Choosing a tidier name orphans those: compose
# tries to start a second stack, the bind on 5433 fails, and the error names a port rather than a
# rename. Verified against a running stack rather than chosen.
export COMPOSE_PROJECT_NAME := gv-v1

# **The two spellings, kept apart deliberately.** Alembic reads a bare `DATABASE_URL` — it is a
# separate tool with its own configuration and knows nothing of this application's `GV_` prefix. The
# application reads `GV_DATABASE_URL` through `Settings`. Following one instruction with the other
# variable set produces a server that will not start and blames a setting you just made (#505), so
# both are spelled out here rather than left to whoever reads the runbook next.
BARE_URL := postgresql+psycopg://gv:gv@localhost:5433/$(DEMO_DB)
TEST_URL := postgresql+psycopg://gv:gv@localhost:5433/$(TEST_DB)

.PHONY: check check-fast db db-stop install up down token migrate serve worker dispatch \
	demo demo-env worktree where testdb

install:            ## install exactly what CI installs
	python -m pip install -e ".[ai,dev,extraction,rules,platform,reports]"

check: testdb       ## the full CI chain, locally, against this checkout's own test database
	DATABASE_URL="$(TEST_URL)" python scripts/check.py

check-fast: testdb  ## same, without semgrep, for a tight edit loop
	DATABASE_URL="$(TEST_URL)" python scripts/check.py --fast

db: testdb          ## start PostgreSQL so the persistence tests run instead of skipping
	@echo
	@echo "  export DATABASE_URL=$(TEST_URL)"
	@echo
	@echo "  A bare DATABASE_URL, because that is what the test fixture and alembic read."
	@echo "  The application reads GV_DATABASE_URL instead; see 'make where'."

db-stop:            ## stop the whole stack (kept: it is what README and CONTRIBUTING already name)
	docker compose down

up:                 ## start PostgreSQL and the Hatchet engine, and wait until both are healthy
	docker compose up -d --wait
	@echo
	@echo "  engine dashboard: http://localhost:8888"
	@echo "  next: make token   (once), then make migrate"

down:               ## stop the stack and remove the containers
	docker compose down

token:              ## mint a client token for the worker, and show where to put it
	@docker compose exec -T hatchet ./hatchet-admin token create --config ./config 2>/dev/null | tail -1 > .hatchet-token
	@echo "Add this to your .env (the GV_ prefix is what app/config.py reads):"
	@echo
	@echo "  GV_HATCHET_TOKEN=$$(cat .hatchet-token)"
	@echo
	@echo "Also written to .hatchet-token, which is gitignored. It expires in 90 days;"
	@echo "run make token again for a new one."

# A bare DATABASE_URL below: alembic is a separate tool and knows nothing of this app's GV_ prefix.
migrate:            ## bring the application database to head — the step that is easy to forget
	DATABASE_URL="$(BARE_URL)" alembic upgrade head
	@echo
	@echo "  /ready will now answer 200; it returns 503 while the schema is behind."

serve:              ## run the API
	# `--factory` because app/main.py exposes create_app() and no module-level `app`. That is
	# deliberate — a module-level singleton would make every test share one configuration — so the
	# flag is not a workaround, it is the consequence.
	uvicorn "app.main:create_app" --factory --reload --port $(API_PORT)

worker:             ## run the worker: executes the review stages (#415)
	python -m workflow.worker

dispatch:           ## run the dispatcher: drains the outbox into the engine (#415)
	python -m workflow.dispatcher

where:              ## show this checkout's database names and ports
	@echo "  checkout   : $(notdir $(CURDIR))$(if $(IS_WORKTREE), (linked worktree),)"
	@echo "  demo db    : $(DEMO_DB)"
	@echo "  test db    : $(TEST_DB)"
	@echo "  API port   : $(API_PORT)"
	@echo "  vite port  : $(VITE_PORT)"
	@echo
	@echo "  alembic wants a BARE DATABASE_URL : $(BARE_URL)"
	@echo "  the app wants GV_DATABASE_URL     : $(BARE_URL)"

# Verified by asking the database afterwards, not by trusting an exit status. The create line reported
# success while creating nothing — `docker compose exec` failed against a project whose container did
# not exist, the `||` swallowed it, and the target printed a database name that was not there. The
# tests then failed with "database does not exist", three steps from the cause.
testdb:             ## create this checkout's test database if it does not exist
	@docker compose up -d --wait db >/dev/null
	@docker compose exec -T db psql -U gv -d postgres -tAc \
		"SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 \
		|| docker compose exec -T db psql -U gv -d postgres -c 'CREATE DATABASE "$(TEST_DB)" OWNER gv' >/dev/null
	@docker compose exec -T db psql -U gv -d postgres -tAc \
		"SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 \
		|| { echo "  could not create $(TEST_DB) — is the stack up? try 'make up'"; exit 1; }
	@echo "  test database: $(TEST_DB)"

demo:               ## one command to a working demo: stack, schema, rulebook, API and UI
	@scripts/demo.sh

worktree:           ## make a worktree with its own database and ports: make worktree NAME=my-branch
	@test -n "$(NAME)" || { echo "usage: make worktree NAME=<branch>"; exit 2; }
	@scripts/worktree.sh "$(NAME)"

demo-env:           ## print this checkout's settings as shell assignments (used by scripts/demo.sh)
	@echo 'DEMO_DB="$(DEMO_DB)"'
	@echo 'TEST_DB="$(TEST_DB)"'
	@echo 'API_PORT="$(API_PORT)"'
	@echo 'VITE_PORT="$(VITE_PORT)"'
	@echo 'BARE_URL="$(BARE_URL)"'
	@echo 'TEST_URL="$(TEST_URL)"'
