from __future__ import annotations

import pytest

from scripts.check_pytest_shard_coverage import parse_node_ids, validate_partition


def test_parse_node_ids_ignores_collection_summary_and_warnings() -> None:
    output = """tests/api/test_items.py::test_create
tests/api/test_items.py::test_read[param]
================ 2 tests collected ================
WARNING: not a node id
"""
    assert parse_node_ids(output) == [
        "tests/api/test_items.py::test_create",
        "tests/api/test_items.py::test_read[param]",
    ]


def test_validate_partition_accepts_exact_disjoint_coverage() -> None:
    validate_partition(
        ["tests/test_a.py::test_a", "tests/test_b.py::test_b"],
        [["tests/test_a.py::test_a"], ["tests/test_b.py::test_b"]],
    )


@pytest.mark.parametrize(
    ("all_node_ids", "shards", "reason"),
    [
        (["tests/a.py::test_a"], [[], ["tests/a.py::test_a"]], "shard 1 is empty"),
        (
            ["tests/a.py::test_a"],
            [["tests/a.py::test_a"], ["tests/a.py::test_a"]],
            "duplicate=1",
        ),
        (
            ["tests/a.py::test_a", "tests/b.py::test_b"],
            [["tests/a.py::test_a"]],
            "missing=1",
        ),
        (
            ["tests/a.py::test_a"],
            [["tests/a.py::test_a", "tests/b.py::test_b"]],
            "unexpected=1",
        ),
    ],
)
def test_validate_partition_rejects_gaps_or_overlap(all_node_ids, shards, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        validate_partition(all_node_ids, shards)
