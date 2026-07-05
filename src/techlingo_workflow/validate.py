from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from typing import Any

from pydantic import ValidationError

from .config import WorkflowConfig
from .llm import LLMClient
from .models import (
    BloomsLevel,
    Course,
    Feedback,
    FillGapsExercise,
    Flashcard,
    Lesson,
    LessonGen,
    MultiChoiceExercise,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
    ValidationIssue,
    ValidationReport,
)
from .prompts import a5_lesson_repair_prompt


def _count_lessons(course: Course) -> int:
    return sum(len(m.lessons) for m in course.modules)


# ---------------------------------------------------------------------------
# Content-quality helpers (deterministic, no LLM)
# ---------------------------------------------------------------------------

# Function words stripped before comparing exercise content, so that shared
# scaffolding ("What is...", "Which of the following...") doesn't inflate
# similarity between genuinely different questions.
_STOPWORDS = frozenset(
    """a an the is are was were be been being to of in on for and or nor not no
    this that these those it its they them their there here can could do does
    did done will would should must may might what which who whom whose when
    where why how true false statement following select choose pick mark""".split()
)


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _exercise_signature(ex: Any) -> set[str]:
    """Tokens representing the FACT an exercise tests (mechanical stems excluded).

    Two exercises with near-identical signatures test the same fact — the
    "same question, different wrapper" failure mode — regardless of type.
    """
    pieces: list[str] = []
    if isinstance(ex, (SingleChoiceExercise, MultiChoiceExercise)):
        pieces.append(ex.prompt)
        pieces.extend(o.text for o in ex.options if o.is_correct)
    elif isinstance(ex, TrueFalseExercise):
        pieces.append(ex.statement)
    elif isinstance(ex, FillGapsExercise):
        for part in ex.parts:
            if getattr(part, "type", None) == "text":
                pieces.append(part.text)
            elif getattr(part, "type", None) == "gap":
                pieces.extend(part.accepted_answers)
    elif isinstance(ex, RearrangeExercise):
        pieces.append(" ".join(ex.correct_order))
    return _content_tokens(" ".join(pieces))


# Bloom levels each question type can legitimately carry. rearrange/fill_gaps/
# true_false are mechanically recall/comprehension formats; higher-order Bloom
# levels need a scenario with a decision point, which only choice types support.
_ALLOWED_BLOOMS_BY_TYPE: dict[str, frozenset[BloomsLevel]] = {
    "single_choice": frozenset(BloomsLevel),
    "multi_choice": frozenset(BloomsLevel),
    "true_false": frozenset({BloomsLevel.remembering, BloomsLevel.understanding}),
    "fill_gaps": frozenset({BloomsLevel.remembering, BloomsLevel.understanding}),
    "rearrange": frozenset({BloomsLevel.remembering, BloomsLevel.understanding}),
}

