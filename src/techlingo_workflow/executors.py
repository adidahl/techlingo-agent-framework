from __future__ import annotations

import asyncio
import json
import os

from agent_framework import WorkflowContext, executor
from typing_extensions import Never

from .events import StageLogEvent
from .io import write_json
from .llm import LLMClient
from .models import (
    ConceptAtom,
    Course,
    Lesson,
    LessonGen,
    Module,
    PipelineState,
    ValidationReport,
    WorkflowRunResult,
    TextAnalysisResult,
)
from .prompts import (
    a1_modularizer_prompt,
    a2_lesson_prompt,
    a3_lesson_prompt,
    a4_lesson_prompt,
    analyzer_prompt,
    reviewer_prompt,
)
from .validate import (
    a1_owns_validation_issue,
    concept_identity_meta_matches,
    concept_meta_reference_matches,
    issues_by_lesson,
    repair_course_if_needed,
    validate_course,
)
from .worksheet import (
    CELL_QUOTAS,
    DEFAULT_DEPTH,
    WorksheetBudgetError,
    assign_tf_answers,
    build_lesson_worksheet,
    worksheet_applies,
)


def _artifact_path(state: PipelineState, name: str) -> str:
    return f"{state.run_dir}/artifacts/{name}"


# Cap on simultaneous per-lesson LLM calls (rate-limit friendly, still ~Nx
# faster than sequential for full-course passes). Tune with
# TECHLINGO_MAX_CONCURRENCY (2-3 recommended for subscription CLI backends).
def _max_concurrent_lesson_calls() -> int:
    try:
        return max(1, int(os.getenv("TECHLINGO_MAX_CONCURRENCY", "4")))
    except ValueError:
        return 4


MAX_CONCURRENT_LESSON_CALLS = _max_concurrent_lesson_calls()


async def _gather_limited(coros: list) -> list:
    sem = asyncio.Semaphore(MAX_CONCURRENT_LESSON_CALLS)

    async def _guarded(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_guarded(c) for c in coros))


def _tf_answer_patterns(tf_per_lesson: int, num_lessons: int) -> list[list[bool]]:
    """Course-wide alternating true_false answer pattern, sliced per lesson.

    Dictating exact answers per lesson keeps the course balanced even though
    each lesson is generated in an isolated completion (starts with False
    because generators default to all-true).
    """
    total = tf_per_lesson * num_lessons
    seq = [i % 2 == 1 for i in range(total)]
    return [seq[i * tf_per_lesson : (i + 1) * tf_per_lesson] for i in range(num_lessons)]


def _lesson_to_plain(lesson: Lesson) -> Lesson:
    """LessonGen -> Lesson (drops the thought_process field from artifacts)."""
    return Lesson.model_validate(lesson.model_dump(exclude={"thought_process"}))


def _all_lesson_keys(course: Course) -> list[str]:
    return [f"{mi}:{li}" for mi, m in enumerate(course.modules) for li in range(len(m.lessons))]


def _failed_lesson_keys(report: ValidationReport | None) -> set[str] | None:
    """Lesson keys ("mi:li") with errors, or None when a full regeneration is needed.

    None is returned when any error is not attributable to a single lesson
    (e.g. wrong module count) — those can only be fixed by regenerating all.
    """
    if report is None:
        return None
    per_lesson = issues_by_lesson(report)
    lesson_error_paths = {p for issues in per_lesson.values() for i in issues if i.severity == "error" for p in [i.path]}
    keys = {k for k, issues in per_lesson.items() if any(i.severity == "error" for i in issues)}
    for issue in report.issues:
        if issue.severity == "error" and issue.path not in lesson_error_paths:
            return None
    return keys


def _analysis_inventory_json(state: PipelineState) -> str | None:
    """Compact JSON of the extracted content parts, for use as an A1 coverage checklist."""
    if state.analysis_result is None:
        return None
    parts = [{"type": p.type.value, "content": p.content} for p in state.analysis_result.parts]
    return json.dumps(parts, ensure_ascii=False, indent=2)


