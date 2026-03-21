"""Lightweight cancellation token passed through agent state."""

from __future__ import annotations

import threading


class CancelToken:
    """Thread-safe cancel flag shared across all agent nodes for a run."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelledError("Run was cancelled by user.")


class RunCancelledError(Exception):
    """Raised inside a node when the run has been cancelled."""
