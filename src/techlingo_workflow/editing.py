"""Post-run course editing: manual exercise edits + single-question regeneration.

The Run Viewer edits `course.internal.json` (the rich internal format). Nothing
ever edits `course.json` directly — after every change the TechLingo-native
output is RE-EMITTED from the internal course and the deterministic quality
gates re-run, so a hand edit can't silently ship a broken or drifted course.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, TypeAdapter, ValidationError

from .config import WorkflowConfig
from .emit import build_techlingo_course, slugify
from .llm import LLMClient
from .models import Course, Exercise, ValidationReport
from .validate import normalize_course, validate_course
from .validate_techlingo import validate_techlingo_course

_EXERCISE_ADAPTER: TypeAdapter = TypeAdapter(Exercise)

INTERNAL_FILE = "course.internal.json"
TECHLINGO_FILE = "course.json"
REPORT_FILE = "validation_report.json"


class EditError(ValueError):
    """User-facing editing failure (bad index, invalid exercise, broken emit)."""


def load_internal_course(run_dir: Path) -> Course:
    path = run_dir / INTERNAL_FILE
    if not path.exists():
        raise EditError(f"{INTERNAL_FILE} not found in {run_dir} — only generated runs can be edited.")
    return Course.model_validate(json.loads(path.read_text(encoding="utf-8")))


def resolve_course_key(run_dir: Path, course: Course) -> str:
    """Reuse the import_key the run already shipped with; keys must stay stable."""
    path = run_dir / TECHLINGO_FILE
    if path.exists():
        try:
            key = json.loads(path.read_text(encoding="utf-8")).get("import_key")
            if key:
                return str(key)
        except (json.JSONDecodeError, OSError):
            pass
    return slugify(course.title, fallback="course")


def infer_config_from_course(course: Course) -> WorkflowConfig:
    """Reconstruct the run's config from the course itself.

    Editing runs long after generation; the original WorkflowConfig isn't stored
    in the run dir. Structure/distribution gates should hold the course to ITS
    OWN shape (which A5 already validated at generation time), not to whatever
    the repo-level config happens to say today.
    """
    lessons = [l for m in course.modules for l in m.lessons]
    if not lessons:
        return WorkflowConfig()
    # Use the most common per-lesson composition (lessons are uniform by construction).
    per_lesson = Counter(len(l.exercises) for l in lessons).most_common(1)[0][0]
    blooms: Counter = Counter()
    types: Counter = Counter()
    sample = next((l for l in lessons if len(l.exercises) == per_lesson), lessons[0])
    for ex in sample.exercises:
        blooms[ex.blooms_level.value] += 1
        types[ex.question_type] += 1
    flashcards = Counter(len(l.flashcards) for l in lessons).most_common(1)[0][0]
    try:
        return WorkflowConfig(
            difficulty=course.difficulty,
            modules_count=len(course.modules),
            min_lessons_total=len(lessons),
            max_lessons_total=len(lessons),
            exercises_per_lesson=per_lesson,
            flashcards_per_lesson=flashcards,
            blooms_distribution=dict(blooms),
            question_type_distribution=dict(types),
        )
    except ValidationError:
        # Course drifted from any self-consistent config; fall back to defaults
        # (gates will report the drift, which is exactly what we want to show).
        return WorkflowConfig()


def _report_payload(report: ValidationReport, exercise: Exercise | None = None) -> dict[str, Any]:
    errors = [i.model_dump() for i in report.issues if i.severity == "error"]
    warnings = [i.model_dump() for i in report.issues if i.severity == "warning"]
    payload: dict[str, Any] = {"ok": report.ok, "errors": errors, "warnings": warnings}
    if exercise is not None:
        payload["exercise"] = exercise.model_dump(mode="json")
    return payload


def save_course(run_dir: Path, course: Course) -> ValidationReport:
    """Persist an edited course: normalize → write internal → re-emit → re-validate.

    The TechLingo-native emit runs BEFORE anything is written: if the edit can't
    produce a valid importer payload, the run dir is left untouched.
    """
    course = normalize_course(course)
    course_key = resolve_course_key(run_dir, course)

    tl_course = build_techlingo_course(course, course_key=course_key)
    tl_problems = validate_techlingo_course(tl_course)
    if tl_problems:
        raise EditError(
            "Edit would produce an invalid TechLingo course:\n"
            + "\n".join(f"- {p}" for p in tl_problems)
        )

    report = validate_course(course, infer_config_from_course(course))

    (run_dir / INTERNAL_FILE).write_text(
        json.dumps(course.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / TECHLINGO_FILE).write_text(
        json.dumps(tl_course.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / REPORT_FILE).write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _get_exercise_slot(course: Course, mi: int, li: int, ei: int) -> Exercise:
    try:
        return course.modules[mi].lessons[li].exercises[ei]
    except IndexError:
        raise EditError(f"No exercise at modules[{mi}].lessons[{li}].exercises[{ei}].")


def apply_exercise_edit(
    course: Course, mi: int, li: int, ei: int, exercise_data: dict[str, Any]
) -> Exercise:
    """Validate an edited exercise and splice it into the course (in place)."""
    current = _get_exercise_slot(course, mi, li, ei)
    try:
        edited = _EXERCISE_ADAPTER.validate_python(exercise_data)
    except ValidationError as e:
        raise EditError(f"Edited exercise does not match the schema:\n{e}")
    if edited.question_type != current.question_type:
        raise EditError(
            f"Changing question_type ({current.question_type} → {edited.question_type}) "
            "would break the lesson's type distribution; create the question in the "
            "desired type instead."
        )
    course.modules[mi].lessons[li].exercises[ei] = edited
    return edited


def regenerate_prompt(course: Course, mi: int, li: int, ei: int, instructions: str | None) -> str:
    lesson = course.modules[mi].lessons[li]
    exercise = lesson.exercises[ei]
    concept = next((c for c in lesson.concepts if c.id == exercise.concept_id), None)
    other_prompts = [
        f"- {ex.prompt}" for j, ex in enumerate(lesson.exercises) if j != ei
    ]
    keep_tf = (
        f"- correct_answer MUST stay exactly {json.dumps(exercise.correct_answer)} "
        "(the course-wide true/false answer pattern is balanced and fixed).\n"
        if exercise.question_type == "true_false"
        else ""
    )
    user_note = (
        f"The author's feedback about the current question:\n{instructions}\n\n"
        if instructions
        else "The author wants a better version of this question.\n\n"
    )
    return (
        "You will improve ONE quiz question from an existing lesson. "
        "Return ONLY the improved exercise as a single JSON object with the exact "
        "same schema as the current exercise below (same field names).\n\n"
        f"Lesson: {lesson.title}\nLearning objective: {lesson.slo}\n\n"
        + (
            "Concept this question must test:\n"
            + json.dumps(concept.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n\n"
            if concept
            else ""
        )
        + user_note
        + "Hard constraints:\n"
        f"- question_type MUST stay \"{exercise.question_type}\".\n"
        f"- blooms_level MUST stay \"{exercise.blooms_level.value}\".\n"
        f"- concept_id MUST stay {json.dumps(exercise.concept_id)}.\n"
        + keep_tf
        + "- Applying/Analyzing questions must be realistic scenarios, not definition recall.\n"
        "- Distractors must be plausible parallel alternatives with a rationale and "
        "better_fit explaining when they WOULD be right.\n"
        "- Do not duplicate any other question in this lesson:\n"
        + "\n".join(other_prompts)
        + "\n\nCurrent exercise JSON:\n"
        + json.dumps(exercise.model_dump(mode="json"), ensure_ascii=False, indent=2)
    )


async def regenerate_exercise(
    course: Course,
    mi: int,
    li: int,
    ei: int,
    llm: LLMClient,
    instructions: Optional[str] = None,
) -> Exercise:
    """Ask the LLM for a better version of one exercise and splice it in."""
    current = _get_exercise_slot(course, mi, li, ei)
    data = await llm.run_json(regenerate_prompt(course, mi, li, ei, instructions))
    try:
        candidate = _EXERCISE_ADAPTER.validate_python(data)
    except ValidationError as e:
        raise EditError(f"Model returned an exercise that does not match the schema:\n{e}")
    # Belt and braces: identity fields are non-negotiable regardless of model output.
    if candidate.question_type != current.question_type:
        raise EditError(
            f"Model changed question_type to {candidate.question_type}; regeneration aborted."
        )
    candidate.blooms_level = current.blooms_level
    candidate.concept_id = current.concept_id
    if current.question_type == "true_false" and candidate.correct_answer != current.correct_answer:
        # Can't silently flip it back — the new statement was written for the
        # flipped answer. Reject so the author can retry (balance stays intact).
        raise EditError(
            "Model flipped the true/false answer (course-wide balance is fixed); "
            "try regenerating again."
        )
    course.modules[mi].lessons[li].exercises[ei] = candidate
    return candidate
