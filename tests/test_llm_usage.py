"""Tests for provider-neutral LLM usage records and aggregates."""

from __future__ import annotations

from core.llm_usage import (
    aggregate_llm_usage,
    aggregate_llm_usage_by_agent,
    empty_llm_usage_summary,
    merge_llm_usage_summaries,
    sanitize_llm_usage_record,
)


def _event(**overrides) -> dict:
    event = {
        "provider": "codex",
        "model": "gpt-5.3-codex",
        "operation": "run_agent",
        "attempt": 1,
        "max_attempts": 4,
        "outcome": "success",
        "elapsed_s": 1.2345,
        "input_chars": 120,
        "output_chars": 30,
    }
    event.update(overrides)
    return event


def _record(**overrides) -> dict:
    event = _event(**overrides)
    record = sanitize_llm_usage_record(event, agent="implementer")
    assert record is not None
    return record


def test_sanitize_llm_usage_record_keeps_only_bounded_metadata() -> None:
    record = sanitize_llm_usage_record(
        {
            "provider": "codex",
            "model": "/Users/alice/private-model",
            "operation": "generate",
            "attempt": 1,
            "max_attempts": 1,
            "outcome": "success",
            "elapsed_s": 0.1256,
            "input_chars": 20,
            "output_chars": 5,
            "reported_tokens": {
                "input_tokens": 7,
                "output_tokens": 3,
                "cached_input_tokens": 2,
                "cache_creation_input_tokens": 1,
                "total_tokens": 13,
                "secret": "PRIVATE_PROVIDER_OUTPUT",
            },
            "prompt": "PRIVATE_PROMPT",
            "output": "PRIVATE_PROVIDER_OUTPUT",
        },
        agent="planner",
    )

    assert record == {
        "agent": "planner",
        "provider": "codex",
        "model": "unknown",
        "operation": "generate",
        "attempt": 1,
        "max_attempts": 1,
        "outcome": "success",
        "elapsed_s": 0.126,
        "input_chars": 20,
        "output_chars": 5,
        "reported_tokens": {
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_input_tokens": 2,
            "cache_creation_input_tokens": 1,
            "total_tokens": 13,
        },
    }


def test_sanitize_llm_usage_record_rejects_cross_platform_paths_and_urls() -> None:
    windows_path = _record(model="C:/Users/alice/private-model")
    url = _record(provider="https://provider.example/private")

    assert windows_path["model"] == "unknown"
    assert url["provider"] == "unknown"


def test_sanitize_llm_usage_record_preserves_supported_display_model() -> None:
    record = _record(provider="antigravity", model="Gemini 3.5 Flash (High)")

    assert record["model"] == "Gemini 3.5 Flash (High)"


def test_sanitize_llm_usage_record_rejects_invalid_required_values() -> None:
    assert sanitize_llm_usage_record({}, agent="planner") is None
    assert sanitize_llm_usage_record(_event(attempt=2, max_attempts=1), agent="planner") is None
    assert sanitize_llm_usage_record(_event(outcome="maybe"), agent="planner") is None


def test_aggregate_llm_usage_tracks_known_and_unknown_evidence() -> None:
    records = [
        _record(reported_tokens={"input_tokens": 10, "output_tokens": 4}),
        _record(
            attempt=2,
            outcome="timeout",
            elapsed_s=2.0,
            input_chars=120,
            output_chars=None,
            error_type="TimeoutExpired",
        ),
    ]

    summary = aggregate_llm_usage(records)

    assert summary == {
        "attempts": 2,
        "successful_attempts": 1,
        "failed_attempts": 1,
        "timeout_attempts": 1,
        "elapsed_s": 3.234,
        "input_chars": 240,
        "output_chars": 30,
        "output_chars_known_attempts": 1,
        "reported_token_attempts": 1,
        "reported_tokens": {"input_tokens": 10, "output_tokens": 4},
    }


def test_aggregate_llm_usage_by_agent_and_merge_are_deterministic() -> None:
    planner = _record(operation="generate")
    planner["agent"] = "planner"
    implementer = _record(operation="run_agent", reported_tokens={"input_tokens": 5})

    by_agent = aggregate_llm_usage_by_agent([implementer, planner])
    merged = merge_llm_usage_summaries(list(by_agent.values()))

    assert list(by_agent) == ["implementer", "planner"]
    assert merged["attempts"] == 2
    assert merged["input_chars"] == 240
    assert merged["reported_token_attempts"] == 1
    assert merged["reported_tokens"] == {"input_tokens": 5}


def test_empty_llm_usage_summary_represents_unknown_tokens() -> None:
    summary = empty_llm_usage_summary()

    assert summary["attempts"] == 0
    assert summary["reported_token_attempts"] == 0
    assert summary["reported_tokens"] == {}


def test_aggregates_treat_malformed_record_collections_as_unknown() -> None:
    assert aggregate_llm_usage(None) == empty_llm_usage_summary()
    assert aggregate_llm_usage_by_agent(None) == {}