def _stamp_default_depths(a1_map: dict) -> None:
    """Fill missing/invalid concept depths with the default, in place.

    The A1 retry loop asks for a depth on every atom; this is the safety net
    that keeps the worksheet contract total even when the best surviving map
    is imperfect — every downstream consumer (worksheets, validation, graph)
    can then rely on depth being present.
    """
    for module in a1_map.get("modules") or []:
        for lesson in module.get("lessons") or []:
            for concept in lesson.get("concepts") or []:
                if isinstance(concept, dict) and concept.get("depth") not in CELL_QUOTAS:
                    concept["depth"] = DEFAULT_DEPTH


def _a1_meta_reference_problems(a1_map: dict) -> list[str]:
    """Find concept summaries that depend on source/curriculum exposition."""

    problems: list[str] = []
    for mi, module in enumerate(a1_map.get("modules") or []):
        for li, lesson in enumerate(module.get("lessons") or []):
            for ci, concept in enumerate(lesson.get("concepts") or []):
                if not isinstance(concept, dict):
                    continue
                concept_id = (concept.get("id") or concept.get("label") or "?").strip()
                identity_matches = concept_identity_meta_matches(
                    concept.get("id") or "", concept.get("label") or ""
                )
                matches = concept_meta_reference_matches(concept.get("summary") or "")
                if identity_matches:
                    field, span = identity_matches[0]
                    kind = "meta/navigation identity"
                    path = f"modules[{mi}].lessons[{li}].concepts[{ci}].{field}"
                elif matches:
                    kind, span = matches[0]
                    path = f"modules[{mi}].lessons[{li}].concepts[{ci}].summary"
                else:
                    continue
                problems.append(
                    f"{path}: "
                    f"Concept '{concept_id}' contains {kind} {span!r} and refers to "
                    "the course or source presentation instead of stating the fact "
                    "directly. Meta/navigation content is not learnable material — "
                    "replace it with a self-contained domain fact from the source."
                )
    return problems


def _seed_concepts_from_map(course: Course, a1_map: dict) -> None:
    """Restore each exact authoritative A1 concept pack after A2.

    Index-aligned: A2 must mirror the map's module/lesson structure, so position
    is the join key. Concept id/order/depth/summary/confusables define the
    worksheet and graph merge, so no model echo is allowed to mutate them.
    Best-effort malformed legacy maps still skip; exact-budget A1 maps fail
    closed before reaching this function.
    """
    map_modules = a1_map.get("modules") or []
    for mi, module in enumerate(course.modules):
        if mi >= len(map_modules):
            break
        map_lessons = map_modules[mi].get("lessons") or []
        for li, lesson in enumerate(module.lessons):
            if li >= len(map_lessons):
                break
            raw = map_lessons[li].get("concepts") or []
            try:
                lesson.concepts = [ConceptAtom.model_validate(c) for c in raw]
            except Exception:  # noqa: BLE001 - legacy malformed packs remain validator-owned
                continue


