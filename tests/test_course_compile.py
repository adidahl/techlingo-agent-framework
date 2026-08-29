"""Tests for the deterministic build fold-in (course_build.py) and the
curriculum compiler + bundle writer (compiler.py). No LLM calls — the pipeline
result is synthesized.

Run without pytest:  PYTHONPATH=src python tests/test_course_compile.py
Or with pytest:      PYTHONPATH=src pytest tests/test_course_compile.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from techlingo_workflow.compiler import build_concept_registry, compile_workspace, write_bundle
from techlingo_workflow.course_build import (
    BuildPlan,
    _insert_modules,
    apply_id_remap,
    carry_over_protected_items,
    config_hash,
    convert_course_result,
    plan_build,
    resolve_build_config,
)
from techlingo_workflow.graph_merge import merge_source_concepts
from techlingo_workflow.publication_safety import banks_sha256
from techlingo_workflow.models import (
    BloomsLevel,
    ChoiceOption,
    ConceptAtom,
    Course,
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
from techlingo_workflow.workspace import (
    BankItem,
    BuildState,
    CompileConfig,
    Curriculum,
    CurriculumLesson,
    CurriculumModule,
    LessonBank,
    SourcePublication,
    SourceState,
    Workspace,
    WorkspaceError,
    init_workspace,
    sha256_text,
)


# ---------------------------------------------------------------------------
# Synthetic internal course (mirrors what the A1–A5 pipeline produces)
# ---------------------------------------------------------------------------


def _choice(blooms: BloomsLevel, prompt: str, concept_id: str) -> SingleChoiceExercise:
    return SingleChoiceExercise(
        blooms_level=blooms,
        prompt=prompt,
        concept_id=concept_id,
        options=[
            ChoiceOption(text="Right answer", is_correct=True, rationale="r"),
            ChoiceOption(text="Wrong A", is_correct=False, error_type="e", rationale="r"),
            ChoiceOption(text="Wrong B", is_correct=False, error_type="e", rationale="r"),
        ],
        feedback_for_correct="ok",
    )


def _multi(concept_id: str) -> MultiChoiceExercise:
    return MultiChoiceExercise(
        blooms_level=BloomsLevel.analyzing_evaluating,
        prompt="A team must pick capabilities. Which apply?",
        concept_id=concept_id,
        options=[
            ChoiceOption(text="Correct 1", is_correct=True, rationale="r"),
            ChoiceOption(text="Correct 2", is_correct=True, rationale="r"),
            ChoiceOption(text="Wrong", is_correct=False, error_type="e", rationale="r"),
        ],
    )


def _tf(concept_id: str, answer: bool) -> TrueFalseExercise:
    return TrueFalseExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="Mark true or false.",
        statement="LLMs generalize better than SLMs.",
        correct_answer=answer,
        concept_id=concept_id,
    )


def _fill(concept_id: str) -> FillGapsExercise:
    return FillGapsExercise(
        blooms_level=BloomsLevel.remembering,
        prompt="Complete the sentence.",
        concept_id=concept_id,
        parts=[
            FillGapsTextPart(text="Compared to LLMs, "),
            FillGapsGapPart(accepted_answers=["small"], rejected_answers=["large"]),
            FillGapsTextPart(text=" language models are cheaper."),
        ],
    )


def _rearrange(concept_id: str) -> RearrangeExercise:
    order = ["Generative AI", "creates", "new content"]
    return RearrangeExercise(
        blooms_level=BloomsLevel.understanding,
        prompt="Arrange the sentence.",
        concept_id=concept_id,
        word_bank=["creates", "new content", "Generative AI"],
        correct_order=order,
    )


def _internal_course() -> Course:
    lesson1 = Lesson(
        title="What Is Generative AI?",
        slo="Explain what generative AI produces.",
        concepts=[
            ConceptAtom(id="generative-ai", label="Generative AI", summary="AI that creates new content from prompts."),
            ConceptAtom(id="llm-vs-slm", label="LLM vs SLM", summary="Large models generalize; small models are cheap and focused."),
        ],
        exercises=[
            _choice(BloomsLevel.remembering, "What does generative AI do?", "generative-ai"),
            _choice(BloomsLevel.remembering, "Which output can it produce?", "generative-ai"),
            _tf("llm-vs-slm", True),
            _fill("llm-vs-slm"),
            _choice(BloomsLevel.applying, "A startup needs a local on-device model...", "llm-vs-slm"),
            _multi("generative-ai"),
        ],
        flashcards=[Flashcard(front="Generative AI", back="Creates new content", hint=None)],
    )
    lesson2 = Lesson(
        title="AI Agents",
        slo="Describe the elements of an agent.",
        concepts=[
            ConceptAtom(id="ai-agents", label="AI agents", summary="Model + instructions + tools that automate tasks."),
        ],
        exercises=[
            _choice(BloomsLevel.understanding, "What are an agent's elements?", "ai-agents"),
            _rearrange("ai-agents"),
        ],
        flashcards=[],
    )
    return Course(title="1. Intro to AI", modules=[Module(title="Intro to AI", lessons=[lesson1, lesson2])])


# ---------------------------------------------------------------------------
# convert / remap / carry-over
# ---------------------------------------------------------------------------


def test_convert_course_result_shapes_everything():
    course = _internal_course()
    converted = convert_course_result(
        course,
        source_file="1. Intro to AI.md",
        source_sha="sha-src",
        taken_lesson_keys=set(),
        taken_module_keys=set(),
    )
    assert len(converted.modules) == 1
    module = converted.modules[0]
    assert module.key == "1-intro-to-ai"           # slug of the FILE stem (author's title)
    assert module.title == "1. Intro to AI"
    assert module.source_file == "1. Intro to AI.md"
    assert [l.key for l in module.lessons] == ["what-is-generative-ai", "ai-agents"]
    assert module.lessons[0].concepts == ["generative-ai", "llm-vs-slm"]

    bank1 = converted.banks[0]
    assert bank1.lesson == "what-is-generative-ai" and bank1.module == "1-intro-to-ai"
    # rungs: 2x choice remembering -> R1 v1/v2; tf understanding -> R2; fill -> R3;
    # applying choice -> R4; analyzing multi -> R5
    keys = [it.item_key for it in bank1.items]
    assert keys == [
        "what-is-generative-ai/generative-ai/r1/v1",
        "what-is-generative-ai/generative-ai/r1/v2",
        "what-is-generative-ai/llm-vs-slm/r2/v1",
        "what-is-generative-ai/llm-vs-slm/r3/v1",
        "what-is-generative-ai/llm-vs-slm/r4/v1",
        "what-is-generative-ai/generative-ai/r5/v1",
    ]
    assert all(it.source_hash == "sha-src" for it in bank1.items)
    assert all(it.payload_hash for it in bank1.items)
    assert len(bank1.flashcards) == 1
    assert len(converted.banks[1].items) == 2


def test_convert_dedupes_lesson_keys_against_taken():
    course = _internal_course()
    converted = convert_course_result(
        course,
        source_file="1. Intro to AI.md",
        source_sha="s",
        taken_lesson_keys={"what-is-generative-ai"},   # another module already owns this slug
        taken_module_keys=set(),
    )
    assert converted.modules[0].lessons[0].key == "what-is-generative-ai-2"


def test_apply_id_remap_rewrites_everywhere():
    course = _internal_course()
    converted = convert_course_result(
        course, source_file="f.md", source_sha="s", taken_lesson_keys=set(), taken_module_keys=set()
    )
    remap = {"generative-ai": "generative-ai", "llm-vs-slm": "llm-vs-slm-canonical", "ai-agents": "ai-agents"}
    apply_id_remap(converted, remap)
    assert converted.modules[0].lessons[0].concepts == ["generative-ai", "llm-vs-slm-canonical"]
    bank1 = converted.banks[0]
    tf_item = bank1.items[2]
    assert tf_item.concept_id == "llm-vs-slm-canonical"
    assert tf_item.payload["concept_id"] == "llm-vs-slm-canonical"
    assert tf_item.item_key == "what-is-generative-ai/llm-vs-slm-canonical/r2/v1"


def test_carry_over_protected_items():
    course = _internal_course()
    converted = convert_course_result(
        course, source_file="f.md", source_sha="s", taken_lesson_keys=set(), taken_module_keys=set()
    )
    new_bank = converted.banks[0]
    generated_order = [item.item_key for item in new_bank.items]
    pinned = new_bank.items[0].model_copy(deep=True)
    pinned.pinned = True
    pinned.payload["prompt"] = "HUMAN-TUNED PROMPT"
    edited = new_bank.items[2].model_copy(deep=True)
    edited.provenance = "human-edited"
    edited.payload["statement"] = "A human rewrote this true statement."
    old_bank = LessonBank(lesson=new_bank.lesson, module=new_bank.module, items=[pinned, edited])

    carry_over_protected_items(new_bank, old_bank)
    by_key = {it.item_key: it for it in new_bank.items}
    assert by_key[pinned.item_key].payload["prompt"] == "HUMAN-TUNED PROMPT"  # protected wins collision
    assert by_key[edited.item_key].provenance == "human-edited"
    assert by_key[edited.item_key].payload["statement"] == "A human rewrote this true statement."
    assert [item.item_key for item in new_bank.items] == generated_order
    assert len(new_bank.items) == 6  # no duplicates


def test_carry_over_protected_items_rejects_row_contract_drift():
    mutations = {
        "concept_id": lambda item: setattr(item, "concept_id", "different-concept"),
        "rung": lambda item: setattr(item, "rung", 2),
        "variant": lambda item: setattr(item, "variant", 9),
        "payload.concept_id": lambda item: item.payload.__setitem__(
            "concept_id", "different-concept"
        ),
        "payload.question_type": lambda item: item.payload.__setitem__(
            "question_type", "multi_choice"
        ),
        "payload.blooms_level": lambda item: item.payload.__setitem__(
            "blooms_level", BloomsLevel.applying.value
        ),
    }
    for field, mutate in mutations.items():
        converted = convert_course_result(
            _internal_course(),
            source_file="f.md",
            source_sha="s",
            taken_lesson_keys=set(),
            taken_module_keys=set(),
        )
        new_bank = converted.banks[0]
        protected = new_bank.items[0].model_copy(deep=True)
        protected.pinned = True
        mutate(protected)
        old_bank = LessonBank(
            lesson=new_bank.lesson,
            module=new_bank.module,
            items=[protected],
        )
        try:
            carry_over_protected_items(new_bank, old_bank)
            assert False, f"expected protected {field} drift to be rejected"
        except WorkspaceError as error:
            assert protected.item_key in str(error)
            assert field in str(error)


def test_carry_over_protected_items_rejects_true_false_answer_flip():
    converted = convert_course_result(
        _internal_course(),
        source_file="f.md",
        source_sha="s",
        taken_lesson_keys=set(),
        taken_module_keys=set(),
    )
    new_bank = converted.banks[0]
    protected = new_bank.items[2].model_copy(deep=True)
    protected.provenance = "human-edited"
    protected.payload["correct_answer"] = not protected.payload["correct_answer"]
    old_bank = LessonBank(
        lesson=new_bank.lesson,
        module=new_bank.module,
        items=[protected],
    )

    try:
        carry_over_protected_items(new_bank, old_bank)
        assert False, "expected protected true/false answer drift to be rejected"
    except WorkspaceError as error:
        assert protected.item_key in str(error)
        assert "payload.correct_answer" in str(error)


def test_carry_over_protected_items_keeps_noncolliding_protected_content():
    converted = convert_course_result(
        _internal_course(),
        source_file="f.md",
        source_sha="s",
        taken_lesson_keys=set(),
        taken_module_keys=set(),
    )
    new_bank = converted.banks[0]
    protected = new_bank.items[0].model_copy(deep=True)
    protected.item_key = f"{new_bank.lesson}/human-authored/r1/v1"
    protected.concept_id = "human-authored"
    protected.payload["concept_id"] = "human-authored"
    protected.provenance = "human-authored"
    old_bank = LessonBank(
        lesson=new_bank.lesson,
        module=new_bank.module,
        items=[protected],
    )

    carry_over_protected_items(new_bank, old_bank)

    assert new_bank.items[-1] is protected
    assert len(new_bank.items) == 7


# ---------------------------------------------------------------------------
# Build planning + module insertion
# ---------------------------------------------------------------------------


def _tmp_ws(tmp: Path) -> Workspace:
    docs = tmp / "docs"
    docs.mkdir()
    (docs / "1. First.md").write_text("alpha", encoding="utf-8")
    (docs / "2. Second.md").write_text("beta", encoding="utf-8")
    return init_workspace(tmp / "ws", course_id="demo", title="Demo", source_files=list(docs.iterdir()))


def test_plan_build_incremental_logic():
    with tempfile.TemporaryDirectory() as td:
        ws = _tmp_ws(Path(td))
        config = resolve_build_config({}, "beginner")
        h = config_hash(config)

        plan = plan_build(ws, BuildState(), h)
        assert [r for _, r in plan.dirty] == ["new file", "new file"]

        report_hash = sha256_text("validated")
        empty_bank_hash = banks_sha256({})

        def valid_state(content: str) -> SourceState:
            source_hash = sha256_text(content)
            return SourceState(
                sha256=source_hash,
                status="ok",
                validation_ok=True,
                config_sha256=h,
                validation_report_sha256=report_hash,
                last_known_good=SourcePublication(
                    source_sha256=source_hash,
                    config_sha256=h,
                    bank_sha256=empty_bank_hash,
                    validation_report_sha256=report_hash,
                    promoted_at="2026-08-20T00:00:00Z",
                    module_keys=[],
                ),
            )

        state = BuildState(
            workflow_config_hash=h,
            bank_sha256=empty_bank_hash,
            sources={
                "1. First.md": valid_state("alpha"),
                "2. Second.md": valid_state("beta"),
            },
        )
        plan = plan_build(ws, state, h)
        assert plan.dirty == [] and len(plan.clean) == 2

        (ws.sources_dir / "1. First.md").write_text("alpha EDITED", encoding="utf-8")
        plan = plan_build(ws, state, h)
        assert [(p.name, r) for p, r in plan.dirty] == [("1. First.md", "content changed")]

        state.sources["1. First.md"] = SourceState(sha256=sha256_text("alpha EDITED"), status="failed")
        plan = plan_build(ws, state, h)
        assert [(p.name, r) for p, r in plan.dirty] == [("1. First.md", "previous build failed")]

        state.sources["1. First.md"] = SourceState(
            sha256=sha256_text("alpha EDITED"), status="ok", validation_ok=False
        )
        plan = plan_build(ws, state, h)
        assert [(p.name, r) for p, r in plan.dirty] == [
            ("1. First.md", "previous validation missing or failed")
        ]

        state.sources["1. First.md"] = valid_state("alpha EDITED")
        plan = plan_build(ws, state, "different-config-hash-stored")
        assert [r for _, r in plan.dirty] == ["workflow config changed", "workflow config changed"]

        # An interrupted migration may leave the workspace-wide config hash on
        # the old snapshot after every source has already been rebound. Exact
        # per-source LKG evidence is sufficient to resume without rebuilding.
        state.workflow_config_hash = "old-hash"
        plan = plan_build(ws, state, h)
        assert plan.dirty == [] and len(plan.clean) == 2

        plan = plan_build(ws, state, h, force=True)
        assert [r for _, r in plan.dirty] == ["forced", "forced"]


def test_plan_build_resumes_partial_config_migration_from_per_source_evidence():
    with tempfile.TemporaryDirectory() as td:
        ws = _tmp_ws(Path(td))
        first_bank = LessonBank(lesson="lesson-first", module="module-first", items=[])
        second_bank = LessonBank(lesson="lesson-second", module="module-second", items=[])
        ws.save_bank(first_bank)
        ws.save_bank(second_bank)

        old_hash = config_hash(resolve_build_config({}, "beginner"))
        new_hash = config_hash(
            resolve_build_config({"worksheet_items_per_lesson": 30}, "beginner")
        )
        report_hash = sha256_text("validated")

        def bound_state(
            source_name: str,
            cfg_hash: str,
            module_key: str,
            bank: LessonBank,
        ) -> SourceState:
            source_hash = ws.source_hash(ws.sources_dir / source_name)
            source_bank_hash = banks_sha256([bank])
            return SourceState(
                sha256=source_hash,
                status="ok",
                module_keys=[module_key],
                validation_ok=True,
                config_sha256=cfg_hash,
                validation_report_sha256=report_hash,
                last_known_good=SourcePublication(
                    source_sha256=source_hash,
                    config_sha256=cfg_hash,
                    bank_sha256=source_bank_hash,
                    validation_report_sha256=report_hash,
                    promoted_at="2026-08-20T00:00:00Z",
                    module_keys=[module_key],
                ),
            )

        state = BuildState(
            # Global evidence stays on the last fully completed build until all
            # sources have been promoted under the new configuration.
            workflow_config_hash=old_hash,
            bank_sha256=sha256_text("pre-migration-bank-set"),
            sources={
                "1. First.md": bound_state(
                    "1. First.md", new_hash, "module-first", first_bank
                ),
                "2. Second.md": bound_state(
                    "2. Second.md", old_hash, "module-second", second_bank
                ),
            },
        )

        plan = plan_build(ws, state, new_hash)
        assert [source.name for source in plan.clean] == ["1. First.md"]
        assert [(source.name, reason) for source, reason in plan.dirty] == [
            ("2. Second.md", "workflow config changed")
        ]

        # Once the global config hash advances, a stale global bank hash is no
        # longer migration state: it is canonical-content tampering and must
        # dirty every otherwise-current source.
        state.sources["2. Second.md"] = bound_state(
            "2. Second.md", new_hash, "module-second", second_bank
        )
        state.workflow_config_hash = new_hash
        plan = plan_build(ws, state, new_hash)
        assert plan.clean == []
        assert [(source.name, reason) for source, reason in plan.dirty] == [
            ("1. First.md", "publication evidence no longer matches canonical content"),
            ("2. Second.md", "publication evidence no longer matches canonical content"),
        ]


def test_resolve_build_config_lessons_override():
    config = resolve_build_config({}, "beginner")
    assert (config.min_lessons_total, config.max_lessons_total) == (5, 6)
    assert config.exercises_per_lesson == 30  # owner default (2026-07-16)
    assert config.worksheet_items_per_lesson is None
    assert sum(config.blooms_distribution.values()) == 30
    assert sum(config.question_type_distribution.values()) == 30
    budgeted = resolve_build_config({"worksheet_items_per_lesson": 30}, "beginner")
    assert budgeted.worksheet_items_per_lesson == 30
    assert config_hash(budgeted) != config_hash(config)

    fast = resolve_build_config({}, "beginner", lessons_override=1)
    assert (fast.min_lessons_total, fast.max_lessons_total) == (1, 1)
    # Different content shape -> different hash -> a later full build re-dirties everything.
    assert config_hash(fast) != config_hash(config)
    # course.yaml overrides still apply beneath the pin. Overriding the exercise
    # count requires matching distributions (config cross-field validation).
    custom = resolve_build_config(
        {
            "exercises_per_lesson": 15,
            "worksheet_items_per_lesson": 12,
            "blooms_distribution": {"Remembering": 3, "Understanding": 4, "Applying": 4, "Analyzing/Evaluating": 4},
            "question_type_distribution": {"single_choice": 4, "multi_choice": 4, "true_false": 3, "fill_gaps": 2, "rearrange": 2},
        },
        "advanced",
        lessons_override=2,
    )
    assert (custom.min_lessons_total, custom.max_lessons_total) == (2, 2)
    assert custom.exercises_per_lesson == 15
    assert custom.worksheet_items_per_lesson == 12
    assert custom.difficulty.value == "advanced"


def test_resolve_build_config_rejects_unknown_policy_and_distribution_keys():
    for override in (
        {"worksheet_item_per_lesson": 30},
        {
            "question_type_distribution": {
                "single_choice": 8,
                "multi_choice": 8,
                "true_false": 5,
                "fill_gaps": 4,
                "rearrange": 4,
                "short_answer": 1,
            }
        },
    ):
        try:
            resolve_build_config(override, "beginner")
            assert False, f"unsupported workflow override was accepted: {override}"
        except ValueError:
            pass


def test_insert_modules_replaces_in_place_and_inserts_naturally():
    cur = Curriculum(
        modules=[
            CurriculumModule(key="m1", title="One", source_file="1. First.md"),
            CurriculumModule(key="authored", title="My videos", authored=True),
            CurriculumModule(key="m3", title="Three", source_file="3. Third.md"),
        ]
    )
    # Replace existing (same position).
    _insert_modules(cur, [CurriculumModule(key="m1b", title="One v2", source_file="1. First.md")], source_file="1. First.md")
    assert [m.key for m in cur.modules] == ["m1b", "authored", "m3"]
    # New file inserts before the first generated module that sorts after it;
    # the authored module never moves.
    _insert_modules(cur, [CurriculumModule(key="m2", title="Two", source_file="2. Second.md")], source_file="2. Second.md")
    assert [m.key for m in cur.modules] == ["m1b", "authored", "m2", "m3"]


# ---------------------------------------------------------------------------
# Compile + bundle (integration over a synthetic workspace)
# ---------------------------------------------------------------------------


def _populate_ws_from_course(ws: Workspace, course: Course, source_name: str) -> None:
    """The deterministic fold-in exactly as build_source does it (minus the LLM)."""
    curriculum = ws.load_curriculum()
    graph = ws.load_graph()

    converted = convert_course_result(
        course,
        source_file=source_name,
        source_sha=ws.source_hash(ws.sources_dir / source_name),
        taken_lesson_keys={l.key for m in curriculum.modules if m.source_file != source_name for l in m.lessons},
        taken_module_keys={m.key for m in curriculum.modules if m.source_file != source_name},
    )
    atoms_by_lesson = {}
    for module, internal_module in zip(converted.modules, course.modules):
        for cur_lesson, internal_lesson in zip(module.lessons, internal_module.lessons):
            atoms_by_lesson[cur_lesson.key] = internal_lesson.concepts
    merge = merge_source_concepts(graph, atoms_by_lesson, source_file=source_name)
    apply_id_remap(converted, merge.id_remap)
    for bank in converted.banks:
        ws.save_bank(bank)
    _insert_modules(curriculum, converted.modules, source_file=source_name)
    ws.save_curriculum(curriculum)
    ws.save_graph(merge.graph)


def _flat_cfg(**overrides) -> CompileConfig:
    """Phase-1 flat compilation (the back-compat path under test)."""
    base = dict(levels=1, checkpoints="none", final_review=False)
    base.update(overrides)
    return CompileConfig(**base)


def _leveled_cfg(**overrides) -> CompileConfig:
    """Phase-2a levels, with checkpoints/final review off unless a test opts in."""
    base = dict(levels=3, checkpoints="none", final_review=False)
    base.update(overrides)
    return CompileConfig(**base)


def _compiled_workspace(tmp: Path, cfg: CompileConfig | None = None, course: Course | None = None) -> Workspace:
    docs = tmp / "docs"
    docs.mkdir()
    (docs / "1. Intro to AI.md").write_text("# Intro\nsource text", encoding="utf-8")
    ws = init_workspace(tmp / "ws", course_id="demo", title="Demo Course", source_files=list(docs.iterdir()))
    ws.save_compile_config(cfg if cfg is not None else _flat_cfg())
    _populate_ws_from_course(ws, course if course is not None else _internal_course(), "1. Intro to AI.md")
    # Synthetic fold-in represents a pipeline result whose hard validation
    # passed.  Publication now requires that fact to be explicit and hashed.
    source = ws.sources_dir / "1. Intro to AI.md"
    workflow_hash = config_hash(resolve_build_config({}, "beginner"))
    source_hash = ws.source_hash(source)
    report_hash = sha256_text("synthetic-valid-report")
    source_banks = list(ws.iter_banks())
    source_bank_hash = banks_sha256(source_banks)
    module_keys = [
        module.key
        for module in ws.load_curriculum().modules
        if module.source_file == source.name
    ]
    ws.save_build_state(
        BuildState(
            workflow_config_hash=workflow_hash,
            bank_sha256=source_bank_hash,
            sources={
                source.name: SourceState(
                    sha256=source_hash,
                    status="ok",
                    module_keys=module_keys,
                    validation_ok=True,
                    config_sha256=workflow_hash,
                    validation_report_sha256=report_hash,
                    last_known_good=SourcePublication(
                        source_sha256=source_hash,
                        config_sha256=workflow_hash,
                        bank_sha256=source_bank_hash,
                        validation_report_sha256=report_hash,
                        promoted_at="2026-08-20T00:00:00Z",
                        module_keys=module_keys,
                    ),
                )
            },
        )
    )
    return ws


def _units_by_key(compiled) -> dict[str, object]:
    return {u.import_key: u for m in compiled.tl_course.modules for u in m.lessons}


def _item_keys(unit) -> list[str]:
    return [q.options["item_key"] for q in unit.exercises]


def test_compile_workspace_produces_valid_flat_course():
    """levels: 1 must keep producing today's flat output (back-compat path)."""
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td))
        compiled = compile_workspace(ws.root)
        assert compiled.problems == []
        tl = compiled.tl_course
        assert tl.import_key == "demo" and tl.title == "Demo Course"
        assert [m.import_key for m in tl.modules] == ["1-intro-to-ai"]  # no review module
        units = tl.modules[0].lessons
        assert [u.import_key for u in units] == ["what-is-generative-ai", "ai-agents"]  # no -lN units, no checkpoint
        assert len(units[0].exercises) == 6 and len(units[0].flashcards) == 1
        assert units[0].flashcards[0].import_key == "what-is-generative-ai-f0"
        q1 = units[0].exercises[0]
        assert q1.import_key == "what-is-generative-ai-q1"
        expected = {
            "what-is-generative-ai/generative-ai/r1/v1",
            "what-is-generative-ai/generative-ai/r1/v2",
            "what-is-generative-ai/llm-vs-slm/r2/v1",
            "what-is-generative-ai/llm-vs-slm/r3/v1",
            "what-is-generative-ai/llm-vs-slm/r4/v1",
            "what-is-generative-ai/generative-ai/r5/v1",
        }
        # Flat mode still emits every active bank item exactly once, but v2
        # deliberately schedules the learner-facing order instead of retaining
        # the pathological raw bank blocks.
        assert set(_item_keys(units[0])) == expected
        assert len(_item_keys(units[0])) == len(expected)
        assert q1.options["item_key"] in expected
        assert q1.options["concept_id"] in {"generative-ai", "llm-vs-slm"}
        assert 1 <= q1.options["rung"] <= 5
        assert compiled.sequence_quality.ok
        assert compiled.unit_counts == {"lesson": 2}
        assert compiled.notes == []


