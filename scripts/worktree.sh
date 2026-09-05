#!/usr/bin/env bash
#
# A worktree for one session, with its own database and its own ports.
#
# **Why a worktree rather than a second clone.** The problem is not disk: two sessions in one checkout
# overwrite each other's `frontend/main/.env.local`, race on `dist/`, and — the expensive one — share
# a test database whose `pg_trgm` extension cannot be shared. A worktree gives each session its own
# working tree while keeping one `.git`, so branches and objects stay in one place.
#
# **The database is the part that matters.** `tests/app/postgres_fixture.py` already isolates each
# test in its own schema, so schemas were never the problem: `0020_pg_trgm` creates the extension
# unqualified, into whichever schema is first on `search_path`, and an extension is database-wide. The
# second concurrent run's `IF NOT EXISTS` finds it and skips, and its schema then cannot see
# `gin_trgm_ops`. Every failure looked like a regression. The Makefile derives a database name per
# worktree, which is the level at which the extension actually is isolated.
#
#     make worktree NAME=512-some-branch
#
set -euo pipefail

cd "$(dirname "$0")/.."
NAME="$1"
ROOT="$(git rev-parse --show-toplevel)"
DEST="$ROOT/.claude/worktrees/$NAME"

if [ -d "$DEST" ]; then
  echo "  $DEST already exists — 'cd' into it, or 'git worktree remove' it first." >&2
  exit 1
fi

# A new branch off the current HEAD unless one of that name already exists, in which case check it
# out. Guessing wrongly either way costs somebody their work.
if git show-ref --verify --quiet "refs/heads/$NAME"; then
  git worktree add "$DEST" "$NAME"
else
  git worktree add -b "$NAME" "$DEST"
fi

# `.env` is gitignored, so a worktree starts without one and the API refuses to start. Copied rather
# than symlinked: the database URL inside it has to differ per worktree, which a symlink could not do.
if [ -f "$ROOT/.env" ]; then
  DEMO_DB="$(make --no-print-directory -C "$DEST" demo-env | sed -n 's/^DEMO_DB="\(.*\)"$/\1/p')"
  sed "s#/gv\$#/$DEMO_DB#" "$ROOT/.env" > "$DEST/.env"
  echo "  .env copied, pointed at $DEMO_DB"
fi

echo
make --no-print-directory -C "$DEST" where
cat <<EOF

  cd $DEST
  make check        # runs against its own test database
  make demo         # its own ports, its own data

EOF
