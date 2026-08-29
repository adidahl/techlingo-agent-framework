"""Tests for Phase 2b cell-quota generation: worksheet expansion (worksheet.py),
prompt inputs (prompts.py), the variant-aware quality gates (validate.py), and
depth/rung plumbing into the workspace (course_build.py / graph_merge.py).

Run without pytest:  PYTHONPATH=src python tests/test_worksheet.py
Or with pytest:      PYTHONPATH=src pytest tests/test_worksheet.py
"""

from __future__ import annotations

import asyncio
from collections import Counter
from unittest.mock import patch

import techlingo_workflow.executors as workflow_executors
from techlingo_workflow.config import DifficultyLevel, WorkflowConfig
from techlingo_workflow.course_build import convert_course_result
from techlingo_workflow.executors import (
    _restore_concept_metadata,
    _seed_concepts_from_map,
    _stamp_default_depths,
    _validate_a1_map,
    _validation_retry_target,
)
from techlingo_workflow.graph_merge import merge_source_concepts
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
    PipelineState,
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
    ValidationIssue,
    ValidationReport,
)
from techlingo_workflow.prompts import (
    a1_modularizer_prompt,
    a2_lesson_prompt,
    a5_lesson_repair_prompt,
)
from techlingo_workflow.validate import (
    _content_quality_issues,
    _restore_lesson_depths,
    normalize_course,
    validate_course,
)
from techlingo_workflow.workspace import ConceptGraph, derive_rung
from techlingo_workflow.worksheet import (
    CELL_QUOTAS,
    WorksheetBudgetError,
    assign_tf_answers,
    build_lesson_worksheet,
    concept_cells,
    format_worksheet_rows,
    required_rungs,
    worksheet_applies,
    worksheet_blooms_distribution,
    worksheet_size_bounds,
    worksheet_type_distribution,
)
from techlingo_workflow.workflow import should_loop_to_a1, should_loop_to_a2

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _atoms() -> list[ConceptAtom]:
    return [
        ConceptAtom(
            id="vector-index",
            label="Vector index",
            summary="A vector index is a structure for fast similarity lookup over embeddings.",
            depth="fact",
            confusable_with=["index-refresh"],
        ),
        ConceptAtom(
            id="index-refresh",
            label="Index refresh",
            summary="An index refresh rebuilds stale entries after new data is ingested.",
            depth="mechanism",
            confusable_with=["vector-index"],
        ),
    ]


def _opt(text: str, correct: bool, *, scenario: bool = False) -> ChoiceOption:
    return ChoiceOption(
        text=text,
        is_correct=correct,
        error_type=None if correct else "misconception",
        rationale=f"Relevant because {text.lower()} relates to the tested behavior.",
        better_fit=None if correct else f"Would be right in a question about {text.lower()}.",
        feedback=None
        if correct or not scenario
        else Feedback(intrinsic="The stale results persist.", instructional="Match the fix to the failure."),
    )


def _sc(prompt: str, correct: str, wrong: list[str], blooms: BloomsLevel, concept_id: str) -> SingleChoiceExercise:
    scenario = blooms in {BloomsLevel.applying, BloomsLevel.analyzing_evaluating}
    return SingleChoiceExercise(
        blooms_level=blooms,
        prompt=prompt,
        concept_id=concept_id,
        options=[_opt(correct, True)] + [_opt(w, False, scenario=scenario) for w in wrong],
    )


def _mc(prompt: str, correct: list[str], wrong: list[str], concept_id: str) -> MultiChoiceExercise:
    return MultiChoiceExercise(
        blooms_level=BloomsLevel.remembering,
        prompt=prompt,
        concept_id=concept_id,
        options=[_opt(c, True) for c in correct] + [_opt(w, False) for w in wrong],
    )


def _tf(statement: str, answer: bool, concept_id: str) -> TrueFalseExercise:
    return TrueFalseExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="True or false?",
        concept_id=concept_id,
        statement=statement,
        correct_answer=answer,
    )


def _fg(before: str, answers: list[str], concept_id: str) -> FillGapsExercise:
    return FillGapsExercise(
        blooms_level=BloomsLevel.remembering,
        prompt="Fill in the missing term.",
        concept_id=concept_id,
        parts=[FillGapsTextPart(text=before), FillGapsGapPart(accepted_answers=answers), FillGapsTextPart(text=".")],
        explanation="The term names the structure the sentence describes.",
    )


def _ra(tokens: list[str], concept_id: str) -> RearrangeExercise:
    return RearrangeExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="Order the steps.",
        concept_id=concept_id,
        word_bank=list(tokens),
        correct_order=list(tokens),
        explanation="The pipeline only works in this sequence.",
    )


def _worksheet_lesson() -> Lesson:
    """A lesson exactly matching the worksheet of _atoms(): fact (5 items) +
    mechanism (7 items) = 12 exercises, surfaces/facts distinct across cells."""
    vi, ir = "vector-index", "index-refresh"
    exercises = [
        # vector-index (fact): R1 x2, R2 x2, R3 x1
        _sc("Which option describes a vector index?",
            "A structure for fast similarity lookup",
            ["A relational join table", "A file compression scheme", "A network routing plan"],
            BloomsLevel.remembering, vi),
        _mc("Select the statements that are true of vector indexes.",
            ["It accelerates nearest neighbor search", "It stores embeddings for retrieval"],
            ["It encrypts database rows", "It schedules cron jobs"], vi),
        _tf("A vector index performs relational joins across tables.", False, vi),
        _tf("Embedding lookups become faster when a vector index is present.", True, vi),
        _fg("The structure that speeds up similarity search over embeddings is called a ",
            ["vector index"], vi),
        # index-refresh (mechanism): R1 x2, R2 x2, R3 x2, R4 x1
        _sc("What does an index refresh accomplish?",
            "It rebuilds stale entries after data changes",
            ["It rotates access keys", "It renders the dashboard", "It deletes user accounts"],
            BloomsLevel.remembering, ir),
        _mc("Select the true statements about an index refresh.",
            ["It runs after ingestion completes", "It keeps lookups current"],
            ["It encrypts backups", "It compiles source code"], ir),
        _tf("An index refresh rotates security credentials for the cluster.", False, ir),
        _tf("Query results stay current because refresh cycles rebuild stale entries.", True, ir),
        _fg("After new documents are ingested, the job that brings results up to date is the ",
            ["index refresh"], ir),
        _ra(["Ingest new documents", "Detect stale entries", "Rebuild the affected index", "Serve fresh answers"], ir),
        _sc("A retail analytics team ingests nightly sales documents and users complain that "
            "search results lag a day behind. What should the engineer schedule to fix this?",
            "An index refresh after each nightly ingestion",
            ["A credential rotation", "A dashboard re-render", "A full database backup"],
            BloomsLevel.applying, ir),
    ]
    return Lesson(
        title="Indexing basics",
        slo="Explain what vector indexes are and when a refresh keeps them current.",
        concepts=_atoms(),
        exercises=exercises,
        flashcards=[
            Flashcard(front="What is a vector index?", back="A structure for fast similarity lookup."),
            Flashcard(front="When does a refresh run?", back="After new data is ingested."),
        ],
    )