# Similarity thresholds for near-duplicate detection.
_DUP_WITHIN_LESSON = 0.6  # error: same fact re-asked in another wrapper
_DUP_ACROSS_LESSONS = 0.8  # warning: same fact drilled in two lessons


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

            # Concept coverage: every exercise targets one of the lesson's concept
            # atoms, spread as evenly as possible — this is what prevents five
            # re-skins of the same fact. (Skipped when the lesson has no concepts,
            # e.g. legacy runs from maps without content packs.)
            if lesson.concepts:
                concept_ids = {c.id for c in lesson.concepts}
                assigned: Counter[str] = Counter()
                for ei, ex in enumerate(lesson.exercises):
                    ex_path = f"{base_path}.exercises[{ei}]"
                    if not ex.concept_id:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.concept_id",
                                message="Exercise must set concept_id to one of the lesson's concept ids "
                                f"({sorted(concept_ids)}).",
                            )
                        )
                    elif ex.concept_id not in concept_ids:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.concept_id",
                                message=f"concept_id '{ex.concept_id}' is not one of this lesson's concepts "
                                f"({sorted(concept_ids)}).",
                            )
                        )
                    else:
                        assigned[ex.concept_id] += 1
                # Coverage checks only make sense once every exercise carries a
                # valid id (missing/unknown ids already errored above).
                if sum(assigned.values()) == len(lesson.exercises) and lesson.exercises:
                    import math

                    # Coverage floor: use as many DISTINCT concepts as possible.
                    required_distinct = min(len(concept_ids), len(lesson.exercises))
                    if len(assigned) < required_distinct:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{base_path}.exercises[*].concept_id",
                                message=f"Exercises must cover at least {required_distinct} distinct concepts "
                                f"(got {len(assigned)}: {dict(assigned)}). Spread exercises across "
                                f"different concepts instead of revisiting the same one.",
                            )
                        )
                    # Over-drill cap: no single concept hogs the lesson. The +1
                    # slack matters: generators reliably land one over the exact
                    # even split, and that is pedagogically fine — the gate is
                    # for gross over-drilling, not perfect balance.
                    cap = max(math.ceil(len(lesson.exercises) / len(concept_ids)) + 1, 2)
                    over = {cid: n for cid, n in assigned.items() if n > cap}
                    if over:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{base_path}.exercises[*].concept_id",
                                message=f"Concept(s) over-drilled (max {cap} exercises per concept in this "
                                f"lesson): {over}. Move the extra exercises to uncovered concepts.",
                            )
                        )

            # Scenario + structure + feedback checks for Applying / Analyzing/Evaluating
            for ei, ex in enumerate(lesson.exercises):
                ex_path = f"{base_path}.exercises[{ei}]"
                prompt_lc = ex.prompt.lower()

                # Bloom <-> type coupling: higher-order levels need a scenario with a
                # decision point, which only choice types can carry.
                allowed = _ALLOWED_BLOOMS_BY_TYPE.get(ex.question_type, frozenset(BloomsLevel))
                if ex.blooms_level not in allowed:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            path=f"{ex_path}.blooms_level",
                            message=f"{ex.question_type} cannot carry Bloom level '{ex.blooms_level.value}'. "
                            "Applying/Analyzing must be scenario-based single_choice or multi_choice; "
                            "true_false/fill_gaps/rearrange must be Remembering or Understanding.",
                        )
                    )

                if ex.blooms_level in {BloomsLevel.applying, BloomsLevel.analyzing_evaluating}:
                    # A scenario prompt describes a situation before the question,
                    # which needs length — keyword lists alone false-positive on
                    # perfectly good scenarios ("A product team is choosing...").
                    # Only warn when the prompt is BOTH short and keyword-free.
                    looks_like_scenario = len(ex.prompt.split()) >= 12 or any(
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
                    # TechLingo mobile has a single text input, so fill_blank must
                    # carry exactly one gap (see TECHLINGO_OUTPUT_PLAN.md §Phase-0.6).
                    if gap_count != 1:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.parts",
                                message=f"fill_gaps must include exactly 1 gap part, got {gap_count}.",
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
                    # Fewer than 4 tokens is trivially solvable (a 2-3 chunk split
                    # gives the answer away); more than 8 exceeds the app's UX.
                    if len(ex.word_bank) < 4:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.word_bank",
                                message=f"rearrange.word_bank must contain 4-8 tokens, got {len(ex.word_bank)}. "
                                "Split the sentence/process into smaller reorderable pieces.",
                            )
                        )
                    elif len(ex.word_bank) > 8:
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                path=f"{ex_path}.word_bank",
                                message=f"rearrange.word_bank has {len(ex.word_bank)} tokens; prefer 4-8.",
                            )
                        )
                    long_tokens = [t for t in ex.word_bank if len(t.split()) > 4]
                    if long_tokens:
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                path=f"{ex_path}.word_bank",
                                message=f"rearrange tokens should be at most 4 words each; too long: {long_tokens}.",
                            )
                        )
                    if len(ex.correct_order) < 4:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.correct_order",
                                message=f"rearrange.correct_order must contain 4-8 tokens, got {len(ex.correct_order)}.",
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
                    # Ambiguity gate: two or more comma-terminated tokens means the
                    # sentence enumerates interchangeable list items ("power chatbots,"
                    # "create content," ...) — several orders are equally correct, but
                    # the app grades against exactly one. Only order-forced content
                    # (process steps, grammatically constrained sentences) is valid.
                    comma_tokens = [t for t in ex.correct_order if t.rstrip().endswith(",")]
                    if len(comma_tokens) >= 2:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                path=f"{ex_path}.correct_order",
                                message=(
                                    f"rearrange order is ambiguous: tokens {comma_tokens} are "
                                    "interchangeable list items, so multiple orders are correct "
                                    "but the app accepts only one. Rewrite as an order-forced "
                                    "sequence (process steps, cause->effect) or use a different "
                                    "question type for this fact."
                                ),
                            )
                        )

    issues.extend(_content_quality_issues(course))

    counts: dict[str, Any] = {
        "modules": len(course.modules),
        "lessons_total": lesson_count,
    }

    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues, counts=counts, repaired=False)


