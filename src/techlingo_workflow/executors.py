from __future__ import annotations

import json

from agent_framework import WorkflowContext, executor
from typing_extensions import Never

from .events import StageLogEvent
from .io import write_json
from .llm import LLMClient
from .models import ConceptAtom, Course, PipelineState, ValidationReport, WorkflowRunResult, TextAnalysisResult
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


def _analysis_inventory_json(state: PipelineState) -> str | None:
    """Compact JSON of the extracted content parts, for use as an A1 coverage checklist."""
    if state.analysis_result is None:
        return None
    parts = [{"type": p.type.value, "content": p.content} for p in state.analysis_result.parts]
    return json.dumps(parts, ensure_ascii=False, indent=2)


def _seed_concepts_from_map(course: Course, a1_map: dict) -> None:
    """Copy per-lesson concepts from the A1 map into the course when A2 dropped them.

    Index-aligned: A2 must mirror the map's module/lesson structure, so position
    is the join key. Best-effort — mismatched shapes just skip.
    """
    map_modules = a1_map.get("modules") or []
    for mi, module in enumerate(course.modules):
        if mi >= len(map_modules):
            break
        map_lessons = map_modules[mi].get("lessons") or []
        for li, lesson in enumerate(module.lessons):
            if li >= len(map_lessons):
                break
            if lesson.concepts:
                continue
            raw = map_lessons[li].get("concepts") or []
            try:
                lesson.concepts = [ConceptAtom.model_validate(c) for c in raw]
            except Exception:  # noqa: BLE001 - malformed map concepts must never sink the run
                continue


def _validate_a1_map(a1_map: dict, config) -> list[str]:
    """Deterministic structural check of the A1 course map.

    The self-correction loop re-runs A2, never A1, so a structurally wrong map
    (wrong module count, missing concepts) poisons every downstream attempt.
    Validate it immediately and retry A1 itself while it's still cheap.
    """
    problems: list[str] = []
    modules = a1_map.get("modules") or []
    if len(modules) != config.modules_count:
        problems.append(f"Course map must have EXACTLY {config.modules_count} modules; got {len(modules)}.")
    total_lessons = sum(len(m.get("lessons") or []) for m in modules)
    if not (config.min_lessons_total <= total_lessons <= config.max_lessons_total):
        problems.append(
            f"Course map must have {config.min_lessons_total}-{config.max_lessons_total} lessons in total; "
            f"got {total_lessons}."
        )
    # Meta/navigation content is not learnable material; a concept about the
    # course itself produces "What is this course about?"-style questions.
    meta_markers = (
        "this course",
        "this module",
        "this training",
        "this unit",
        "course overview",
        "module overview",
        "training module",
        "high-level overview",
    )
    # Mirror of the cap computed in a1_modularizer_prompt: a lesson with E
    # exercises cannot cover more than E concepts, and E-1 leaves one concept
    # room for a second exercise.
    max_concepts = max(2, min(6, config.exercises_per_lesson - 1))
    seen_concept_ids: set[str] = set()
    for mi, module in enumerate(modules):
        for li, lesson in enumerate(module.get("lessons") or []):
            concepts = lesson.get("concepts") or []
            if not (2 <= len(concepts) <= max_concepts):
                problems.append(
                    f"modules[{mi}].lessons[{li}] must define 2-{max_concepts} concepts; got {len(concepts)}."
                )
            for c in concepts:
                cid = (c.get("id") or "").strip()
                if not cid or not c.get("summary", "").strip():
                    problems.append(f"modules[{mi}].lessons[{li}] has a concept without id/summary.")
                elif cid in seen_concept_ids:
                    problems.append(f"Concept id '{cid}' is duplicated; ids must be unique across the course.")
                else:
                    seen_concept_ids.add(cid)
                meta_text = f"{cid} {c.get('label', '')} {c.get('summary', '')}".lower()
                if any(marker in meta_text for marker in meta_markers):
                    problems.append(
                        f"Concept '{cid or c.get('label', '?')}' is about the course/module itself. "
                        "Meta/navigation content is not learnable material — replace it with a "
                        "domain fact from the source."
                    )
    return problems


def _restore_concept_metadata(prev: Course, new: Course) -> None:
    """Re-attach concepts/concept_id that a rewrite stage (A3/A4) dropped.

    Those stages never add or remove lessons/exercises, so index alignment holds;
    the metadata is not content the rewrite should touch, making deterministic
    restoration safer than trusting the LLM to echo it back.
    """
    for pm, nm in zip(prev.modules, new.modules):
        for pl, nl in zip(pm.lessons, nm.lessons):
            if pl.concepts and not nl.concepts:
                nl.concepts = [c.model_copy(deep=True) for c in pl.concepts]
            for pe, ne in zip(pl.exercises, nl.exercises):
                if not ne.concept_id and pe.concept_id:
                    ne.concept_id = pe.concept_id


