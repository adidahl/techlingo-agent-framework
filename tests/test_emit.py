"""Tests for the deterministic internal -> TechLingo transform (emit.py).

Run without pytest:  PYTHONPATH=src python tests/test_emit.py
Or with pytest:      PYTHONPATH=src pytest tests/test_emit.py
"""

from __future__ import annotations

import json

from techlingo_workflow.emit import build_techlingo_course, emit_and_validate, slugify
from techlingo_workflow.models import (
    BloomsLevel,
    ChoiceOption,
    Course,
    Feedback,
    FillGapsExercise,
    FillGapsGapPart,
    FillGapsTextPart,
    Flashcard,
    Lesson,
    Module,
    MultiChoiceExercise,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
)
from techlingo_workflow.validate_techlingo import validate_techlingo_course


def _single_choice() -> SingleChoiceExercise:
    return SingleChoiceExercise(
        blooms_level=BloomsLevel.remembering,
        prompt="What is generative AI?",
        options=[
            ChoiceOption(text="Creates human-like content", is_correct=True, rationale="r"),
            ChoiceOption(text="A camera", is_correct=False, error_type="e", rationale="r", better_fit="b"),
            ChoiceOption(text="A block", is_correct=False, error_type="e", rationale="r", better_fit="b"),
            ChoiceOption(text="A person", is_correct=False, error_type="e", rationale="r", better_fit="b"),
        ],
        feedback_for_correct="Correct.",
    )


def _multi_choice() -> MultiChoiceExercise:
    return MultiChoiceExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="Which are examples?",
        options=[
            ChoiceOption(text="Creates content", is_correct=True, rationale="r"),
            ChoiceOption(text="Uses patterns", is_correct=True, rationale="r"),
            ChoiceOption(text="Magic", is_correct=False, error_type="e", rationale="r", better_fit="b"),
            ChoiceOption(text="Fixed answer", is_correct=False, error_type="e", rationale="r", better_fit="b"),
        ],
        feedback_for_correct="Correct.",
    )


def _true_false() -> TrueFalseExercise:
    return TrueFalseExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="Mark true or false.",
        statement="Generative AI creates human-like content.",
        correct_answer=True,
        feedback_for_correct="Yes.",
        feedback_for_incorrect=Feedback(intrinsic="i", instructional="x"),
    )


def _fill_gaps(accepted=None) -> FillGapsExercise:
    return FillGapsExercise(
        blooms_level=BloomsLevel.applying,
        prompt="Complete the sentence.",
        parts=[
            FillGapsTextPart(text="Generative AI can create "),
            FillGapsGapPart(accepted_answers=accepted or ["human-like content"], placeholder="content"),
            FillGapsTextPart(text=" using learned patterns."),
        ],
    )


def _rearrange() -> RearrangeExercise:
    tokens = ["Generative", "AI", "creates", "human-like", "content"]
    return RearrangeExercise(
        blooms_level=BloomsLevel.analyzing_evaluating,
        prompt="Reconstruct the sentence.",
        word_bank=list(reversed(tokens)),
        correct_order=tokens,
    )


def _course(exercises, flashcards=None, module_title="Responsible AI", lesson_title="Fairness and accountability") -> Course:
    lesson = Lesson(
        title=lesson_title,
        slo="Explain fairness.",
        exercises=exercises,
        flashcards=flashcards or [],
    )
    return Course(title="AI-900: Fundamentals", modules=[Module(title=module_title, lessons=[lesson])])


def test_slugify():
    assert slugify("Fairness and Accountability") == "fairness-and-accountability"
    assert slugify("Čašća nač!!") == "casca-nac"
    assert slugify("") == "item"
    assert slugify("   ---   ") == "item"


def test_single_choice_index_answer():
    tl = build_techlingo_course(_course([_single_choice()]), course_key="ai-900")
    q = tl.modules[0].lessons[0].exercises[0]
    assert q.question_type == "multiple_choice"
    assert q.question_text == "What is generative AI?"
    assert q.correct_answer == "0"
    assert q.options["original_question_type"] == "single_choice"
    assert q.options["blooms_level"] == "Remembering"
    assert q.explanation == "Correct."
    assert len(q.options["options"]) == 4


def test_multi_choice_index_array():
    tl = build_techlingo_course(_course([_multi_choice()]), course_key="ai-900")
    q = tl.modules[0].lessons[0].exercises[0]
    assert q.question_type == "multiple_choice"
    assert q.correct_answer == "[0,1]"  # compact, no whitespace
    assert q.options["original_question_type"] == "multi_choice"


def test_true_false_uses_statement():
    tl = build_techlingo_course(_course([_true_false()]), course_key="ai-900")
    q = tl.modules[0].lessons[0].exercises[0]
    assert q.question_type == "true_false"
    assert q.question_text == "Generative AI creates human-like content."
    assert q.correct_answer == "true"
    assert q.options["feedback_for_incorrect"] == {"intrinsic": "i", "instructional": "x"}


