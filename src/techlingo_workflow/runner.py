"""Reusable workflow-run streamer.

The `course build` orchestrator runs the A0–A5 pipeline once per source file
and needs the same progress streaming the `run` command does. This is that
loop, extracted with an injectable `echo` so callers control output. The legacy
`run`/`analyze` commands keep their inline copies until they retire (Phase 1
migration ground rule: don't churn working legacy code).
"""

from __future__ import annotations

import time
from typing import Callable

from .models import PipelineState, WorkflowRunResult


def _get_executor_id(evt: object) -> str | None:
    return (
        getattr(evt, "executor_id", None)
        or getattr(evt, "executorId", None)
        or getattr(evt, "ExecutorId", None)
    )


async def stream_pipeline(
    workflow,
    state: PipelineState,
    *,
    echo: Callable[[str], None] = print,
    prefix: str = "",
) -> WorkflowRunResult:
    """Run the course workflow, echoing stage progress; return the final output.

    Raises RuntimeError when the workflow errors or finishes without output —
    callers decide whether that fails the whole build or just one source file.
    """
    output: WorkflowRunResult | None = None
    started_at: dict[str, float] = {}

    async for evt in workflow.run_stream(state):
        name = evt.__class__.__name__
        executor_id = _get_executor_id(evt)
        ts = time.strftime("%H:%M:%S")

        if name == "StageLogEvent":
            msg = getattr(evt, "message", None)
            if msg:
                echo(f"[{ts}] {prefix}{msg}")
        elif name in {"ExecutorInvokedEvent", "ExecutorInvokeEvent"} and executor_id:
            started_at[executor_id] = time.monotonic()
            echo(f"[{ts}] {prefix}START {executor_id}")
        elif name in {"ExecutorCompletedEvent", "ExecutorCompleteEvent"} and executor_id:
            dt = ""
            if executor_id in started_at:
                dt = f" ({time.monotonic() - started_at[executor_id]:.1f}s)"
            echo(f"[{ts}] {prefix}DONE  {executor_id}{dt}")
        elif name == "ExecutorFailedEvent" and executor_id:
            details = getattr(evt, "details", None)
            msg = getattr(details, "message", None) if details is not None else None
            echo(f"[{ts}] {prefix}FAIL  {executor_id}: {msg or 'unknown error'}")

        if name == "WorkflowOutputEvent":
            output = getattr(evt, "data", None)
        if name == "WorkflowErrorEvent":
            exc = getattr(evt, "exception", None)
            raise RuntimeError(str(exc) if exc is not None else "WorkflowErrorEvent")

    if output is None:
        raise RuntimeError("Workflow completed without WorkflowOutputEvent.")
    return output