def _content_quality_issues(course: Course) -> list[ValidationIssue]:
    """Course-wide content-quality gates: duplicates, T/F balance, tautologies, length bias.

    All deterministic. Errors feed the A5 repair + A2 self-correction loop with
    exact paths, so the generator gets actionable feedback instead of vague
    "be more varied" instructions.
    """
    issues: list[ValidationIssue] = []

    # Collect signatures once.
    entries: list[tuple[str, str, Any, set[str]]] = []  # (lesson_key, ex_path, ex, signature)
    for mi, mod in enumerate(course.modules):
        for li, lesson in enumerate(mod.lessons):
            lesson_key = f"{mi}:{li}"
            for ei, ex in enumerate(lesson.exercises):
                ex_path = f"modules[{mi}].lessons[{li}].exercises[{ei}]"
                entries.append((lesson_key, ex_path, ex, _exercise_signature(ex)))

    # Near-duplicate detection: the "same fact, different wrapper" failure mode.
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            lk_i, path_i, _, sig_i = entries[i]
            lk_j, path_j, _, sig_j = entries[j]
            sim = _jaccard(sig_i, sig_j)
            if lk_i == lk_j and sim >= _DUP_WITHIN_LESSON:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        path=path_j,
                        message=f"Near-duplicate of {path_i} (content similarity {sim:.2f}): both test the "
                        "same fact. Rewrite one to target a different fact or aspect.",
                    )
                )
            elif lk_i != lk_j and sim >= _DUP_ACROSS_LESSONS:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        path=path_j,
                        message=f"Very similar to {path_i} in another lesson (similarity {sim:.2f}).",
                    )
                )

    # True/false answer balance: if every statement is true (or every one false),
    # learners stop reading the statements.
    tf = [(path, ex) for _, path, ex, _ in entries if isinstance(ex, TrueFalseExercise)]
    if len(tf) >= 3:
        true_count = sum(1 for _, ex in tf if ex.correct_answer)
        if true_count == len(tf) or true_count == 0:
            value = "true" if true_count else "false"
            issues.append(
                ValidationIssue(
                    severity="error",
                    path="modules[*].true_false",
                    message=f"All {len(tf)} true_false exercises have correct_answer={value}. Roughly half "
                    "must be false — write false statements by swapping ONE detail for a "
                    "confusable sibling term.",
                )
            )

    # Tautology: a correct option whose content words all appear in the prompt is
    # answerable without any knowledge.
    for _, path, ex, _ in entries:
        if isinstance(ex, (SingleChoiceExercise, MultiChoiceExercise)):
            prompt_tokens = _content_tokens(ex.prompt)
            for oi, opt in enumerate(ex.options):
                if not opt.is_correct:
                    continue
                opt_tokens = _content_tokens(opt.text)
                if opt_tokens and opt_tokens <= prompt_tokens:
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            path=f"{path}.options[{oi}]",
                            message=f"Correct option '{opt.text}' restates the prompt (tautology); the "
                            "question is answerable without knowledge.",
                        )
                    )

    # Length bias: if the correct single_choice option is almost always the longest,
    # learners can game the quiz without reading.
    sc = [ex for _, _, ex, _ in entries if isinstance(ex, SingleChoiceExercise) and len(ex.options) >= 4]
    if len(sc) >= 4:
        biased = 0
        for ex in sc:
            correct = [o.text for o in ex.options if o.is_correct]
            wrong = [o.text for o in ex.options if not o.is_correct]
            if correct and wrong and all(len(correct[0]) > len(w) for w in wrong):
                biased += 1
        if biased / len(sc) >= 0.75:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    path="modules[*].single_choice",
                    message=f"In {biased}/{len(sc)} single_choice exercises the correct option is strictly "
                    "the longest — a giveaway pattern. Balance option lengths.",
                )
            )

    return issues


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


