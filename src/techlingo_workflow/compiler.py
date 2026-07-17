"""Curriculum compiler: workspace -> importable artifacts (ARCHITECTURE.md §5–6).

Pure and deterministic — no LLM calls, and every selection is seeded from
compile.yaml (D9: same workspace + same compile.yaml -> byte-identical bundle,
manifest timestamp/version aside). Two compilation shapes:

  * ``levels: 1`` — the Phase-1 FLAT path (one unit per lesson), kept
    byte-identical for today's importer;
  * ``levels: 2..3`` — Phase 2a: each lesson emits one unit per level
    (D5 bridge: level = separate unit, import_key ``<lesson-key>-l<N>``),
    with recycling, per-module checkpoints and an optional course-wide
    final review (§5.1–5.2).

Emitted artifact forms:

  * a **bundle** directory (`dist/<course-id>-v<N>/`): manifest + per-unit
    files + concept registry + bank copies — the incremental-import contract;
  * a **flat** `course.flat.json`: the single-file TechLingo-native course
    today's importer already consumes (same TLQuestion encodings; with
    levels >= 2 it simply carries the level units — still importable, D5).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .emit import emit_question
from .techlingo_models import TLCourse, TLFlashcard, TLModule, TLQuestion, TLUnit
from .validate_techlingo import validate_techlingo_course
from .workspace import (
    BankFlashcard,
    BankItem,
    CompileConfig,
    Concept,
    ConceptGraph,
    Curriculum,
    CurriculumLesson,
    CurriculumModule,
    LessonBank,
    Workspace,
    parse_exercise,
    sha256_text,
    utc_now_iso,
)

BUNDLE_SCHEMA = "bundle-v1"

# Fallbacks when compile.yaml carries a partial `recycle:` map (§5.1 defaults).
DEFAULT_RECYCLE = {"l2": 0.40, "l3": 0.30}

# Final review is one course-wide unit; cap it at ~2 sessions' worth of items
# so a large course degrades to "the most important concepts" instead of an
# exam over every concept (checkpoints already cover each module 1-2×/concept).
FINAL_REVIEW_SESSIONS = 2

# Priority for final-review sampling when depth exists (§5.2: decision/
# mechanism-weighted). Unknown depth (Phase-1 banks) ranks between mechanism
# and fact: still eligible, never preferred over known-deep concepts.
_DEPTH_RANK = {"decision": 0, "mechanism": 1, None: 2, "fact": 3}


# ---------------------------------------------------------------------------
# Deterministic selection helpers
# ---------------------------------------------------------------------------


def _seeded_rng(seed: int, *scope: str) -> random.Random:
    """One RNG per (seed, scope) — hashlib-based so results are stable across
    processes (Python's `hash()` is salted; never use it here)."""
    material = ":".join((str(seed), *scope)).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _active_items(bank: LessonBank) -> list[BankItem]:
    return [it for it in bank.items if it.status != "retired"]


def _confusable_count(concepts_by_id: dict[str, Concept], concept_id: str) -> int:
    concept = concepts_by_id.get(concept_id)
    return len(concept.confusable_with) if concept else 0


def _level_rung_ranges(levels: int) -> list[tuple[int, int]]:
    """Rung partition per §3.2/§5.1: L1 recognize/judge, L2 produce/apply,
    L3 analyze. With levels=2 the top two collapse into one Apply+ unit."""
    if levels == 2:
        return [(1, 2), (3, 5)]
    return [(1, 2), (3, 4), (5, 5)]


@dataclass
class _UnitSpec:
    """A composed unit before TLQuestion emission (BankItems in final order)."""

    import_key: str
    title: str
    slo: str
    kind: str  # "lesson" | "l1".."l3" | "checkpoint" | "final_review"
    items: list[BankItem]
    flashcards: list[BankFlashcard] = field(default_factory=list)


def _emit_unit(spec: _UnitSpec) -> TLUnit:
    questions: list[TLQuestion] = []
    for i, item in enumerate(spec.items, start=1):
        exercise = parse_exercise(item.payload)
        question = emit_question(exercise, f"{spec.import_key}-q{i}")
        # Stable identities for runtime/telemetry (positional import_keys are an
        # app-import detail and must never be used as analytics keys — D10).
        question.options["item_key"] = item.item_key
        question.options["rung"] = item.rung
        questions.append(question)
    flashcards = [
        TLFlashcard(import_key=f"{spec.import_key}-f{i}", front=fc.front, back=fc.back, hint=fc.hint)
        for i, fc in enumerate(spec.flashcards, start=0)
    ]
    return TLUnit(import_key=spec.import_key, title=spec.title, slo=spec.slo, exercises=questions, flashcards=flashcards)


# ---------------------------------------------------------------------------
# Level composition (§5.1, adapted to Phase-1 banks — Phase 2a)
# ---------------------------------------------------------------------------


def _fresh_items(active: list[BankItem], lo: int, hi: int) -> list[BankItem]:
    """The level's own content: per (concept, rung) cell the LOWEST active
    variant; higher variants stay unseen as recycling/checkpoint fuel — that is
    what lets later levels repeat a *concept* without repeating the *question*.
    Rung-5 cells ship every variant (§5.1 "R5(v1[,v2])" — nothing recycles R5
    later; checkpoints may legitimately re-ask)."""
    best: dict[tuple[Optional[str], int], BankItem] = {}
    all_r5: list[BankItem] = []
    for it in active:
        if not (lo <= it.rung <= hi):
            continue
        if it.rung == 5:
            all_r5.append(it)
            continue
        cell = (it.concept_id, it.rung)
        cur = best.get(cell)
        if cur is None or it.variant < cur.variant:
            best[cell] = it
    return list(best.values()) + all_r5


def _compose_lesson_levels(
    lesson: CurriculumLesson,
    bank: LessonBank,
    concepts_by_id: dict[str, Concept],
    cfg: CompileConfig,
    seen: set[str],
) -> tuple[list[_UnitSpec], list[str]]:
    """One unit per level for this lesson. Level N recycles from level N-1's
    rung range: `recycle["lN"]` share of the lesson's concepts get ONE recycled
    item each — an unseen variant of the same (concept, rung) cell when the
    bank has one. Concepts still holding unseen candidates are recycled first
    (2b banks oversample variants precisely for this); a seen item repeats only
    when the quota exceeds the unseen-capable concepts. Priority within each
    tier: most confusables first, then seeded round-robin (§5.1's
    deterministic "hardest first" proxy)."""
    active = _active_items(bank)
    pos = {it.item_key: i for i, it in enumerate(active)}
    concept_order: list[str] = []
    for it in active:
        if it.concept_id and it.concept_id not in concept_order:
            concept_order.append(it.concept_id)

    ranges = _level_rung_ranges(cfg.levels)
    specs: list[_UnitSpec] = []
    notes: list[str] = []
    for n, (lo, hi) in enumerate(ranges, start=1):
        fresh = _fresh_items(active, lo, hi)

        recycled: list[BankItem] = []
        if n >= 2:
            quota = float(cfg.recycle.get(f"l{n}", DEFAULT_RECYCLE.get(f"l{n}", 0.0)))
            prev_lo, prev_hi = ranges[n - 2]
            pool_by_concept: dict[str, list[BankItem]] = {}
            for it in active:
                if it.concept_id and prev_lo <= it.rung <= prev_hi:
                    pool_by_concept.setdefault(it.concept_id, []).append(it)
            eligible = [c for c in concept_order if c in pool_by_concept]
            n_recycle = min(_round_half_up(quota * len(concept_order)), len(eligible))
            if n_recycle > 0:
                rng = _seeded_rng(cfg.seed, "recycle", lesson.key, f"l{n}")
                jitter = {c: rng.random() for c in eligible}
                has_unseen = {
                    c: any(it.item_key not in seen for it in pool_by_concept[c]) for c in eligible
                }
                ranked = sorted(
                    eligible,
                    key=lambda c: (not has_unseen[c], -_confusable_count(concepts_by_id, c), jitter[c]),
                )
                for concept_id in ranked[:n_recycle]:
                    candidates = pool_by_concept[concept_id]
                    unseen = [it for it in candidates if it.item_key not in seen]
                    pick_from = unseen or candidates
                    recycled.append(min(pick_from, key=lambda it: (it.rung, it.variant, pos[it.item_key])))

        items = sorted(fresh + recycled, key=lambda it: (it.rung, pos[it.item_key]))  # easy -> hard
        if len({it.item_key for it in items}) != len(items):  # invariant: never twice in ONE unit
            raise AssertionError(f"duplicate item within unit {lesson.key}-l{n}")

        flashcards = list(bank.flashcards) if n == 1 else []  # flashcards attach to Foundations only
        if not items and not flashcards:
            notes.append(f"lesson '{lesson.key}': level {n} has no items for this bank — unit skipped")
            continue
        seen.update(it.item_key for it in items)
        specs.append(
            _UnitSpec(
                import_key=f"{lesson.key}-l{n}",
                title=f"{lesson.title} · Level {n}",
                slo=lesson.slo,
                kind=f"l{n}",
                items=items,
                flashcards=flashcards,
            )
        )
    return specs, notes


# ---------------------------------------------------------------------------
# Checkpoints & final review (§5.2)
# ---------------------------------------------------------------------------


def _sample_review(
    pass1_order: list[str],
    pass2_order: list[str],
    by_concept: dict[str, list[BankItem]],
    pos: dict[str, tuple],
    seen: set[str],
    *,
    budget: Optional[int],
    grow_to: int,
) -> list[BankItem]:
    """The §5.2 sampler: 1 item per concept (pass 1), then a second item per
    concept in pass2 priority order until `grow_to` items — so every concept
    gets 1-2 probes. Item preference: highest rung first (a checkpoint is a
    mastery probe and the future placement test), unseen variants next, then
    lowest variant; falling back to seen items is legitimate re-asking."""

    def preference(it: BankItem) -> tuple:
        return (-it.rung, 1 if it.item_key in seen else 0, it.variant, pos[it.item_key])

    chosen: list[BankItem] = []
    chosen_keys: set[str] = set()
    for concept_id in pass1_order:
        if budget is not None and len(chosen) >= budget:
            break
        candidates = sorted(by_concept.get(concept_id, []), key=preference)
        if candidates:
            chosen.append(candidates[0])
            chosen_keys.add(candidates[0].item_key)
    for concept_id in pass2_order:
        if len(chosen) >= grow_to:
            break
        candidates = sorted(
            (it for it in by_concept.get(concept_id, []) if it.item_key not in chosen_keys), key=preference
        )
        if candidates:
            chosen.append(candidates[0])
            chosen_keys.add(candidates[0].item_key)
    return sorted(chosen, key=lambda it: (it.rung, pos[it.item_key]))  # easy -> hard


def _collect_concept_items(
    lessons: list[tuple[tuple, CurriculumLesson]], banks: dict[str, LessonBank]
) -> tuple[list[str], dict[str, list[BankItem]], dict[str, tuple]]:
    """First-seen concept order + items per concept + stable item positions,
    over the given (position-prefix, lesson) pairs."""
    concept_order: list[str] = []
    by_concept: dict[str, list[BankItem]] = {}
    pos: dict[str, tuple] = {}
    for prefix, lesson in lessons:
        bank = banks.get(lesson.key)
        if bank is None:
            continue
        for bi, it in enumerate(_active_items(bank)):
            pos[it.item_key] = (*prefix, bi)
            if not it.concept_id:
                continue
            if it.concept_id not in by_concept:
                concept_order.append(it.concept_id)
                by_concept[it.concept_id] = []
            by_concept[it.concept_id].append(it)
    return concept_order, by_concept, pos


def _compose_checkpoint(
    module: CurriculumModule,
    banks: dict[str, LessonBank],
    concepts_by_id: dict[str, Concept],
    cfg: CompileConfig,
    seen: set[str],
) -> Optional[_UnitSpec]:
    """Module checkpoint (`<module-key>-checkpoint`): 1-2 items per concept of
    the module, grown toward session_size_hint hardest-concepts-first."""
    lessons = [((li,), lesson) for li, lesson in enumerate(module.lessons)]
    concept_order, by_concept, pos = _collect_concept_items(lessons, banks)
    if not concept_order:
        return None
    rng = _seeded_rng(cfg.seed, "checkpoint", module.key)
    jitter = {c: rng.random() for c in concept_order}
    pass2 = sorted(concept_order, key=lambda c: (-_confusable_count(concepts_by_id, c), jitter[c]))
    items = _sample_review(concept_order, pass2, by_concept, pos, seen, budget=None, grow_to=cfg.session_size_hint)
    if not items:
        return None
    seen.update(it.item_key for it in items)
    return _UnitSpec(
        import_key=f"{module.key}-checkpoint",
        title=f"{module.title} · Checkpoint",
        slo=f"Review the key concepts of {module.title}.",
        kind="checkpoint",
        items=items,
    )


def _compose_final_review(
    course_id: str,
    curriculum: Curriculum,
    banks: dict[str, LessonBank],
    concepts_by_id: dict[str, Concept],
    cfg: CompileConfig,
    seen: set[str],
) -> Optional[_UnitSpec]:
    """Course-wide final review: the same sampler, concepts weighted
    decision > mechanism > unknown > fact (§5.2; Phase-1 banks with null depth
    degrade to confusables-count order), capped at ~FINAL_REVIEW_SESSIONS
    sessions' worth of items."""
    lessons = [
        ((mi, li), lesson)
        for mi, module in enumerate(curriculum.modules)
        for li, lesson in enumerate(module.lessons)
    ]
    concept_order, by_concept, pos = _collect_concept_items(lessons, banks)
    if not concept_order:
        return None
    rng = _seeded_rng(cfg.seed, "final-review")
    jitter = {c: rng.random() for c in concept_order}

    def priority(concept_id: str) -> tuple:
        concept = concepts_by_id.get(concept_id)
        depth = concept.depth if concept else None
        return (_DEPTH_RANK.get(depth, 2), -_confusable_count(concepts_by_id, concept_id), jitter[concept_id])

    ranked = sorted(concept_order, key=priority)
    budget = max(FINAL_REVIEW_SESSIONS * cfg.session_size_hint, 1)
    items = _sample_review(ranked, ranked, by_concept, pos, seen, budget=budget, grow_to=budget)
    if not items:
        return None
    seen.update(it.item_key for it in items)
    return _UnitSpec(
        import_key=f"{course_id}-final-review",
        title="Final Review",
        slo="Review the most important concepts from across the course.",
        kind="final_review",
        items=items,
    )


# ---------------------------------------------------------------------------
# Workspace -> TLCourse
# ---------------------------------------------------------------------------


@dataclass
class CompiledCourse:
    tl_course: TLCourse
    curriculum: Curriculum
    graph: ConceptGraph
    banks: dict[str, LessonBank]
    problems: list[str]
    skipped_modules: list[str]
    cfg: CompileConfig
    unit_counts: dict[str, int]  # units per kind: "lesson"/"l1".."l3"/"checkpoint"/"final_review"
    notes: list[str]


def compile_workspace(course_dir: str | Path) -> CompiledCourse:
    """Assemble the TL course from curriculum + banks per compile.yaml.
    Modules without any bank content (e.g. authored modules before CMS support
    lands) are skipped and reported, not fatal."""
    ws = Workspace(course_dir).require()
    meta = ws.load_meta()
    curriculum = ws.load_curriculum()
    graph = ws.load_graph()
    cfg = ws.load_compile_config()
    concepts_by_id = graph.by_id()

    banks: dict[str, LessonBank] = {}
    for bank in ws.iter_banks():
        banks[bank.lesson] = bank

    seen: set[str] = set()  # item_keys already placed in an earlier unit — "unseen" preference keys on this
    notes: list[str] = []
    unit_counts: Counter[str] = Counter()
    modules: list[TLModule] = []
    skipped: list[str] = []
    for module in curriculum.modules:
        units: list[TLUnit] = []
        for lesson in module.lessons:
            bank = banks.get(lesson.key)
            if bank is None:
                continue
            if cfg.levels <= 1:
                # Phase-1 flat path: one unit per lesson, full bank in bank order.
                spec = _UnitSpec(
                    import_key=lesson.key,
                    title=lesson.title,
                    slo=lesson.slo,
                    kind="lesson",
                    items=_active_items(bank),
                    flashcards=list(bank.flashcards),
                )
                seen.update(it.item_key for it in spec.items)
                specs = [spec]
            else:
                specs, lesson_notes = _compose_lesson_levels(lesson, bank, concepts_by_id, cfg, seen)
                notes.extend(lesson_notes)
            for spec in specs:
                units.append(_emit_unit(spec))
                unit_counts[spec.kind] += 1
        if units and cfg.checkpoints == "per_module":
            checkpoint = _compose_checkpoint(module, banks, concepts_by_id, cfg, seen)
            if checkpoint is not None:
                units.append(_emit_unit(checkpoint))
                unit_counts[checkpoint.kind] += 1
        if not units:
            skipped.append(module.key)
            continue
        modules.append(TLModule(import_key=module.key, title=module.title, lessons=units))

    if cfg.final_review and modules:
        review = _compose_final_review(meta.id, curriculum, banks, concepts_by_id, cfg, seen)
        if review is not None:
            modules.append(TLModule(import_key=f"{meta.id}-review", title="Course Review", lessons=[_emit_unit(review)]))
            unit_counts[review.kind] += 1

    tl_course = TLCourse(
        import_key=meta.id,
        title=meta.title,
        difficulty=meta.difficulty,
        source_summary=None,
        modules=modules,
    )
    problems = validate_techlingo_course(tl_course)
    return CompiledCourse(
        tl_course=tl_course,
        curriculum=curriculum,
        graph=graph,
        banks=banks,
        problems=problems,
        skipped_modules=skipped,
        cfg=cfg,
        unit_counts=dict(unit_counts),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Concept registry (concepts.json — the runtime needs this for mastery)
# ---------------------------------------------------------------------------


def build_concept_registry(graph: ConceptGraph, banks: dict[str, LessonBank]) -> list[dict[str, Any]]:
    rungs_by_concept: dict[str, set[int]] = {}
    for bank in banks.values():
        for item in bank.items:
            if item.status == "retired" or not item.concept_id:
                continue
            rungs_by_concept.setdefault(item.concept_id, set()).add(item.rung)

    registry: list[dict[str, Any]] = []
    for c in graph.concepts:
        registry.append(
            {
                "id": c.id,
                "label": c.label,
                "summary": c.summary,
                "depth": c.depth,
                "confusable_with": c.confusable_with,
                "lessons": c.lessons,
                "status": c.status,
                "rungs": sorted(rungs_by_concept.get(c.id, set())),
            }
        )
    return registry


# ---------------------------------------------------------------------------
# Bundle writer
# ---------------------------------------------------------------------------


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def next_bundle_version(dist_dir: Path, course_id: str) -> int:
    if not dist_dir.exists():
        return 1
    pattern = re.compile(re.escape(course_id) + r"-v(\d+)$")
    versions = [
        int(m.group(1))
        for p in dist_dir.iterdir()
        if p.is_dir() and (m := pattern.match(p.name))
    ]
    return max(versions, default=0) + 1


@dataclass
class BundleOutput:
    bundle_dir: Path
    flat_path: Optional[Path]
    version: int


def write_bundle(
    course_dir: str | Path,
    compiled: CompiledCourse,
    *,
    flat: bool = True,
) -> BundleOutput:
    ws = Workspace(course_dir).require()
    meta = ws.load_meta()
    version = next_bundle_version(ws.dist_dir, meta.id)
    bundle_dir = ws.dist_dir / f"{meta.id}-v{version}"
    bundle_dir.mkdir(parents=True, exist_ok=False)

    entities: list[dict[str, Any]] = []

    def write_entity(rel_path: str, kind: str, key: str, data: Any) -> None:
        text = _dump_json(data)
        path = bundle_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        entities.append({"kind": kind, "key": key, "path": rel_path, "sha256": sha256_text(text)})

    # Per-unit files — the incremental-import granularity.
    for module in compiled.tl_course.modules:
        for unit in module.lessons:
            write_entity(f"units/{unit.import_key}.json", "unit", unit.import_key, unit.model_dump(mode="json"))

    # Course tree (keys/titles/order only; content lives in units/).
    tree = {
        "schema_version": compiled.tl_course.schema_version,
        "import_key": compiled.tl_course.import_key,
        "title": compiled.tl_course.title,
        "difficulty": compiled.tl_course.difficulty,
        "modules": [
            {
                "import_key": m.import_key,
                "title": m.title,
                "units": [u.import_key for u in m.lessons],
            }
            for m in compiled.tl_course.modules
        ],
    }
    write_entity("course.json", "course", compiled.tl_course.import_key, tree)

    # Concept registry — runtime mastery keys on these ids.
    registry = build_concept_registry(compiled.graph, compiled.banks)
    write_entity("concepts.json", "concepts", compiled.tl_course.import_key, registry)

    # Full exercise bank — Phase 3 runtime session composition reads this;
    # older importers simply ignore it.
    for lesson_key, bank in sorted(compiled.banks.items()):
        write_entity(f"bank/{lesson_key}.json", "bank", lesson_key, bank.model_dump(mode="json"))

    flat_path: Optional[Path] = None
    if flat:
        flat_path = bundle_dir / "course.flat.json"
        flat_text = _dump_json(compiled.tl_course.model_dump(mode="json"))
        flat_path.write_text(flat_text, encoding="utf-8")
        entities.append(
            {"kind": "flat-course", "key": compiled.tl_course.import_key, "path": "course.flat.json", "sha256": sha256_text(flat_text)}
        )

    manifest = {
        "schema_version": BUNDLE_SCHEMA,
        "course": {
            "import_key": meta.id,
            "title": meta.title,
            "difficulty": meta.difficulty,
            "version": version,
        },
        "created_at": utc_now_iso(),
        "generator": "techlingo-agent-framework",
        "compile": {
            "levels": compiled.cfg.levels,
            "recycle": compiled.cfg.recycle,
            "checkpoints": compiled.cfg.checkpoints,
            "final_review": compiled.cfg.final_review,
            "session_size_hint": compiled.cfg.session_size_hint,
            "seed": compiled.cfg.seed,
        },
        "entities": entities,
    }
    (bundle_dir / "manifest.json").write_text(_dump_json(manifest), encoding="utf-8")

    return BundleOutput(bundle_dir=bundle_dir, flat_path=flat_path, version=version)
