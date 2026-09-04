# Shortcuts for the checks CI runs, and for running the stack locally (#416, F3.2).
#
# `make check` is the one that matters for a change — it is the same chain CI runs, in the same order,
# and it says plainly what it could not verify. The rest of this file is about getting the application
# to actually run, which until this story was not possible: the API and the durable workflow seam were
# both built and there was no documented way to start either.
#
# CONTRIBUTING.md has the runbook in order, from a clone to a package the API has accepted.

.PHONY: check check-fast db db-stop install up down token migrate serve worker dispatch

install:            ## install exactly what CI installs
	python -m pip install -e ".[ai,dev,extraction,rules,platform,reports]"

check:              ## the full CI chain, locally
	python scripts/check.py

check-fast:         ## same, without semgrep, for a tight edit loop
	python scripts/check.py --fast

db:                 ## start PostgreSQL so the persistence tests run instead of skipping
	docker compose up -d --wait db
	@echo
	@echo "  export DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv"

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

migrate:            ## bring the application database to head — the step that is easy to forget
	alembic upgrade head
	@echo
	@echo "  /ready will now answer 200; it returns 503 while the schema is behind."

serve:              ## run the API
	# `--factory` because app/main.py exposes create_app() and no module-level `app`. That is
	# deliberate — a module-level singleton would make every test share one configuration — so the
	# flag is not a workaround, it is the consequence.
	uvicorn "app.main:create_app" --factory --reload --port 8000

worker:             ## run the worker: executes the review stages (#415)
	python -m workflow.worker

dispatch:           ## run the dispatcher: drains the outbox into the engine (#415)
	python -m workflow.dispatcher