# Varied replacements for the generic mechanical stems LLMs habitually repeat
# ("Is this statement true or false?" x6 reads like a template, because it is one).
# Rotation is deterministic (per-type counter), so runs stay reproducible.
_GENERIC_TF_STEMS = {
    "is this statement true or false",
    "is the following statement true or false",
    "true or false",
    "mark the statement as true or false",
    "decide if this statement is true or false",
}
_TF_TEMPLATES = [
    "True or false?",
    "Decide whether this statement is true or false.",
    "Is the following statement accurate?",
    "Evaluate this statement.",
]
_GENERIC_FILL_STEMS = {
    "choose the missing word",
    "choose the missing phrase",
    "fill in the blank",
    "fill in the missing word",
    "complete the sentence",
    "type the missing word",
}
_FILL_TEMPLATES = [
    "Fill in the missing term.",
    "Complete the sentence with the correct term.",
    "Type the term that completes this sentence.",
    "Which term completes this sentence?",
]
_GENERIC_REARRANGE_STEMS = {
    "reconstruct the sentence",
    "rearrange the sentence",
    "order the steps",
    "arrange the following steps",
    "put the words in order",
    "arrange the words in the correct order",
}
_REARRANGE_TEMPLATES = [
    "Put the pieces in the correct order.",
    "Arrange the parts to form the correct statement.",
    "Order the pieces correctly.",
    "Reconstruct the statement from the pieces.",
]

_BLOOM_RANK = {
    BloomsLevel.remembering: 0,
    BloomsLevel.understanding: 1,
    BloomsLevel.applying: 2,
    BloomsLevel.analyzing_evaluating: 3,
}
# Within a Bloom level: recognition (choice/judgment) before production
# (typing/arranging) — the Duolingo-style ladder.
_MECHANIC_RANK = {
    "single_choice": 0,
    "multi_choice": 1,
    "true_false": 2,
    "fill_gaps": 3,
    "rearrange": 4,
}


def _normalize_stem(prompt: str) -> str:
    return re.sub(r"[^a-z ]", "", prompt.lower()).strip()


def _shuffled_word_bank(correct_order: list[str]) -> list[str]:
    """Seeded shuffle of the tokens, guaranteed != correct_order when possible.

    The app renders word_bank as the draggable pool; shipping it pre-arranged
    hands the learner the solved exercise.
    """
    import random

    if len(set(correct_order)) < 2:
        return list(correct_order)
    for seed in range(len(correct_order), len(correct_order) + 16):
        candidate = list(correct_order)
        random.Random(seed).shuffle(candidate)
        if candidate != correct_order:
            return candidate
    return list(reversed(correct_order))


def normalize_course(course: Course) -> Course:
    """Deterministically enforce mechanical constraints the LLM may violate.

    Some constraints are purely mechanical and should never be left to chance:
    - rearrange word banks are rebuilt as a seeded shuffle of correct_order (same
      multiset guaranteed, and never shipped pre-arranged);
    - missing error_type labels get a default;
    - generic repeated prompt stems rotate through varied templates;
    - exercises are ordered easy->hard (Bloom level, then recognition->production
      mechanics), the Duolingo-style ladder within a lesson.
    """
    tf_counter = 0
    fill_counter = 0
    rearrange_counter = 0

    for module in course.modules:
        for lesson in module.lessons:
            for ex in lesson.exercises:
                if isinstance(ex, RearrangeExercise):
                    ex.word_bank = _shuffled_word_bank(ex.correct_order)
                    if _normalize_stem(ex.prompt) in _GENERIC_REARRANGE_STEMS:
                        ex.prompt = _REARRANGE_TEMPLATES[rearrange_counter % len(_REARRANGE_TEMPLATES)]
                    rearrange_counter += 1
                elif isinstance(ex, TrueFalseExercise):
                    if _normalize_stem(ex.prompt) in _GENERIC_TF_STEMS:
                        ex.prompt = _TF_TEMPLATES[tf_counter % len(_TF_TEMPLATES)]
                    tf_counter += 1
                elif isinstance(ex, FillGapsExercise):
                    if _normalize_stem(ex.prompt) in _GENERIC_FILL_STEMS:
                        ex.prompt = _FILL_TEMPLATES[fill_counter % len(_FILL_TEMPLATES)]
                    fill_counter += 1
                elif isinstance(ex, (SingleChoiceExercise, MultiChoiceExercise)):
                    # `error_type` is a required category label on every incorrect
                    # option; downstream stages (A3/A4) that rewrite options sometimes
                    # drop it. It is not content-sensitive, so guarantee its presence
                    # with a sensible default instead of letting it block the course.
                    for opt in ex.options:
                        if not opt.is_correct and not (opt.error_type and opt.error_type.strip()):
                            opt.error_type = "misconception"

            # Easy -> hard ladder (stable sort keeps original order within a rung).
            lesson.exercises.sort(
                key=lambda e: (_BLOOM_RANK.get(e.blooms_level, 0), _MECHANIC_RANK.get(e.question_type, 0))
            )
    return course


