from __future__ import annotations

import json

from agent_framework import WorkflowContext, executor
from typing_extensions import Never

from .events import StageLogEvent
from .io import write_json
from .llm import LLMClient
from .models import Course, PipelineState, ValidationReport, WorkflowRunResult
from .prompts import (
    a1_modularizer_prompt,
    a2_scaffolder_prompt,
    a3_scenario_designer_prompt,
    a4_feedback_architect_prompt,
)
from .validate import repair_course_if_needed, validate_course


def _artifact_path(state: PipelineState, name: str) -> str:
    return f"{state.run_dir}/artifacts/{name}"


@executor(id="a1_modularizer")
async def a1_modularizer(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    await ctx.add_event(StageLogEvent("A1: starting modularizer (course map)"))
    llm = LLMClient(model_id=state.model_id, name="A1_Modularizer")
    await ctx.add_event(StageLogEvent("A1: calling LLM"))
    data = await llm.run_json(a1_modularizer_prompt(state.input_text, difficulty=state.difficulty))
    await ctx.add_event(StageLogEvent("A1: received LLM response, writing artifact"))
    state.a1_course_map = data
    write_json(_artifact_path(state, "a1_course_map.json"), data)
    await ctx.add_event(StageLogEvent("A1: done, forwarding to A2"))
    await ctx.send_message(state)


@executor(id="a2_scaffolder")
async def a2_scaffolder(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    if state.a1_course_map is None:
        raise RuntimeError("A2 requires A1 course map.")
    await ctx.add_event(StageLogEvent("A2: starting scaffolder (8 exercises per lesson)"))
    llm = LLMClient(model_id=state.model_id, name="A2_Scaffolder")
    course_map_json = json.dumps(state.a1_course_map, ensure_ascii=False, indent=2)
    await ctx.add_event(StageLogEvent("A2: calling LLM (this step can take a few minutes)"))
    data = await llm.run_json(a2_scaffolder_prompt(course_map_json, difficulty=state.difficulty))
    await ctx.add_event(StageLogEvent("A2: received LLM response, validating schema"))
    course = Course.model_validate(data)
    course.difficulty = state.difficulty
    state.a2_course = course
    await ctx.add_event(StageLogEvent("A2: writing artifact, forwarding to A3"))
    write_json(_artifact_path(state, "a2_course.json"), course.model_dump(mode="json"))
    await ctx.send_message(state)


@executor(id="a3_scenario_designer")
async def a3_scenario_designer(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    if state.a2_course is None:
        raise RuntimeError("A3 requires A2 course.")
    await ctx.add_event(StageLogEvent("A3: starting scenario designer (make L3/L4 scenario-based)"))
    llm = LLMClient(model_id=state.model_id, name="A3_ScenarioDesigner")
    course_json = state.a2_course.model_dump_json(indent=2)
    await ctx.add_event(StageLogEvent("A3: calling LLM"))
    data = await llm.run_json(a3_scenario_designer_prompt(course_json, difficulty=state.difficulty))
    await ctx.add_event(StageLogEvent("A3: received LLM response, validating schema"))
    course = Course.model_validate(data)
    course.difficulty = state.difficulty
    state.a3_course = course
    await ctx.add_event(StageLogEvent("A3: writing artifact, forwarding to A4"))
    write_json(_artifact_path(state, "a3_course.json"), course.model_dump(mode="json"))
    await ctx.send_message(state)


@executor(id="a4_feedback_architect")
async def a4_feedback_architect(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    if state.a3_course is None:
        raise RuntimeError("A4 requires A3 course.")
    await ctx.add_event(StageLogEvent("A4: starting feedback architect (paired feedback for distractors)"))
    llm = LLMClient(model_id=state.model_id, name="A4_FeedbackArchitect")
    course_json = state.a3_course.model_dump_json(indent=2)
    await ctx.add_event(StageLogEvent("A4: calling LLM"))
    data = await llm.run_json(a4_feedback_architect_prompt(course_json, difficulty=state.difficulty))
    await ctx.add_event(StageLogEvent("A4: received LLM response, validating schema"))
    course = Course.model_validate(data)
    course.difficulty = state.difficulty
    state.a4_course = course
    await ctx.add_event(StageLogEvent("A4: writing artifact, forwarding to A5"))
    write_json(_artifact_path(state, "a4_course.json"), course.model_dump(mode="json"))
    await ctx.send_message(state)


@executor(id="a5_validator")
async def a5_validator(state: PipelineState, ctx: WorkflowContext[Never, WorkflowRunResult]) -> None:
    if state.a4_course is None:
        raise RuntimeError("A5 requires A4 course.")

    # Deterministic validation + optional repair
    llm = LLMClient(model_id=state.model_id, name="A5_ValidatorRepair")
    await ctx.add_event(StageLogEvent("A5: validating output + repairing if needed"))
    repaired_course, report = await repair_course_if_needed(
        state.a4_course, llm, max_repairs=1, source_text=state.input_text
    )
    repaired_course.difficulty = state.difficulty
    state.a5_course = repaired_course
    state.validation_report = report

    await ctx.add_event(StageLogEvent("A5: writing final artifacts"))
    write_json(_artifact_path(state, "a5_course.json"), repaired_course.model_dump(mode="json"))
    write_json(_artifact_path(state, "validation_report.json"), report.model_dump())

    # Emit final workflow output
    await ctx.add_event(StageLogEvent("A5: done, emitting final output"))
    await ctx.yield_output(
        WorkflowRunResult(
            run_id=state.run_id,
            run_dir=state.run_dir,
            course=repaired_course,
            validation_report=report,
        )
    )