def _config() -> WorkflowConfig:
    # exercises_per_lesson deliberately DIFFERENT from the worksheet-derived 12:
    # worksheet lessons must be held to their own shape, not this legacy value.
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


def _course(lesson: Lesson) -> Course:
    return Course(title="T", modules=[Module(title="M", lessons=[lesson])])


def _errors(issues) -> list[str]:
    return [i.message for i in issues if i.severity == "error"]


# ---------------------------------------------------------------------------
# Worksheet expansion math
# ---------------------------------------------------------------------------


def test_quota_expansion_per_depth():
    assert len(concept_cells("c", "fact")) == 5
    assert len(concept_cells("c", "mechanism")) == 7
    assert len(concept_cells("c", "decision")) == 9
    # Unclassified depth degrades to the fact quota (never crashes).
    assert len(concept_cells("c", None)) == 5
    # Exact fact expansion: rungs, variants, type rotation, Blooms.
    fact = [(c.rung, c.variant, c.question_type, c.blooms_level) for c in concept_cells("c", "fact")]
    assert fact == [
        (1, 1, "single_choice", "Remembering"),
        (1, 2, "multi_choice", "Remembering"),
        (2, 1, "true_false", "Understanding"),
        (2, 2, "true_false", "Understanding"),
        (3, 1, "fill_gaps", "Remembering"),
    ]
    assert required_rungs("fact") == (1, 2, 3)
    assert required_rungs("mechanism") == (1, 2, 3, 4)
    assert required_rungs("decision") == (1, 2, 3, 4, 5)


def test_mixed_lesson_worksheet_math():
    atoms = [
        ConceptAtom(id="a", label="A", summary="A.", depth="fact"),
        ConceptAtom(id="b", label="B", summary="B.", depth="mechanism"),
        ConceptAtom(id="c", label="C", summary="C.", depth="decision"),
    ]
    cells = build_lesson_worksheet(atoms)
    assert len(cells) == 5 + 7 + 9
    assert worksheet_type_distribution(cells) == {
        "single_choice": 6,
        "multi_choice": 4,
        "true_false": 6,
        "fill_gaps": 3,
        "rearrange": 2,
    }
    assert worksheet_blooms_distribution(cells) == {
        "Remembering": 8,
        "Understanding": 8,
        "Applying": 3,
        "Analyzing/Evaluating": 2,
    }
    # Concept-major order: all of a's cells before b's before c's.
    order = [c.concept_id for c in cells]
    assert order == ["a"] * 5 + ["b"] * 7 + ["c"] * 9


def test_exact_budget_preserves_ladders_and_apportions_optional_bands_deterministically():
    atoms = [
        ConceptAtom(
            id=f"mechanism-{index}",
            label=f"Mechanism {index}",
            summary=f"Mechanism {index} has a source-grounded process.",
            depth="mechanism",
        )
        for index in range(6)
    ]
    assert worksheet_size_bounds(atoms) == (24, 42)

    first = build_lesson_worksheet(atoms, item_budget=30)
    second = build_lesson_worksheet(atoms, item_budget=30)
    assert first == second
    assert len(first) == 30

    by_concept = Counter(cell.concept_id for cell in first)
    assert set(by_concept.values()) == {5}  # one optional row per concept
    for atom in atoms:
        assert {cell.rung for cell in first if cell.concept_id == atom.id} == {1, 2, 3, 4}

    optional = [cell for cell in first if cell.variant == 2]
    assert sum(cell.rung in {1, 2} for cell in optional) == 3
    assert sum(cell.rung in {3, 4} for cell in optional) == 3
    assert sum(cell.rung == 5 for cell in optional) == 0

    # Selection never changes the authoritative emitted order.
    assert [
        (cell.concept_id, cell.rung, cell.variant) for cell in first
    ] == sorted(
        ((cell.concept_id, cell.rung, cell.variant) for cell in first),
        key=lambda row: (int(row[0].rsplit("-", 1)[1]), row[1], row[2]),
    )

    # Tight budgets retain every required v1 row but only some v2 rows.  The
    # first mechanic therefore alternates by concept instead of making the few
    # retained v2 rows the sole source of level diversity.
    rung1_v1 = [
        cell.question_type
        for cell in first
        if cell.rung == 1 and cell.variant == 1
    ]
    rung3_v1 = [
        cell.question_type
        for cell in first
        if cell.rung == 3 and cell.variant == 1
    ]
    assert rung1_v1 == ["single_choice", "multi_choice"] * 3
    assert rung3_v1 == ["fill_gaps", "rearrange"] * 3


def test_exact_budget_rejects_padding_or_incomplete_ladders():
    atoms = _atoms()
    assert worksheet_size_bounds(atoms) == (7, 12)
    for budget in (6, 13):
        try:
            build_lesson_worksheet(atoms, item_budget=budget)
            assert False, f"infeasible worksheet budget {budget} was accepted"
        except WorksheetBudgetError as error:
            assert "infeasible" in str(error)

    # None is the backwards-compatible full-envelope policy.
    assert build_lesson_worksheet(atoms, item_budget=None) == build_lesson_worksheet(atoms)


def test_budget_spreads_practice_variants_across_distinct_eligible_concepts():
    depths = ("mechanism", "decision", "fact", "fact", "decision", "decision")
    atoms = [
        ConceptAtom(
            id=f"concept-{index}",
            label=f"Concept {index}",
            summary=f"Concept {index} is grounded in the source.",
            depth=depth,
        )
        for index, depth in enumerate(depths)
    ]
    cells = build_lesson_worksheet(atoms, item_budget=30)
    practice_optional_concepts = {
        cell.concept_id
        for cell in cells
        if cell.variant == 2 and cell.rung in {3, 4}
    }
    assert len(practice_optional_concepts) == 2


