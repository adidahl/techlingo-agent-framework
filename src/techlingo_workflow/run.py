from __future__ import annotations

from typing import AsyncIterator, Optional

from agent_framework import WorkflowOutputEvent

from .config import WorkflowConfig
from .io import new_run_dir
from .models import PipelineState, WorkflowRunResult
from .workflow import build_techlingo_workflow


async def run_pipeline(
    *,
    input_text: str,
    out_dir: str,
    model_id: str,
    difficulty,
    config: WorkflowConfig | None = None,
) -> WorkflowRunResult:
    workflow = build_techlingo_workflow()
    run_id, run_dir = new_run_dir(out_dir)
    state = PipelineState(
        run_id=run_id,
        run_dir=str(run_dir),
        input_text=input_text,
        model_id=model_id,
        difficulty=difficulty,
        config=config or WorkflowConfig(),
    )

    output: Optional[WorkflowRunResult] = None
    async for event in workflow.run_stream(state):
        if isinstance(event, WorkflowOutputEvent):
            output = event.data

    if output is None:
        raise RuntimeError("Workflow completed without WorkflowOutputEvent.")
    return output


async def run_pipeline_stream(
    *,
    input_text: str,
    out_dir: str,
    model_id: str,
    difficulty,
    config: WorkflowConfig | None = None,
) -> AsyncIterator[object]:
    workflow = build_techlingo_workflow()
    run_id, run_dir = new_run_dir(out_dir)
    state = PipelineState(
        run_id=run_id,
        run_dir=str(run_dir),
        input_text=input_text,
        model_id=model_id,
        difficulty=difficulty,
        config=config or WorkflowConfig(),
    )
    async for event in workflow.run_stream(state):
        yield event

