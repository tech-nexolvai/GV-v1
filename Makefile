# Shortcuts for the checks CI runs. `make check` is the one that matters — it is the same chain,
# in the same order, and it says plainly what it could not verify.

.PHONY: ci check check-fast db db-stop install hooks

install:            ## install exactly what CI installs, and the pre-push hook
	python -m pip install -e ".[ai,dev,rules,platform,reports]"
	@$(MAKE) --no-print-directory hooks

hooks:              ## point git at .githooks, so pre-push runs the fast chain
	git config core.hooksPath .githooks
	@echo "  pre-push hook active. Skip once with: git push --no-verify"

ci:                 ## local CI: the full chain, with the database started so nothing skips
	python scripts/check.py --with-db

check:              ## the same chain, without starting the database
	python scripts/check.py

check-fast:         ## same, without semgrep, for a tight edit loop
	python scripts/check.py --fast

db:                 ## start PostgreSQL so the persistence tests run instead of skipping
	docker compose up -d db
	@echo
	@echo "  export DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv"

db-stop:
	docker compose down
