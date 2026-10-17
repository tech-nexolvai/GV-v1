"""Every chat tool keeps one contract, and no prompt keeps a retired word (the prompt audit).

What the model sees of a tool is its description and its schema, so both are held to one shape:
the description says what the tool does, when to use it, what it returns and gives one short
example that is valid against the tool's own schema; every object in the schema is closed with its
`required` list; every field says what it means. The prompts use the reviewer's words (Wrong /
Your decision / Right / Not compared; Confirmed / Likely / AI reading only), never the old ones,
and name no tool that is gone or that their reader does not have.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from app.review.assistant.chat_engine import (
    COST_CAP_NOTE,
    FULL_CHECK_INSTRUCTION,
    FULL_CHECK_LEAD_INSTRUCTION,
    TURN_LIMIT_NOTE,
    WRAP_UP_NOTE,
)
from app.review.assistant.chat_findings import boxless_note, coverage_note, decision_help_note
from app.review.assistant.chat_tools import (
    FABLE_NOTE,
    HELPER_SYSTEM_PROMPT,
    HELPER_TOOL_NAMES,
    HELPER_TOOLS,
    LEAD_SYSTEM_PROMPT,
    LEAD_TOOLS,
)
from app.review.assistant.full_check_brief import checklist_send_back, helper_brief
from app.review.assistant.tool_schema import BOX_UNITS

SKILL = Path("app/review/assistant/skills/gv-review/SKILL.md")
_EXAMPLE = re.compile(r"Example: (\{.*\})\s*$", re.DOTALL)

#: Every tool either role can call, once each.
ALL_TOOLS: dict[str, Mapping[str, Any]] = {
    str(tool["name"]): tool for tool in (*LEAD_TOOLS, *HELPER_TOOLS)
}


def _objects(schema: Mapping[str, Any], where: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every object schema inside `schema` (itself included), with where it is."""
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if "object" in kinds:
        yield where, schema
        for name, field in (schema.get("properties") or {}).items():
            yield from _objects(field, f"{where}.{name}")
    if "array" in kinds and isinstance(schema.get("items"), Mapping):
        yield from _objects(schema["items"], f"{where}[]")


# ---- the tool contract ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_says_what_when_returns_and_one_valid_example(name: str) -> None:
    tool = ALL_TOOLS[name]
    words = str(tool["description"])
    assert len(words) >= 200, f"{name}: a description of {len(words)} characters says too little"
    assert re.search(r"\bUse (it|helpers|this)\b|\bUse it when\b", words), f"{name}: no 'Use ...'"
    assert "Returns" in words, f"{name}: does not say what it returns"
    found = _EXAMPLE.search(words)
    assert found, f"{name}: no example at the end of its description"
    example = json.loads(found.group(1))
    jsonschema.validate(example, tool["input_schema"])
    assert words.count("Example:") == 1, f"{name}: one example only"


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_schema_is_closed_with_required_fields_and_described_fields(name: str) -> None:
    schema = ALL_TOOLS[name]["input_schema"]
    jsonschema.Draft202012Validator.check_schema(schema)
    for where, obj in _objects(schema, name):
        assert obj.get("additionalProperties") is False, f"{where}: not closed"
        assert isinstance(obj.get("required"), list), f"{where}: no required list"
        properties = obj.get("properties") or {}
        assert set(obj["required"]) <= set(properties), f"{where}: requires an unknown field"
        for field_name, field in properties.items():
            assert str(field.get("description") or "").strip(), f"{where}.{field_name}: undescribed"
            if "enum" in field:
                assert field["enum"] and len(set(field["enum"])) == len(field["enum"]), field_name


def test_every_box_says_its_units_the_same_way() -> None:
    boxes = 0
    for name, tool in ALL_TOOLS.items():
        for where, obj in _objects(tool["input_schema"], name):
            if {"x0", "y0", "x1", "y1"} <= set(obj.get("properties") or {}):
                boxes += 1
                assert BOX_UNITS in str(obj.get("description")), where
                if where.endswith(("arch_box", "vendor_box")):
                    # #1550: a finding's box without a page takes the finding's page; say so.
                    assert "finding's" in obj["properties"]["page"]["description"], where
    assert boxes >= 6


def test_the_helpers_tool_list_is_named_the_same_everywhere() -> None:
    names = [str(tool["name"]) for tool in HELPER_TOOLS]
    assert list(HELPER_TOOL_NAMES) == names
    agent = next(tool for tool in LEAD_TOOLS if tool["name"] == "Agent")
    assert ", ".join(names) in str(agent["description"])


def test_no_tool_is_offered_twice_and_the_lead_has_every_helper_tool() -> None:
    lead = [str(tool["name"]) for tool in LEAD_TOOLS]
    assert len(lead) == len(set(lead))
    assert set(HELPER_TOOL_NAMES) <= set(lead)


# ---- the prompts' words --------------------------------------------------------------------------