@executor(id="content_inventory")
async def content_inventory(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    """A0: extract the source's terms/definitions/examples before mapping the course.

    This inventory feeds A1 as a coverage checklist so lesson content packs don't
    silently drop source facts. Failure is non-fatal — the pipeline can still run
    from raw source text alone.
    """
    await ctx.add_event(StageLogEvent("A0: extracting content inventory (terms/definitions/examples)"))
    llm = LLMClient(model_id=state.model_id, name="Content_Inventory")
    try:
        data, result = await llm.run_json_model(analyzer_prompt(state.input_text), TextAnalysisResult)
        if "thought_process" in data and isinstance(data["thought_process"], list):
            thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
            await ctx.add_event(StageLogEvent(f"A0 Thought Process:\n{thought_str}"))
        state.analysis_result = result
        write_json(_artifact_path(state, "a0_content_inventory.json"), result.model_dump(mode="json"))
        await ctx.add_event(
            StageLogEvent(f"A0: inventory ready ({len(result.parts)} parts), forwarding to A1")
        )
    except Exception as e:  # noqa: BLE001 - inventory is an enhancer, not a gate
        await ctx.add_event(
            StageLogEvent(f"A0: inventory extraction failed ({type(e).__name__}); continuing without it")
        )
    await ctx.send_message(state)


@executor(id="a1_modularizer")
async def a1_modularizer(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    await ctx.add_event(StageLogEvent("A1: starting modularizer (course map + concept packs)"))
    llm = LLMClient(model_id=state.model_id, name="A1_Modularizer")

    base_prompt = a1_modularizer_prompt(
        state.input_text,
        difficulty=state.difficulty,
        config=state.config,
        override_title=state.override_title,
        analysis_json=_analysis_inventory_json(state),
    )

    # The A5 loop re-runs A2, never A1 — so the map must be structurally right
    # before anything downstream runs. Retry A1 with concrete feedback while a
    # bad map is still one cheap LLM call instead of a wasted multi-stage loop.
    # If no attempt comes back clean, keep the one with the FEWEST problems.
    MAP_ATTEMPTS = 3
    prompt = base_prompt
    data: dict = {}
    best_problem_count: int | None = None
    for attempt in range(1, MAP_ATTEMPTS + 1):
        await ctx.add_event(StageLogEvent(f"A1: calling LLM (map attempt {attempt}/{MAP_ATTEMPTS})"))
        candidate = await llm.run_json(prompt)
        problems = _validate_a1_map(candidate, state.config)
        if best_problem_count is None or len(problems) < best_problem_count:
            data = candidate
            best_problem_count = len(problems)
        if not problems:
            break
        problems_str = "\n".join(f"- {p}" for p in problems)
        await ctx.add_event(
            StageLogEvent(f"A1: map attempt {attempt} structurally invalid:\n{problems_str}")
        )
        prompt = (
            "CRITICAL: Your previous course map violated these STRUCTURAL requirements:\n"
            f"{problems_str}\n\n"
            "Produce a corrected course map that satisfies every requirement below EXACTLY.\n\n"
            f"{base_prompt}"
        )
    if best_problem_count:
        await ctx.add_event(
            StageLogEvent(
                f"A1: no fully valid map after {MAP_ATTEMPTS} attempts; using best one "
                f"({best_problem_count} problem(s))."
            )
        )
    await ctx.add_event(StageLogEvent("A1: received LLM response, writing artifact"))

    if "thought_process" in data and isinstance(data["thought_process"], list):
        thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
        await ctx.add_event(StageLogEvent(f"A1 Thought Process:\n{thought_str}"))

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
    data, course = await llm.run_json_model(a2_scaffolder_prompt(
        course_map_json,
        state.input_text,
        difficulty=state.difficulty,
        config=state.config,
        override_title=state.override_title,
        validation_issues=validation_issues
    ), Course)
    await ctx.add_event(StageLogEvent("A2: received LLM response, validating schema"))

    if "thought_process" in data and isinstance(data["thought_process"], list):
        thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
        await ctx.add_event(StageLogEvent(f"A2 Thought Process:\n{thought_str}"))

    course.difficulty = state.difficulty
    # If A2 forgot to echo the concept packs back, restore them from the A1 map so
    # downstream coverage validation still has ground truth.
    _seed_concepts_from_map(course, state.a1_course_map)
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
    data, course = await llm.run_json_model(
        a3_scenario_designer_prompt(course_json, state.input_text, difficulty=state.difficulty, config=state.config),
        Course,
    )
    await ctx.add_event(StageLogEvent("A3: received LLM response, validating schema"))

    if "thought_process" in data and isinstance(data["thought_process"], list):
        thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
        await ctx.add_event(StageLogEvent(f"A3 Thought Process:\n{thought_str}"))

    course.difficulty = state.difficulty
    _restore_concept_metadata(state.a2_course, course)
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
    data, course = await llm.run_json_model(
        a4_feedback_architect_prompt(course_json, state.input_text, difficulty=state.difficulty, config=state.config),
        Course,
    )
    await ctx.add_event(StageLogEvent("A4: received LLM response, validating schema"))

    if "thought_process" in data and isinstance(data["thought_process"], list):
        thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
        await ctx.add_event(StageLogEvent(f"A4 Thought Process:\n{thought_str}"))

    course.difficulty = state.difficulty
    _restore_concept_metadata(state.a3_course, course)
    state.a4_course = course
    await ctx.add_event(StageLogEvent("A4: writing artifact, forwarding to A5"))
    write_json(_artifact_path(state, "a4_course.json"), course.model_dump(mode="json"))
    await ctx.send_message(state)


@executor(id="a5_validator")
async def a5_validator(state: PipelineState, ctx: WorkflowContext[Never, WorkflowRunResult]) -> None:
    if state.a4_course is None:
        raise RuntimeError("A5 requires A4 course.")

    from .validate import attempt_badness, error_count

    # Deterministic validation + optional repair
    llm = LLMClient(model_id=state.model_id, name="A5_ValidatorRepair")
    await ctx.add_event(StageLogEvent("A5: validating output + repairing if needed"))
    repaired_course, report = await repair_course_if_needed(
        state.a4_course, llm, state.config, max_repairs=2, source_text=state.input_text
    )
    repaired_course.difficulty = state.difficulty
    state.a5_course = repaired_course
    state.validation_report = report

    # Track the best attempt across the self-correction loop: a regenerated
    # attempt can come back worse than the one it was meant to fix (e.g. losing
    # modules), so the final output is the best-scoring attempt seen. Ranked by
    # weighted severity (structural > shape > content), not raw error count.
    if state.best_report is None or attempt_badness(report) < attempt_badness(state.best_report):
        state.best_course = repaired_course
        state.best_report = report

    # Log repairs thought process if available
    if repaired_course.thought_process:
        thought_str = "\n".join([f"  > {t}" for t in repaired_course.thought_process])
        await ctx.add_event(StageLogEvent(f"A5 Repair Thought Process:\n{thought_str}"))

    # Log validation issues
    if not report.ok:
        issues_str = "\n".join([f"  - [{i.severity.upper()}] {i.path}: {i.message}" for i in report.issues])
        await ctx.add_event(StageLogEvent(f"A5 Validation Issues:\n{issues_str}"))
    else:
        await ctx.add_event(StageLogEvent("A5 Validation passed."))

    # Loop Logic: If invalid and we haven't maxed out retries, send back to A2
    MAX_RETRIES = 2
    if not report.ok and state.retry_count < MAX_RETRIES:
        state.retry_count += 1
        await ctx.add_event(StageLogEvent(f"A5: Validation failed (errors found). Looping back to A2 (Attempt {state.retry_count}/{MAX_RETRIES})."))
        # We DO NOT yield output here. We loop back.
        # The edges in workflow.py will handle the routing, but we need to ensure we don't proceed to 'yield_output'.
        await ctx.send_message(state)
        return

    final_course = state.best_course or repaired_course
    final_report = state.best_report or report
    if final_course is not repaired_course:
        await ctx.add_event(
            StageLogEvent(
                f"A5: last attempt had {error_count(report)} errors; keeping earlier best attempt "
                f"with {error_count(final_report)} errors instead."
            )
        )
    state.a5_course = final_course
    state.validation_report = final_report

    await ctx.add_event(StageLogEvent("A5: writing final artifacts"))
    write_json(_artifact_path(state, "a5_course.json"), final_course.model_dump(mode="json"))
    write_json(_artifact_path(state, "validation_report.json"), final_report.model_dump())

    # Emit final workflow output
    await ctx.add_event(StageLogEvent("A5: done, emitting final output"))
    await ctx.yield_output(
        WorkflowRunResult(
            run_id=state.run_id,
            run_dir=state.run_dir,
            course=final_course,
            validation_report=final_report,
        )
    )



@executor(id="text_analyzer")
async def text_analyzer(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    await ctx.add_event(StageLogEvent("Analyzer: starting text analysis"))
    llm = LLMClient(model_id=state.model_id, name="Text_Analyzer")
    
    await ctx.add_event(StageLogEvent("Analyzer: calling LLM"))
    data, result = await llm.run_json_model(analyzer_prompt(state.input_text), TextAnalysisResult)

    await ctx.add_event(StageLogEvent("Analyzer: received LLM response, parsing"))

    if "thought_process" in data and isinstance(data["thought_process"], list):
        thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
        await ctx.add_event(StageLogEvent(f"Analyzer Thought Process:\n{thought_str}"))

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
    data, final_result = await llm.run_json_model(reviewer_prompt(state.input_text, current_json), TextAnalysisResult)

    await ctx.add_event(StageLogEvent("Reviewer: received LLM response, parsing"))

    if "thought_process" in data and isinstance(data["thought_process"], list):
        thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
        await ctx.add_event(StageLogEvent(f"Reviewer Thought Process:\n{thought_str}"))

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