def test_compile_skips_retired_items_and_keeps_dense_keys():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td))
        bank = ws.load_bank("what-is-generative-ai")
        bank.items[0].status = "retired"
        ws.save_bank(bank)
        compiled = compile_workspace(ws.root)
        unit = compiled.tl_course.modules[0].lessons[0]
        assert len(unit.exercises) == 5
        assert unit.exercises[0].import_key == "what-is-generative-ai-q1"  # dense positional keys
        keys = _item_keys(unit)
        assert "what-is-generative-ai/generative-ai/r1/v1" not in keys
        assert len(keys) == len(set(keys)) == 5
        assert [q.import_key for q in unit.exercises] == [
            f"what-is-generative-ai-q{index}" for index in range(1, 6)
        ]


def test_compile_is_deterministic():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td))
        a = compile_workspace(ws.root).tl_course.model_dump(mode="json")
        b = compile_workspace(ws.root).tl_course.model_dump(mode="json")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_concept_registry_reports_available_rungs():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td))
        compiled = compile_workspace(ws.root)
        registry = {c["id"]: c for c in build_concept_registry(compiled.graph, compiled.banks)}
        assert registry["generative-ai"]["rungs"] == [1, 5]
        assert registry["llm-vs-slm"]["rungs"] == [2, 3, 4]
        assert registry["ai-agents"]["rungs"] == [2, 3]
        assert registry["generative-ai"]["status"] == "active"


