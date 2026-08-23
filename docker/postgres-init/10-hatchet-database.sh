#!/bin/sh
# Give Hatchet its own logical database on the same PostgreSQL server (#416, F3.2).
#
# Two databases, one server — the layout `.env.example` already implies with `DATABASE_URL` and
# `HATCHET_DATABASE_URL`. They are separate because the engine owns its own schema and runs its own
# migrations: sharing one database would let `hatchet-migrate` and Alembic both claim authority over
# the same namespace, and `AGENTS.md` §3 is explicit that PostgreSQL holds our business truth while the
# engine only holds execution state. Keeping them apart is what makes "the engine is replaceable" true
# rather than aspirational.
#
# Runs only when the data directory is empty, which for this stack is every start — the `db` service
# deliberately has no volume, because a stale test database that survives a schema change produces
# confusing failures rather than useful state.
#
# `set -e` matters: if this fails, the container must fail loudly. A silently missing database would
# show up later as Hatchet failing to migrate, which reads like an engine bug rather than a setup step
# that did not run.
set -e

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE DATABASE hatchet OWNER $POSTGRES_USER;
SQL

echo "created the 'hatchet' database for the engine, alongside '$POSTGRES_DB' for the application"
