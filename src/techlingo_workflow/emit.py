"""Deterministic transform: internal ``Course`` -> TechLingo-native ``TLCourse``.

The A1-A5 pipeline produces our rich internal models. This module converts them
into the TechLingo output contract right before the final ``course.json`` is
written. Everything here is deterministic (no LLM), so the tricky derived fields
(index-based ``correct_answer``, question-type renames, ``import_key`` slugs)
cannot drift the way LLM-emitted values would.

See TECHLINGO_OUTPUT_PLAN.md for the mapping and Phase-0 decisions.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Optional

from .models import (
    Course,
    Feedback,
    FillGapsExercise,
    Flashcard,
    Lesson,
    Module,
    MultiChoiceExercise,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
)
from .techlingo_models import (
    TLCourse,
    TLFlashcard,
    TLModule,
    TLQuestion,
    TLUnit,
)

# Internal question_type -> TechLingo question_type.
_TYPE_MAP = {
    "single_choice": "multiple_choice",
    "multi_choice": "multiple_choice",
    "true_false": "true_false",
    "fill_gaps": "fill_blank",
    "rearrange": "arrange_sentence",
}


def slugify(text: str, *, max_words: int = 8, fallback: str = "item") -> str:
    """Stable lowercase ASCII slug: no spaces, no timestamps, no random IDs.

    Deterministic for a given input so the same title always yields the same
    ``import_key``. Non-ASCII is transliterated/stripped; the result is trimmed
    to ``max_words`` words to keep keys readable.
    """
    # Transliterate accented chars to ASCII (č -> c, é -> e, ...).
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    # Keep alphanumerics; everything else becomes a separator.
    words = re.findall(r"[a-z0-9]+", ascii_text)
    slug = "-".join(words[:max_words])
    return slug or fallback


class _KeyRegistry:
    """Guarantees unique slugs within a scope by appending -2, -3, ... on collision."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def unique(self, base: str) -> str:
        count = self._seen.get(base, 0)
        self._seen[base] = count + 1
        return base if count == 0 else f"{base}-{count + 1}"


def _feedback_to_jsonable(fb: Any) -> Any:
    """Feedback may be a paired object or a plain string (FeedbackLike)."""
    if fb is None:
        return None
    if isinstance(fb, Feedback):
        return fb.model_dump()
    return fb  # already a str (or plain jsonable)


def _option_to_jsonable(opt: Any) -> dict[str, Any]:
    return {
        "text": opt.text,
        "is_correct": opt.is_correct,
        "error_type": opt.error_type,
        "rationale": opt.rationale,
        "better_fit": opt.better_fit,
        "feedback": _feedback_to_jsonable(opt.feedback),
    }


def _correct_indices(options: list) -> list[int]:
    return [i for i, o in enumerate(options) if o.is_correct]


def _emit_choice(ex, blooms: str, original_type: str, import_key: str) -> TLQuestion:
    """single_choice / multi_choice -> multiple_choice with index-based answer."""
    idxs = _correct_indices(ex.options)
    if original_type == "single_choice":
        # Exactly one correct (guaranteed by internal validation): "0"
        correct_answer = str(idxs[0]) if idxs else "0"
    else:
        # Multi-answer: compact JSON index array, e.g. "[0,1]"
        correct_answer = json.dumps(idxs, separators=(",", ":"))
    return TLQuestion(
        import_key=import_key,
        question_type="multiple_choice",
        question_text=ex.prompt,
        options={
            "options": [_option_to_jsonable(o) for o in ex.options],
            "blooms_level": blooms,
            "original_question_type": original_type,
            "concept_id": ex.concept_id,
        },
        correct_answer=correct_answer,
        explanation=ex.feedback_for_correct,
    )


def _emit_true_false(ex, blooms: str, import_key: str) -> TLQuestion:
    return TLQuestion(
        import_key=import_key,
        question_type="true_false",
        # question_text is the statement, NOT the "Mark true or false" prompt.
        question_text=ex.statement,
        options={
            "blooms_level": blooms,
            "original_question_type": "true_false",
            "concept_id": ex.concept_id,
            "feedback_for_incorrect": _feedback_to_jsonable(ex.feedback_for_incorrect),
        },
        correct_answer="true" if ex.correct_answer else "false",
        explanation=ex.feedback_for_correct,
    )