def test_write_bundle_manifest_hashes_and_versioning():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td))
        compiled = compile_workspace(ws.root)
        out1 = write_bundle(ws.root, compiled, flat=True)
        assert out1.version == 1
        assert out1.bundle_dir.name == "demo-v1"
        assert out1.flat_path is not None and out1.flat_path.exists()

        manifest = json.loads((out1.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "bundle-v1"
        assert manifest["compile"]["levels"] == 1 and manifest["compile"]["seed"] == 901
        kinds = {e["kind"] for e in manifest["entities"]}
        assert kinds == {
            "unit",
            "course",
            "concepts",
            "bank",
            "flat-course",
            "sequence-quality",
        }
        for entity in manifest["entities"]:
            text = (out1.bundle_dir / entity["path"]).read_text(encoding="utf-8")
            assert sha256_text(text) == entity["sha256"], f"hash mismatch for {entity['path']}"

        # Flat course in the bundle parses as a valid TL course tree.
        flat = json.loads(out1.flat_path.read_text(encoding="utf-8"))
        assert flat["import_key"] == "demo" and len(flat["modules"]) == 1
        quality = json.loads((out1.bundle_dir / "quality_report.json").read_text(encoding="utf-8"))
        assert quality["schema_version"] == "sequence-quality-v1"
        assert quality["summary"]["ok"] is True

        out2 = write_bundle(ws.root, compiled, flat=False)
        assert out2.version == 2 and out2.flat_path is None


# ---------------------------------------------------------------------------
# Phase 2a: levels (level = unit), recycling, checkpoints, final review
# ---------------------------------------------------------------------------


def _ratio_course() -> Course:
    """5 concepts, each with exactly one R1 (remembering choice) and one R3
    (fill) item — makes the recycle arithmetic exact: 0.4*5 -> 2, 0.3*5 -> 2."""
    names = [
        ("neural-pruning", "Neural pruning", "Neural pruning removes redundant weights from a trained network."),
        ("vector-indexing", "Vector indexing", "Vector indexing accelerates nearest-neighbour search over embeddings."),
        ("token-streaming", "Token streaming", "Token streaming sends partial completions to the client while decoding."),
        ("prompt-caching", "Prompt caching", "Prompt caching reuses attention states across repeated prefixes."),
        ("model-sharding", "Model sharding", "Model sharding splits parameters across multiple devices."),
    ]
    atoms = [ConceptAtom(id=cid, label=label, summary=summary) for cid, label, summary in names]
    exercises = []
    for cid, label, _ in names:
        exercises.append(_choice(BloomsLevel.remembering, f"What does {label.lower()} do?", cid))
        exercises.append(_fill(cid))
    lesson = Lesson(
        title="Optimization Techniques",
        slo="Recall five optimization techniques.",
        concepts=atoms,
        exercises=exercises,
        flashcards=[],
    )
    return Course(title="1. Intro to AI", modules=[Module(title="Intro to AI", lessons=[lesson])])


def test_levels_partition_by_rung_with_recycling():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg())
        compiled = compile_workspace(ws.root)
        assert compiled.problems == []
        assert [m.import_key for m in compiled.tl_course.modules] == ["1-intro-to-ai"]
        units = compiled.tl_course.modules[0].lessons
        assert [u.import_key for u in units] == [
            "what-is-generative-ai-l1",
            "what-is-generative-ai-l2",
            "what-is-generative-ai-l3",
            "ai-agents-l1",
            "ai-agents-l2",
        ]
        by_key = _units_by_key(compiled)

        # L1 Foundations: one experience-aware variant per R1/R2 cell.
        l1 = by_key["what-is-generative-ai-l1"]
        assert l1.title == "What Is Generative AI? · Level 1"
        assert _item_keys(l1) == [
            "what-is-generative-ai/generative-ai/r1/v1",
            "what-is-generative-ai/llm-vs-slm/r2/v1",
        ]
        assert [q.import_key for q in l1.exercises] == ["what-is-generative-ai-l1-q1", "what-is-generative-ai-l1-q2"]

        # L2 Apply: fresh R3/R4 + one recycled R1/R2 item (0.4 * 2 concepts -> 1).
        l2 = by_key["what-is-generative-ai-l2"]
        assert l2.title == "What Is Generative AI? · Level 2"
        keys2 = _item_keys(l2)
        assert len(keys2) == 3
        assert {
            "what-is-generative-ai/llm-vs-slm/r3/v1",
            "what-is-generative-ai/llm-vs-slm/r4/v1",
        }.issubset(keys2)
        assert (set(keys2) - {
            "what-is-generative-ai/llm-vs-slm/r3/v1",
            "what-is-generative-ai/llm-vs-slm/r4/v1",
        }).pop() in {
            "what-is-generative-ai/generative-ai/r1/v2",  # unseen variant of the R1 cell
            "what-is-generative-ai/llm-vs-slm/r2/v1",     # or the seen-repeat fallback
        }

        # L3 Master: R5 + recycled R3/R4 (0.3 * 2 -> 1; only llm-vs-slm has any).
        l3 = by_key["what-is-generative-ai-l3"]
        assert l3.title == "What Is Generative AI? · Level 3"
        assert _item_keys(l3) == [
            "what-is-generative-ai/llm-vs-slm/r3/v1",
            "what-is-generative-ai/generative-ai/r5/v1",
        ]

        # The shared scheduler avoids rigid rung blocks while keeping every
        # transition locally understandable (at most a two-rung soft drop in
        # this adversarial tiny pool) and all hard sequence gates valid.
        for unit in units:
            rungs = [q.options["rung"] for q in unit.exercises]
            assert max((left - right for left, right in zip(rungs, rungs[1:])), default=0) <= 2
            assert len(_item_keys(unit)) == len(set(_item_keys(unit)))

        # ai-agents: 1 concept -> both recycle quotas round to 0; no R5 -> L3 skipped.
        assert _item_keys(by_key["ai-agents-l1"]) == ["ai-agents/ai-agents/r2/v1"]
        assert _item_keys(by_key["ai-agents-l2"]) == ["ai-agents/ai-agents/r3/v1"]
        assert compiled.unit_counts == {"l1": 2, "l2": 2, "l3": 1}
        assert any("ai-agents" in note and "level 3" in note for note in compiled.notes)


def test_recycling_prefers_unseen_variants_with_seen_repeat_fallback():
    with tempfile.TemporaryDirectory() as td:
        # quota 1.0 recycles EVERY concept -> both branches are exercised.
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg(recycle={"l2": 1.0, "l3": 1.0}))
        compiled = compile_workspace(ws.root)
        by_key = _units_by_key(compiled)
        keys2 = _item_keys(by_key["what-is-generative-ai-l2"])
        assert len(keys2) == 4
        # generative-ai has an unseen R1 v2 -> recycled instead of repeating v1
        assert "what-is-generative-ai/generative-ai/r1/v2" in keys2
        assert "what-is-generative-ai/generative-ai/r1/v1" not in keys2
        # llm-vs-slm has no unseen R1/R2 variant -> exact repeat (acceptable until 2b)
        assert "what-is-generative-ai/llm-vs-slm/r2/v1" in keys2
        # no item may appear twice WITHIN one unit
        for m in compiled.tl_course.modules:
            for u in m.lessons:
                keys = _item_keys(u)
                assert len(keys) == len(set(keys)), u.import_key


