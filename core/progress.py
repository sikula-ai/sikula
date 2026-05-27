"""Progress heartbeat helpers for long-running pipeline operations."""

from __future__ import annotations

import logging
import threading
import time
from types import TracebackType

from core.state import StateStore, TaskState

log = logging.getLogger(__name__)


class ActiveOperationHeartbeat:
    """Persist and log a heartbeat while a blocking operation is running."""

    def __init__(
        self,
        store: StateStore,
        state: TaskState,
        *,
        phase: str,
        interval_s: int,
        agent: str | None = None,
        scope: str | None = None,
        message: str | None = None,
    ) -> None:
        self._store = store
        self._state = state
        self._phase = phase
        self._interval_s = interval_s
        self._agent = agent
        self._scope = scope
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_monotonic = 0.0
        self._active = False

    def __enter__(self) -> "ActiveOperationHeartbeat":
        if self._interval_s <= 0:
            return self
        self._active = True
        self._started_monotonic = time.monotonic()
        self._state.start_active_operation(
            self._phase,
            agent=self._agent,
            scope=self._scope,
            message=self._message,
            heartbeat_interval_seconds=self._interval_s,
        )
        self._store.save(self._state)
        self._thread = threading.Thread(
            target=self._run,
            name=f"sikula-heartbeat-{self._phase}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._active:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._state.clear_active_operation()
        self._store.update_active_operation(self._state.task_id, None)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            elapsed_s = max(0, int(time.monotonic() - self._started_monotonic))
            message = self._message or f"{self._phase} running"
            heartbeat = self._state.heartbeat_active_operation(message=message)
            if heartbeat is not None:
                self._store.update_active_operation(self._state.task_id, heartbeat)
            log.info("Still running: %s (%s)", self._label(), _fmt_elapsed(elapsed_s))

    def _label(self) -> str:
        if self._agent:
            return self._agent
        if self._scope:
            return f"{self._scope} {self._phase}"
        return self._phase


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