def test_fill_blank_single_answer():
    tl = build_techlingo_course(_course([_fill_gaps()]), course_key="ai-900")
    q = tl.modules[0].lessons[0].exercises[0]
    assert q.question_type == "fill_blank"
    assert q.question_text == "Generative AI can create ___ using learned patterns."
    assert q.correct_answer == "human-like content"
    assert q.options["original_question_type"] == "fill_gaps"


def test_fill_blank_multiple_answers():
    tl = build_techlingo_course(
        _course([_fill_gaps(accepted=["human-like content", "content like a human"])]),
        course_key="ai-900",
    )
    q = tl.modules[0].lessons[0].exercises[0]
    assert q.correct_answer == json.dumps(
        ["human-like content", "content like a human"], separators=(",", ":")
    )


def test_arrange_joined_sentence():
    tl = build_techlingo_course(_course([_rearrange()]), course_key="ai-900")
    q = tl.modules[0].lessons[0].exercises[0]
    assert q.question_type == "arrange_sentence"
    assert q.correct_answer == "Generative AI creates human-like content"
    assert q.correct_answer.split(" ") == q.options["correct_order"]


def test_positional_import_keys():
    fcs = [Flashcard(front="What is a token?", back="A unit of text."), Flashcard(front="B", back="b")]
    tl = build_techlingo_course(_course([_single_choice(), _true_false()], flashcards=fcs), course_key="ai-900")
    unit = tl.modules[0].lessons[0]
    assert tl.import_key == "ai-900"
    assert tl.modules[0].import_key == "responsible-ai"
    assert unit.import_key == "fairness-and-accountability"
    assert [q.import_key for q in unit.exercises] == [
        "fairness-and-accountability-q1",
        "fairness-and-accountability-q2",
    ]
    assert [f.import_key for f in unit.flashcards] == [
        "fairness-and-accountability-f0",
        "fairness-and-accountability-f1",
    ]


def test_duplicate_titles_get_unique_keys():
    lessons = [
        Lesson(title="Intro", slo="s", exercises=[_single_choice()]),
        Lesson(title="Intro", slo="s", exercises=[_single_choice()]),
    ]
    course = Course(title="C", modules=[Module(title="M", lessons=lessons)])
    tl = build_techlingo_course(course, course_key="c")
    keys = [u.import_key for u in tl.modules[0].lessons]
    assert keys == ["intro", "intro-2"]


def test_emit_and_validate_passes():
    course = _course(
        [_single_choice(), _multi_choice(), _true_false(), _fill_gaps(), _rearrange()],
        flashcards=[Flashcard(front="f", back="b")],
    )
    tl = emit_and_validate(course, course_key="ai-900")
    assert validate_techlingo_course(tl) == []


def test_emit_arrange_expands_interchangeable_groups():
    ra = _rearrange()
    ra.interchangeable_groups = [[1, 2]]
    tl = build_techlingo_course(_course([ra]), course_key="k")
    q = tl.modules[0].lessons[0].exercises[0]
    orders = q.options["accepted_orders"]
    assert orders[0] == ra.correct_order  # canonical first
    assert len(orders) == 2
    swapped = list(ra.correct_order)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    assert swapped in orders
    assert q.options["interchangeable_groups"] == [[1, 2]]


def test_emit_arrange_without_groups_omits_accepted_orders():
    tl = build_techlingo_course(_course([_rearrange()]), course_key="k")
    assert "accepted_orders" not in tl.modules[0].lessons[0].exercises[0].options


def test_emit_fill_blank_carries_rejected_answers():
    fg = _fill_gaps()
    gap = next(p for p in fg.parts if getattr(p, "type", None) == "gap")
    gap.rejected_answers = ["SLM"]
    tl = build_techlingo_course(_course([fg]), course_key="k")
    q = tl.modules[0].lessons[0].exercises[0]
    assert q.options["rejected_answers"] == ["SLM"]
    gap_part = next(p for p in q.options["parts"] if p.get("type") == "gap")
    assert gap_part["rejected_answers"] == ["SLM"]


def test_expand_accepted_orders_bounded_and_deduped():
    from techlingo_workflow.emit import expand_accepted_orders

    order = ["a", "b", "c", "d"]
    orders = expand_accepted_orders(order, [[0, 1], [2, 3]])  # 2 independent pairs -> 4
    assert orders[0] == order and len(orders) == 4
    assert ["b", "a", "d", "c"] in orders
    # identical tokens permute into duplicates -> deduped to just the canonical
    assert expand_accepted_orders(["x", "x", "y"], [[0, 1]]) == [["x", "x", "y"]]


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