def test_recycle_ratios_honored():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg(), course=_ratio_course())
        compiled = compile_workspace(ws.root)
        assert compiled.problems == []
        by_key = _units_by_key(compiled)
        l1 = by_key["optimization-techniques-l1"]
        l2 = by_key["optimization-techniques-l2"]
        l3 = by_key["optimization-techniques-l3"]
        assert len(l1.exercises) == 5 and all(q.options["rung"] == 1 for q in l1.exercises)
        # L2: 5 fresh R3 + round_half_up(0.4 * 5) = 2 recycled R1, distinct concepts.
        recycled = [q for q in l2.exercises if q.options["rung"] <= 2]
        assert len(l2.exercises) == 7 and len(recycled) == 2
        assert len({q.options["concept_id"] for q in recycled}) == 2
        # L3: bank has no R5 -> only recycling: round_half_up(0.3 * 5) = 2 R3 repeats.
        assert [q.options["rung"] for q in l3.exercises] == [3, 3]
        assert len({q.options["concept_id"] for q in l3.exercises}) == 2


def test_flashcards_attach_to_level1_only():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg(checkpoints="per_module", final_review=True))
        compiled = compile_workspace(ws.root)
        for m in compiled.tl_course.modules:
            for u in m.lessons:
                if u.import_key == "what-is-generative-ai-l1":
                    assert [f.import_key for f in u.flashcards] == ["what-is-generative-ai-l1-f0"]
                else:
                    assert u.flashcards == [], u.import_key


