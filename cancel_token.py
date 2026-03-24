"""Lightweight cancellation token passed through agent state."""

from __future__ import annotations

import threading


# Thread-local storage: each graph worker thread stores its token here so
# agent nodes can call raise_if_cancelled() without needing it in AgentState.
_local = threading.local()


def set_current_token(token: CancelToken) -> None:
    """Bind a token to the current thread (called by the graph worker)."""
    _local.token = token


def get_current_token() -> CancelToken | None:
    """Return the cancel token bound to the current thread, or None if unset."""
    return getattr(_local, "token", None)


def raise_if_cancelled() -> None:
    """Check the current thread's token and raise if cancelled. No-op if unset."""
    token: CancelToken | None = getattr(_local, "token", None)
    if token is not None:
        token.raise_if_cancelled()


class CancelToken:
    """Thread-safe cancel flag shared across all agent nodes for a run."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Mark this token as cancelled."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """Return True when cancellation has been requested."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise RunCancelledError if the token has been cancelled."""
        if self._event.is_set():
            raise RunCancelledError("Run was cancelled by user.")


class RunCancelledError(Exception):
    """Raised inside a node when the run has been cancelled."""
