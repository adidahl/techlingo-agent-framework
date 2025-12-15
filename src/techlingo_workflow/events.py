from __future__ import annotations

from agent_framework import WorkflowEvent


class StageLogEvent(WorkflowEvent):
    """Simple human-readable progress event emitted by executors."""

    def __init__(self, message: str):
        super().__init__(message)


