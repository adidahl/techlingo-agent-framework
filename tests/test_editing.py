"""Tests for post-run course editing (editing.py): manual edits, save/re-emit,
and single-question regeneration over a fake backend.

Run without pytest:  PYTHONPATH=src python tests/test_editing.py
Or with pytest:      PYTHONPATH=src pytest tests/test_editing.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from techlingo_workflow.editing import (
    EditError,
    apply_exercise_edit,
    infer_config_from_course,
    load_internal_course,
    regenerate_exercise,
    resolve_course_key,
    save_course,
)
from techlingo_workflow.llm import LLMClient
from techlingo_workflow.models import (
    BloomsLevel,
    ChoiceOption,
    Course,
    Lesson,
    Module,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
)


def _single_choice(prompt: str = "Which capability creates new content?") -> SingleChoiceExercise:
    return SingleChoiceExercise(
        blooms_level=BloomsLevel.remembering,
        prompt=prompt,
        concept_id="c1",
        options=[
            ChoiceOption(text="Generative AI", is_correct=True, rationale="r"),
            ChoiceOption(text="A camera", is_correct=False, error_type="e", rationale="r", better_fit="b"),
            ChoiceOption(text="A ruler", is_correct=False, error_type="e", rationale="r", better_fit="b"),
            ChoiceOption(text="A chair", is_correct=False, error_type="e", rationale="r", better_fit="b"),
        ],
        feedback_for_correct="Correct.",
    )


def _true_false() -> TrueFalseExercise:
    return TrueFalseExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="Evaluate the statement.",
        concept_id="c1",
        statement="Generative AI only produces text.",
        correct_answer=False,
        feedback_for_correct="Right.",
    )


def _rearrange() -> RearrangeExercise:
    return RearrangeExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="Arrange the steps.",
        concept_id="c1",
        word_bank=["data", "Gather", "model", "the", "Train"],
        correct_order=["Gather", "data", "Train", "the", "model"],
    )


def _course() -> Course:
    return Course(
        title="Edit Test Course",
        modules=[
            Module(
                title="M1",
                lessons=[
                    Lesson(
                        title="L1",
                        slo="Understand generative AI.",
                        exercises=[_single_choice(), _true_false(), _rearrange()],
                    )
                ],
            )
        ],
    )


def _write_run_dir(tmp: Path, course: Course) -> Path:
    run_dir = tmp / "run-test"
    run_dir.mkdir()
    (run_dir / "course.internal.json").write_text(
        json.dumps(course.model_dump(mode="json")), encoding="utf-8"
    )
    (run_dir / "course.json").write_text(
        json.dumps({"import_key": "stable-key-123"}), encoding="utf-8"
    )
    return run_dir


class FakeBackend:
    name = "fake"
    model_label = "fake:model"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    async def complete(self, prompt, *, system, response_model=None, timeout_s=0.0):
        self.prompts.append(prompt)
        return self.outputs.pop(0)


# ---------------------------------------------------------------------------


def test_apply_exercise_edit_updates_course():
    course = _course()
    data = course.modules[0].lessons[0].exercises[0].model_dump(mode="json")
    data["prompt"] = "An improved, more scenario-driven prompt?"
    edited = apply_exercise_edit(course, 0, 0, 0, data)
    assert edited.prompt.startswith("An improved")
    assert course.modules[0].lessons[0].exercises[0].prompt == edited.prompt


def test_apply_exercise_edit_rejects_type_change():
    course = _course()
    data = course.modules[0].lessons[0].exercises[1].model_dump(mode="json")  # true_false
    data["question_type"] = "single_choice"
    data["options"] = [{"text": "x", "is_correct": True}]
    try:
        apply_exercise_edit(course, 0, 0, 1, data)
        assert False, "type change accepted"
    except EditError as e:
        assert "question_type" in str(e)


def test_apply_exercise_edit_rejects_bad_schema():
    course = _course()
    try:
        apply_exercise_edit(course, 0, 0, 0, {"question_type": "single_choice", "prompt": 42})
        assert False, "invalid exercise accepted"
    except EditError:
        pass


def test_apply_exercise_edit_rejects_bad_index():
    course = _course()
    try:
        apply_exercise_edit(course, 0, 0, 99, {})
        assert False, "bad index accepted"
    except EditError as e:
        assert "exercises[99]" in str(e)


def test_save_course_writes_and_reemits():
    with tempfile.TemporaryDirectory() as tmp:
        course = _course()
        run_dir = _write_run_dir(Path(tmp), course)
        data = course.modules[0].lessons[0].exercises[0].model_dump(mode="json")
        data["prompt"] = "Edited prompt for the emit test?"
        apply_exercise_edit(course, 0, 0, 0, data)
        report = save_course(run_dir, course)

        internal = json.loads((run_dir / "course.internal.json").read_text())
        assert internal["modules"][0]["lessons"][0]["exercises"][0]["prompt"].startswith("Edited prompt")
        tl = json.loads((run_dir / "course.json").read_text())
        assert tl["import_key"] == "stable-key-123"  # stable key survives re-emit
        assert tl["modules"], "TechLingo-native course re-emitted"
        assert (run_dir / "validation_report.json").exists()
        assert report.issues is not None  # report produced (ok-ness depends on gates)


def test_save_course_rearrange_word_bank_reshuffled():
    with tempfile.TemporaryDirectory() as tmp:
        course = _course()
        run_dir = _write_run_dir(Path(tmp), course)
        ex = course.modules[0].lessons[0].exercises[2]
        ex.word_bank = list(ex.correct_order)  # author pastes the solved order
        save_course(run_dir, course)
        saved = json.loads((run_dir / "course.internal.json").read_text())
        wb = saved["modules"][0]["lessons"][0]["exercises"][2]["word_bank"]
        co = saved["modules"][0]["lessons"][0]["exercises"][2]["correct_order"]
        assert sorted(wb) == sorted(co) and wb != co  # normalized back to a shuffle


def test_save_course_rejects_broken_emit_and_leaves_files_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        course = _course()
        run_dir = _write_run_dir(Path(tmp), course)
        before = (run_dir / "course.internal.json").read_text()
        for opt in course.modules[0].lessons[0].exercises[0].options:
            opt.is_correct = False  # no correct answer -> invalid TechLingo output
        try:
            save_course(run_dir, course)
            assert False, "broken emit accepted"
        except EditError as e:
            assert "invalid TechLingo course" in str(e)
        assert (run_dir / "course.internal.json").read_text() == before  # untouched


def test_infer_config_matches_observed_shape():
    config = infer_config_from_course(_course())
    assert config.exercises_per_lesson == 3
    assert config.question_type_distribution == {
        "single_choice": 1, "true_false": 1, "rearrange": 1,
    }
    assert config.modules_count == 1


def test_resolve_course_key_prefers_existing():
    with tempfile.TemporaryDirectory() as tmp:
        course = _course()
        run_dir = _write_run_dir(Path(tmp), course)
        assert resolve_course_key(run_dir, course) == "stable-key-123"
        (run_dir / "course.json").unlink()
        assert resolve_course_key(run_dir, course) == "edit-test-course"


def test_regenerate_exercise_splices_and_pins_identity():
    course = _course()
    improved = course.modules[0].lessons[0].exercises[0].model_dump(mode="json")
    improved["prompt"] = "A rich scenario: a hospital team needs to draft patient letters..."
    improved["blooms_level"] = "Applying"       # model drifted -> must be pinned back
    improved["concept_id"] = "c999"             # model drifted -> must be pinned back
    fake = FakeBackend([json.dumps(improved)])
    llm = LLMClient(backend=fake)
    regen = asyncio.run(regenerate_exercise(course, 0, 0, 0, llm, "make it scenario-based"))
    assert regen.prompt.startswith("A rich scenario")
    assert regen.blooms_level == BloomsLevel.remembering  # pinned
    assert regen.concept_id == "c1"  # pinned
    assert course.modules[0].lessons[0].exercises[0].prompt == regen.prompt
    assert "make it scenario-based" in fake.prompts[0]  # author note reached the model
    assert "MUST stay \"single_choice\"" in fake.prompts[0]


def test_regenerate_rejects_true_false_answer_flip():
    course = _course()
    flipped = course.modules[0].lessons[0].exercises[1].model_dump(mode="json")
    flipped["statement"] = "Generative AI can produce images."
    flipped["correct_answer"] = True  # flip vs original False
    fake = FakeBackend([json.dumps(flipped)])
    llm = LLMClient(backend=fake)
    try:
        asyncio.run(regenerate_exercise(course, 0, 0, 1, llm, None))
        assert False, "answer flip accepted"
    except EditError as e:
        assert "flipped" in str(e)
    # original untouched
    assert course.modules[0].lessons[0].exercises[1].correct_answer is False


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return failures


if __name__ == "__main__":
    import sys

    sys.exit(1 if _run_all() else 0)