def test_module_checkpoint_sampling():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg(checkpoints="per_module"))
        compiled = compile_workspace(ws.root)
        assert compiled.problems == []
        module = compiled.tl_course.modules[0]
        checkpoint = module.lessons[-1]  # checkpoint closes the module
        assert checkpoint.import_key == "1-intro-to-ai-checkpoint"
        assert checkpoint.title == "1. Intro to AI · Checkpoint"
        assert compiled.unit_counts["checkpoint"] == 1
        assert checkpoint.flashcards == []
        by_concept: dict[str, list[str]] = {}
        for q in checkpoint.exercises:
            by_concept.setdefault(q.options["concept_id"], []).append(q.options["item_key"])
        # 1-2 items per module concept; hint 12 leaves room to grow to 2 each.
        assert set(by_concept) == {"generative-ai", "llm-vs-slm", "ai-agents"}
        assert all(len(keys) == 2 for keys in by_concept.values())
        # highest available rung is always probed (seen repeats are legitimate here)
        assert "what-is-generative-ai/generative-ai/r5/v1" in by_concept["generative-ai"]
        assert "what-is-generative-ai/llm-vs-slm/r4/v1" in by_concept["llm-vs-slm"]
        assert "ai-agents/ai-agents/r3/v1" in by_concept["ai-agents"]
        report = next(
            unit_report
            for unit_report in compiled.sequence_quality.units
            if unit_report.metrics.unit_path.endswith(":1-intro-to-ai-checkpoint")
        )
        assert report.metrics.largest_downward_rung_jump <= 2
        assert report.metrics.nondecreasing_transition_ratio >= 0.6
        assert report.metrics.maximum_mechanic_streak <= 2
        assert not [issue for issue in report.issues if issue.severity == "error"]


