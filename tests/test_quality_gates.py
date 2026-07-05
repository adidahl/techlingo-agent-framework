"""Tests for the deterministic content-quality gates and normalization.

Run without pytest:  PYTHONPATH=src python tests/test_quality_gates.py
Or with pytest:      PYTHONPATH=src pytest tests/test_quality_gates.py
"""

from __future__ import annotations

from collections import Counter

from techlingo_workflow.config import WorkflowConfig
from techlingo_workflow.emit import build_techlingo_course
from techlingo_workflow.executors import (
    _failed_lesson_keys,
    _restore_concept_metadata,
    _seed_concepts_from_map,
    _tf_answer_patterns,
)
from techlingo_workflow.models import (
    BloomsLevel,
    ChoiceOption,
    ConceptAtom,
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
from techlingo_workflow.models import ValidationIssue, ValidationReport
from techlingo_workflow.validate import (
    _content_quality_issues,
    attempt_badness,
    issues_by_lesson,
    normalize_course,
    validate_course,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _small_config() -> WorkflowConfig:
    return WorkflowConfig(
        modules_count=1,
        min_lessons_total=1,
        max_lessons_total=1,
        exercises_per_lesson=5,
        flashcards_per_lesson=2,
        blooms_distribution={
            "Remembering": 1,
            "Understanding": 2,
            "Applying": 1,
            "Analyzing/Evaluating": 1,
        },
        question_type_distribution={
            "single_choice": 1,
            "multi_choice": 1,
            "true_false": 1,
            "fill_gaps": 1,
            "rearrange": 1,
        },
    )


def _concepts() -> list[ConceptAtom]:
    return [
        ConceptAtom(
            id="object-detection",
            label="Object detection",
            summary="Object detection locates specific objects in an image.",
            confusable_with=["image-classification"],
        ),
        ConceptAtom(
            id="image-classification",
            label="Image classification",
            summary="Image classification predicts the main subject label of an image.",
            confusable_with=["object-detection"],
        ),
    ]


def _sc(concept_id="object-detection", blooms=BloomsLevel.applying, prompt=None) -> SingleChoiceExercise:
    fb = Feedback(intrinsic="i", instructional="x")
    return SingleChoiceExercise(
        blooms_level=blooms,
        concept_id=concept_id,
        prompt=prompt
        or "You are building a shelf-monitoring app and must locate every bottle in a photo. Which capability fits this decision?",
        options=[
            ChoiceOption(text="Object detection", is_correct=True, rationale="r"),
            ChoiceOption(text="Image classification", is_correct=False, error_type="confuses siblings", rationale="r", better_fit="b", feedback=fb),
            ChoiceOption(text="Speech recognition", is_correct=False, error_type="wrong modality", rationale="r", better_fit="b", feedback=fb),
            ChoiceOption(text="Sentiment analysis", is_correct=False, error_type="wrong modality", rationale="r", better_fit="b", feedback=fb),
        ],
        feedback_for_correct="Correct.",
    )


def _mc(concept_id="image-classification", blooms=BloomsLevel.analyzing_evaluating) -> MultiChoiceExercise:
    fb = Feedback(intrinsic="i", instructional="x")
    return MultiChoiceExercise(
        blooms_level=blooms,
        concept_id=concept_id,
        prompt="You are triaging a photo archive and must tag each photo's main subject. Which decisions rely on labels predicted for whole images?",
        options=[
            ChoiceOption(text="Tagging each photo's subject", is_correct=True, rationale="r"),
            ChoiceOption(text="Routing photos into albums by subject", is_correct=True, rationale="r"),
            ChoiceOption(text="Drawing boxes around each bottle", is_correct=False, error_type="confuses siblings", rationale="r", better_fit="b", feedback=fb),
            ChoiceOption(text="Transcribing an interview recording", is_correct=False, error_type="wrong modality", rationale="r", better_fit="b", feedback=fb),
        ],
        feedback_for_correct="Correct.",
    )


def _tf(concept_id="object-detection", statement=None, answer=False) -> TrueFalseExercise:
    return TrueFalseExercise(
        blooms_level=BloomsLevel.understanding,
        concept_id=concept_id,
        prompt="True or false?",
        statement=statement or "Image classification identifies the location of specific objects in an image.",
        correct_answer=answer,
        feedback_for_correct="Yes.",
        feedback_for_incorrect=Feedback(intrinsic="i", instructional="x"),
    )


def _fg(concept_id="image-classification") -> FillGapsExercise:
    return FillGapsExercise(
        blooms_level=BloomsLevel.remembering,
        concept_id=concept_id,
        prompt="Fill in the missing term.",
        parts=[
            FillGapsTextPart(text="A model trained with labeled images to predict the main subject performs "),
            FillGapsGapPart(accepted_answers=["image classification"], placeholder="term"),
        ],
    )


def _ra(concept_id="object-detection") -> RearrangeExercise:
    tokens = ["Collect labeled images", "train the model", "run it on new photos", "review detected objects"]
    return RearrangeExercise(
        blooms_level=BloomsLevel.understanding,
        concept_id=concept_id,
        prompt="Order the steps.",
        word_bank=list(reversed(tokens)),
        correct_order=tokens,
    )


def _lesson(exercises=None, concepts=None) -> Lesson:
    return Lesson(
        title="Computer Vision Tasks",
        slo="Distinguish object detection from image classification.",
        concepts=_concepts() if concepts is None else concepts,
        exercises=exercises if exercises is not None else [_sc(), _mc(), _tf(), _fg(), _ra()],
        flashcards=[Flashcard(front="f1", back="b1"), Flashcard(front="f2", back="b2")],
    )


def _course(lessons=None) -> Course:
    return Course(title="CV", modules=[Module(title="Vision", lessons=lessons or [_lesson()])])


def _errors(report_or_issues):
    issues = getattr(report_or_issues, "issues", report_or_issues)
    return [i for i in issues if i.severity == "error"]


# ---------------------------------------------------------------------------
# Config feasibility
# ---------------------------------------------------------------------------

def test_default_config_is_coupling_feasible():
    WorkflowConfig()  # must not raise


def test_config_rejects_infeasible_bloom_type_coupling():
    try:
        WorkflowConfig(
            exercises_per_lesson=5,
            blooms_distribution={"Remembering": 1, "Understanding": 1, "Applying": 2, "Analyzing/Evaluating": 1},
            question_type_distribution={"single_choice": 1, "multi_choice": 1, "true_false": 1, "fill_gaps": 1, "rearrange": 1},
        )
    except ValueError as e:
        assert "Applying" in str(e)
    else:
        raise AssertionError("Infeasible Bloom/type coupling config was accepted.")


# ---------------------------------------------------------------------------
# validate_course: coupling + concept coverage
# ---------------------------------------------------------------------------

def test_valid_fixture_course_passes():
    report = validate_course(_course(), _small_config())
    assert _errors(report) == [], [i.message for i in _errors(report)]


def test_higher_order_bloom_on_rearrange_is_rejected():
    bad = _ra()
    bad.blooms_level = BloomsLevel.analyzing_evaluating
    # Swap Bloom levels so the distribution still matches the config.
    course = _course([_lesson(exercises=[_sc(), _mc(blooms=BloomsLevel.understanding), _tf(), _fg(), bad])])
    report = validate_course(course, _small_config())
    assert any("cannot carry Bloom level" in i.message for i in _errors(report))


def test_missing_concept_id_is_rejected():
    ex = _sc(concept_id=None)
    course = _course([_lesson(exercises=[ex, _mc(), _tf(), _fg(), _ra()])])
    report = validate_course(course, _small_config())
    assert any("must set concept_id" in i.message for i in _errors(report))


def test_unknown_concept_id_is_rejected():
    ex = _sc(concept_id="not-a-real-concept")
    course = _course([_lesson(exercises=[ex, _mc(), _tf(), _fg(), _ra()])])
    report = validate_course(course, _small_config())
    assert any("not one of this lesson's concepts" in i.message for i in _errors(report))


def test_overdrilled_concept_is_rejected():
    # All 5 exercises on object-detection: cap is max(ceil(5/2)+1, 2) = 4,
    # so 5 on one concept must be rejected.
    course = _course([
        _lesson(exercises=[_sc(), _mc("object-detection"), _tf("object-detection"), _fg("object-detection"), _ra("object-detection")])
    ])
    report = validate_course(course, _small_config())
    assert any("over-drilled" in i.message for i in _errors(report))


def test_one_over_even_split_is_tolerated():
    # 3 on object-detection, 2 on image-classification: within the +1 slack.
    course = _course([
        _lesson(exercises=[_sc(), _mc("object-detection"), _tf("image-classification"), _fg(), _ra("object-detection")])
    ])
    report = validate_course(course, _small_config())
    assert not any("over-drilled" in i.message for i in _errors(report))


def test_low_distinct_concept_coverage_is_rejected():
    # 5 concepts available, 5 exercises, but only 2 distinct concepts used.
    five_concepts = _concepts() + [
        ConceptAtom(id=f"extra-{i}", label=f"Extra {i}", summary=f"Extra fact {i}.") for i in range(3)
    ]
    course = _course([
        _lesson(
            concepts=five_concepts,
            exercises=[_sc(), _mc("object-detection"), _tf("image-classification"), _fg(), _ra("image-classification")],
        )
    ])
    report = validate_course(course, _small_config())
    assert any("distinct concepts" in i.message for i in _errors(report))


def test_lessons_without_concepts_skip_concept_checks():
    course = _course([_lesson(concepts=[], exercises=[_sc(None), _mc(None), _tf(None), _fg(None), _ra(None)])])
    report = validate_course(course, _small_config())
    assert not any("concept" in i.message.lower() for i in _errors(report))


# ---------------------------------------------------------------------------
# Content-quality gates
# ---------------------------------------------------------------------------

def test_same_fact_in_two_wrappers_is_flagged():
    # A true_false statement and a fill_gaps sentence carrying the same fact.
    tf = _tf(statement="Generative AI can create new content such as text and images.", answer=True)
    fg = FillGapsExercise(
        blooms_level=BloomsLevel.remembering,
        prompt="Fill in the missing term.",
        parts=[
            FillGapsTextPart(text="Generative AI can create new content such as text and "),
            FillGapsGapPart(accepted_answers=["images"]),
        ],
    )
    lesson = _lesson(exercises=[tf, fg], concepts=[])
    issues = _content_quality_issues(_course([lesson]))
    assert any("Near-duplicate" in i.message for i in _errors(issues))


def test_distinct_facts_are_not_flagged_as_duplicates():
    issues = _content_quality_issues(_course())
    assert not any("Near-duplicate" in i.message for i in _errors(issues))


def test_all_true_statements_are_flagged():
    tfs = [
        _tf(statement="Object detection locates objects in images.", answer=True),
        _tf(statement="Image classification predicts a label for a whole image.", answer=True),
        _tf(statement="Speech recognition transcribes audio into text.", answer=True),
    ]
    issues = _content_quality_issues(_course([_lesson(exercises=tfs, concepts=[])]))
    assert any("correct_answer=true" in i.message for i in _errors(issues))


def test_balanced_true_false_is_not_flagged():
    tfs = [
        _tf(statement="Object detection locates objects in images.", answer=True),
        _tf(statement="Image classification draws boxes around objects.", answer=False),
        _tf(statement="Speech recognition transcribes audio into text.", answer=True),
    ]
    issues = _content_quality_issues(_course([_lesson(exercises=tfs, concepts=[])]))
    assert not any("true_false" in i.path for i in _errors(issues))


def test_tautological_correct_option_is_flagged():
    ex = _sc(prompt="Which statements match language models? Language models learn from data.")
    ex.options[0].text = "Language models learn from data"
    issues = _content_quality_issues(_course([_lesson(exercises=[ex], concepts=[])]))
    assert any("tautology" in i.message for i in issues)


def test_rearrange_with_two_giveaway_chunks_is_rejected():
    ra = RearrangeExercise(
        blooms_level=BloomsLevel.understanding,
        concept_id="object-detection",
        prompt="Order the steps.",
        word_bank=["Responsible AI includes", "fairness and transparency."],
        correct_order=["Responsible AI includes", "fairness and transparency."],
    )
    course = _course([_lesson(exercises=[_sc(), _mc(), _tf(), _fg(), ra])])
    report = validate_course(course, _small_config())
    assert any("4-8 tokens" in i.message for i in _errors(report))


def test_rearrange_interchangeable_list_items_are_rejected():
    # "power chatbots, / create content, / translate text," can be reordered
    # freely and still be correct — the app grades one order, so this is unfair.
    ra = RearrangeExercise(
        blooms_level=BloomsLevel.understanding,
        concept_id="object-detection",
        prompt="Reconstruct the sentence.",
        word_bank=["create content,", "Generative AI is", "power chatbots,", "commonly used to",
                   "translate text,", "and summarize documents"],
        correct_order=["Generative AI is", "commonly used to", "power chatbots,",
                       "create content,", "translate text,", "and summarize documents"],
    )
    course = _course([_lesson(exercises=[_sc(), _mc(), _tf(), _fg(), ra])])
    report = validate_course(course, _small_config())
    assert any("ambiguous" in i.message and i.severity == "error" for i in report.issues)


def test_rearrange_order_forced_sentence_is_not_flagged_as_ambiguous():
    # A single mid-sentence comma (e.g. "After training,") does not make the
    # order interchangeable; the gate must not fire on order-forced content.
    ra = RearrangeExercise(
        blooms_level=BloomsLevel.understanding,
        concept_id="object-detection",
        prompt="Order the steps.",
        word_bank=["the model", "After training,", "labels new images", "automatically"],
        correct_order=["After training,", "the model", "labels new images", "automatically"],
    )
    course = _course([_lesson(exercises=[_sc(), _mc(), _tf(), _fg(), ra])])
    report = validate_course(course, _small_config())
    assert not any("ambiguous" in i.message for i in report.issues)


# ---------------------------------------------------------------------------
# normalize_course
# ---------------------------------------------------------------------------

def test_word_bank_is_shuffled_and_multiset_preserved():
    ra = _ra()
    ra.word_bank = list(ra.correct_order)  # LLM shipped it pre-arranged
    course = normalize_course(_course([_lesson(exercises=[_sc(), _mc(), _tf(), _fg(), ra])]))
    ra_out = next(e for l in course.modules[0].lessons for e in l.exercises if isinstance(e, RearrangeExercise))
    assert Counter(ra_out.word_bank) == Counter(ra_out.correct_order)
    assert ra_out.word_bank != ra_out.correct_order


def test_generic_stems_are_rotated_and_custom_prompts_kept():
    tf1 = _tf(statement="Object detection locates objects.", answer=True)
    tf1.prompt = "Is this statement true or false?"
    tf2 = _tf(statement="A model needs training data.", answer=False)
    tf2.prompt = "Considering a photo archive project, is this claim accurate?"
    course = normalize_course(_course([_lesson(exercises=[tf1, tf2], concepts=[])]))
    prompts = [e.prompt for l in course.modules[0].lessons for e in l.exercises]
    assert "Is this statement true or false?" not in prompts  # generic stem replaced
    assert "Considering a photo archive project, is this claim accurate?" in prompts  # custom kept


def test_exercises_are_ordered_easy_to_hard():
    course = normalize_course(_course([_lesson(exercises=[_mc(), _ra(), _sc(), _tf(), _fg()])]))
    ordered = course.modules[0].lessons[0].exercises
    blooms = [e.blooms_level for e in ordered]
    ranks = [
        {BloomsLevel.remembering: 0, BloomsLevel.understanding: 1, BloomsLevel.applying: 2, BloomsLevel.analyzing_evaluating: 3}[b]
        for b in blooms
    ]
    assert ranks == sorted(ranks)
    # Remembering fill_gaps first, Analyzing multi_choice last.
    assert ordered[0].question_type == "fill_gaps"
    assert ordered[-1].question_type == "multi_choice"


def test_attempt_badness_prefers_many_content_errors_over_one_structural():
    structural = ValidationReport(
        ok=False,
        issues=[ValidationIssue(severity="error", path="modules", message="Expected exactly 3 modules, got 1.")],
    )
    content = ValidationReport(
        ok=False,
        issues=[
            ValidationIssue(severity="error", path=f"modules[0].lessons[{i}].exercises[0]", message="Near-duplicate of something.")
            for i in range(7)
        ],
    )
    assert attempt_badness(content) < attempt_badness(structural)
    shape = ValidationReport(
        ok=False,
        issues=[ValidationIssue(severity="error", path="modules[0].lessons[0].exercises", message="Expected exactly 5 exercises, got 15.")],
    )
    assert attempt_badness(content) < attempt_badness(shape)
    assert attempt_badness(shape) < attempt_badness(structural)


# ---------------------------------------------------------------------------
# Chunked generation helpers
# ---------------------------------------------------------------------------

def test_tf_answer_patterns_alternate_course_wide():
    patterns = _tf_answer_patterns(3, 2)
    assert patterns == [[False, True, False], [True, False, True]]
    flat = [a for p in patterns for a in p]
    assert abs(flat.count(True) - flat.count(False)) <= 1


def _report(issues):
    return ValidationReport(ok=False, issues=issues)


def test_issues_by_lesson_groups_by_path():
    report = _report([
        ValidationIssue(severity="error", path="modules[0].lessons[1].exercises[2].prompt", message="x"),
        ValidationIssue(severity="error", path="modules[1].lessons[0].exercises", message="y"),
        ValidationIssue(severity="warning", path="modules[0].lessons[1].exercises[3]", message="z"),
        ValidationIssue(severity="error", path="modules", message="course-level"),
    ])
    grouped = issues_by_lesson(report)
    assert set(grouped) == {"0:1", "1:0"}
    assert len(grouped["0:1"]) == 2


def test_failed_lesson_keys_returns_only_failing_lessons():
    report = _report([
        ValidationIssue(severity="error", path="modules[0].lessons[1].exercises[2]", message="dup"),
        ValidationIssue(severity="warning", path="modules[2].lessons[0].exercises[0]", message="minor"),
    ])
    assert _failed_lesson_keys(report) == {"0:1"}


def test_failed_lesson_keys_requests_full_regen_on_course_level_error():
    report = _report([
        ValidationIssue(severity="error", path="modules", message="Expected exactly 3 modules, got 1."),
        ValidationIssue(severity="error", path="modules[0].lessons[0].exercises[1]", message="dup"),
    ])
    assert _failed_lesson_keys(report) is None


# ---------------------------------------------------------------------------
# Concept metadata plumbing
# ---------------------------------------------------------------------------

def test_emit_carries_concept_id():
    tl = build_techlingo_course(_course(), course_key="cv")
    types_to_concepts = {
        q.options["original_question_type"]: q.options.get("concept_id")
        for q in tl.modules[0].lessons[0].exercises
    }
    assert types_to_concepts["single_choice"] == "object-detection"
    assert types_to_concepts["fill_gaps"] == "image-classification"


def test_seed_concepts_from_map_fills_missing_concepts():
    course = _course([_lesson(concepts=[])])
    a1_map = {
        "modules": [
            {
                "lessons": [
                    {"concepts": [{"id": "x", "label": "X", "summary": "X is a thing.", "confusable_with": []}]}
                ]
            }
        ]
    }
    _seed_concepts_from_map(course, a1_map)
    assert [c.id for c in course.modules[0].lessons[0].concepts] == ["x"]


def test_restore_concept_metadata_reattaches_dropped_fields():
    prev = _course()
    new = _course([_lesson(concepts=[])])
    for lesson in new.modules[0].lessons:
        for ex in lesson.exercises:
            ex.concept_id = None
    _restore_concept_metadata(prev, new)
    new_lesson = new.modules[0].lessons[0]
    assert [c.id for c in new_lesson.concepts] == [c.id for c in prev.modules[0].lessons[0].concepts]
    assert [e.concept_id for e in new_lesson.exercises] == [
        e.concept_id for e in prev.modules[0].lessons[0].exercises
    ]


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
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    _run_all()