def _validate_a1_map(a1_map: dict, config) -> list[str]:
    """Deterministic structural check of the A1 course map.

    A structurally wrong map (wrong module count, missing concepts) poisons
    every downstream attempt. Validate it immediately and retry A1 itself while
    it is still cheap; later concept-level fidelity errors also route to A1.
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
            for ci, c in enumerate(concepts):
                concept_path = f"modules[{mi}].lessons[{li}].concepts[{ci}]"
                if not isinstance(c, dict):
                    problems.append(
                        f"{concept_path} must be an object matching the ConceptAtom schema."
                    )
                    continue
                try:
                    ConceptAtom.model_validate(c)
                except Exception as error:  # noqa: BLE001 - report the exact A1 schema failure
                    problems.append(
                        f"{concept_path} must match the ConceptAtom schema: {error}"
                    )
                cid = (c.get("id") or "").strip()
                if not cid or not c.get("summary", "").strip():
                    problems.append(f"{concept_path} has a concept without id/summary.")
                elif cid in seen_concept_ids:
                    problems.append(f"Concept id '{cid}' is duplicated; ids must be unique across the course.")
                else:
                    seen_concept_ids.add(cid)
                if not (c.get("label") or "").strip():
                    problems.append(f"{concept_path}.label must be a non-empty human-readable name.")
                if c.get("depth") not in CELL_QUOTAS:
                    problems.append(
                        f"Concept '{cid or c.get('label', '?')}' must declare \"depth\" as one of "
                        f"{sorted(CELL_QUOTAS)} (got {c.get('depth')!r}). Depth drives how many "
                        "exercises the concept gets per difficulty rung."
                    )
            budget = config.worksheet_items_per_lesson
            depths = [
                concept.get("depth")
                for concept in concepts
                if isinstance(concept, dict) and concept.get("depth") in CELL_QUOTAS
            ]
            if budget is not None and len(depths) == len(concepts):
                minimum = sum(len(CELL_QUOTAS[depth]) for depth in depths)
                maximum = sum(sum(CELL_QUOTAS[depth].values()) for depth in depths)
                if not (minimum <= budget <= maximum):
                    problems.append(
                        f"modules[{mi}].lessons[{li}] cannot satisfy worksheet item budget "
                        f"{budget}: complete rung coverage requires at least {minimum} rows "
                        f"and the full quota provides at most {maximum}. Regroup source-grounded "
                        "concepts across lessons without omitting facts or changing their semantic depth."
                    )
    problems.extend(_a1_meta_reference_problems(a1_map))
    return problems


def _validation_retry_target(report: ValidationReport, course: Course) -> str:
    """Return the earliest authoritative stage that can fix hard errors.

    Concept packs come from A1 and are deliberately restored after every A2-A5
    model echo. Sending a concept-level error to A2 therefore cannot converge;
    all other repairable content remains on the cheaper lesson-local A2 path.
    """

    if any(a1_owns_validation_issue(issue, course) for issue in report.issues):
        return "a1"
    return "a2"


def _reset_after_a1_retry(state: PipelineState) -> None:
    """Invalidate results derived from the superseded A1 concept authority."""

    state.a2_course = None
    state.a3_course = None
    state.a4_course = None
    state.a5_course = None
    state.validation_report = None
    state.best_course = None
    state.best_report = None
    state.dirty_lessons = None
    state.retry_target = None


def _restore_concept_metadata(prev: Course, new: Course) -> None:
    """Pin worksheet identity across A3/A4 rewrite-only stages.

    Those stages never add or remove lessons/exercises, so index alignment holds;
    the authoritative concept pack and each row's concept_id are not content
    the rewrite may change.
    """
    for pm, nm in zip(prev.modules, new.modules):
        for pl, nl in zip(pm.lessons, nm.lessons):
            nl.concepts = [c.model_copy(deep=True) for c in pl.concepts]
            for pe, ne in zip(pl.exercises, nl.exercises):
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
    map_retrying = (
        state.retry_target == "a1"
        and state.validation_report is not None
        and not state.validation_report.ok
    )
    if map_retrying:
        owner_course = state.a5_course or state.a4_course or state.best_course
        map_issues = [
            issue
            for issue in state.validation_report.issues
            if a1_owns_validation_issue(issue, owner_course)
        ]
        feedback = "\n".join(
            f"- {issue.path}: {issue.message}" for issue in map_issues
        )
        previous_map = json.dumps(state.a1_course_map, ensure_ascii=False, indent=2)
        base_prompt = (
            "CRITICAL: The previous A1 concept map caused these confirmed hard-validation "
            "errors. Correct only the implicated concept fields. Preserve the exact module "
            "and lesson layout, titles, SLOs, concept count and order, and every concept ID, "
            "depth, and confusable_with list unless an error explicitly identifies that field. "
            "For a .summary error, rewrite only that summary as a self-contained source-grounded "
            "domain fact. Do not churn unaffected bank identities.\n"
            f"{feedback}\n\n"
            "PREVIOUS A1 MAP TO CORRECT (copy all unaffected fields exactly):\n"
            f"{previous_map}\n\n{base_prompt}"
        )

    # The map must be structurally right before anything downstream runs. Retry
    # A1 with concrete feedback while a bad map is still one cheap LLM call
    # instead of a wasted multi-stage loop.
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
    remaining_problems = _validate_a1_map(data, state.config)
    remaining_meta_problems = _a1_meta_reference_problems(data)
    strict_map_gate = (
        state.config.worksheet_items_per_lesson is not None
        or bool(remaining_meta_problems)
    )
    if remaining_problems and strict_map_gate:
        details = "\n".join(f"- {problem}" for problem in remaining_problems)
        gate_label = (
            "structurally valid exact-budget course map"
            if state.config.worksheet_items_per_lesson is not None
            else "course map without source-exposition concepts"
        )
        raise ValueError(
            f"A1 could not produce a {gate_label} "
            f"after {MAP_ATTEMPTS} attempts:\n{details}"
        )
    if best_problem_count:
        await ctx.add_event(
            StageLogEvent(
                f"A1: no fully valid map after {MAP_ATTEMPTS} attempts; using best one "
                f"({best_problem_count} problem(s))."
            )
        )
    # Legacy/full-envelope runs preserve the historical best-effort fallback.
    # Exact-budget maps already passed the strict depth/bounds gate above.
    _stamp_default_depths(data)
    await ctx.add_event(StageLogEvent("A1: received LLM response, writing artifact"))

    if "thought_process" in data and isinstance(data["thought_process"], list):
        thought_str = "\n".join([f"  > {t}" for t in data["thought_process"]])
        await ctx.add_event(StageLogEvent(f"A1 Thought Process:\n{thought_str}"))

    state.a1_course_map = data
    if map_retrying:
        # A new map invalidates every downstream lesson and best-attempt score.
        # Clear them so A2 performs a full generation against the new authority
        # instead of reusing lessons from the previous map by position.
        _reset_after_a1_retry(state)
    write_json(_artifact_path(state, "a1_course_map.json"), data)
    await ctx.add_event(StageLogEvent("A1: done, forwarding to A2"))
    await ctx.send_message(state)


@executor(id="a2_scaffolder")
async def a2_scaffolder(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    if state.a1_course_map is None:
        raise RuntimeError("A2 requires A1 course map.")

    map_modules = state.a1_course_map.get("modules") or []
    course_title = state.override_title or state.a1_course_map.get("title") or "Course"
    lesson_entries: list[tuple[int, int, dict, str]] = []  # (mi, li, lesson_map, module_title)
    for mi, module in enumerate(map_modules):
        for li, lesson_map in enumerate(module.get("lessons") or []):
            lesson_entries.append((mi, li, lesson_map, module.get("title", f"Module {mi + 1}")))

    # Cross-lesson context: tell each generator what the OTHER lessons own, so it
    # stays inside its own concept pack.
    def _other_lessons_note(current_key: str) -> str:
        lines = []
        for mi, li, lesson_map, _ in lesson_entries:
            if f"{mi}:{li}" == current_key:
                continue
            ids = [c.get("id", "?") for c in (lesson_map.get("concepts") or [])]
            lines.append(f"- \"{lesson_map.get('title', '?')}\": {ids}")
        if not lines:
            return ""
        return "Other lessons in this course own these concepts — do NOT write exercises about them:\n" + "\n".join(lines)

    # Cell worksheets (Phase 2b): expand each lesson's concept pack into the
    # exact (concept, rung, variant, type, Bloom) plan, with true_false answers
    # alternated course-wide across the worksheets. Deterministic from the map,
    # so loop retries regenerate against an identical contract. A lesson whose
    # concepts don't parse (or are absent) falls back to the legacy
    # config-distribution prompt below.
    worksheets: list[list | None] = []
    for _mi, _li, lesson_map, _mt in lesson_entries:
        try:
            atoms = [ConceptAtom.model_validate(c) for c in (lesson_map.get("concepts") or [])]
            if (
                state.config.worksheet_items_per_lesson is not None
                and not worksheet_applies(atoms)
            ):
                raise ValueError(
                    "Exact worksheet policy requires a non-empty, fully depth-classified "
                    "authoritative concept pack."
                )
            worksheets.append(
                build_lesson_worksheet(
                    atoms,
                    item_budget=state.config.worksheet_items_per_lesson,
                )
                if atoms
                else None
            )
        except WorksheetBudgetError:
            # A budget violation is a structural A1 failure, never a reason to
            # silently fall back to legacy distribution generation.
            raise
        except Exception:  # noqa: BLE001 - malformed legacy packs retain historical fallback
            if state.config.worksheet_items_per_lesson is not None:
                # An exact worksheet policy must never degrade into a different
                # prompt/count contract because its authoritative A1 pack is
                # malformed. A1 normally catches this; fail closed if A2 is
                # invoked directly or an artifact is externally corrupted.
                raise
            worksheets.append(None)
    assign_tf_answers([ws for ws in worksheets if ws])

    # Deterministic per-lesson true/false answer pattern (course-wide alternation)
    # for lessons WITHOUT a worksheet (legacy fallback).
    tf_per_lesson = state.config.question_type_distribution.get("true_false", 0)
    tf_patterns = _tf_answer_patterns(tf_per_lesson, len(lesson_entries))

    # Retry economics: regenerate ONLY the lessons that failed validation; clean
    # lessons are reused untouched from the last validated course. None => all.
    retrying = state.validation_report is not None and not state.validation_report.ok
    prev_course = state.a5_course if retrying else None
    failed_keys = _failed_lesson_keys(state.validation_report) if retrying else None
    per_lesson_issues = issues_by_lesson(state.validation_report) if retrying else {}
    structure_matches = prev_course is not None and [
        len(m.lessons) for m in prev_course.modules
    ] == [len(m.get("lessons") or []) for m in map_modules]
    partial = retrying and failed_keys is not None and structure_matches

    if partial:
        to_generate = [e for e in lesson_entries if f"{e[0]}:{e[1]}" in failed_keys]
        await ctx.add_event(
            StageLogEvent(
                f"A2: self-correcting retry {state.retry_count} — regenerating only "
                f"{len(to_generate)}/{len(lesson_entries)} failed lesson(s): {sorted(failed_keys)}"
            )
        )
    else:
        to_generate = lesson_entries
        note = f" (retry {state.retry_count}, full regeneration)" if retrying else ""
        await ctx.add_event(
            StageLogEvent(f"A2: generating {len(to_generate)} lessons concurrently{note} "
                          f"(max {MAX_CONCURRENT_LESSON_CALLS} at a time)")
        )

    async def _gen_lesson(mi: int, li: int, lesson_map: dict, module_title: str) -> tuple[int, int, Lesson]:
        key = f"{mi}:{li}"
        issues = [i.model_dump() for i in per_lesson_issues.get(key, []) if i.severity == "error"] or None
        llm = LLMClient(model_id=state.model_id, name=f"A2_Scaffolder_{mi}_{li}")
        seq_index = next(idx for idx, e in enumerate(lesson_entries) if (e[0], e[1]) == (mi, li))
        _, lesson = await llm.run_json_model(
            a2_lesson_prompt(
                json.dumps(lesson_map, ensure_ascii=False, indent=2),
                state.input_text,
                difficulty=state.difficulty,
                config=state.config,
                course_title=course_title,
                module_title=module_title,
                other_lessons_note=_other_lessons_note(key),
                tf_answers=tf_patterns[seq_index],
                worksheet=worksheets[seq_index],
                validation_issues=issues,
            ),
            LessonGen,
        )
        await ctx.add_event(StageLogEvent(f"A2: lesson {key} (\"{lesson.title}\") generated"))
        return mi, li, _lesson_to_plain(lesson)

    results = await _gather_limited([_gen_lesson(*e) for e in to_generate])
    generated = {f"{mi}:{li}": lesson for mi, li, lesson in results}

    # Assemble the course: fresh lessons where generated, reused ones elsewhere.
    modules: list[Module] = []
    for mi, module in enumerate(map_modules):
        lessons: list[Lesson] = []
        for li in range(len(module.get("lessons") or [])):
            key = f"{mi}:{li}"
            if key in generated:
                lessons.append(generated[key])
            else:
                lessons.append(prev_course.modules[mi].lessons[li])
        modules.append(Module(title=module.get("title", f"Module {mi + 1}"), lessons=lessons))

    course = Course(title=course_title, difficulty=state.difficulty, modules=modules)
    # If a lesson generator forgot to echo the concept pack back, restore it from
    # the A1 map so downstream coverage validation still has ground truth.
    _seed_concepts_from_map(course, state.a1_course_map)

    state.a2_course = course
    state.dirty_lessons = sorted(generated.keys())
    await ctx.add_event(StageLogEvent("A2: writing artifact, forwarding to A3"))
    write_json(_artifact_path(state, "a2_course.json"), course.model_dump(mode="json"))
    await ctx.send_message(state)


async def _rewrite_lessons_chunked(
    state: PipelineState,
    ctx: WorkflowContext[PipelineState],
    source_course: Course,
    *,
    stage: str,
    agent_prefix: str,
    prompt_builder,
) -> Course:
    """Shared chunked rewrite for A3/A4: process only the dirty lessons, concurrently.

    Clean lessons pass through untouched — they already carry the previous
    pass's scenarios/feedback and re-running them only risks damage.
    """
    dirty = set(state.dirty_lessons) if state.dirty_lessons is not None else set(_all_lesson_keys(source_course))
    course = source_course.model_copy(deep=True)

    targets: list[tuple[int, int, Lesson]] = [
        (mi, li, lesson)
        for mi, m in enumerate(course.modules)
        for li, lesson in enumerate(m.lessons)
        if f"{mi}:{li}" in dirty
    ]
    await ctx.add_event(
        StageLogEvent(
            f"{stage}: rewriting {len(targets)}/{sum(len(m.lessons) for m in course.modules)} lesson(s) "
            f"concurrently (max {MAX_CONCURRENT_LESSON_CALLS} at a time)"
        )
    )

    async def _rewrite(mi: int, li: int, lesson: Lesson) -> tuple[int, int, Lesson]:
        llm = LLMClient(model_id=state.model_id, name=f"{agent_prefix}_{mi}_{li}")
        _, updated = await llm.run_json_model(
            prompt_builder(lesson.model_dump_json(indent=2)),
            LessonGen,
        )
        await ctx.add_event(StageLogEvent(f"{stage}: lesson {mi}:{li} (\"{lesson.title}\") done"))
        return mi, li, _lesson_to_plain(updated)

    results = await _gather_limited([_rewrite(*t) for t in targets])
    for mi, li, updated in results:
        course.modules[mi].lessons[li] = updated

    course.difficulty = state.difficulty
    _restore_concept_metadata(source_course, course)
    return course


@executor(id="a3_scenario_designer")
async def a3_scenario_designer(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    if state.a2_course is None:
        raise RuntimeError("A3 requires A2 course.")
    await ctx.add_event(StageLogEvent("A3: starting scenario designer (make L3/L4 scenario-based)"))
    course = await _rewrite_lessons_chunked(
        state,
        ctx,
        state.a2_course,
        stage="A3",
        agent_prefix="A3_ScenarioDesigner",
        prompt_builder=lambda lesson_json: a3_lesson_prompt(
            lesson_json, state.input_text, difficulty=state.difficulty, config=state.config
        ),
    )
    state.a3_course = course
    await ctx.add_event(StageLogEvent("A3: writing artifact, forwarding to A4"))
    write_json(_artifact_path(state, "a3_course.json"), course.model_dump(mode="json"))
    await ctx.send_message(state)


@executor(id="a4_feedback_architect")
async def a4_feedback_architect(state: PipelineState, ctx: WorkflowContext[PipelineState]) -> None:
    if state.a3_course is None:
        raise RuntimeError("A4 requires A3 course.")
    await ctx.add_event(StageLogEvent("A4: starting feedback architect (paired feedback for distractors)"))
    course = await _rewrite_lessons_chunked(
        state,
        ctx,
        state.a3_course,
        stage="A4",
        agent_prefix="A4_FeedbackArchitect",
        prompt_builder=lambda lesson_json: a4_lesson_prompt(
            lesson_json, state.input_text, difficulty=state.difficulty, config=state.config
        ),
    )
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

    # Loop logic: map-owned errors return to A1; lesson-content errors use the
    # cheaper A2 partial-regeneration path.
    MAX_RETRIES = 2
    if not report.ok and state.retry_count < MAX_RETRIES:
        state.retry_count += 1
        state.retry_target = _validation_retry_target(report, repaired_course)
        await ctx.add_event(
            StageLogEvent(
                "A5: Validation failed (errors found). Looping back to "
                f"{state.retry_target.upper()} (Attempt {state.retry_count}/{MAX_RETRIES})."
            )
        )
        # We DO NOT yield output here. We loop back.
        # The edges in workflow.py will handle the routing, but we need to ensure we don't proceed to 'yield_output'.
        await ctx.send_message(state)
        return

    state.retry_target = None

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
