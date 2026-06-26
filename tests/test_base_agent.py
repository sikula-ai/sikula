"""Tests for agents/base_agent.py — shared helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.base_agent import AGENT_SECURITY_PREFIX, READONLY_AGENT_PREFIX, load_extra_rules, read_only_agent_prompt
from tools.base_tool import ToolResult


def _file_tool(content: str | None) -> MagicMock:
    tool = MagicMock()
    if content is None:
        tool.read.return_value = ToolResult(success=False, output="", error="not found")
    else:
        tool.read.return_value = ToolResult(success=True, output=content)
    return tool


class TestReadOnlyAgentPrompt:
    def test_inserts_readonly_constraint_after_security_prefix(self):
        prompt = AGENT_SECURITY_PREFIX + "Review this change."

        result = read_only_agent_prompt(prompt)

        assert result.startswith(AGENT_SECURITY_PREFIX)
        assert result[len(AGENT_SECURITY_PREFIX) :].startswith(READONLY_AGENT_PREFIX)
        assert "use project-relative paths" in result
        assert "file:// URIs" in result
        assert "Return the requested analysis, review, or generated content" in result
        assert result.endswith("Review this change.")

    def test_does_not_duplicate_readonly_constraint(self):
        prompt = AGENT_SECURITY_PREFIX + READONLY_AGENT_PREFIX + "Review this change."

        result = read_only_agent_prompt(prompt)

        assert result == prompt


class TestLoadExtraRules:
    def test_returns_empty_when_not_configured(self):
        assert load_extra_rules({}, "reviewer", _file_tool("some content")) == ""

    def test_returns_empty_when_file_tool_is_none(self):
        config = {"reviewer": {"extra_rules": "prompts/rules.md"}}
        assert load_extra_rules(config, "reviewer", None) == ""

    def test_returns_empty_when_file_read_fails(self):
        config = {"reviewer": {"extra_rules": "prompts/rules.md"}}
        assert load_extra_rules(config, "reviewer", _file_tool(None)) == ""

    def test_returns_empty_when_file_is_blank(self):
        config = {"reviewer": {"extra_rules": "prompts/rules.md"}}
        assert load_extra_rules(config, "reviewer", _file_tool("   \n")) == ""

    def test_returns_formatted_section_with_content(self):
        config = {"reviewer": {"extra_rules": "prompts/rules.md"}}
        result = load_extra_rules(config, "reviewer", _file_tool("Always check nulls."))
        assert "## Project-specific rules" in result
        assert "take priority" in result
        assert "Always check nulls." in result

    def test_reads_path_from_correct_agent_key(self):
        config = {
            "reviewer": {"extra_rules": "reviewer_rules.md"},
            "security_reviewer": {"extra_rules": "security_rules.md"},
        }
        tool = _file_tool("content")
        load_extra_rules(config, "security_reviewer", tool)
        tool.read.assert_called_once_with("security_rules.md")

    def test_section_appended_after_newlines(self):
        config = {"planner": {"extra_rules": "rules.md"}}
        result = load_extra_rules(config, "planner", _file_tool("Rule A."))
        assert result.startswith("\n\n## Project-specific rules")