#: The words and tools the prompts no longer use, with what replaced them.
RETIRED: dict[str, str] = {
    r"\bTo check\b": "the trust level is 'AI reading only'",
    r"\bNeeds checking\b": "the status is 'Your decision'",
    r"Download marked-up PDF": "the cloud drawing (made by code)",
    r"\bdelegate\b": "the Agent tool",
    r"`plan`|`bash`": "TaskCreate / bash_tool",
    r"pages/<file>/": "pages/<upload name without .pdf>/",
}


def _prompts() -> dict[str, str]:
    brief = helper_brief([3, 4], file_name="/mnt/user-data/uploads/set.pdf", review_document={})
    return {
        "lead system prompt": LEAD_SYSTEM_PROMPT,
        "helper system prompt": HELPER_SYSTEM_PROMPT,
        "full check": FULL_CHECK_INSTRUCTION,
        "full check, code-planned": FULL_CHECK_LEAD_INSTRUCTION,
        "helper brief": brief,
        "checklist send-back": checklist_send_back({3: "no CHECKLIST line"}) or "",
        "notes": "\n".join(
            [
                COST_CAP_NOTE,
                TURN_LIMIT_NOTE,
                WRAP_UP_NOTE,
                boxless_note([{"id": "F1", "page": 3, "item": "x", "missing": ["vendor_box"]}]),
                coverage_note([5, 6], "set.pdf"),
                decision_help_note([{"id": "F2", "page": 3, "item": "x"}]),
            ]
        ),
        "Fable note": FABLE_NOTE,
        "tools": json.dumps([tool["description"] for tool in ALL_TOOLS.values()]),
        "SKILL.md": SKILL.read_text(encoding="utf-8"),
    }


@pytest.mark.parametrize("pattern", sorted(RETIRED))
def test_no_prompt_uses_a_retired_word_or_tool(pattern: str) -> None:
    for where, text in _prompts().items():
        found = re.search(pattern, text)
        assert found is None, f"{where} says {found.group(0)!r}: use {RETIRED[pattern]}"


def test_the_prompts_use_the_reviewers_words() -> None:
    for where in ("lead system prompt", "SKILL.md"):
        text = _prompts()[where]
        for words in ("Wrong", "Your decision", "Right", "Not compared", "AI reading only"):
            assert words in text, (where, words)
    findings = next(tool for tool in LEAD_TOOLS if tool["name"] == "report_finding")
    status = findings["input_schema"]["properties"]["status"]["description"]
    for words in ("different = Wrong", "check = Your", "same = Right"):
        assert words in status


def test_a_helpers_prompts_name_only_tools_it_has() -> None:
    lead_only = {str(tool["name"]) for tool in LEAD_TOOLS} - set(HELPER_TOOL_NAMES)
    prompts = _prompts()
    for where in ("helper system prompt", "helper brief", "checklist send-back"):
        for name in lead_only:
            assert not re.search(rf"\b{re.escape(name)}\b", prompts[where]), (where, name)


def test_one_rule_for_checking_a_helpers_finding() -> None:
    """A helper's finding is a lead to verify unless the app lists it as backed: the same rule in
    the lead's prompt, the full check, the code-planned full check and SKILL.md."""
    prompts = _prompts()
    for where in ("lead system prompt", "full check", "SKILL.md"):
        assert "backed" in prompts[where], where
    assert "backed need no re-check" in prompts["full check, code-planned"]
    assert "Verify every lead" not in prompts["SKILL.md"]


def test_the_effort_scaling_is_the_same_everywhere() -> None:
    prompts = _prompts()
    agent = next(tool for tool in LEAD_TOOLS if tool["name"] == "Agent")
    for text in (prompts["SKILL.md"], str(agent["description"])):
        assert "3-5 pages per helper" in text
    for text in (prompts["lead system prompt"], prompts["SKILL.md"]):
        assert "1-3 tool calls" in text
    assert "only for whole-set work" in str(agent["description"])


# ---- the size budget -----------------------------------------------------------------------------


def test_the_prompts_stay_within_their_budgets() -> None:
    """Characters, measured at chat-v13 (the audit), with a little room. The tools grew on purpose
    (what each field means, when to use the tool, one example); the prompts shrank."""
    budgets = {
        "lead system prompt": 2600,
        "helper system prompt": 1400,
        "full check": 1600,
        "full check, code-planned": 1900,
        "Fable note": 400,
        "SKILL.md": 20000,
    }
    prompts = _prompts()
    for where, most in budgets.items():
        assert len(prompts[where]) <= most, (where, len(prompts[where]))
    # chat-v16 (#1558): the numbered dimension labels' fields (zoom marks and guides,
    # architect_dim / vendor_dim on report and update, cite's dim) took about 900 characters.
    assert len(json.dumps(list(LEAD_TOOLS))) <= 45_000
    assert len(json.dumps(list(HELPER_TOOLS))) <= 29_000