def test_worksheet_rungs_consistent_with_derive_rung():
    # The persisted rung and the legacy fallback must never disagree for
    # worksheet-generated content (course_build relies on this).
    for depth in CELL_QUOTAS:
        for cell in concept_cells("x", depth):
            assert derive_rung(cell.blooms_level, cell.question_type) == cell.rung, (depth, cell)


def test_tf_answers_alternate_course_wide_and_split_variant_pairs():
    ws1 = build_lesson_worksheet([ConceptAtom(id="a", label="A", summary="A.", depth="fact")])
    ws2 = build_lesson_worksheet([ConceptAtom(id="b", label="B", summary="B.", depth="decision")])
    assign_tf_answers([ws1, ws2])
    tf_cells = [c for c in ws1 + ws2 if c.question_type == "true_false"]
    assert [c.tf_answer for c in tf_cells] == [False, True, False, True]
    # Every R2 variant pair gets one false + one true — the statements are
    # forced to genuinely differ.
    by_cell: dict[tuple[str, int], set[bool]] = {}
    for c in tf_cells:
        by_cell.setdefault((c.concept_id, c.rung), set()).add(bool(c.tf_answer))
    assert all(v == {False, True} for v in by_cell.values())
    # Non-TF cells never get an answer assigned.
    assert all(c.tf_answer is None for c in ws1 + ws2 if c.question_type != "true_false")


def test_worksheet_applies_predicate():
    assert not worksheet_applies([])
    full = _atoms()
    assert worksheet_applies(full)
    partial = _atoms()
    partial[1].depth = None
    assert not worksheet_applies(partial)


def test_format_worksheet_rows_dictates_cells():
    ws = build_lesson_worksheet([ConceptAtom(id="a", label="A", summary="A.", depth="fact")])
    assign_tf_answers([ws])
    rows = format_worksheet_rows(ws)
    assert 'concept_id="a" | question_type=single_choice | blooms_level=Remembering | R1 Recognize | variant 1 of 2' in rows
    assert "correct_answer MUST be false" in rows
    assert "correct_answer MUST be true" in rows


# ---------------------------------------------------------------------------
# Prompt inputs (A2 worksheet mode vs legacy; A5 repair)
# ---------------------------------------------------------------------------


def _prompt_kwargs() -> dict:
    return dict(
        difficulty=DifficultyLevel.beginner,
        config=_config(),
        course_title="T",
        module_title="M",
        other_lessons_note="",
    )


def test_a2_prompt_worksheet_mode_carries_cell_plan():
    ws = build_lesson_worksheet(_atoms())
    assign_tf_answers([ws])
    prompt = a2_lesson_prompt('{"title": "L"}', "SRC", tf_answers=[], worksheet=ws, **_prompt_kwargs())
    assert f"Generate exactly {len(ws)} exercises" in prompt
    assert "EXERCISE WORKSHEET" in prompt
    # Every cell row appears verbatim.
    for line in format_worksheet_rows(ws).splitlines():
        assert line in prompt, line
    # Variant + rung guidance present; legacy machinery absent.
    assert "variants may test the same core fact" in prompt.lower() or "MAY test the same core fact" in prompt
    assert "R5 Analyze" in prompt
    assert "MANDATORY ANSWER VALUES" in prompt
    assert "USE THIS EXACT ASSIGNMENT" not in prompt
    assert "MANDATORY ANSWER PATTERN" not in prompt


def test_a1_prompt_explains_exact_worksheet_budget_without_depth_distortion():
    cfg = WorkflowConfig.model_validate(
        {**_config().model_dump(mode="json"), "worksheet_items_per_lesson": 10}
    )
    prompt = a1_modularizer_prompt(
        "Source fact.", difficulty=DifficultyLevel.beginner, config=cfg
    )
    assert "EXACT WORKSHEET BUDGET" in prompt
    assert "exactly 10 worksheet rows" in prompt
    assert "Never omit a source fact" in prompt
    assert "misclassify a concept's depth" in prompt


def test_a2_prompt_legacy_mode_unchanged_machinery():
    cfg = _config()
    prompt = a2_lesson_prompt(
        '{"title": "L"}', "SRC", tf_answers=[False], worksheet=None, **_prompt_kwargs()
    )
    assert f"Generate exactly {cfg.exercises_per_lesson} exercises" in prompt
    assert "USE THIS EXACT ASSIGNMENT" in prompt
    assert "MANDATORY ANSWER PATTERN" in prompt
    assert "EXERCISE WORKSHEET" not in prompt


def test_a5_repair_prompt_uses_worksheet_expectations():
    ws = build_lesson_worksheet(_atoms())
    assign_tf_answers([ws])
    prompt = a5_lesson_repair_prompt("{}", "[]", _config(), worksheet=ws)
    assert f"The lesson has exactly {len(ws)} exercises." in prompt
    assert "Cell worksheet" in prompt
    assert "this exact row order" in prompt
    assert "correct_answer MUST be false" in prompt
    assert 'concept_id="vector-index"' in prompt
    assert "MAY test the same fact but MUST differ clearly in surface" in prompt
    # Legacy repair keeps config-driven counts.
    legacy = a5_lesson_repair_prompt("{}", "[]", _config(), worksheet=None)
    assert "The lesson has exactly 5 exercises." in legacy


# ---------------------------------------------------------------------------
# Quality gates (validate.py)
# ---------------------------------------------------------------------------


def test_worksheet_lesson_valid_passes_with_derived_shape():
    report = validate_course(_course(_worksheet_lesson()), _config())
    assert _errors(report.issues) == [], _errors(report.issues)
    assert report.ok


