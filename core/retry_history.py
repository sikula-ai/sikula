"""Helpers for recording LLM retry events into task history."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from core.state import StateStore, TaskState


@contextmanager
def llm_retry_history(agent, agent_name: str, state: TaskState, store: StateStore) -> Iterator[None]:
    """Record provider retry and usage callbacks from an agent's LLM client."""
    llm = getattr(agent, "llm", None)
    previous_retry_observer = None
    previous_usage_observer = None

    def _record_llm_retry(event: dict[str, object]) -> None:
        result = str(event.get("error") or "LLM retry")
        state.record(agent_name, "llm_retry", result)
        state.history[-1].update(
            {
                "provider": event.get("provider"),
                "model": event.get("model"),
                "operation": event.get("operation"),
                "attempt": event.get("attempt"),
                "max_attempts": event.get("max_attempts"),
                "delay_s": event.get("delay_s"),
                "error_type": event.get("error_type"),
            }
        )
        store.save(state)

    def _record_llm_usage(event: dict[str, object]) -> None:
        state.record_llm_usage(agent_name, event)
        store.save(state)

    if llm is not None and hasattr(llm, "set_retry_observer"):
        previous_retry_observer = llm.set_retry_observer(_record_llm_retry)
    if llm is not None and hasattr(llm, "set_usage_observer"):
        previous_usage_observer = llm.set_usage_observer(_record_llm_usage)
    try:
        yield
    finally:
        if llm is not None and hasattr(llm, "set_usage_observer"):
            llm.set_usage_observer(previous_usage_observer)
        if llm is not None and hasattr(llm, "set_retry_observer"):
            llm.set_retry_observer(previous_retry_observer)
