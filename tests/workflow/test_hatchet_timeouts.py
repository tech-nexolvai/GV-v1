"""Every Hatchet task states its own timeouts; none relies on the SDK's defaults (#1381).

The SDK cancels a task that has not started within 5 minutes or not finished within 60 seconds,
unless the task states otherwise. A drawing set's reading takes most of an hour, so under the
defaults it would be cancelled part way. These tests build the real worker against a recording
client (no engine) and read what each task was registered with.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from workflow.review import (
    QUEUED_PACKAGE_WAITS_UP_TO,
    STAGE_TAKES_UP_TO,
    WORKFLOW_NAME,
    stage_order,
)
from workflow.run_checks_worker import CHECK_RUN_TAKES_UP_TO, QUEUED_RUN_WAITS_UP_TO
from workflow.signed_export_worker import SIGNED_EXPORT_TAKES_UP_TO, SIGNED_EXPORT_WAITS_UP_TO

#: The SDK's defaults (`hatchet_sdk/runnables/workflow.py:Workflow.task`).
SDK_SCHEDULE_DEFAULT = timedelta(minutes=5)
SDK_EXECUTION_DEFAULT = timedelta(seconds=60)


class _Recorder:
    """Enough of a Hatchet client (and of a workflow) to register against."""

    def __init__(self) -> None:
        self.workflows: list[_Recorder] = []
        self.workflow_kwargs: dict[str, object] = {}
        self.tasks: list[dict[str, object]] = []
        self.worker_call: dict[str, object] | None = None

    def workflow(self, **kwargs: object) -> _Recorder:
        declared = _Recorder()
        declared.workflow_kwargs = kwargs
        self.workflows.append(declared)
        return declared

    def task(self, **kwargs: object) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        self.tasks.append(kwargs)
        return lambda fn: fn

    def worker(self, *args: object, **kwargs: object) -> None:
        del args
        self.worker_call = kwargs


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, object]]]:
    """Each workflow the Hatchet worker registers, by name, with the tasks it declared."""
    from app.config import Settings
    from workflow import hatchet_app

    client = _Recorder()
    monkeypatch.setattr(hatchet_app, "hatchet_client", lambda _settings: client)
    settings = Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://gv:gv@localhost/gv",
        hatchet_token="a-token",
        _env_file=None,
    )
    hatchet_app.build_worker(settings, factory=object())  # type: ignore[arg-type]
    assert client.worker_call is not None
    return {
        str(workflow.workflow_kwargs["name"]): workflow.tasks
        for workflow in client.worker_call["workflows"]  # type: ignore[attr-defined]
    }


def test_no_hatchet_task_relies_on_the_sdk_s_timeouts(
    registered: dict[str, list[dict[str, object]]],
) -> None:
    """Any task, including one added later, must state both limits, above the defaults and finite."""
    assert set(registered) == {WORKFLOW_NAME, "generate_signed_exports", "run_checks"}
    for workflow, tasks in registered.items():
        assert tasks, f"{workflow} declared no task"
        for task in tasks:
            where = f"{workflow}.{task['name']}"
            for setting, default in (
                ("schedule_timeout", SDK_SCHEDULE_DEFAULT),
                ("execution_timeout", SDK_EXECUTION_DEFAULT),
            ):
                assert setting in task, f"{where} falls back to the SDK's {setting} of {default}"
                value = task[setting]
                assert isinstance(value, timedelta), f"{where}: {setting} is not a timedelta"
                assert default < value <= timedelta(days=1), f"{where}: {setting} is {value}"


def test_each_package_review_stage_has_its_stated_limits(
    registered: dict[str, list[dict[str, object]]],
) -> None:
    tasks = registered[WORKFLOW_NAME]
    assert [task["name"] for task in tasks] == list(stage_order())
    assert set(STAGE_TAKES_UP_TO) == set(
        stage_order()
    ), "a stage with no limit, or a limit for none"
    assert QUEUED_PACKAGE_WAITS_UP_TO == timedelta(hours=24)
    assert dict(STAGE_TAKES_UP_TO) == {
        "ingest": timedelta(minutes=30),
        "extract_pages": timedelta(hours=4),
        "match": timedelta(minutes=30),
        "validate_evidence": timedelta(hours=1),
        "run_checks": timedelta(minutes=30),
        "generate_outputs": timedelta(minutes=30),
    }
    for task in tasks:
        assert task["schedule_timeout"] == QUEUED_PACKAGE_WAITS_UP_TO
        assert task["execution_timeout"] == STAGE_TAKES_UP_TO[str(task["name"])]
    # The reading stage is the long one: it must outlast the 13 Oct reading (55 min) several times.
    assert STAGE_TAKES_UP_TO["extract_pages"] >= 4 * timedelta(minutes=55)
    # The checks stage runs the same work as a check run on its own, so it gets the same limit.
    assert STAGE_TAKES_UP_TO["run_checks"] == CHECK_RUN_TAKES_UP_TO


def test_signed_export_publication_has_its_stated_limits(
    registered: dict[str, list[dict[str, object]]],
) -> None:
    (task,) = registered["generate_signed_exports"]
    assert task["name"] == "publish_signed_exports"
    assert task["schedule_timeout"] == SIGNED_EXPORT_WAITS_UP_TO == timedelta(hours=12)
    assert task["execution_timeout"] == SIGNED_EXPORT_TAKES_UP_TO == timedelta(minutes=30)


def test_run_checks_keeps_its_limits(registered: dict[str, list[dict[str, object]]]) -> None:
    (task,) = registered["run_checks"]
    assert task["schedule_timeout"] == QUEUED_RUN_WAITS_UP_TO
    assert task["execution_timeout"] == CHECK_RUN_TAKES_UP_TO