_LESSON_PATH_RE = re.compile(r"^modules\[(\d+)\]\.lessons\[(\d+)\]")


def issues_by_lesson(report: ValidationReport | None) -> dict[str, list[ValidationIssue]]:
    """Group issues by the lesson ("mi:li") their path points into.

    Issues whose path does not name a specific lesson (course-level structure,
    cross-course balance) are omitted — they cannot be fixed lesson-locally.
    """
    grouped: dict[str, list[ValidationIssue]] = {}
    if report is None:
        return grouped
    for issue in report.issues:
        m = _LESSON_PATH_RE.match(issue.path)
        if m:
            grouped.setdefault(f"{m.group(1)}:{m.group(2)}", []).append(issue)
    return grouped


def error_count(report: ValidationReport) -> int:
    return sum(1 for i in report.issues if i.severity == "error")


def attempt_badness(report: ValidationReport) -> tuple[int, int, int]:
    """Rank an attempt for best-attempt-wins comparisons (lower is better).

    Lexicographic severity classes — a raw error count would happily trade
    "collapsed 3 modules into 1" for a couple of content-level nitpicks:
      1. structural: wrong module count / total lesson count (course unusable),
      2. shape: per-lesson exercise/flashcard counts, type mix, Bloom distribution,
      3. content: everything else (coverage, duplicates, balance, fidelity).
    """
    structural = shape = content = 0
    for i in report.issues:
        if i.severity != "error":
            continue
        if i.path in ("modules", "modules[*].lessons"):
            structural += 1
        elif (
            i.message.startswith("Expected exactly")
            or "type mix" in i.message
            or "Bloom distribution" in i.message
        ):
            shape += 1
        else:
            content += 1
    return (structural, shape, content)


