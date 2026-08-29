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
from .publication_safety import banks_sha256, hash_data
from .runner import stream_pipeline
from .workspace import (
    BUILD_STATE_SCHEMA,
    BankItem,
    BankFlashcard,
    BuildState,
    ConceptGraph,
    Curriculum,
    CurriculumLesson,
    CurriculumModule,
    LessonBank,
    SourcePublication,
    SourceState,
    Workspace,
    WorkspaceError,
    _atomic_write_text,
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
# derive both distributions from their CELL WORKSHEET (worksheet.py quota
# table: fact 5 / mechanism 7 / decision 9 rows per concept). By default every
# quota row is generated. A per-course `worksheet_items_per_lesson` override
# applies an exact, ladder-preserving worksheet budget before generation. The
# legacy values below still size A1's concept cap and serve depthless lessons.
BUILD_CONFIG_DEFAULTS: dict = {
    "modules_count": 1,
    "min_lessons_total": 5,
    "max_lessons_total": 6,
    "exercises_per_lesson": 30,
    "worksheet_items_per_lesson": None,
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
    worksheet_items_per_lesson: int | None = None,
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
            if worksheet_applies(lesson.concepts):
                worksheet = build_lesson_worksheet(
                    lesson.concepts, item_budget=worksheet_items_per_lesson
                )
                if len(lesson.exercises) != len(worksheet):
                    raise WorkspaceError(
                        f"lesson {lesson.title!r} has {len(lesson.exercises)} exercises but its "
                        f"authoritative worksheet has {len(worksheet)} rows"
                    )
                cell_rungs = cell_rung_index(worksheet)
            else:
                cell_rungs = {}

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
    items survive a rebuild.

    A same-key protected item may replace wording and feedback, but it may not
    replace the A5-validated worksheet row contract. Collisions are replaced in
    place so protected content cannot reorder canonical worksheet identities.
    """
    if old_bank is None:
        return
    protected = [it for it in old_bank.items if it.pinned or it.provenance != "generated"]
    if not protected:
        return

    def row_contract(item: BankItem) -> dict[str, object]:
        contract: dict[str, object] = {
            "concept_id": item.concept_id,
            "rung": item.rung,
            "variant": item.variant,
            "payload.concept_id": item.payload.get("concept_id"),
            "payload.question_type": item.payload.get("question_type"),
            "payload.blooms_level": item.payload.get("blooms_level"),
        }
        if item.payload.get("question_type") == "true_false":
            contract["payload.correct_answer"] = item.payload.get("correct_answer")
        return contract

    regenerated_by_key = {item.item_key: item for item in new_bank.items}
    protected_by_key = {item.item_key: item for item in protected}
    for item_key, protected_item in protected_by_key.items():
        regenerated_item = regenerated_by_key.get(item_key)
        if regenerated_item is None:
            continue
        expected = row_contract(regenerated_item)
        actual = row_contract(protected_item)
        changed = sorted(
            field
            for field in set(expected) | set(actual)
            if canonical_json(expected.get(field)) != canonical_json(actual.get(field))
        )
        if changed:
            raise WorkspaceError(
                f"protected item {item_key!r} conflicts with its validated worksheet row; "
                f"changed immutable field(s): {', '.join(changed)}"
            )

    regenerated_keys = set(regenerated_by_key)
    new_bank.items = [
        protected_by_key.get(item.item_key, item)
        for item in new_bank.items
    ]
    new_bank.items.extend(
        item for item in protected if item.item_key not in regenerated_keys
    )


# ---------------------------------------------------------------------------
# Build planning (incremental)
# ---------------------------------------------------------------------------


@dataclass
class BuildPlan:
    dirty: list[tuple[Path, str]] = field(default_factory=list)  # (source, reason)
    clean: list[Path] = field(default_factory=list)


def plan_build(ws: Workspace, state: BuildState, cfg_hash: str, *, force: bool = False) -> BuildPlan:
    banks = list(ws.iter_banks())
    banks_by_lesson = {bank.lesson: bank for bank in banks}
    current_bank_hash = banks_sha256(banks_by_lesson)
    global_bank_evidence_ok = (
        state.schema_version == BUILD_STATE_SCHEMA
        and state.bank_sha256 is not None
        and state.bank_sha256 == current_bank_hash
    )
    global_config_evidence_ok = state.workflow_config_hash == cfg_hash
    plan = BuildPlan()
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
        elif prev.validation_ok is not True:
            plan.dirty.append((src, "previous validation missing or failed"))
        elif prev.config_sha256 is not None and prev.config_sha256 != cfg_hash:
            plan.dirty.append((src, "workflow config changed"))
        elif state.schema_version != BUILD_STATE_SCHEMA:
            plan.dirty.append((src, "legacy publication evidence requires rebuild"))
        elif prev.config_sha256 is None or prev.validation_report_sha256 is None:
            plan.dirty.append((src, "publication evidence missing"))
        elif prev.last_known_good is None:
            plan.dirty.append((src, "last-known-good publication evidence missing"))
        else:
            lkg = prev.last_known_good
            source_banks = {
                key: bank for key, bank in banks_by_lesson.items() if bank.module in lkg.module_keys
            }
            source_evidence_ok = (
                lkg.source_sha256 == prev.sha256
                and lkg.config_sha256 == prev.config_sha256 == cfg_hash
                and lkg.validation_report_sha256 == prev.validation_report_sha256
                and lkg.module_keys == prev.module_keys
                and lkg.bank_sha256 == banks_sha256(source_banks)
            )
            # During an interrupted configuration migration the workspace-wide
            # hashes deliberately remain on the last fully built snapshot. A
            # source already promoted under the new config is nevertheless
            # clean when its own LKG binds the exact source, report, config, and
            # canonical banks. Once the global config hash advances, a global
            # bank-hash mismatch again indicates post-build tampering.
            if not source_evidence_ok or (
                global_config_evidence_ok and not global_bank_evidence_ok
            ):
                plan.dirty.append((src, "publication evidence no longer matches canonical content"))
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
    validation_report_sha256: Optional[str] = None
    bank_sha256: Optional[str] = None
    promoted_state: Optional[BuildState] = field(default=None, repr=False)


def _promote_generated_state(
    ws: Workspace,
    *,
    curriculum: Curriculum,
    graph: ConceptGraph,
    banks: list[LessonBank],
    stale_lesson_keys: set[str],
    build_state: Optional[BuildState] = None,
    expected_snapshot_sha256: Optional[dict[str, str]] = None,
    expected_source: Optional[tuple[Path, str]] = None,
) -> None:
    """Promote one validated source with rollback across canonical files.

    Each individual save is an atomic replace (workspace.py).  This wrapper
    additionally snapshots every affected file and restores the complete LKG
    set on any exception, including Ctrl-C during the short promotion window.
    Pipeline execution and validation happen before this function is called.
    """

    # The shared workspace lock spans the complete multi-file transaction and
    # its final build-state commit.  Individual save methods take the same
    # re-entrant lock, so no other cooperating build or publisher can observe
    # or promote an interleaved canonical state.
    with ws.publication_lock():
        if expected_snapshot_sha256 is not None:
            current_snapshot = {
                "curriculum": hash_data(ws.load_curriculum()),
                "graph": hash_data(ws.load_graph()),
                "banks": banks_sha256(list(ws.iter_banks())),
                "build_state": hash_data(ws.load_build_state()),
            }
            changed = [
                name
                for name, expected_hash in expected_snapshot_sha256.items()
                if current_snapshot.get(name) != expected_hash
            ]
            if changed:
                raise WorkspaceError(
                    "canonical workspace changed while the source challenger was being "
                    f"prepared ({', '.join(changed)}); retry the build"
                )
        if expected_source is not None:
            source_path, source_hash = expected_source
            if not source_path.exists() or ws.source_hash(source_path) != source_hash:
                raise WorkspaceError(
                    f"source changed while its challenger was being prepared: {source_path.name}"
                )

        paths = [ws.build_state_path] if build_state is not None else []
        paths.extend(ws.bank_path(bank.lesson) for bank in banks)
        paths.extend(ws.bank_path(key) for key in stale_lesson_keys)
        paths.extend([ws.graph_path, ws.curriculum_path])
        # Preserve insertion order while removing collisions (a regenerated bank
        # can also appear in stale_lesson_keys when a caller supplies bad input).
        paths = list(dict.fromkeys(paths))
        originals = {path: path.read_bytes() if path.exists() else None for path in paths}

        try:
            for bank in banks:
                ws.save_bank(bank)
            for stale_key in stale_lesson_keys - {bank.lesson for bank in banks}:
                ws.delete_bank(stale_key)
            ws.save_graph(graph)
            # Install curriculum after every bank it references and the matching
            # graph are durable.  When supplied, build_state is the final commit
            # marker and is rolled back with the content on interruption.
            ws.save_curriculum(curriculum)
            if build_state is not None:
                ws.save_build_state(build_state)
        except BaseException as promotion_error:
            rollback_errors: list[str] = []
            for path, original in originals.items():
                try:
                    if original is None:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                    else:
                        _atomic_write_text(path, original.decode("utf-8"))
                except BaseException as rollback_error:  # pragma: no cover - catastrophic filesystem failure
                    rollback_errors.append(f"{path}: {rollback_error}")
            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise RuntimeError(
                    f"promotion failed and rollback was incomplete: {details}"
                ) from promotion_error
            raise


def build_source(
    ws: Workspace,
    src: Path,
    *,
    model_label: str,
    config: WorkflowConfig,
    echo: Callable[[str], None],
    build_state: Optional[BuildState] = None,
    config_sha256: Optional[str] = None,
) -> SourceBuildOutcome:
    """Run the pipeline for ONE source file and fold the result into the
    workspace. Any exception is caught by the caller (per-file isolation)."""
    file_stem = src.stem
    file_slug = slugify(file_stem, fallback="source")
    source_text = src.read_text(encoding="utf-8")
    source_sha = sha256_text(source_text)

    # Fresh, wiped run dir per source keeps artifacts from going stale.
    run_dir = ws.build_dir / file_slug
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    state = PipelineState(
        run_id=f"{ws.root.name}-{file_slug}",
        run_dir=str(run_dir),
        input_text=source_text,
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

    validation_report_sha = hash_data(result.validation_report)
    errors = [issue for issue in result.validation_report.issues if issue.severity == "error"]
    if not result.validation_report.ok or errors:
        return SourceBuildOutcome(
            source=src.name,
            ok=False,
            validation_ok=False,
            error=f"blocking validation failed ({len(errors)} error(s))",
            validation_report_sha256=validation_report_sha,
        )

    # ---- deterministic fold-in ------------------------------------------------
    # Load every canonical fold-in input and its precondition hashes from one
    # lock-protected snapshot.  The expensive model run already completed, so
    # the lock is held only for deterministic IO.
    with ws.publication_lock():
        if not src.exists() or ws.source_hash(src) != source_sha:
            raise WorkspaceError(
                f"source changed while its challenger was being generated: {src.name}"
            )
        curriculum = ws.load_curriculum()
        graph = ws.load_graph()
        canonical_banks = {bank.lesson: bank for bank in ws.iter_banks()}
        if build_state is not None:
            build_state = ws.load_build_state()
        promotion_snapshot = {
            "curriculum": hash_data(curriculum),
            "graph": hash_data(graph),
            "banks": banks_sha256(canonical_banks),
            "build_state": hash_data(ws.load_build_state()),
        }

    old_modules = [m for m in curriculum.modules if m.source_file == src.name]
    replaced_lesson_keys = {l.key for m in old_modules for l in m.lessons}
    old_banks = {
        key: canonical_banks[key]
        for key in replaced_lesson_keys
        if key in canonical_banks
    }

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
        worksheet_items_per_lesson=config.worksheet_items_per_lesson,
    )

    atoms_by_lesson = {}
    for module, internal_module in zip(converted.modules, result.course.modules):
        for cur_lesson, internal_lesson in zip(module.lessons, internal_module.lessons):
            atoms_by_lesson[cur_lesson.key] = internal_lesson.concepts

    merge = merge_source_concepts(
        graph, atoms_by_lesson, source_file=src.name, replaced_lesson_keys=replaced_lesson_keys
    )
    apply_id_remap(converted, merge.id_remap)

    # Promote only after hard validation: banks (respecting protected items),
    # curriculum, and graph move together or roll back to the previous LKG.
    new_lesson_keys = {b.lesson for b in converted.banks}
    for bank in converted.banks:
        carry_over_protected_items(bank, old_banks.get(bank.lesson))
        if (
            config.worksheet_items_per_lesson is not None
            and worksheet_applies(atoms_by_lesson.get(bank.lesson, []))
        ):
            active_count = sum(item.status != "retired" for item in bank.items)
            if active_count != config.worksheet_items_per_lesson:
                raise WorkspaceError(
                    f"bank {bank.lesson!r} would contain {active_count} active items after "
                    "preserving pinned/human content, but the exact worksheet policy requires "
                    f"{config.worksheet_items_per_lesson}; resolve the protected-item budget "
                    "conflict before promotion"
                )

    _insert_modules(curriculum, converted.modules, source_file=src.name)
    source_bank_hash = banks_sha256(converted.banks)
    promoted_state: Optional[BuildState] = None
    if build_state is not None:
        if not config_sha256:
            raise ValueError("config_sha256 is required when promoting build state")
        promoted_state = build_state.model_copy(deep=True)
        built_at = utc_now_iso()
        promoted_state.sources[src.name] = SourceState(
            sha256=source_sha,
            status="ok",
            built_at=built_at,
            module_keys=[module.key for module in converted.modules],
            validation_ok=True,
            config_sha256=config_sha256,
            validation_report_sha256=validation_report_sha,
            last_known_good=SourcePublication(
                source_sha256=source_sha,
                config_sha256=config_sha256,
                bank_sha256=source_bank_hash,
                validation_report_sha256=validation_report_sha,
                promoted_at=built_at,
                module_keys=[module.key for module in converted.modules],
            ),
        )
        projected_banks = dict(canonical_banks)
        for stale_key in replaced_lesson_keys - new_lesson_keys:
            projected_banks.pop(stale_key, None)
        projected_banks.update({bank.lesson: bank for bank in converted.banks})
        promoted_state.bank_sha256 = banks_sha256(projected_banks)
    _promote_generated_state(
        ws,
        curriculum=curriculum,
        graph=merge.graph,
        banks=converted.banks,
        stale_lesson_keys=replaced_lesson_keys - new_lesson_keys,
        build_state=promoted_state,
        expected_snapshot_sha256=promotion_snapshot,
        expected_source=(src, source_sha),
    )

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
        validation_report_sha256=validation_report_sha,
        bank_sha256=source_bank_hash,
        promoted_state=promoted_state,
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
            outcome = build_source(
                ws,
                src,
                model_label=model_label,
                config=config,
                echo=echo,
                build_state=state,
                config_sha256=cfg_hash,
            )
        except Exception as e:  # noqa: BLE001 — per-file isolation is the point
            import traceback

            # Keep the innermost frames — "IndexError: list index out of range"
            # alone cost a debugging session; the frame names the culprit.
            frames = traceback.format_exception(e)
            tail = " | ".join(line.strip().replace("\n", " ") for line in frames[-3:])
            outcome = SourceBuildOutcome(source=src.name, ok=False, error=f"{type(e).__name__}: {e} [{tail[:400]}]")
            echo(f"FAILED {src.name}: {outcome.error}")
        outcomes.append(outcome)

        if outcome.ok:
            if outcome.promoted_state is None:
                raise AssertionError("successful source build did not atomically promote build state")
            state = outcome.promoted_state
        else:
            # Merge the failed attempt into the latest state while holding the
            # same shared lock.  A long-running failed challenger must never
            # overwrite another process's successful source promotion.
            with ws.publication_lock():
                state = ws.load_build_state()
                previous = state.sources.get(src.name)
                current_source_hash = (
                    ws.source_hash(src) if src.exists() else sha256_text("")
                )
                state.sources[src.name] = SourceState(
                    sha256=current_source_hash,
                    status="failed",
                    built_at=utc_now_iso(),
                    module_keys=previous.module_keys if previous else [],
                    validation_ok=outcome.validation_ok,
                    config_sha256=cfg_hash,
                    validation_report_sha256=outcome.validation_report_sha256,
                    error=outcome.error,
                    last_known_good=previous.last_known_good if previous else None,
                )
                # No canonical content changed, so this single atomic checkpoint
                # is sufficient for a failed attempt and remains Ctrl-C safe.
                ws.save_build_state(state)

    # The workspace-wide config hash is advanced only when every current
    # source is a validated success under this exact config.  Per-source hashes
    # make interrupted config migrations resume correctly on the next build.
    with ws.publication_lock():
        state = ws.load_build_state()
        current_names = {source.name for source in ws.iter_sources()}
        fully_built = all(
            (source_state := state.sources.get(name)) is not None
            and source_state.status == "ok"
            and source_state.validation_ok is True
            and source_state.config_sha256 == cfg_hash
            and source_state.validation_report_sha256 is not None
            and source_state.last_known_good is not None
            for name in current_names
        )
        if fully_built:
            state.workflow_config_hash = cfg_hash
            state.bank_sha256 = banks_sha256(list(ws.iter_banks()))
        ws.save_build_state(state)

    return outcomes
