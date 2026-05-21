"""Tests for agents/init_agent.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agents.base_agent import AGENT_SECURITY_PREFIX
from agents.init_agent import InitAgent


class TestInitAgent:
    def _make_agent(self, output: str = "# Guidelines\n- use snake_case") -> tuple[InitAgent, MagicMock]:
        llm = MagicMock()
        llm.run_readonly_agent.return_value = output
        agent = InitAgent(llm, "Python")
        return agent, llm

    def test_calls_run_readonly_agent(self, tmp_path: Path):
        agent, llm = self._make_agent()
        agent.generate_guidelines(tmp_path)
        llm.run_readonly_agent.assert_called_once()

    def test_passes_project_root_as_cwd(self, tmp_path: Path):
        agent, llm = self._make_agent()
        agent.generate_guidelines(tmp_path)
        _, kwargs = llm.run_readonly_agent.call_args
        assert kwargs.get("cwd") == tmp_path or llm.run_readonly_agent.call_args[0][1] == tmp_path

    def test_prompt_contains_tech_stack(self, tmp_path: Path):
        agent, llm = self._make_agent()
        agent.generate_guidelines(tmp_path)
        prompt_sent = llm.run_readonly_agent.call_args[0][0]
        assert "Python" in prompt_sent

    def test_prompt_contains_system_and_user(self, tmp_path: Path):
        agent, llm = self._make_agent()
        agent.generate_guidelines(tmp_path)
        prompt_sent = llm.run_readonly_agent.call_args[0][0]
        assert "conventions" in prompt_sent.lower()
        assert "guidelines.md" in prompt_sent

    def test_prompt_requires_stable_first_heading(self, tmp_path: Path):
        agent, llm = self._make_agent()
        agent.generate_guidelines(tmp_path)
        prompt_sent = llm.run_readonly_agent.call_args[0][0]
        assert "The first line of your response must be exactly: # Development Guidelines" in prompt_sent

    def test_returns_llm_output(self, tmp_path: Path):
        expected = "# My Guidelines\n- rule one"
        agent, llm = self._make_agent(output=expected)
        result = agent.generate_guidelines(tmp_path)
        assert result == expected

    def test_strips_leading_agent_progress_from_output(self, tmp_path: Path):
        output = """\
I’ll inspect the project structure first.
I found README.md and source files.
# Not the generated guidelines yet
This is still progress text.
# Development Guidelines

## Project Structure
- Keep source files in src/.
"""
        agent, llm = self._make_agent(output=output)
        result = agent.generate_guidelines(tmp_path)
        assert result == "# Development Guidelines\n\n## Project Structure\n- Keep source files in src/."

    def test_falls_back_to_first_markdown_heading(self, tmp_path: Path):
        output = """\
Progress line.
# Project Guidelines
- rule one
"""
        agent, llm = self._make_agent(output=output)
        result = agent.generate_guidelines(tmp_path)
        assert result == "# Project Guidelines\n- rule one"

    def test_strips_surrounding_whitespace_from_output_without_heading(self, tmp_path: Path):
        agent, llm = self._make_agent(output="\n  - rule one\n")
        result = agent.generate_guidelines(tmp_path)
        assert result == "- rule one"

    def test_different_tech_stack_in_prompt(self, tmp_path: Path):
        llm = MagicMock()
        llm.run_readonly_agent.return_value = "guidelines"
        agent = InitAgent(llm, "Kotlin/Android")
        agent.generate_guidelines(tmp_path)
        prompt_sent = llm.run_readonly_agent.call_args[0][0]
        assert "Kotlin/Android" in prompt_sent

    def test_security_prefix_prepended(self, tmp_path: Path):
        agent, llm = self._make_agent()
        agent.generate_guidelines(tmp_path)
        prompt_sent = llm.run_readonly_agent.call_args[0][0]
        assert prompt_sent.startswith(AGENT_SECURITY_PREFIX)
