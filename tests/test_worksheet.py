"""Tests for Phase 2b cell-quota generation: worksheet expansion (worksheet.py),
prompt inputs (prompts.py), the variant-aware quality gates (validate.py), and
depth/rung plumbing into the workspace (course_build.py / graph_merge.py).

Run without pytest:  PYTHONPATH=src python tests/test_worksheet.py
Or with pytest:      PYTHONPATH=src pytest tests/test_worksheet.py
"""

from __future__ import annotations

from collections import Counter

from techlingo_workflow.config import DifficultyLevel, WorkflowConfig
from techlingo_workflow.course_build import convert_course_result
from techlingo_workflow.executors import (
    _restore_concept_metadata,
    _seed_concepts_from_map,
    _stamp_default_depths,
    _validate_a1_map,
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
    RearrangeExercise,
    SingleChoiceExercise,
    TrueFalseExercise,
)
from techlingo_workflow.prompts import a2_lesson_prompt, a5_lesson_repair_prompt
from techlingo_workflow.validate import _content_quality_issues, validate_course
from techlingo_workflow.workspace import ConceptGraph, derive_rung
from techlingo_workflow.worksheet import (
    CELL_QUOTAS,
    assign_tf_answers,
    build_lesson_worksheet,
    concept_cells,
    format_worksheet_rows,
    required_rungs,
    worksheet_applies,
    worksheet_blooms_distribution,
    worksheet_type_distribution,
)

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
    prompt = a5_lesson_repair_prompt("{}", "[]", _config(), worksheet=ws)
    assert f"The lesson has exactly {len(ws)} exercises." in prompt
    assert "Cell worksheet" in prompt
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
        # L3 recycles the mechanism concept's UNSEEN R3 variant, not a repeat of
        # the fact concept's only (already seen) R3 item.
        l3_keys = [q.options["item_key"] for u in level_units if u.import_key.endswith("-l3") for q in u.exercises]
        assert l3_keys == ["indexing-basics/index-refresh/r3/v2"], l3_keys

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
