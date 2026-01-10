from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .config import WorkflowConfig
from .llm import LLMClient
from .models import (
    BloomsLevel,
    Course,
    Feedback,
    FillGapsExercise,
    Flashcard,
    MultiChoiceExercise,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
    ValidationIssue,
    ValidationReport,
)
from .prompts import a5_repair_prompt


def _count_lessons(course: Course) -> int:
    return sum(len(m.lessons) for m in course.modules)


def validate_course(course: Course, config: WorkflowConfig) -> ValidationReport:
    issues: list[ValidationIssue] = []

    # Module count
    if len(course.modules) != config.modules_count:
        issues.append(
            ValidationIssue(
                severity="error",
                path="modules",
                message=f"Expected exactly {config.modules_count} modules, got {len(course.modules)}.",
            )
        )

    # Lesson count
    lesson_count = _count_lessons(course)
    if not (config.min_lessons_total <= lesson_count <= config.max_lessons_total):
        issues.append(
            ValidationIssue(
                severity="error",
                path="modules[*].lessons",
                message=f"Expected total lessons {config.min_lessons_total}–{config.max_lessons_total}, got {lesson_count}.",
            )
        )

    # Per-lesson checks
    for mi, mod in enumerate(course.modules):
        for li, lesson in enumerate(mod.lessons):
            base_path = f"modules[{mi}].lessons[{li}]"
            if not lesson.slo.strip():
                issues.append(
                    ValidationIssue(severity="error", path=f"{base_path}.slo", message="SLO must be non-empty.")
                )

            # Flashcards checks (schema v2)
            if len(lesson.flashcards) != config.flashcards_per_lesson:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        path=f"{base_path}.flashcards",
                        message=f"Expected exactly {config.flashcards_per_lesson} flashcards, got {len(lesson.flashcards)}.",
                    )
                )
            for fi, fc in enumerate(lesson.flashcards):
                fc_path = f"{base_path}.flashcards[{fi}]"
                # (Type annotation to help linters / IDEs; pydantic already validated model.)
                _ = fc  # type: Flashcard
                if not fc.front.strip():
                    issues.append(
                        ValidationIssue(severity="error", path=f"{fc_path}.front", message="Flashcard front must be non-empty.")
                    )
                if not fc.back.strip():
                    issues.append(
                        ValidationIssue(severity="error", path=f"{fc_path}.back", message="Flashcard back must be non-empty.")
                    )

            if len(lesson.exercises) != config.exercises_per_lesson:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        path=f"{base_path}.exercises",
                        message=f"Expected exactly {config.exercises_per_lesson} exercises, got {len(lesson.exercises)}.",
                    )
                )
                # Skip deeper distribution checks if exercise count is wrong
                continue

            levels = [ex.blooms_level.value for ex in lesson.exercises]
            dist = Counter(levels)
            expected = config.blooms_distribution
            if dist != expected:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        path=f"{base_path}.exercises[*].blooms_level",
                        message=f"Bloom distribution must be {expected}, got {dict(dist)}.",
                    )
                )

            # Exercise type mix (schema v2)
            type_counts = Counter(ex.question_type for ex in lesson.exercises)
            expected_types = Counter(config.question_type_distribution)
            if type_counts != expected_types:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        path=f"{base_path}.exercises[*].question_type",
                        message=f"Exercise type mix must be {dict(expected_types)}, got {dict(type_counts)}.",
                    )
                )

            # Scenario + structure + feedback checks for Applying / Analyzing/Evaluating
            for ei, ex in enumerate(lesson.exercises):
                ex_path = f"{base_path}.exercises[{ei}]"
                prompt_lc = ex.prompt.lower()
                if ex.blooms_level in {BloomsLevel.applying, BloomsLevel.analyzing_evaluating}:
                    looks_like_scenario = any(
                        key in prompt_lc
                        for key in (
                            "scenario",
                            "you are",
                            "as a ",
                            "imagine you",
                            "your team",
                            "decision",
                            "what should you do",
                            "what do you do",
                        )
                    )
                    if not looks_like_scenario:
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                path=f"{ex_path}.prompt",
                                message="Applying/Analyzing exercise should clearly read as a scenario with a decision point.",
                            )
                        )

                # Type-specific structural validation + feedback rules
                if isinstance(ex, SingleChoiceExercise):
                    if len(ex.options) != 4:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.options",
                                message=f"single_choice must have exactly 4 options, got {len(ex.options)}.",
                            )
                        )
                    correct = [o for o in ex.options if o.is_correct]
                    if len(correct) != 1:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.options[*].is_correct",
                                message=f"single_choice must have exactly 1 correct option, got {len(correct)}.",
                            )
                        )
                    for oi, opt in enumerate(ex.options):
                        if not opt.text.strip():
                            issues.append(
                                ValidationIssue(severity="error", path=f"{ex_path}.options[{oi}].text", message="Option text must be non-empty.")
                            )
                        # New Rationale + Better Fit checks
                        if not opt.rationale or not opt.rationale.strip():
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].rationale",
                                    message="All options must include a rationale.",
                                )
                            )
                        if not opt.is_correct and (not opt.better_fit or not opt.better_fit.strip()):
                             issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].better_fit",
                                    message="Incorrect options must include a 'better_fit' explanation.",
                                )
                            )

                        if not opt.is_correct and not (opt.error_type and opt.error_type.strip()):
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].error_type",
                                    message="Incorrect options must include error_type.",
                                )
                            )
                        if (
                            ex.blooms_level in {BloomsLevel.applying, BloomsLevel.analyzing_evaluating}
                            and not opt.is_correct
                            and not isinstance(opt.feedback, Feedback)
                        ):
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].feedback",
                                    message="Scenario incorrect options must include paired feedback (intrinsic + instructional).",
                                )
                            )

                elif isinstance(ex, MultiChoiceExercise):
                    if len(ex.options) != 4:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.options",
                                message=f"multi_choice must have exactly 4 options, got {len(ex.options)}.",
                            )
                        )
                    correct = [o for o in ex.options if o.is_correct]
                    if len(correct) not in {2, 3}:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.options[*].is_correct",
                                message=f"multi_choice must have 2 or 3 correct options, got {len(correct)}.",
                            )
                        )
                    for oi, opt in enumerate(ex.options):
                        if not opt.text.strip():
                            issues.append(
                                ValidationIssue(severity="error", path=f"{ex_path}.options[{oi}].text", message="Option text must be non-empty.")
                            )
                        # New Rationale + Better Fit checks
                        if not opt.rationale or not opt.rationale.strip():
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].rationale",
                                    message="All options must include a rationale.",
                                )
                            )
                        if not opt.is_correct and (not opt.better_fit or not opt.better_fit.strip()):
                             issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].better_fit",
                                    message="Incorrect options must include a 'better_fit' explanation.",
                                )
                            )

                        if not opt.is_correct and not (opt.error_type and opt.error_type.strip()):
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].error_type",
                                    message="Incorrect options must include error_type.",
                                )
                            )
                        if (
                            ex.blooms_level in {BloomsLevel.applying, BloomsLevel.analyzing_evaluating}
                            and not opt.is_correct
                            and not isinstance(opt.feedback, Feedback)
                        ):
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    path=f"{ex_path}.options[{oi}].feedback",
                                    message="Scenario incorrect options must include paired feedback (intrinsic + instructional).",
                                )
                            )

                elif isinstance(ex, TrueFalseExercise):
                    if not ex.statement.strip():
                        issues.append(
                            ValidationIssue(severity="error", path=f"{ex_path}.statement", message="true_false.statement must be non-empty.")
                        )
                    if (
                        ex.blooms_level in {BloomsLevel.applying, BloomsLevel.analyzing_evaluating}
                        and not isinstance(ex.feedback_for_incorrect, Feedback)
                    ):
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.feedback_for_incorrect",
                                message="Scenario true/false must include feedback_for_incorrect (intrinsic + instructional).",
                            )
                        )

                elif isinstance(ex, FillGapsExercise):
                    gap_count = sum(1 for p in ex.parts if getattr(p, "type", None) == "gap")
                    if gap_count < 1:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.parts",
                                message="fill_gaps must include at least 1 gap part.",
                            )
                        )
                    for pi, part in enumerate(ex.parts):
                        if getattr(part, "type", None) == "text":
                            if not part.text.strip():
                                issues.append(
                                    ValidationIssue(
                                        severity="error",
                                        path=f"{ex_path}.parts[{pi}].text",
                                        message="fill_gaps text parts must be non-empty.",
                                    )
                                )
                        elif getattr(part, "type", None) == "gap":
                            if not part.accepted_answers or not all(a.strip() for a in part.accepted_answers):
                                issues.append(
                                    ValidationIssue(
                                        severity="error",
                                        path=f"{ex_path}.parts[{pi}].accepted_answers",
                                        message="fill_gaps gap parts must include non-empty accepted_answers.",
                                    )
                                )

                elif isinstance(ex, RearrangeExercise):
                    if len(ex.word_bank) < 2:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.word_bank",
                                message="rearrange.word_bank must contain at least 2 tokens.",
                            )
                        )
                    if len(ex.correct_order) < 2:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.correct_order",
                                message="rearrange.correct_order must contain at least 2 tokens.",
                            )
                        )
                    if any(not t.strip() for t in ex.word_bank):
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.word_bank",
                                message="rearrange.word_bank tokens must be non-empty.",
                            )
                        )
                    if any(not t.strip() for t in ex.correct_order):
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.correct_order",
                                message="rearrange.correct_order tokens must be non-empty.",
                            )
                        )
                    if Counter(ex.word_bank) != Counter(ex.correct_order):
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.correct_order",
                                message="rearrange.correct_order must use the same tokens (multiset) as word_bank.",
                            )
                        )

    counts: dict[str, Any] = {
        "modules": len(course.modules),
        "lessons_total": lesson_count,
    }

    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues, counts=counts, repaired=False)