def test_budgeted_worksheet_validation_uses_selected_rows_and_reassigned_tf_values():
    lesson = _worksheet_lesson()
    full = build_lesson_worksheet(lesson.concepts)
    selected = build_lesson_worksheet(lesson.concepts, item_budget=10)
    assign_tf_answers([selected])
    generated = []
    available = list(zip(full, lesson.exercises))
    for cell in selected:
        match = next(
            (
                index
                for index, (candidate, _exercise) in enumerate(available)
                if candidate.concept_id == cell.concept_id
                and candidate.rung == cell.rung
                and candidate.question_type == cell.question_type
            ),
            None,
        )
        if match is None:
            # The mechanism's sole R4 row rotates from single- to multi-choice
            # in an exact-budget worksheet, so there is no legacy full-row
            # counterpart to reuse in this fixture.
            exercise = MultiChoiceExercise(
                blooms_level=BloomsLevel.applying,
                prompt=(
                    "Nightly document ingestion leaves similarity results stale. "
                    "Select the two actions that restore current search behavior."
                ),
                concept_id=cell.concept_id,
                options=[
                    _opt("Run an index refresh after ingestion", True, scenario=True),
                    _opt("Rebuild the affected stale entries", True, scenario=True),
                    _opt("Rotate the cluster credentials", False, scenario=True),
                    _opt("Render the analytics dashboard", False, scenario=True),
                ],
            )
        else:
            _candidate, exercise = available.pop(match)
        if exercise.question_type == "true_false":
            exercise.correct_answer = bool(cell.tf_answer)
        generated.append(exercise)
    lesson.exercises = generated
    cfg = WorkflowConfig.model_validate(
        {**_config().model_dump(mode="json"), "worksheet_items_per_lesson": 10}
    )
    report = validate_course(_course(lesson), cfg)
    assert report.ok, _errors(report.issues)


def test_worksheet_rows_are_authoritative_not_only_aggregate_counts():
    lesson = _worksheet_lesson()
    # These two rows occupy the same concept/rung cell. Swapping them preserves
    # every aggregate count but would invert the encounter-order variant IDs.
    lesson.exercises[0], lesson.exercises[1] = lesson.exercises[1], lesson.exercises[0]
    errors = _errors(validate_course(_course(lesson), _config()).issues)
    assert any("Worksheet row 1 / variant v1" in error and "question_type" in error for error in errors)
    assert any("Worksheet row 2 / variant v2" in error and "question_type" in error for error in errors)


def test_normalization_preserves_authoritative_worksheet_row_order():
    lesson = _worksheet_lesson()
    before = [
        (
            exercise.concept_id,
            exercise.question_type,
            exercise.blooms_level,
            getattr(exercise, "correct_answer", None),
        )
        for exercise in lesson.exercises
    ]

    course = normalize_course(_course(lesson))
    normalized_lesson = course.modules[0].lessons[0]
    after = [
        (
            exercise.concept_id,
            exercise.question_type,
            exercise.blooms_level,
            getattr(exercise, "correct_answer", None),
        )
        for exercise in normalized_lesson.exercises
    ]

    assert after == before
    report = validate_course(course, _config())
    assert report.ok, _errors(report.issues)


def test_worksheet_true_false_answer_is_bound_to_exact_variant_row():
    lesson = _worksheet_lesson()
    # Global and per-cell T/F balance remain one true/one false, but v1/v2 are
    # reversed. The exact worksheet gate must catch this before bank fold-in.
    lesson.exercises[2].correct_answer = True
    lesson.exercises[3].correct_answer = False
    errors = _errors(validate_course(_course(lesson), _config()).issues)
    assert any("Worksheet row 3 / variant v1" in error and "correct_answer" in error for error in errors)
    assert any("Worksheet row 4 / variant v2" in error and "correct_answer" in error for error in errors)


def test_ladder_incomplete_is_error():
    lesson = _worksheet_lesson()
    # Drop the mechanism concept's only R4 exercise (the last one) AND its R3
    # rearrange so the count error doesn't mask the ladder check — instead,
    # remove R4 and add a second R1 single_choice to keep the count at 12.
    lesson.exercises = lesson.exercises[:-1] + [
        _sc("Which phrasing captures an index refresh most precisely?",
            "Maintenance that rebuilds outdated entries",
            ["A key rotation policy", "A UI rendering pass", "An account cleanup sweep"],
            BloomsLevel.remembering, "index-refresh"),
    ]
    report = validate_course(_course(lesson), _config())
    errors = _errors(report.issues)
    assert any("Ladder incomplete" in e and "'index-refresh'" in e and "R4" in e for e in errors), errors
    assert any("R1" in e and "got 3" in e for e in errors), errors  # variant overflow reported too


def test_cell_variant_shortfall_and_unexpected_cell():
    lesson = _worksheet_lesson()
    # Rewire the fact concept's fill_gaps (R3) onto the mechanism concept:
    # vector-index loses its required R3 (ladder), index-refresh R3 overflows.
    for ex in lesson.exercises:
        if isinstance(ex, FillGapsExercise) and ex.concept_id == "vector-index":
            ex.concept_id = "index-refresh"
    report = validate_course(_course(lesson), _config())
    errors = _errors(report.issues)
    assert any("Ladder incomplete" in e and "'vector-index'" in e and "R3" in e for e in errors), errors
    assert any("Cell (concept 'index-refresh', R3" in e and "got 3" in e for e in errors), errors
    # And an exercise at a rung the worksheet never asked for:
    lesson2 = _worksheet_lesson()
    lesson2.exercises[1] = _sc(
        "A librarian curates embeddings for a search feature and wonders which structure "
        "to introduce for faster lookups. What should she adopt for the catalog?",
        "A vector index over the embeddings",
        ["A nightly tape archive", "A print-friendly stylesheet", "A manual card catalog"],
        BloomsLevel.applying, "vector-index",
    )
    report2 = validate_course(_course(lesson2), _config())
    errors2 = _errors(report2.issues)
    assert any("Unexpected cell" in e and "'vector-index'" in e and "R4" in e for e in errors2), errors2


def test_same_cell_variants_may_share_fact_but_not_surface():
    # Two R1 probes of the same concept sharing the FACT (high signature
    # overlap) but with different surfaces: allowed (no near-duplicate error).
    a = _sc("Which option describes a vector index?",
            "A structure for fast similarity lookup over embeddings",
            ["A relational join table", "A file compression scheme", "A network routing plan"],
            BloomsLevel.remembering, "vector-index")
    b = _mc("Select every accurate description of a vector index.",
            ["A structure for fast similarity lookup over embeddings",
             "An acceleration layer for nearest neighbor retrieval"],
            ["A row-level encryption scheme", "A cron scheduling table"], "vector-index")
    lesson = Lesson(title="L", slo="S", concepts=_atoms()[:1], exercises=[a, b], flashcards=[])
    issues = _content_quality_issues(_course(lesson))
    assert not any("Near-duplicate" in i.message for i in issues), [i.message for i in issues]

    # Same cell with a near-identical SURFACE: blocked by the variant tier.
    b2 = _mc("Which option describes a vector index?",
             ["A structure for fast similarity lookup over embeddings",
              "An acceleration layer for nearest neighbor retrieval"],
             ["A row-level encryption scheme", "A cron scheduling table"], "vector-index")
    lesson2 = Lesson(title="L", slo="S", concepts=_atoms()[:1], exercises=[a, b2], flashcards=[])
    issues2 = _content_quality_issues(_course(lesson2))
    assert any(
        "Same-cell variant" in i.message and i.severity == "error" for i in issues2
    ), [i.message for i in issues2]