def _emit_fill_blank(ex, blooms: str, import_key: str) -> TLQuestion:
    """fill_gaps -> fill_blank. Assumes exactly one gap (enforced upstream)."""
    text_pieces: list[str] = []
    accepted: list[str] = []
    for part in ex.parts:
        ptype = getattr(part, "type", None)
        if ptype == "gap":
            text_pieces.append("___")
            if not accepted:  # first gap wins (single-gap contract)
                accepted = list(part.accepted_answers)
        else:
            text_pieces.append(part.text)

    if len(accepted) <= 1:
        correct_answer = accepted[0] if accepted else ""
    else:
        # Multiple accepted answers: compact JSON array string so the app can
        # match against any of them.
        correct_answer = json.dumps(accepted, separators=(",", ":"))

    return TLQuestion(
        import_key=import_key,
        question_type="fill_blank",
        question_text="".join(text_pieces),
        options={
            "parts": [p.model_dump() for p in ex.parts],
            "blooms_level": blooms,
            "original_question_type": "fill_gaps",
            "concept_id": ex.concept_id,
        },
        correct_answer=correct_answer,
        explanation=None,
    )


def _emit_arrange(ex, blooms: str, import_key: str) -> TLQuestion:
    return TLQuestion(
        import_key=import_key,
        question_type="arrange_sentence",
        question_text=ex.prompt,
        options={
            "word_bank": list(ex.word_bank),
            "correct_order": list(ex.correct_order),
            "blooms_level": blooms,
            "original_question_type": "rearrange",
            "concept_id": ex.concept_id,
        },
        # Mobile builds draggable words from correct_answer.split(" ").
        correct_answer=" ".join(ex.correct_order),
        explanation=None,
    )


def _emit_exercise(ex, import_key: str) -> TLQuestion:
    blooms = ex.blooms_level.value
    if isinstance(ex, SingleChoiceExercise):
        return _emit_choice(ex, blooms, "single_choice", import_key)
    if isinstance(ex, MultiChoiceExercise):
        return _emit_choice(ex, blooms, "multi_choice", import_key)
    if isinstance(ex, TrueFalseExercise):
        return _emit_true_false(ex, blooms, import_key)
    if isinstance(ex, FillGapsExercise):
        return _emit_fill_blank(ex, blooms, import_key)
    if isinstance(ex, RearrangeExercise):
        return _emit_arrange(ex, blooms, import_key)
    raise TypeError(f"Unknown exercise type: {type(ex).__name__}")


def _emit_flashcard(fc: Flashcard, import_key: str) -> TLFlashcard:
    return TLFlashcard(import_key=import_key, front=fc.front, back=fc.back, hint=fc.hint)


def _emit_unit(lesson: Lesson, unit_key: str) -> TLUnit:
    # Positional import_keys: questions 1-based (match question_number),
    # flashcards 0-based (match page_order). Same unit + slot => same key across
    # re-runs, so the importer upserts the slot instead of duplicating.
    exercises = [
        _emit_exercise(ex, f"{unit_key}-q{i}")
        for i, ex in enumerate(lesson.exercises, start=1)
    ]
    flashcards = [
        _emit_flashcard(fc, f"{unit_key}-f{i}")
        for i, fc in enumerate(lesson.flashcards, start=0)
    ]
    return TLUnit(
        import_key=unit_key,
        title=lesson.title,
        slo=lesson.slo,
        exercises=exercises,
        flashcards=flashcards,
    )


def _emit_module(module: Module, module_key: str) -> TLModule:
    unit_reg = _KeyRegistry()
    units = [
        _emit_unit(lesson, unit_reg.unique(slugify(lesson.title, fallback="unit")))
        for lesson in module.lessons
    ]
    return TLModule(import_key=module_key, title=module.title, lessons=units)


def emit_and_validate(course: Course, *, course_key: str) -> TLCourse:
    """Transform to TechLingo-native and fail loudly if the output isn't native."""
    from .validate_techlingo import TechLingoValidationError, validate_techlingo_course

    tl = build_techlingo_course(course, course_key=course_key)
    problems = validate_techlingo_course(tl)
    if problems:
        raise TechLingoValidationError(problems)
    return tl


def build_techlingo_course(course: Course, *, course_key: str) -> TLCourse:
    """Transform an internal ``Course`` into the TechLingo output contract."""
    module_reg = _KeyRegistry()
    modules = [
        _emit_module(m, module_reg.unique(slugify(m.title, fallback="module")))
        for m in course.modules
    ]
    difficulty = (
        course.difficulty.value if hasattr(course.difficulty, "value") else str(course.difficulty)
    )
    return TLCourse(
        import_key=course_key,
        title=course.title,
        difficulty=difficulty,
        source_summary=course.source_summary,
        modules=modules,
    )
