from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from techlingo_workflow.models import (
    Course,
    Feedback,
    MultiChoiceExercise,
    SingleChoiceExercise,
)
from .models import ChoiceUIOption


def list_runs(outputs_dir: Path) -> list[Path]:
    if not outputs_dir.exists():
        return []
    runs = sorted(outputs_dir.glob("run-*"), key=lambda p: p.name, reverse=True)
    return [p for p in runs if p.is_dir()]


def load_course(run_dir: Path) -> Course:
    course_path = run_dir / "course.json"
    if not course_path.exists():
        raise FileNotFoundError(f"Missing course.json at {course_path}")
    data: dict[str, Any] = json.loads(course_path.read_text(encoding="utf-8"))
    data = _coerce_v1_to_v2(data)
    return Course.model_validate(data)


def load_json_preview(path: Path, max_chars: int = 80_000) -> str:
    txt = path.read_text(encoding="utf-8", errors="replace")
    if len(txt) <= max_chars:
        return txt
    return txt[:max_chars] + "\n\n…(truncated)…\n"


def flatten_exercises(course: Course) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for mi, mod in enumerate(course.modules):
        for li, lesson in enumerate(mod.lessons):
            for ei, ex in enumerate(lesson.exercises):
                flat.append(
                    {
                        "module_index": mi,
                        "lesson_index": li,
                        "exercise_index": ei,
                        "module_title": mod.title,
                        "lesson_title": lesson.title,
                        "slo": lesson.slo,
                        "blooms_level": ex.blooms_level.value,
                        "question_type": ex.question_type,
                        "exercise": ex,
                    }
                )
    return flat


def choice_options_for_exercise(ex: SingleChoiceExercise | MultiChoiceExercise, *, seed: int) -> list[ChoiceUIOption]:
    opts: list[ChoiceUIOption] = []
    for oi, o in enumerate(ex.options):
        fb = o.feedback if isinstance(o.feedback, Feedback) else None
        opts.append(ChoiceUIOption(id=str(oi), label=o.text, is_correct=o.is_correct, feedback=fb, rationale=o.rationale))
    rnd = random.Random(seed)
    rnd.shuffle(opts)
    return opts


def normalize_text(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def accepted_match(user: str, accepted: list[str]) -> bool:
    u = normalize_text(user)
    return any(u == normalize_text(a) for a in accepted)


def _coerce_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """
    Best-effort compat shim for older runs.
    """
    schema_version = str(data.get("schema_version") or "v1")
    if schema_version == "v2":
        return data

    if "modules" not in data:
        return data

    for mod in data.get("modules", []):
        for lesson in mod.get("lessons", []):
            lesson.setdefault("flashcards", [])
            exercises = lesson.get("exercises", [])
            new_exercises: list[dict[str, Any]] = []
            for ex in exercises:
                if "question_type" in ex and "prompt" in ex:
                    new_exercises.append(ex)
                    continue

                prompt = ex.get("question_text", "")
                correct_answer = ex.get("correct_answer", "")
                distractors = ex.get("distractors", []) or []

                options: list[dict[str, Any]] = []
                options.append({"text": str(correct_answer), "is_correct": True, "error_type": None, "feedback": None})
                for d in distractors:
                    fb = d.get("feedback")
                    options.append(
                        {
                            "text": str(d.get("text", "")),
                            "is_correct": False,
                            "error_type": d.get("error_type") or "distractor",
                            "feedback": fb,
                        }
                    )

                options = options[:4]
                while len(options) < 4:
                    options.append(
                        {
                            "text": "None of the above.",
                            "is_correct": False,
                            "error_type": "filler",
                            "feedback": None,
                        }
                    )

                new_exercises.append(
                    {
                        "blooms_level": ex.get("blooms_level"),
                        "question_type": "single_choice",
                        "prompt": str(prompt),
                        "options": options,
                        "feedback_for_correct": ex.get("feedback_for_correct"),
                    }
                )
            lesson["exercises"] = new_exercises

    data["schema_version"] = "v2"
    return data
