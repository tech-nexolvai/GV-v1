"""Run the assistant's worker (#1871): the chat's messages and set packs the API queued.

With `GV_ASSISTANT_RUN_IN_WORKER=1` the API never runs a chat message (a full check above all) or
builds a set pack itself: it writes a job row and streams the job's progress from PostgreSQL. This
runs them, each in a process of its own. Start it with the API's own environment (the same database,
chat root, store, provider key and assistant settings): the job's process rebuilds each message
exactly as the API's route would.

Usage:

    python scripts/assistant_worker.py --watch                     # until SIGTERM / Ctrl-C
    python scripts/assistant_worker.py --watch --message-slots 2 --pack-slots 1

SIGTERM stores each running answer as interrupted (Continue) before it exits. The logic lives in
`app/review/assistant/job_worker.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.review.assistant.job_worker import main

if __name__ == "__main__":
    sys.exit(main())