def test_checkpoint_growth_respects_session_size_hint():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg(checkpoints="per_module", session_size_hint=4))
        compiled = compile_workspace(ws.root)
        checkpoint = _units_by_key(compiled)["1-intro-to-ai-checkpoint"]
        assert len(checkpoint.exercises) == 4  # 3 concepts x 1, then one growth item
        counts: dict[str, int] = {}
        for q in checkpoint.exercises:
            counts[q.options["concept_id"]] = counts.get(q.options["concept_id"], 0) + 1
        assert set(counts) == {"generative-ai", "llm-vs-slm", "ai-agents"}
        assert all(1 <= c <= 2 for c in counts.values())


def test_final_review_course_wide_and_depth_weighted():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg(final_review=True))
        compiled = compile_workspace(ws.root)
        review_module = compiled.tl_course.modules[-1]
        assert review_module.import_key == "demo-review" and review_module.title == "Course Review"
        unit = review_module.lessons[0]
        assert unit.import_key == "demo-final-review" and unit.title == "Final Review"
        assert compiled.unit_counts["final_review"] == 1
        concepts = [q.options["concept_id"] for q in unit.exercises]
        # Phase-1 banks: depth is null everywhere -> every concept stays eligible.
        assert set(concepts) == {"generative-ai", "llm-vs-slm", "ai-agents"}
        assert len(unit.exercises) == 6  # grew to 2 per concept within the 2x-hint budget

        # With depth known, decision/mechanism outrank facts under a tight budget (§5.2).
        graph = ws.load_graph()
        depths = {"generative-ai": "decision", "llm-vs-slm": "mechanism", "ai-agents": "fact"}
        for concept in graph.concepts:
            concept.depth = depths[concept.id]
        ws.save_graph(graph)
        cfg = ws.load_compile_config()
        cfg.session_size_hint = 1  # final-review budget = 2 items
        ws.save_compile_config(cfg)
        unit2 = _units_by_key(compile_workspace(ws.root))["demo-final-review"]
        assert {q.options["concept_id"] for q in unit2.exercises} == {"generative-ai", "llm-vs-slm"}
        assert len(unit2.exercises) == 2


