"""Multi-document course builds over a workspace (ARCHITECTURE.md §4).

`build_course()` is the factory orchestrator: it detects dirty source files by
content hash, runs the existing single-document A0–A5 pipeline once per dirty
file (1 source file = 1 course module by default), and converts each result
into workspace form — concept graph (merged, stable ids), curriculum, and
per-lesson exercise banks.

Everything outside the per-file pipeline call is deterministic. The pipeline
itself is untouched — this module treats a run as a black box producing an
internal `Course` (proven machinery, RESILIENCE_PLAN.md).
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import DifficultyLevel, WorkflowConfig
from .emit import slugify
from .graph_merge import merge_source_concepts
from .io import write_json
from .models import Course, PipelineState
from .runner import stream_pipeline
from .workspace import (
    BankItem,
    BankFlashcard,
    BuildState,
    Curriculum,
    CurriculumLesson,
    CurriculumModule,
    LessonBank,
    SourceState,
    Workspace,
    canonical_json,
    derive_rung,
    make_item_key,
    natural_sort_key,
    payload_hash,
    sha256_text,
    utc_now_iso,
)
from .worksheet import build_lesson_worksheet, cell_rung_index, worksheet_applies

# Per-source-file pipeline defaults: one module per file, lesson count sized
# for a typical Learn-style module. course.yaml `workflow:` overrides these.
#
# PRECEDENCE (Phase 2b): lessons generated from a depth-classified concept pack
# derive exercises_per_lesson and both distributions from their CELL WORKSHEET
# (worksheet.py quota table: fact 5 / mechanism 7 / decision 9 items per
# concept) — the values below then only size A1's concepts-per-lesson cap and
# serve lessons without a pack (legacy runs). They still must satisfy the
# WorkflowConfig sum/coupling validator.
BUILD_CONFIG_DEFAULTS: dict = {
    "modules_count": 1,
    "min_lessons_total": 5,
    "max_lessons_total": 6,
    "exercises_per_lesson": 30,
    "flashcards_per_lesson": 4,
    # Legacy-path Blooms (worksheet lessons derive theirs): R/U feed rungs R1-R3
    # (levels 1-2), Applying/Analyzing feed R4/R5 (levels 2-3).
    "blooms_distribution": {
        "Remembering": 8,
        "Understanding": 10,
        "Applying": 6,
        "Analyzing/Evaluating": 6,
    },
    "question_type_distribution": {
        "single_choice": 8,
        "multi_choice": 8,
        "true_false": 6,
        "fill_gaps": 4,
        "rearrange": 4,
    },
}


def resolve_build_config(
    meta_workflow_overrides: dict, difficulty: str, *, lessons_override: Optional[int] = None
) -> WorkflowConfig:
    """Defaults <- course.yaml `workflow:` overrides <- --lessons N.

    `lessons_override` pins the lesson count per source file (min = max = N) —
    the fast-test lever: `--lessons 1` builds one lesson per file instead of
    5-6. It changes the config hash, so switching back to a full build
    correctly marks everything dirty again.
    """
    merged = {**BUILD_CONFIG_DEFAULTS, **(meta_workflow_overrides or {}), "difficulty": difficulty}
    if lessons_override is not None:
        merged["min_lessons_total"] = lessons_override
        merged["max_lessons_total"] = lessons_override
    return WorkflowConfig.model_validate(merged)


def config_hash(config: WorkflowConfig) -> str:
    return sha256_text(canonical_json(config.model_dump(mode="json")))


# ---------------------------------------------------------------------------
# Deterministic conversion: internal Course -> workspace pieces
# ---------------------------------------------------------------------------


@dataclass
class ConvertedSource:
    modules: list[CurriculumModule]
    banks: list[LessonBank]


def _unique_key(base: str, taken: set[str]) -> str:
    if base not in taken:
        taken.add(base)
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    key = f"{base}-{n}"
    taken.add(key)
    return key


def convert_course_result(
    course: Course,
    *,
    source_file: str,
    source_sha: str,
    taken_lesson_keys: set[str],
    taken_module_keys: set[str],
) -> ConvertedSource:
    """Deterministically project a per-file pipeline result into curriculum
    modules + lesson banks. Concept ids here are still the RAW atom ids from
    this run — `apply_id_remap()` rewrites them after the graph merge."""
    file_stem = Path(source_file).stem
    modules: list[CurriculumModule] = []
    banks: list[LessonBank] = []

    single_module = len(course.modules) == 1
    for module in course.modules:
        # The filename is the author's title for the module (same decision as
        # course-title-from-filename, RESILIENCE_PLAN §8).
        title = file_stem if single_module else module.title
        module_key = _unique_key(slugify(title, fallback="module"), taken_module_keys)
        cur_lessons: list[CurriculumLesson] = []

        for lesson in module.lessons:
            lesson_key = _unique_key(slugify(lesson.title, fallback="lesson"), taken_lesson_keys)
            concept_ids: list[str] = []
            for atom in lesson.concepts:
                if atom.id and atom.id not in concept_ids:
                    concept_ids.append(atom.id)
            cur_lessons.append(
                CurriculumLesson(key=lesson_key, title=lesson.title, slo=lesson.slo, concepts=concept_ids)
            )

            # Worksheet lessons (Phase 2b): the rung was ASSIGNED at generation
            # time by the cell worksheet — persist that assignment on the bank
            # item. derive_rung() stays as the fallback for legacy payloads
            # (lessons without a depth-classified concept pack).
            cell_rungs: dict[tuple[str, str, str], int] = (
                cell_rung_index(build_lesson_worksheet(lesson.concepts))
                if worksheet_applies(lesson.concepts)
                else {}
            )

            variant_counter: dict[tuple[str, int], int] = {}
            items: list[BankItem] = []
            for ex in lesson.exercises:
                rung = cell_rungs.get(
                    (ex.concept_id or "", ex.question_type, ex.blooms_level.value)
                ) or derive_rung(ex.blooms_level.value, ex.question_type)
                cid = ex.concept_id or None
                counter_key = (cid or "general", rung)
                variant = variant_counter.get(counter_key, 0) + 1
                variant_counter[counter_key] = variant
                payload = ex.model_dump(mode="json")
                items.append(
                    BankItem(
                        item_key=make_item_key(lesson_key, cid, rung, variant),
                        concept_id=cid,
                        rung=rung,
                        variant=variant,
                        payload=payload,
                        payload_hash=payload_hash(payload),
                        source_hash=source_sha,
                    )
                )
            banks.append(
                LessonBank(
                    lesson=lesson_key,
                    module=module_key,
                    items=items,
                    flashcards=[
                        BankFlashcard(front=fc.front, back=fc.back, hint=fc.hint)
                        for fc in lesson.flashcards
                    ],
                )
            )

        modules.append(
            CurriculumModule(key=module_key, title=title, source_file=source_file, lessons=cur_lessons)
        )

    return ConvertedSource(modules=modules, banks=banks)


def apply_id_remap(converted: ConvertedSource, id_remap: dict[str, str]) -> None:
    """Rewrite raw atom ids to canonical graph ids everywhere they appear:
    curriculum concept lists, bank item identity, and inside exercise payloads
    (emit.py reads payload['concept_id'])."""

    def remap(cid: Optional[str]) -> Optional[str]:
        if cid is None:
            return None
        return id_remap.get(cid, cid)

    for module in converted.modules:
        for lesson in module.lessons:
            seen: list[str] = []
            for cid in lesson.concepts:
                mapped = remap(cid)
                if mapped and mapped not in seen:
                    seen.append(mapped)
            lesson.concepts = seen

    for bank in converted.banks:
        variant_counter: dict[tuple[str, int], int] = {}
        for item in bank.items:
            item.concept_id = remap(item.concept_id)
            item.payload["concept_id"] = item.concept_id
            # item_key embeds the concept id — rebuild it (variant numbering
            # restarts per canonical id so keys stay dense and deterministic).
            counter_key = (item.concept_id or "general", item.rung)
            variant = variant_counter.get(counter_key, 0) + 1
            variant_counter[counter_key] = variant
            item.variant = variant
            item.item_key = make_item_key(bank.lesson, item.concept_id, item.rung, variant)
            item.payload_hash = payload_hash(item.payload)


def carry_over_protected_items(new_bank: LessonBank, old_bank: LessonBank | None) -> None:
    """Regeneration contract (ARCHITECTURE.md §3.5): pinned or human-touched
    items survive a rebuild. On key collision the protected item wins."""
    if old_bank is None:
        return
    protected = [it for it in old_bank.items if it.pinned or it.provenance != "generated"]
    if not protected:
        return
    protected_keys = {it.item_key for it in protected}
    new_bank.items = [it for it in new_bank.items if it.item_key not in protected_keys]
    new_bank.items.extend(protected)


# ---------------------------------------------------------------------------
# Build planning (incremental)
# ---------------------------------------------------------------------------


@dataclass
class BuildPlan:
    dirty: list[tuple[Path, str]] = field(default_factory=list)  # (source, reason)
    clean: list[Path] = field(default_factory=list)


def plan_build(ws: Workspace, state: BuildState, cfg_hash: str, *, force: bool = False) -> BuildPlan:
    plan = BuildPlan()
    config_changed = state.workflow_config_hash is not None and state.workflow_config_hash != cfg_hash
    for src in ws.iter_sources():
        prev = state.sources.get(src.name)
        if force:
            plan.dirty.append((src, "forced"))
        elif prev is None:
            plan.dirty.append((src, "new file"))
        elif prev.sha256 != ws.source_hash(src):
            plan.dirty.append((src, "content changed"))
        elif prev.status == "failed":
            plan.dirty.append((src, "previous build failed"))
        elif config_changed:
            plan.dirty.append((src, "workflow config changed"))
        else:
            plan.clean.append(src)
    return plan


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _insert_modules(
    curriculum: Curriculum, new_modules: list[CurriculumModule], *, source_file: str
) -> None:
    """Replace this source's modules in place; for a new source, insert at its
    natural position among generated modules. Authored modules never move."""
    old_indices = [i for i, m in enumerate(curriculum.modules) if m.source_file == source_file]
    if old_indices:
        first = old_indices[0]
        for i in reversed(old_indices):
            del curriculum.modules[i]
        curriculum.modules[first:first] = new_modules
        return

    my_key = natural_sort_key(source_file)
    insert_at = len(curriculum.modules)
    for i, m in enumerate(curriculum.modules):
        if m.source_file is not None and natural_sort_key(m.source_file) > my_key:
            insert_at = i
            break
    curriculum.modules[insert_at:insert_at] = new_modules


@dataclass
class SourceBuildOutcome:
    source: str
    ok: bool
    validation_ok: Optional[bool] = None
    error: Optional[str] = None
    module_keys: list[str] = field(default_factory=list)
    lessons: int = 0
    items: int = 0
    concepts_created: int = 0
    concepts_matched: int = 0
    concepts_retired: int = 0


def build_source(
    ws: Workspace,
    src: Path,
    *,
    model_label: str,
    config: WorkflowConfig,
    echo: Callable[[str], None],
) -> SourceBuildOutcome:
    """Run the pipeline for ONE source file and fold the result into the
    workspace. Any exception is caught by the caller (per-file isolation)."""
    file_stem = src.stem
    file_slug = slugify(file_stem, fallback="source")
    source_sha = ws.source_hash(src)

    # Fresh, wiped run dir per source keeps artifacts from going stale.
    run_dir = ws.build_dir / file_slug
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    state = PipelineState(
        run_id=f"{ws.root.name}-{file_slug}",
        run_dir=str(run_dir),
        input_text=src.read_text(encoding="utf-8"),
        model_id=model_label,
        difficulty=DifficultyLevel(config.difficulty),
        config=config,
        override_title=file_stem,
    )

    # Deferred: pulls in the agent_framework stack, which planning/status and
    # the deterministic unit tests must not need.
    from .workflow import build_techlingo_workflow

    workflow = build_techlingo_workflow()
    result = asyncio.run(stream_pipeline(workflow, state, echo=echo, prefix=f"[{file_slug}] "))
    result.course.title = file_stem  # filename is the author's title (RESILIENCE_PLAN §8)

    write_json(run_dir / "course.internal.json", result.course.model_dump(mode="json"))
    write_json(run_dir / "validation_report.json", result.validation_report.model_dump())

    # ---- deterministic fold-in ------------------------------------------------
    curriculum = ws.load_curriculum()
    graph = ws.load_graph()

    old_modules = [m for m in curriculum.modules if m.source_file == src.name]
    replaced_lesson_keys = {l.key for m in old_modules for l in m.lessons}
    old_banks = {}
    for key in replaced_lesson_keys:
        try:
            old_banks[key] = ws.load_bank(key)
        except FileNotFoundError:
            pass

    taken_lesson_keys = {
        l.key for m in curriculum.modules if m.source_file != src.name for l in m.lessons
    }
    taken_module_keys = {m.key for m in curriculum.modules if m.source_file != src.name}

    converted = convert_course_result(
        result.course,
        source_file=src.name,
        source_sha=source_sha,
        taken_lesson_keys=taken_lesson_keys,
        taken_module_keys=taken_module_keys,
    )

    atoms_by_lesson = {}
    for module, internal_module in zip(converted.modules, result.course.modules):
        for cur_lesson, internal_lesson in zip(module.lessons, internal_module.lessons):
            atoms_by_lesson[cur_lesson.key] = internal_lesson.concepts

    merge = merge_source_concepts(
        graph, atoms_by_lesson, source_file=src.name, replaced_lesson_keys=replaced_lesson_keys
    )
    apply_id_remap(converted, merge.id_remap)

    # Persist: banks (respecting protected items), curriculum, graph.
    new_lesson_keys = {b.lesson for b in converted.banks}
    for bank in converted.banks:
        carry_over_protected_items(bank, old_banks.get(bank.lesson))
        ws.save_bank(bank)
    for stale_key in replaced_lesson_keys - new_lesson_keys:
        ws.delete_bank(stale_key)

    _insert_modules(curriculum, converted.modules, source_file=src.name)
    ws.save_curriculum(curriculum)
    ws.save_graph(merge.graph)

    return SourceBuildOutcome(
        source=src.name,
        ok=True,
        validation_ok=result.validation_report.ok,
        module_keys=[m.key for m in converted.modules],
        lessons=sum(len(m.lessons) for m in converted.modules),
        items=sum(len(b.items) for b in converted.banks),
        concepts_created=len(merge.created),
        concepts_matched=len(set(merge.matched)),
        concepts_retired=len(merge.retired),
    )


def build_course(
    course_dir: str | Path,
    *,
    model_label: str,
    force: bool = False,
    only: Optional[list[str]] = None,
    lessons_override: Optional[int] = None,
    echo: Callable[[str], None] = print,
) -> list[SourceBuildOutcome]:
    """Incrementally build every dirty source of the workspace. Per-file
    failures don't stop the build; they're recorded and retried next time."""
    ws = Workspace(course_dir).require()
    meta = ws.load_meta()
    config = resolve_build_config(meta.workflow, meta.difficulty, lessons_override=lessons_override)
    cfg_hash = config_hash(config)

    state = ws.load_build_state()
    plan = plan_build(ws, state, cfg_hash, force=force)
    if only:
        wanted = set(only)
        plan.dirty = [(p, r) for p, r in plan.dirty if p.name in wanted or p.stem in wanted]

    if not plan.dirty:
        echo("Nothing to build — all sources are up to date.")
        return []

    echo(f"Building {len(plan.dirty)} source(s), {len(plan.clean)} clean:")
    for src, reason in plan.dirty:
        echo(f"  - {src.name}  [{reason}]")

    outcomes: list[SourceBuildOutcome] = []
    for src, _reason in plan.dirty:
        echo(f"\n=== {src.name} ===")
        try:
            outcome = build_source(ws, src, model_label=model_label, config=config, echo=echo)
        except Exception as e:  # noqa: BLE001 — per-file isolation is the point
            import traceback

            # Keep the innermost frames — "IndexError: list index out of range"
            # alone cost a debugging session; the frame names the culprit.
            frames = traceback.format_exception(e)
            tail = " | ".join(line.strip().replace("\n", " ") for line in frames[-3:])
            outcome = SourceBuildOutcome(source=src.name, ok=False, error=f"{type(e).__name__}: {e} [{tail[:400]}]")
            echo(f"FAILED {src.name}: {outcome.error}")
        outcomes.append(outcome)

        state.sources[src.name] = SourceState(
            sha256=ws.source_hash(src),
            status="ok" if outcome.ok else "failed",
            built_at=utc_now_iso(),
            module_keys=outcome.module_keys,
            validation_ok=outcome.validation_ok,
        )
        state.workflow_config_hash = cfg_hash
        ws.save_build_state(state)  # checkpoint after every file — Ctrl+C safe

    return outcomes
