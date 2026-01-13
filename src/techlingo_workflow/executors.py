from __future__ import annotations

import json

from agent_framework import WorkflowContext, executor
from typing_extensions import Never

from .events import StageLogEvent
from .io import write_json
from .llm import LLMClient
from .models import Course, PipelineState, ValidationReport, WorkflowRunResult, TextAnalysisResult
from .prompts import (
    a1_modularizer_prompt,
    a2_scaffolder_prompt,
    a3_scenario_designer_prompt,
    a4_feedback_architect_prompt,
    analyzer_prompt,
    reviewer_prompt,
)
from .validate import repair_course_if_needed, validate_course


def _artifact_path(state: PipelineState, name: str) -> str:
    return f"{state.run_dir}/artifacts/{name}"


@executor(id="a1_modularizer")
async def a1_modularizer(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    await ctx.add_event(StageLogEvent("A1: starting modularizer (course map)"))
    llm = LLMClient(model_id=state.model_id, name="A1_Modularizer")
    await ctx.add_event(StageLogEvent("A1: calling LLM"))
    data = await llm.run_json(a1_modularizer_prompt(state.input_text, difficulty=state.difficulty, config=state.config, override_title=state.override_title))
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
    
    # Check for previous validation errors to pass for self-correction
    validation_issues = None
    if state.validation_report and not state.validation_report.ok:
         # Only pass errors, warnings don't trigger a retry usually
         validation_issues = [i.model_dump() for i in state.validation_report.issues if i.severity == "error"]
         await ctx.add_event(StageLogEvent(f"A2: self-correcting retry {state.retry_count}. Injecting {len(validation_issues)} errors."))

    await ctx.add_event(StageLogEvent("A2: calling LLM (this step can take a few minutes)"))
    data = await llm.run_json(a2_scaffolder_prompt(
        course_map_json, 
        difficulty=state.difficulty, 
        config=state.config,
        override_title=state.override_title,
        validation_issues=validation_issues
    ))
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
    data = await llm.run_json(a3_scenario_designer_prompt(course_json, difficulty=state.difficulty, config=state.config))
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
    data = await llm.run_json(a4_feedback_architect_prompt(course_json, difficulty=state.difficulty, config=state.config))
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
        state.a4_course, llm, state.config, max_repairs=1, source_text=state.input_text
    )
    repaired_course.difficulty = state.difficulty
    state.a5_course = repaired_course
    state.validation_report = report

    # Loop Logic: If invalid and we haven't maxed out retries, send back to A2
    MAX_RETRIES = 2
    if not report.ok and state.retry_count < MAX_RETRIES:
        state.retry_count += 1
        await ctx.add_event(StageLogEvent(f"A5: Validation failed (errors found). Looping back to A2 (Attempt {state.retry_count}/{MAX_RETRIES})."))
        # We DO NOT yield output here. We loop back.
        # The edges in workflow.py will handle the routing, but we need to ensure we don't proceed to 'yield_output'.
        await ctx.send_message(state)
        return

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



@executor(id="text_analyzer")
async def text_analyzer(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    await ctx.add_event(StageLogEvent("Analyzer: starting text analysis"))
    llm = LLMClient(model_id=state.model_id, name="Text_Analyzer")
    
    await ctx.add_event(StageLogEvent("Analyzer: calling LLM"))
    data = await llm.run_json(analyzer_prompt(state.input_text))
    
    await ctx.add_event(StageLogEvent("Analyzer: received LLM response, parsing"))
    result = TextAnalysisResult.model_validate(data)
    state.analysis_result = result
    
    write_json(_artifact_path(state, "analysis_initial.json"), result.model_dump(mode="json"))
    await ctx.add_event(StageLogEvent("Analyzer: done, forwarding to Reviewer"))
    await ctx.send_message(state)


@executor(id="text_reviewer")
async def text_reviewer(state: PipelineState, ctx: WorkflowContext[Never, TextAnalysisResult]) -> None:
    if state.analysis_result is None:
        raise RuntimeError("Reviewer requires analysis result.")
        
    await ctx.add_event(StageLogEvent("Reviewer: starting review"))
    llm = LLMClient(model_id=state.model_id, name="Text_Reviewer")
    
    current_json = state.analysis_result.model_dump_json(indent=2)
    
    await ctx.add_event(StageLogEvent("Reviewer: calling LLM to check content"))
    data = await llm.run_json(reviewer_prompt(state.input_text, current_json))
    
    await ctx.add_event(StageLogEvent("Reviewer: received LLM response, parsing"))
    final_result = TextAnalysisResult.model_validate(data)
    state.analysis_result = final_result
    
    await ctx.add_event(StageLogEvent("Reviewer: writing final artifact"))
    write_json(_artifact_path(state, "analysis_final.json"), final_result.model_dump(mode="json"))
    
    # Also write a text summary as requested
    summary_path = f"{state.run_dir}/analysis_summary.txt"
    with open(summary_path, "w") as f:
        f.write(f"Analysis Summary for: {final_result.input_summary}\n")
        f.write(f"Completeness Score: {final_result.metadata.completeness_score}\n")
        f.write(f"Estimated Questions: {final_result.metadata.estimated_questions_needed}\n")
        f.write("--------------------------------------------------\n")
        f.write(f"Terms: {final_result.metadata.parts_by_type.get('term', 0)}\n")
        f.write(f"Definitions: {final_result.metadata.parts_by_type.get('definition', 0)}\n")
        f.write(f"Explanations: {final_result.metadata.parts_by_type.get('explanation', 0)}\n")
        f.write(f"Examples: {final_result.metadata.parts_by_type.get('example', 0)}\n")
        f.write(f"Analogies: {final_result.metadata.parts_by_type.get('analogy', 0)}\n")
        f.write(f"Subjects: {final_result.metadata.parts_by_type.get('subject', 0)}\n")
        f.write("--------------------------------------------------\n")
        f.write("Recommended Configuration:\n")
        f.write(json.dumps(final_result.recommended_config.model_dump(mode="json"), indent=2))
        f.write("\n")
        f.write("\n--- Parts Details ---\n")
        for part in final_result.parts:
            f.write(f"[{part.type.upper()}] {part.content}\n")
            if part.context:
                f.write(f"  Context: {part.context}\n")
            f.write("\n")
            
    await ctx.add_event(StageLogEvent(f"Reviewer: Summary written to {summary_path}"))
    
    await ctx.add_event(StageLogEvent("Reviewer: done, emitting final output"))
    await ctx.yield_output(final_result)