async def check_source_fidelity(course: Course, source_text: str, llm: LLMClient) -> list[ValidationIssue]:
    from .prompts import a5_source_check_prompt

    # For very large texts, we might need chunking, but for this MVP we send it whole.
    # We truncate if strictly necessary, but better to rely on modern context windows.
    course_json = course.model_dump_json(indent=2)
    
    # We expect a JSON object with a list of issues
    try:
        data = await llm.run_json(a5_source_check_prompt(course_json, source_text))
        raw_issues = data.get("issues", [])
        return [
            ValidationIssue(severity="error", path=i.get("path", "unknown"), message=i.get("message", "Source fidelity issue"))
            for i in raw_issues
        ]
    except Exception as e:
        # Fallback: if source check fails (e.g. LLM error), we warn but don't block
        return [
            ValidationIssue(
                severity="warning",
                path="global",
                message=f"Source fidelity check failed to run: {str(e)}",
            )
        ]


async def repair_course_if_needed(
    course: Course, llm: LLMClient, config: WorkflowConfig, *, max_repairs: int = 1, source_text: str | None = None
) -> tuple[Course, ValidationReport]:
    report = validate_course(course, config)
    
    # Run source fidelity check if source_text is provided
    if source_text:
        source_issues = await check_source_fidelity(course, source_text, llm)
        report.issues.extend(source_issues)
        if any(i.severity == "error" for i in source_issues):
            report.ok = False

    if report.ok:
        return course, report

    repaired = course
    for _ in range(max_repairs):
        issues_json = json.dumps([i.model_dump() for i in report.issues], ensure_ascii=False, indent=2)
        course_json = repaired.model_dump_json(indent=2)
        repaired_data = await llm.run_json(a5_repair_prompt(course_json, issues_json, config))
        repaired = Course.model_validate(repaired_data)
        
        # Re-validate structure
        report = validate_course(repaired, config)
        
        # Re-validate source fidelity (optional: can be expensive, but needed for strictness)
        if source_text:
            source_issues = await check_source_fidelity(repaired, source_text, llm)
            report.issues.extend(source_issues)
            if any(i.severity == "error" for i in source_issues):
                report.ok = False

        if report.ok:
            report.repaired = True
            return repaired, report

    return repaired, report