def test_seed_determinism_byte_identical_bundles():
    with tempfile.TemporaryDirectory() as td:
        ws = _compiled_workspace(Path(td), cfg=_leveled_cfg(checkpoints="per_module", final_review=True))
        a = compile_workspace(ws.root)
        b = compile_workspace(ws.root)
        assert json.dumps(a.tl_course.model_dump(mode="json"), sort_keys=True) == json.dumps(
            b.tl_course.model_dump(mode="json"), sort_keys=True
        )
        out_a = write_bundle(ws.root, a, flat=True)
        out_b = write_bundle(ws.root, b, flat=True)
        man_a = json.loads((out_a.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        man_b = json.loads((out_b.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        assert man_a["entities"] == man_b["entities"]  # same paths AND same content hashes
        for entity in man_a["entities"]:
            assert (out_a.bundle_dir / entity["path"]).read_bytes() == (out_b.bundle_dir / entity["path"]).read_bytes()
        # the whole manifest matches too, excluding created_at and the bundle version
        man_a["created_at"] = man_b["created_at"] = ""
        man_a["course"]["version"] = man_b["course"]["version"] = 0
        assert man_a == man_b
        # a different seed still compiles to a valid course
        cfg = ws.load_compile_config()
        cfg.seed = 902
        ws.save_compile_config(cfg)
        assert compile_workspace(ws.root).problems == []


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
