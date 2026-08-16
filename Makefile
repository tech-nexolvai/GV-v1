# Shortcuts for the checks CI runs. `make check` is the one that matters — it is the same chain,
# in the same order, and it says plainly what it could not verify.

.PHONY: check check-fast db db-stop install

install:            ## install exactly what CI installs
	python -m pip install -e ".[dev,rules,platform]"

check:              ## the full CI chain, locally
	python scripts/check.py

check-fast:         ## same, without semgrep, for a tight edit loop
	python scripts/check.py --fast

db:                 ## start PostgreSQL so the persistence tests run instead of skipping
	docker compose up -d db
	@echo
	@echo "  export DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv"

db-stop:
	docker compose down