async def rebalance_true_false(course: Course, llm: LLMClient, source_text: str) -> bool:
    """Targeted micro-repair: flip roughly half of an all-true true_false set to false.

    Generators systematically produce only-true statements even when told to
    alternate, and whole-course repair tends to break structure while "fixing"
    this. Rewriting just the affected statements is narrow and safe. Returns
    True when the course was modified.
    """
    from .prompts import a5_tf_rebalance_prompt

    tf: list[TrueFalseExercise] = [
        ex
        for module in course.modules
        for lesson in module.lessons
        for ex in lesson.exercises
        if isinstance(ex, TrueFalseExercise)
    ]
    if len(tf) < 3:
        return False
    import math

    majority_value = sum(1 for ex in tf if ex.correct_answer) * 2 >= len(tf)
    majority = [(i, ex) for i, ex in enumerate(tf) if ex.correct_answer == majority_value]
    minority_count = len(tf) - len(majority)
    if minority_count >= math.ceil(len(tf) / 3):
        return False  # balanced enough

    # Flip evenly-spaced majority items until the split is roughly half/half. In
    # practice generators emit (almost) all-true, so this rewrites TRUE
    # statements into minimally-corrupted FALSE ones.
    needed = len(tf) // 2 - minority_count
    step = max(len(majority) // max(needed, 1), 1)
    to_flip = majority[::step][:needed]
    if not to_flip:
        return False
    items = [{"index": i, "statement": ex.statement} for i, ex in to_flip]
    try:
        data = await llm.run_json(
            a5_tf_rebalance_prompt(
                json.dumps(items, ensure_ascii=False, indent=2), source_text, to_false=majority_value
            )
        )
        fixes = {f["index"]: f for f in data.get("fixes", []) if isinstance(f, dict) and "index" in f}
    except Exception:  # noqa: BLE001 - micro-repair is best-effort; the balance gate still reports
        return False

    changed = False
    for i, ex in to_flip:
        fix = fixes.get(i)
        new_statement = (fix or {}).get("statement", "").strip() if fix else ""
        if not new_statement:
            continue
        # Only flip the answer when the text was actually corrupted. Rewrites that
        # come back (near-)identical would produce a true statement marked false.
        if _jaccard(_content_tokens(new_statement), _content_tokens(ex.statement)) >= 0.9:
            continue
        ex.statement = new_statement
        ex.correct_answer = not ex.correct_answer
        if isinstance(fix.get("feedback_for_correct"), str) and fix["feedback_for_correct"].strip():
            ex.feedback_for_correct = fix["feedback_for_correct"].strip()
        fb = fix.get("feedback_for_incorrect")
        if isinstance(fb, dict) and fb.get("intrinsic") and fb.get("instructional"):
            ex.feedback_for_incorrect = Feedback(
                intrinsic=str(fb["intrinsic"]), instructional=str(fb["instructional"])
            )
        changed = True
    return changed


async def repair_course_if_needed(
    course: Course, llm: LLMClient, config: WorkflowConfig, *, max_repairs: int = 1, source_text: str | None = None
) -> tuple[Course, ValidationReport]:
    course = normalize_course(course)

    # Targeted micro-repairs first: narrow fixes are far more reliable than
    # asking a model to regenerate the whole course.
    if source_text:
        try:
            if await rebalance_true_false(course, llm, source_text):
                course = normalize_course(course)
        except Exception:  # noqa: BLE001 - never let a micro-repair sink the run
            pass

    report = validate_course(course, config)

    # Run source fidelity check if source_text is provided
    if source_text:
        source_issues = await check_source_fidelity(course, source_text, llm)
        report.issues.extend(source_issues)
        if any(i.severity == "error" for i in source_issues):
            report.ok = False

    if report.ok:
        return course, report

    # LLM repairs sometimes make things WORSE. Repair is chunked per lesson —
    # only the offending lessons are rewritten (clean ones can't be damaged) and
    # the best version seen is returned, never blindly the last attempt.
    best_course, best_report = course, report

    repaired = course
    for _ in range(max_repairs):
        per_lesson = issues_by_lesson(report)
        failing = {
            key: issues
            for key, issues in per_lesson.items()
            if any(i.severity == "error" for i in issues)
        }
        if not failing:
            # Only course-level errors remain (module/lesson counts, cross-course
            # balance) — not fixable lesson-locally; the A5->A2 loop handles them.
            break

        async def _repair_lesson(key: str, issues: list[ValidationIssue]) -> tuple[str, Lesson] | None:
            mi, li = (int(x) for x in key.split(":"))
            lesson = repaired.modules[mi].lessons[li]
            issues_json = json.dumps([i.model_dump() for i in issues], ensure_ascii=False, indent=2)
            lesson_llm = LLMClient(model_id=llm.model_id, name=f"A5_LessonRepair_{mi}_{li}")
            try:
                _, candidate = await lesson_llm.run_json_model(
                    a5_lesson_repair_prompt(lesson.model_dump_json(indent=2), issues_json, config),
                    LessonGen,
                )
            except (ValidationError, json.JSONDecodeError):
                return None  # keep this lesson as-is; validation still reports it
            return key, Lesson.model_validate(candidate.model_dump(exclude={"thought_process"}))

        sem = asyncio.Semaphore(4)

        async def _guarded(key: str, issues: list[ValidationIssue]):
            async with sem:
                return await _repair_lesson(key, issues)

        results = await asyncio.gather(*(_guarded(k, v) for k, v in failing.items()))

        candidate_course = repaired.model_copy(deep=True)
        applied = 0
        for result in results:
            if result is None:
                continue
            key, lesson = result
            mi, li = (int(x) for x in key.split(":"))
            candidate_course.modules[mi].lessons[li] = lesson
            applied += 1
        if applied == 0:
            best_report.issues.append(
                ValidationIssue(
                    severity="warning",
                    path="global",
                    message="Automated lesson repair could not produce valid lessons; returning best available version.",
                )
            )
            break

        repaired = normalize_course(candidate_course)

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

        if attempt_badness(report) < attempt_badness(best_report):
            best_course, best_report = repaired, report

    if best_course is not course:
        best_report.repaired = True
    return best_course, best_report