def test_cross_cell_duplicates_keep_strict_threshold():
    # Same concept at DIFFERENT rungs (different cells) re-asking the same fact
    # in another wrapper: still the classic within-lesson duplicate error.
    a = _sc("Which option describes a vector index?",
            "A structure for fast similarity lookup over embeddings",
            ["A relational join table", "A file compression scheme", "A network routing plan"],
            BloomsLevel.remembering, "vector-index")
    b = _tf("A vector index is a structure for fast similarity lookup over embeddings.",
            True, "vector-index")
    lesson = Lesson(title="L", slo="S", concepts=_atoms()[:1], exercises=[a, b], flashcards=[])
    issues = _content_quality_issues(_course(lesson))
    assert any(
        "Near-duplicate" in i.message and i.severity == "error" for i in issues
    ), [i.message for i in issues]


def test_confusable_reciprocity_warning_for_decision_without_confusables():
    bare = ConceptAtom(id="svc-choice", label="Service choice", summary="Choose the right service.", depth="decision")
    lesson = Lesson(title="L", slo="S", concepts=[bare], exercises=[], flashcards=[])
    report = validate_course(_course(lesson), _config())
    warnings = [i for i in report.issues if i.severity == "warning"]
    assert any(
        "Decision-depth concept 'svc-choice'" in w.message and "confusable_with" in w.message for w in warnings
    ), [w.message for w in warnings]
    # With confusables listed, no reciprocity warning.
    bare.confusable_with = ["other-svc"]
    report2 = validate_course(_course(lesson), _config())
    assert not any("Decision-depth concept" in i.message for i in report2.issues)


def test_legacy_lesson_without_depth_keeps_config_shape():
    # A lesson whose concepts lack depth must be validated against the CONFIG
    # (legacy path): 12 worksheet-shaped exercises against exercises_per_lesson=5
    # is now a count error — proving the precedence switch is depth-driven.
    lesson = _worksheet_lesson()
    for c in lesson.concepts:
        c.depth = None
    report = validate_course(_course(lesson), _config())
    assert any(
        "Expected exactly 5 exercises" in e for e in _errors(report.issues)
    ), _errors(report.issues)


# ---------------------------------------------------------------------------
# Depth / rung plumbing (conversion, merge, echo restoration)
# ---------------------------------------------------------------------------


def test_convert_persists_worksheet_rungs_with_derive_fallback():
    lesson = _worksheet_lesson()
    course = Course(title="T", modules=[Module(title="M", lessons=[lesson])])
    converted = convert_course_result(
        course, source_file="1. T.md", source_sha="sha", taken_lesson_keys=set(), taken_module_keys=set()
    )
    bank = converted.banks[0]
    assert len(bank.items) == 12
    rungs = Counter(it.rung for it in bank.items)
    assert rungs == {1: 4, 2: 4, 3: 3, 4: 1}  # fact(2+2+1) + mechanism(2+2+2+1)
    # Every persisted rung equals the worksheet cell's rung (== derive_rung by
    # the tested invariant), and variants are dense per cell.
    for it in bank.items:
        payload_bloom = it.payload["blooms_level"]
        assert it.rung == derive_rung(payload_bloom, it.payload["question_type"])
    r1_variants = sorted(it.variant for it in bank.items if it.concept_id == "vector-index" and it.rung == 1)
    assert r1_variants == [1, 2]

    # Legacy payloads (no depth on concepts) fall back to derive_rung.
    legacy = _worksheet_lesson()
    for c in legacy.concepts:
        c.depth = None
    converted2 = convert_course_result(
        Course(title="T", modules=[Module(title="M", lessons=[legacy])]),
        source_file="1. T.md", source_sha="sha", taken_lesson_keys=set(), taken_module_keys=set(),
    )
    assert Counter(it.rung for it in converted2.banks[0].items) == rungs


def test_graph_merge_carries_and_refreshes_depth():
    atoms = {"lesson-1": _atoms()}
    first = merge_source_concepts(ConceptGraph(), atoms, source_file="1. T.md")
    by_id = first.graph.by_id()
    assert by_id["vector-index"].depth == "fact"
    assert by_id["index-refresh"].depth == "mechanism"

    # Owned re-extraction with a changed depth refreshes it...
    changed = _atoms()
    changed[0].depth = "mechanism"
    second = merge_source_concepts(
        first.graph, {"lesson-1": changed}, source_file="1. T.md", replaced_lesson_keys={"lesson-1"}
    )
    assert second.graph.by_id()["vector-index"].depth == "mechanism"

    # ...but a depth-less re-extraction never clobbers a known depth.
    bare = _atoms()
    bare[0].depth = None
    third = merge_source_concepts(
        second.graph, {"lesson-1": bare}, source_file="1. T.md", replaced_lesson_keys={"lesson-1"}
    )
    assert third.graph.by_id()["vector-index"].depth == "mechanism"


def test_seed_concepts_restores_depth_dropped_by_a2_echo():
    lesson = Lesson(title="L", slo="S", concepts=_atoms(), exercises=[], flashcards=[])
    for c in lesson.concepts:
        c.depth = None  # A2 echoed the pack but stripped depth
    course = Course(title="T", modules=[Module(title="M", lessons=[lesson])])
    a1_map = {
        "modules": [
            {"lessons": [{"concepts": [c.model_dump(mode="json") for c in _atoms()]}]}
        ]
    }
    _seed_concepts_from_map(course, a1_map)
    assert [c.depth for c in course.modules[0].lessons[0].concepts] == ["fact", "mechanism"]


