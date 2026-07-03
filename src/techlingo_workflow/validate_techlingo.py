"""Output gate for the TechLingo-native course (spec §12).

Runs *after* the deterministic transform. Because the transform is
deterministic, a failure here means either a transform bug or genuinely
malformed content that slipped through internal validation — so the caller
should fail the run loudly rather than ship a non-native ``course.json``.
"""

from __future__ import annotations

import json
from typing import List

from .techlingo_models import SCHEMA_VERSION, TLCourse

_VALID_TYPES = {"multiple_choice", "true_false", "fill_blank", "arrange_sentence"}


class TechLingoValidationError(Exception):
    """Raised when the emitted course is not TechLingo-native."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = problems
        joined = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"TechLingo output validation failed ({len(problems)} problem(s)):\n{joined}")


def _parse_index_array(value: str) -> list[int] | None:
    if not value.startswith("["):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(i, int) for i in parsed):
        return None
    return parsed


def validate_techlingo_course(course: TLCourse) -> List[str]:
    """Return a list of problems; empty list means the output is valid."""
    problems: List[str] = []

    if course.schema_version != SCHEMA_VERSION:
        problems.append(f"course.schema_version must be {SCHEMA_VERSION!r}, got {course.schema_version!r}.")
    if not course.import_key.strip():
        problems.append("course.import_key is missing.")
    if not course.title.strip():
        problems.append("course.title is empty.")

    for mi, module in enumerate(course.modules):
        mpath = f"modules[{mi}]"
        if not module.import_key.strip():
            problems.append(f"{mpath}.import_key is missing.")
        if not module.title.strip():
            problems.append(f"{mpath}.title is empty.")

        for ui, unit in enumerate(module.lessons):
            upath = f"{mpath}.lessons[{ui}]"
            if not unit.import_key.strip():
                problems.append(f"{upath}.import_key is missing.")
            if not unit.title.strip():
                problems.append(f"{upath}.title is empty.")

            for qi, q in enumerate(unit.exercises):
                qpath = f"{upath}.exercises[{qi}]"
                if not q.import_key.strip():
                    problems.append(f"{qpath}.import_key is missing.")
                if q.question_type not in _VALID_TYPES:
                    problems.append(f"{qpath}.question_type {q.question_type!r} is not a TechLingo type.")
                if not q.question_text.strip():
                    problems.append(f"{qpath}.question_text is empty.")
                if not (q.correct_answer or "").strip():
                    problems.append(f"{qpath}.correct_answer is empty.")

                if q.question_type == "multiple_choice":
                    problems.extend(_check_multiple_choice(q, qpath))
                elif q.question_type == "true_false":
                    if q.correct_answer not in {"true", "false"}:
                        problems.append(f"{qpath}.correct_answer must be 'true' or 'false'.")
                elif q.question_type == "fill_blank":
                    problems.extend(_check_fill_blank(q, qpath))
                elif q.question_type == "arrange_sentence":
                    problems.extend(_check_arrange(q, qpath))

            for fi, fc in enumerate(unit.flashcards):
                fpath = f"{upath}.flashcards[{fi}]"
                if not fc.import_key.strip():
                    problems.append(f"{fpath}.import_key is missing.")
                if not fc.front.strip() or not fc.back.strip():
                    problems.append(f"{fpath} front/back must be non-empty.")

    return problems


def _check_multiple_choice(q, qpath: str) -> List[str]:
    problems: List[str] = []
    opts = q.options.get("options")
    if not isinstance(opts, list) or not opts:
        problems.append(f"{qpath}.options.options[] is missing or empty.")
        return problems
    correct_idxs = [i for i, o in enumerate(opts) if o.get("is_correct")]

    arr = _parse_index_array(q.correct_answer)
    if arr is not None:
        # Multi-answer form.
        if len(arr) < 2:
            problems.append(f"{qpath}: multi-answer must have at least 2 correct options.")
        if sorted(arr) != sorted(correct_idxs):
            problems.append(f"{qpath}.correct_answer {q.correct_answer!r} does not match is_correct flags {correct_idxs}.")
    else:
        # Single-answer form: must be a bare index.
        if not q.correct_answer.isdigit():
            problems.append(f"{qpath}.correct_answer must be an index like '0' or an array like '[0,1]'.")
        elif [int(q.correct_answer)] != correct_idxs:
            problems.append(f"{qpath}.correct_answer {q.correct_answer!r} does not match the single correct option {correct_idxs}.")
    return problems


def _check_fill_blank(q, qpath: str) -> List[str]:
    problems: List[str] = []
    parts = q.options.get("parts")
    if not isinstance(parts, list):
        problems.append(f"{qpath}.options.parts[] is missing.")
        return problems
    gap_count = sum(1 for p in parts if p.get("type") == "gap")
    if gap_count != 1:
        problems.append(f"{qpath}: fill_blank must have exactly one gap, got {gap_count}.")
    return problems


def _check_arrange(q, qpath: str) -> List[str]:
    problems: List[str] = []
    correct_order = q.options.get("correct_order")
    if not isinstance(correct_order, list) or not correct_order:
        problems.append(f"{qpath}.options.correct_order[] is missing or empty.")
        return problems
    expected = " ".join(correct_order)
    if q.correct_answer != expected:
        problems.append(f"{qpath}.correct_answer must be the joined correct_order ({expected!r}).")
    return problems