def test_seed_concepts_restores_exact_authoritative_a1_pack_after_a2_drift():
    authoritative = _atoms()
    drifted = [concept.model_copy(deep=True) for concept in reversed(authoritative)]
    drifted[0].id = "renamed-index-refresh"
    drifted[0].label = "Renamed refresh"
    drifted[0].summary = "A2 replaced the source-grounded summary."
    drifted[0].depth = "decision"
    drifted[0].confusable_with = ["invented-concept"]
    drifted[1].label = "Renamed vector index"
    drifted[1].depth = None
    drifted[1].confusable_with = []

    course = Course(
        title="T",
        modules=[
            Module(
                title="M",
                lessons=[Lesson(title="L", slo="S", concepts=drifted, exercises=[], flashcards=[])],
            )
        ],
    )
    a1_map = {
        "modules": [
            {
                "lessons": [
                    {
                        "concepts": [
                            concept.model_dump(mode="json") for concept in authoritative
                        ]
                    }
                ]
            }
        ]
    }

    _seed_concepts_from_map(course, a1_map)

    restored = course.modules[0].lessons[0].concepts
    assert [concept.model_dump(mode="json") for concept in restored] == [
        concept.model_dump(mode="json") for concept in authoritative
    ]


def test_restore_concept_metadata_restores_depth_after_rewrite():
    prev = Course(title="T", modules=[Module(title="M", lessons=[
        Lesson(title="L", slo="S", concepts=_atoms(), exercises=[], flashcards=[])
    ])])
    stripped = _atoms()
    for c in stripped:
        c.depth = None
    new = Course(title="T", modules=[Module(title="M", lessons=[
        Lesson(title="L", slo="S", concepts=stripped, exercises=[], flashcards=[])
    ])])
    _restore_concept_metadata(prev, new)
    assert [c.depth for c in new.modules[0].lessons[0].concepts] == ["fact", "mechanism"]


def test_restore_concept_metadata_pins_exact_prior_pack_and_exercise_concept_ids():
    previous_lesson = _worksheet_lesson()
    rewritten_lesson = previous_lesson.model_copy(deep=True)
    rewritten_lesson.concepts.reverse()
    rewritten_lesson.concepts[0].id = "rewritten-id"
    rewritten_lesson.concepts[0].label = "Rewritten label"
    rewritten_lesson.concepts[0].summary = "A rewrite stage changed authoritative metadata."
    rewritten_lesson.concepts[0].depth = "decision"
    rewritten_lesson.concepts[0].confusable_with = ["invented-concept"]
    for exercise in rewritten_lesson.exercises:
        exercise.concept_id = "rewritten-id"

    previous = Course(
        title="T", modules=[Module(title="M", lessons=[previous_lesson])]
    )
    rewritten = Course(
        title="T", modules=[Module(title="M", lessons=[rewritten_lesson])]
    )

    _restore_concept_metadata(previous, rewritten)

    restored_lesson = rewritten.modules[0].lessons[0]
    assert [concept.model_dump(mode="json") for concept in restored_lesson.concepts] == [
        concept.model_dump(mode="json") for concept in previous_lesson.concepts
    ]
    assert [exercise.concept_id for exercise in restored_lesson.exercises] == [
        exercise.concept_id for exercise in previous_lesson.exercises
    ]


def test_restore_lesson_depths_pins_exact_prior_pack_after_a5_repair():
    previous = _worksheet_lesson()
    repaired = previous.model_copy(deep=True)
    repaired.concepts.reverse()
    repaired.concepts[0].id = "a5-renamed-id"
    repaired.concepts[0].label = "A5 renamed label"
    repaired.concepts[0].summary = "A5 changed the source-grounded summary."
    repaired.concepts[0].depth = "decision"
    repaired.concepts[0].confusable_with = ["invented-concept"]
    repaired.concepts.pop()

    _restore_lesson_depths(previous, repaired)

    assert [concept.model_dump(mode="json") for concept in repaired.concepts] == [
        concept.model_dump(mode="json") for concept in previous.concepts
    ]


def test_worksheet_bank_compiles_levels_with_zero_item_repeats():
    """Integration: worksheet-shaped bank -> level compiler. 2b's oversampled
    variants are what make §5.1's promise real: recycling spends UNSEEN
    variants, so no item ever appears twice across a lesson's level units."""
    import tempfile
    from pathlib import Path

    from techlingo_workflow.compiler import compile_workspace
    from techlingo_workflow.workspace import CompileConfig, Curriculum, init_workspace

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "1. Indexing.md"
        src.write_text("Indexing source.", encoding="utf-8")
        ws = init_workspace(Path(td) / "course", course_id="demo", title="Demo", source_files=[src])
        course = Course(title="T", modules=[Module(title="M", lessons=[_worksheet_lesson()])])
        conv = convert_course_result(
            course, source_file=src.name, source_sha="sha", taken_lesson_keys=set(), taken_module_keys=set()
        )
        merge = merge_source_concepts(
            ConceptGraph(), {conv.modules[0].lessons[0].key: _atoms()}, source_file=src.name
        )
        ws.save_graph(merge.graph)
        ws.save_curriculum(Curriculum(modules=conv.modules))
        for bank in conv.banks:
            ws.save_bank(bank)

        ws.save_compile_config(CompileConfig(levels=3))
        compiled = compile_workspace(ws.root)
        assert compiled.problems == []
        level_units = [
            u for m in compiled.tl_course.modules for u in m.lessons if u.import_key.startswith("indexing-basics-l")
        ]
        assert [u.import_key for u in level_units] == [
            "indexing-basics-l1", "indexing-basics-l2", "indexing-basics-l3",
        ]
        keys = [q.options["item_key"] for u in level_units for q in u.exercises]
        assert len(keys) == len(set(keys)), f"item repeated across levels: {keys}"
        # L3 recycles whichever mechanism R3 variant L2 did not select. The
        # experience-aware selector may choose v1 or v2 first; unseen behavior,
        # not the historical lowest-variant ordering, is the invariant.
        l2_mechanism_r3 = [
            q.options["item_key"]
            for u in level_units
            if u.import_key.endswith("-l2")
            for q in u.exercises
            if q.options["item_key"].startswith("indexing-basics/index-refresh/r3/")
        ]
        l3_keys = [q.options["item_key"] for u in level_units if u.import_key.endswith("-l3") for q in u.exercises]
        assert set(l2_mechanism_r3 + l3_keys) == {
            "indexing-basics/index-refresh/r3/v1",
            "indexing-basics/index-refresh/r3/v2",
        }

        # The levels:1 flat path stays intact for worksheet banks.
        ws.save_compile_config(CompileConfig(levels=1, checkpoints="none", final_review=False))
        flat = compile_workspace(ws.root)
        assert flat.problems == []
        assert flat.unit_counts == {"lesson": 1}
        assert len(flat.tl_course.modules[0].lessons[0].exercises) == 12


def test_a1_map_depth_validation_and_default_stamping():
    concept = {"id": "a", "label": "A", "summary": "A is a thing."}
    a1_map = {"modules": [{"lessons": [{"concepts": [concept, {
        "id": "b", "label": "B", "summary": "B works.", "depth": "mechanism"}]}]}]}
    cfg = _config()
    problems = _validate_a1_map(a1_map, cfg)
    assert any("depth" in p and "'a'" in p for p in problems), problems
    assert not any("'b'" in p for p in problems), problems
    _stamp_default_depths(a1_map)
    assert concept["depth"] == "fact"
    assert _validate_a1_map(a1_map, cfg) == []


def test_exact_budget_a1_map_rejects_missing_or_blank_concept_labels():
    config = WorkflowConfig.model_validate(
        {**_config().model_dump(mode="json"), "worksheet_items_per_lesson": 6}
    )
    for malformed in (
        {
            "id": "missing-label",
            "summary": "A vector index accelerates nearest-neighbor lookup.",
            "depth": "fact",
        },
        {
            "id": "blank-label",
            "label": "   ",
            "summary": "An index refresh updates stale entries after ingestion.",
            "depth": "fact",
        },
    ):
        valid = {
            "id": "valid-sibling",
            "label": "Valid sibling",
            "summary": "A valid sibling supplies another source-grounded fact.",
            "depth": "fact",
        }
        a1_map = {
            "modules": [{"lessons": [{"concepts": [malformed, valid]}]}]
        }

        problems = _validate_a1_map(a1_map, config)

        assert any("label" in problem.lower() for problem in problems), (
            malformed["id"],
            problems,
        )


def _assert_exact_budget_a2_rejects_concepts_before_model(concepts: list[dict]):
    class LegacyFallbackReached(RuntimeError):
        pass

    class RecordingContext:
        async def add_event(self, _event):
            return None

        async def send_message(self, _state):
            return None

    config = WorkflowConfig.model_validate(
        {**_config().model_dump(mode="json"), "worksheet_items_per_lesson": 6}
    )
    state = PipelineState(
        run_id="malformed-exact-budget-map",
        run_dir="/tmp/malformed-exact-budget-map",
        input_text="A vector index accelerates similarity lookup.",
        model_id="test-model",
        config=config,
        a1_course_map={
            "title": "T",
            "modules": [
                {
                    "title": "M",
                    "lessons": [
                        {
                            "title": "L",
                            "slo": "Explain vector lookup.",
                            "concepts": concepts,
                        }
                    ],
                }
            ],
        },
    )

    with patch.object(
        workflow_executors,
        "LLMClient",
        side_effect=LegacyFallbackReached("A2 reached legacy generation"),
    ):
        try:
            asyncio.run(
                workflow_executors.a2_scaffolder._original_func(
                    state, RecordingContext()
                )
            )
        except LegacyFallbackReached as error:
            assert False, str(error)
        except Exception:
            pass  # authoritative parsing must fail before any legacy LLM call
        else:
            assert False, "malformed exact-budget concepts were accepted"


def test_exact_budget_a2_does_not_fall_back_when_authoritative_concepts_do_not_parse():
    _assert_exact_budget_a2_rejects_concepts_before_model(
        [
            {
                "id": "missing-label",
                "summary": "A vector index accelerates similarity lookup.",
                "depth": "fact",
            },
            {
                "id": "valid-sibling",
                "label": "Valid sibling",
                "summary": "A sibling fact keeps the pack structurally feasible.",
                "depth": "fact",
            },
        ]
    )


def test_exact_budget_a2_rejects_empty_authoritative_concept_pack_before_model():
    _assert_exact_budget_a2_rejects_concepts_before_model([])


def test_exact_budget_a2_rejects_depthless_authoritative_concepts_before_model():
    _assert_exact_budget_a2_rejects_concepts_before_model(
        [
            {
                "id": "depthless-vector-index",
                "label": "Depthless vector index",
                "summary": "A vector index accelerates similarity lookup.",
                "depth": None,
            },
            {
                "id": "depthless-refresh",
                "label": "Depthless refresh",
                "summary": "An index refresh updates stale entries after ingestion.",
                "depth": None,
            },
        ]
    )


def test_a1_map_rejects_source_exposition_summaries_as_meta_content():
    concepts = [
        {
            "id": "small-token-example",
            "label": "Small token example",
            "summary": "The example uses only five tokens so it can be visualized.",
            "depth": "fact",
        },
        {
            "id": "described-relationship",
            "label": "Token relationship",
            "summary": "The relationship between tokens is described here before scoring.",
            "depth": "mechanism",
        },
        {
            "id": "simplified-process",
            "label": "Simplified process",
            "summary": "This is intentionally simplified to make the process easier to follow.",
            "depth": "mechanism",
        },
        {
            "id": "definite-example",
            "label": "Example nodes",
            "summary": "The example nodes are connected by weighted edges.",
            "depth": "mechanism",
        },
        {
            "id": "source-example",
            "label": "Source example",
            "summary": "The source's example maps global to globe.",
            "depth": "fact",
        },
        {
            "id": "source-attribution",
            "label": "Source attribution",
            "summary": "The document states that tokenization splits text into tokens.",
            "depth": "fact",
        },
        {
            "id": "navigation",
            "label": "Navigation",
            "summary": "The mechanism was explained several paragraphs above.",
            "depth": "mechanism",
        },
        {
            "id": "curriculum-reference",
            "label": "Curriculum reference",
            "summary": "This module introduces embedding similarity.",
            "depth": "fact",
        },
    ]
    a1_map = {"modules": [{"lessons": [{"concepts": concepts}]}]}

    problems = _validate_a1_map(a1_map, _config())

    for concept in concepts:
        matching = [
            problem
            for problem in problems
            if f"Concept '{concept['id']}'" in problem
        ]
        assert matching, (concept["id"], problems)
        assert any("not learnable material" in problem for problem in matching), matching


def test_a1_map_allows_standalone_examples_and_domain_visualization_terms():
    concepts = [
        {
            "id": "projection",
            "label": "Projection",
            "summary": (
                "Dimensionality reduction projects embeddings into two dimensions for "
                "visualization."
            ),
            "depth": "mechanism",
        },
        {
            "id": "classifier-example",
            "label": "Classifier example",
            "summary": "For example, a classifier can label a message as spam.",
            "depth": "fact",
        },
        {
            "id": "source-document",
            "label": "Source document",
            "summary": "A source document can contain fields that an extractor maps to a schema.",
            "depth": "mechanism",
        },
        {
            "id": "python-module",
            "label": "Python module",
            "summary": "A Python module exposes reusable functions and classes.",
            "depth": "fact",
        },
        {
            "id": "edge-model",
            "label": "Edge model",
            "summary": "A model intentionally simplified for edge deployment uses less compute.",
            "depth": "mechanism",
        },
        {
            "id": "local-example-antecedent",
            "label": "Local example antecedent",
            "summary": (
                "An example uses five tokens. The example illustrates their position IDs."
            ),
            "depth": "fact",
        },
    ]
    a1_map = {"modules": [{"lessons": [{"concepts": concepts}]}]}

    problems = _validate_a1_map(a1_map, _config())
    assert not any("not learnable material" in problem for problem in problems), problems


def test_a1_map_rejects_narrow_meta_identity_markers_with_clean_summaries():
    concepts = [
        {
            "id": "course-overview",
            "label": "Vector indexing",
            "summary": "A vector index accelerates nearest-neighbor lookup.",
            "depth": "fact",
        },
        {
            "id": "grounded-mechanism",
            "label": "This module",
            "summary": "An index refresh updates stale entries after ingestion.",
            "depth": "mechanism",
        },
    ]
    a1_map = {"modules": [{"lessons": [{"concepts": concepts}]}]}

    problems = _validate_a1_map(a1_map, _config())

    for concept in concepts:
        matching = [
            problem
            for problem in problems
            if f"Concept '{concept['id']}'" in problem
        ]
        assert matching, (concept["id"], problems)
        assert any("not learnable material" in problem for problem in matching), matching


def test_course_validation_emits_exact_concept_summary_path_for_meta_reference():
    course = _course(_worksheet_lesson())
    course.modules[0].lessons[0].concepts[1].summary = (
        "The example shows how an index refresh updates stale entries."
    )

    report = validate_course(course, _config())

    matching = [
        issue
        for issue in report.issues
        if issue.path == "modules[0].lessons[0].concepts[1].summary"
    ]
    assert any(issue.severity == "error" and "The example" in issue.message for issue in matching)
    assert _validation_retry_target(report, course) == "a1"


def test_mixed_concept_and_exercise_errors_retry_the_authoritative_a1_stage():
    course = _course(_worksheet_lesson())
    concept_report = ValidationReport(
        ok=False,
        issues=[
            ValidationIssue(
                severity="error",
                path="modules[0].lessons[0].concepts[1].summary",
                message="Confirmed source-exposition meta-reference.",
            ),
            ValidationIssue(
                severity="error",
                path="modules[0].lessons[0].exercises[2]",
                message="Exercise also needs repair.",
            ),
        ],
    )
    assert _validation_retry_target(concept_report, course) == "a1"

    lesson_report = ValidationReport(
        ok=False,
        issues=[
            ValidationIssue(
                severity="warning",
                path="modules[0].lessons[0].concepts[1].summary",
                message="A warning does not invalidate the map.",
            ),
            ValidationIssue(
                severity="error",
                path="modules[0].lessons[0].exercises[2]",
                message="Exercise needs repair.",
            ),
        ],
    )
    assert _validation_retry_target(lesson_report, course) == "a2"


def test_out_of_range_concept_error_does_not_claim_a1_ownership():
    course = _course(_worksheet_lesson())
    report = ValidationReport(
        ok=False,
        issues=[
            ValidationIssue(
                severity="error",
                path="modules[0].lessons[7].concepts[0].summary",
                message="Hallucinated concept path from a source-fidelity check.",
            )
        ],
    )

    assert _validation_retry_target(report, course) == "a2"


def test_actual_workflow_retry_predicates_are_mutually_exclusive():
    report = ValidationReport(
        ok=False,
        issues=[
            ValidationIssue(
                severity="error",
                path="modules[0].lessons[0].exercises[0]",
                message="Exercise needs repair.",
            )
        ],
    )
    state = PipelineState(
        run_id="retry-routing",
        run_dir="/tmp/retry-routing",
        input_text="Grounded source.",
        model_id="test-model",
        validation_report=report,
    )

    for target, expected in (
        (None, (False, False)),
        ("a1", (True, False)),
        ("a2", (False, True)),
    ):
        state.retry_target = target
        actual = (should_loop_to_a1(state), should_loop_to_a2(state))
        assert actual == expected
        assert not all(actual)

    state.validation_report = ValidationReport(ok=True, issues=[])
    state.retry_target = "a1"
    assert not should_loop_to_a1(state)
    assert not should_loop_to_a2(state)


def test_reset_after_a1_retry_clears_downstream_attempt_state_only():
    course = _course(_worksheet_lesson())
    report = ValidationReport(
        ok=False,
        issues=[
            ValidationIssue(
                severity="error",
                path="modules[0].lessons[0].concepts[0].summary",
                message="A1-owned concept error.",
            )
        ],
    )
    a1_map = {"modules": [{"lessons": [{"concepts": []}]}]}
    state = PipelineState(
        run_id="a1-reset",
        run_dir="/tmp/a1-reset",
        input_text="Grounded source.",
        model_id="test-model",
        a1_course_map=a1_map,
        a2_course=course.model_copy(deep=True),
        a3_course=course.model_copy(deep=True),
        a4_course=course.model_copy(deep=True),
        a5_course=course.model_copy(deep=True),
        validation_report=report,
        best_course=course.model_copy(deep=True),
        best_report=report.model_copy(deep=True),
        dirty_lessons=["0:0"],
        retry_count=2,
        retry_target="a1",
    )

    workflow_executors._reset_after_a1_retry(state)

    assert state.a1_course_map == a1_map
    assert state.retry_count == 2
    assert state.a2_course is None
    assert state.a3_course is None
    assert state.a4_course is None
    assert state.a5_course is None
    assert state.validation_report is None
    assert state.best_course is None
    assert state.best_report is None
    assert state.dirty_lessons is None
    assert state.retry_target is None


def test_a1_map_rejects_infeasible_exact_worksheet_budget():
    cfg = WorkflowConfig.model_validate(
        {**_config().model_dump(mode="json"), "worksheet_items_per_lesson": 13}
    )
    a1_map = {
        "modules": [
            {
                "lessons": [
                    {
                        "concepts": [
                            concept.model_dump(mode="json") for concept in _atoms()
                        ]
                    }
                ]
            }
        ]
    }
    problems = _validate_a1_map(a1_map, cfg)
    assert any(
        "worksheet item budget 13" in problem
        and "at least 7" in problem
        and "at most 12" in problem
        for problem in problems
    ), problems


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
