"""Curriculum compiler: workspace -> importable artifacts (ARCHITECTURE.md §5–6).

Pure and deterministic — no LLM calls, and every selection is seeded from
compile.yaml (D9: same workspace + same compile.yaml -> byte-identical bundle,
manifest timestamp/version aside). Two compilation shapes:

  * ``levels: 1`` — the Phase-1 FLAT shape (one unit per lesson), kept
    structurally compatible with today's importer while applying the shared
    learner-experience scheduler and answer-preserving option permutation;
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

import copy
import hashlib
import itertools
import json
import math
import os
import random
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .emit import emit_question
from .experience import (
    CompositionDiagnostics,
    ConstraintRelaxation,
    ExperienceItem,
    ExperiencePolicy,
    compose_experience,
    select_variants,
)
from .publication_safety import (
    PublicationSafetyError,
    banks_sha256,
    hash_data,
    inspect_publication_readiness,
    require_publishable,
)
from .sequence_quality import (
    SequenceQualityPolicy,
    SequenceQualityReport,
    validate_tl_course,
)
from .techlingo_models import TLCourse, TLFlashcard, TLModule, TLQuestion, TLUnit
from .validate_techlingo import validate_techlingo_course
from .workspace import (
    BankFlashcard,
    BankItem,
    CompilationPublication,
    CompileConfig,
    Concept,
    ConceptGraph,
    CourseMeta,
    Curriculum,
    CurriculumLesson,
    CurriculumModule,
    LessonBank,
    Workspace,
    canonical_json,
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
    item_statuses: dict[str, str] = field(default_factory=dict)
    diagnostics: Optional[CompositionDiagnostics] = None


def _experience_policy(cfg: CompileConfig) -> ExperiencePolicy:
    values = cfg.experience
    return ExperiencePolicy(
        max_same_mechanic_streak=values.max_same_mechanic_streak,
        max_same_ui_family_streak=values.max_same_ui_family_streak,
        max_same_true_false_answer_streak=values.max_same_true_false_answer_streak,
        mechanics_window_size=values.mechanics_window_size,
        min_mechanics_per_window=values.min_mechanics_per_window,
        avoid_adjacent_same_concept=values.avoid_adjacent_same_concept,
        max_search_states=values.max_search_states,
        relaxation_order=tuple(values.relaxation_order),
    )


def _sequence_quality_policy(cfg: CompileConfig) -> SequenceQualityPolicy:
    values = cfg.sequence_quality
    return SequenceQualityPolicy(
        experience=_experience_policy(cfg),
        max_same_correct_position_streak=values.max_same_correct_position_streak,
        prompt_stem_words=values.prompt_stem_words,
        max_repeated_prompt_stem=values.max_repeated_prompt_stem,
        max_downward_rung_jump=values.max_downward_rung_jump,
        max_same_rung_streak=values.max_same_rung_streak,
    )


def _item_experience(
    item: BankItem,
    *,
    module_key: Optional[str] = None,
    lesson_key: Optional[str] = None,
    learning_status: str = "new",
) -> ExperienceItem:
    payload = item.payload
    mechanic = str(payload.get("question_type") or "unknown")
    tf_answer = payload.get("correct_answer") if mechanic == "true_false" else None
    correct_indexes = tuple(
        index
        for index, option in enumerate(payload.get("options", []))
        if option.get("is_correct") is True
    )
    prompt = str(
        payload.get("statement")
        if mechanic == "true_false"
        else payload.get("prompt") or ""
    )
    return ExperienceItem(
        item_key=item.item_key,
        concept_id=item.concept_id,
        rung=item.rung,
        variant=item.variant,
        mechanic=mechanic,
        true_false_answer=tf_answer if isinstance(tf_answer, bool) else None,
        correct_option_indexes=correct_indexes,
        blooms_level=payload.get("blooms_level"),
        module_key=module_key,
        lesson_key=lesson_key,
        learning_status=learning_status,
        prompt=prompt,
        payload_hash=item.payload_hash,
    )


def _continues_alternation(values: list[tuple[int, ...]], candidate: tuple[int, ...]) -> bool:
    tail = [*values, candidate][-4:]
    return (
        len(tail) == 4
        and len(set(tail)) == 2
        and tail[0] == tail[2]
        and tail[1] == tail[3]
        and tail[0] != tail[1]
    )


def _balance_choice_options(
    exercise: Any,
    *,
    item_key: str,
    seed: int,
    scope: str,
    position_counts: Counter[tuple[int, ...]],
    position_history: list[tuple[int, ...]],
) -> Any:
    """Deterministically permute presentation order while preserving options.

    Existing banks overwhelmingly put the answer first.  Reordering question
    options is a presentation normalization, not a content edit: every option
    object and correctness flag is retained, and ``emit_question`` derives the
    matching answer indexes after the permutation.
    """

    options = getattr(exercise, "options", None)
    if not isinstance(options, list) or not options:
        return exercise
    correct = [option for option in options if option.is_correct]
    incorrect = [option for option in options if not option.is_correct]
    if not correct or not incorrect:
        return exercise

    count = len(options)
    candidates = [tuple(indexes) for indexes in itertools.combinations(range(count), len(correct))]
    rng = _seeded_rng(seed, "option-order", scope, item_key)
    jitter = {candidate: rng.random() for candidate in candidates}

    def score(candidate: tuple[int, ...]) -> tuple:
        return (
            position_counts[candidate],
            int(bool(position_history) and position_history[-1] == candidate),
            int(_continues_alternation(position_history, candidate)),
            max((sum(index in prior for prior in position_history) for index in candidate), default=0),
            jitter[candidate],
            candidate,
        )

    target = min(candidates, key=score)
    copy = exercise.model_copy(deep=True)
    correct_iter = iter([option.model_copy(deep=True) for option in correct])
    incorrect_iter = iter([option.model_copy(deep=True) for option in incorrect])
    copy.options = [
        next(correct_iter) if index in target else next(incorrect_iter)
        for index in range(count)
    ]

    before = Counter(canonical_json(option.model_dump(mode="json")) for option in options)
    after = Counter(canonical_json(option.model_dump(mode="json")) for option in copy.options)
    if before != after:
        raise AssertionError(f"option permutation changed content for {item_key}")
    derived = tuple(index for index, option in enumerate(copy.options) if option.is_correct)
    if derived != target:
        raise AssertionError(f"option permutation changed answer semantics for {item_key}")
    position_counts[target] += 1
    position_history.append(target)
    return copy


def _emit_unit(
    spec: _UnitSpec,
    *,
    owners: dict[str, tuple[str, str]],
    cfg: CompileConfig,
) -> TLUnit:
    questions: list[TLQuestion] = []
    position_counts: Counter[tuple[int, ...]] = Counter()
    position_history: list[tuple[int, ...]] = []
    for i, item in enumerate(spec.items, start=1):
        exercise = parse_exercise(item.payload)
        if cfg.sequence_quality.permute_choice_options:
            exercise = _balance_choice_options(
                exercise,
                item_key=item.item_key,
                seed=cfg.seed,
                scope=spec.import_key,
                position_counts=position_counts,
                position_history=position_history,
            )
        question = emit_question(exercise, f"{spec.import_key}-q{i}")
        # Stable identities for runtime/telemetry (positional import_keys are an
        # app-import detail and must never be used as analytics keys — D10).
        question.options["item_key"] = item.item_key
        question.options["rung"] = item.rung
        question.options["variant"] = item.variant
        module_key, lesson_key = owners.get(item.item_key, ("", item.item_key.split("/", 1)[0]))
        question.options["module_key"] = module_key
        question.options["lesson_key"] = lesson_key
        question.options["learning_status"] = spec.item_statuses.get(item.item_key, "new")
        questions.append(question)
    flashcards = [
        TLFlashcard(import_key=f"{spec.import_key}-f{i}", front=fc.front, back=fc.back, hint=fc.hint)
        for i, fc in enumerate(spec.flashcards, start=0)
    ]
    return TLUnit(import_key=spec.import_key, title=spec.title, slo=spec.slo, exercises=questions, flashcards=flashcards)


# ---------------------------------------------------------------------------
# Level composition (§5.1, adapted to Phase-1 banks — Phase 2a)
# ---------------------------------------------------------------------------


def _fresh_items(
    active: list[BankItem],
    lo: int,
    hi: int,
    *,
    bank: LessonBank,
    cfg: CompileConfig,
    seen: set[str],
    scope: str,
) -> list[BankItem]:
    """Select one experience-aware variant per fresh non-R5 cell.

    Rung-5 cells still ship every variant (§5.1); no later level recycles R5.
    Unused variants remain in the bank for recycling and runtime review.
    """

    cells: dict[tuple[Optional[str], int], list[BankItem]] = {}
    all_r5: list[BankItem] = []
    for it in active:
        if not (lo <= it.rung <= hi):
            continue
        if it.rung == 5:
            all_r5.append(it)
            continue
        cell = (it.concept_id, it.rung)
        cells.setdefault(cell, []).append(it)
    context = [
        _item_experience(
            item,
            module_key=bank.module,
            lesson_key=bank.lesson,
            learning_status="new",
        )
        for item in all_r5
    ]
    groups = [
        [
            _item_experience(
                item,
                module_key=bank.module,
                lesson_key=bank.lesson,
                learning_status="new",
            )
            for item in group
        ]
        for group in cells.values()
    ]
    selected = select_variants(
        groups,
        seed=cfg.seed,
        scope=scope,
        seen_item_keys=seen,
        context=context,
    ).selected if groups else ()
    by_key = {item.item_key: item for item in active}
    return [by_key[item.item_key] for item in selected] + all_r5


def _pick_recycled_item(
    candidates: list[BankItem],
    *,
    context: list[BankItem],
    seen: set[str],
    cfg: CompileConfig,
    scope: str,
    pos: dict[str, int],
) -> BankItem:
    pick_from = [item for item in candidates if item.item_key not in seen] or candidates
    mechanics = Counter(item.payload.get("question_type", "unknown") for item in context)
    tf_answers = Counter(
        item.payload.get("correct_answer")
        for item in context
        if item.payload.get("question_type") == "true_false"
    )
    positions = Counter(
        tuple(index for index, option in enumerate(item.payload.get("options", [])) if option.get("is_correct"))
        for item in context
        if item.payload.get("options")
    )
    rng = _seeded_rng(cfg.seed, "recycle-item", scope)
    jitter = {item.item_key: rng.random() for item in pick_from}

    def score(item: BankItem) -> tuple:
        mechanic = item.payload.get("question_type", "unknown")
        answer = item.payload.get("correct_answer") if mechanic == "true_false" else None
        option_indexes = tuple(
            index
            for index, option in enumerate(item.payload.get("options", []))
            if option.get("is_correct")
        )
        projected_tf = 0
        if answer is not None:
            projected_tf = abs(
                tf_answers[True] + int(answer is True)
                - tf_answers[False]
                - int(answer is False)
            )
        return (
            mechanics[mechanic],
            projected_tf,
            positions[option_indexes] if option_indexes else 0,
            item.rung,
            item.variant,
            jitter[item.item_key],
            pos[item.item_key],
        )

    return min(pick_from, key=score)


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
        fresh = _fresh_items(
            active,
            lo,
            hi,
            bank=bank,
            cfg=cfg,
            seen=seen,
            scope=f"{lesson.key}-l{n}",
        )

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
                    recycled.append(
                        _pick_recycled_item(
                            pool_by_concept[concept_id],
                            context=[*fresh, *recycled],
                            seen=seen,
                            cfg=cfg,
                            scope=f"{lesson.key}-l{n}:{concept_id}",
                            pos=pos,
                        )
                    )

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
                item_statuses={
                    **{item.item_key: "new" for item in fresh},
                    **{item.item_key: "review" for item in recycled},
                },
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
    cfg: CompileConfig,
    scope: str,
    *,
    budget: Optional[int],
    grow_to: int,
) -> list[BankItem]:
    """Sample 1-2 probes per concept with mastery and rhythm in balance.

    Candidates stay within two rungs of the concept's highest available probe,
    then prefer unseen content and mechanics underrepresented in the review.
    This retains checkpoint difficulty without forcing every concept into one
    homogeneous highest-rung choice block.
    """

    def pick(candidates: list[BankItem], ordinal: int) -> Optional[BankItem]:
        if not candidates:
            return None
        highest = max(item.rung for item in candidates)
        band = [item for item in candidates if item.rung >= max(1, highest - 2)]
        pick_from = [item for item in band if item.item_key not in seen] or band
        mechanics = Counter(item.payload.get("question_type", "unknown") for item in chosen)
        tf_answers = Counter(
            item.payload.get("correct_answer")
            for item in chosen
            if item.payload.get("question_type") == "true_false"
        )
        positions = Counter(
            tuple(
                index
                for index, option in enumerate(item.payload.get("options", []))
                if option.get("is_correct")
            )
            for item in chosen
            if item.payload.get("options")
        )
        rng = _seeded_rng(cfg.seed, "review-item", scope, str(ordinal))
        jitter = {item.item_key: rng.random() for item in pick_from}

        def preference(item: BankItem) -> tuple:
            mechanic = item.payload.get("question_type", "unknown")
            answer = item.payload.get("correct_answer") if mechanic == "true_false" else None
            indexes = tuple(
                index
                for index, option in enumerate(item.payload.get("options", []))
                if option.get("is_correct")
            )
            projected_tf = 0
            if answer is not None:
                projected_tf = abs(
                    tf_answers[True] + int(answer is True)
                    - tf_answers[False]
                    - int(answer is False)
                )
            return (
                mechanics[mechanic],
                projected_tf,
                positions[indexes] if indexes else 0,
                highest - item.rung,
                item.variant,
                jitter[item.item_key],
                pos[item.item_key],
            )

        return min(pick_from, key=preference)

    chosen: list[BankItem] = []
    chosen_keys: set[str] = set()
    for concept_id in pass1_order:
        if budget is not None and len(chosen) >= budget:
            break
        selected = pick(list(by_concept.get(concept_id, [])), len(chosen))
        if selected is not None:
            chosen.append(selected)
            chosen_keys.add(selected.item_key)
    for concept_id in pass2_order:
        if len(chosen) >= grow_to:
            break
        candidates = [
            item
            for item in by_concept.get(concept_id, [])
            if item.item_key not in chosen_keys
        ]
        selected = pick(candidates, len(chosen))
        if selected is not None:
            chosen.append(selected)
            chosen_keys.add(selected.item_key)
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
    items = _sample_review(
        concept_order,
        pass2,
        by_concept,
        pos,
        seen,
        cfg,
        f"{module.key}-checkpoint",
        budget=None,
        grow_to=cfg.session_size_hint,
    )
    if not items:
        return None
    seen.update(it.item_key for it in items)
    return _UnitSpec(
        import_key=f"{module.key}-checkpoint",
        title=f"{module.title} · Checkpoint",
        slo=f"Review the key concepts of {module.title}.",
        kind="checkpoint",
        items=items,
        item_statuses={item.item_key: "review" for item in items},
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
    items = _sample_review(
        ranked,
        ranked,
        by_concept,
        pos,
        seen,
        cfg,
        f"{course_id}-final-review",
        budget=budget,
        grow_to=budget,
    )
    if not items:
        return None
    seen.update(it.item_key for it in items)
    return _UnitSpec(
        import_key=f"{course_id}-final-review",
        title="Final Review",
        slo="Review the most important concepts from across the course.",
        kind="final_review",
        items=items,
        item_statuses={item.item_key: "review" for item in items},
    )


# ---------------------------------------------------------------------------
# Workspace -> TLCourse
# ---------------------------------------------------------------------------


def _schedule_spec(
    spec: _UnitSpec,
    *,
    cfg: CompileConfig,
    owners: dict[str, tuple[str, str]],
) -> _UnitSpec:
    metadata = [
        _item_experience(
            item,
            module_key=owners.get(item.item_key, (None, None))[0],
            lesson_key=owners.get(item.item_key, (None, None))[1],
            learning_status=spec.item_statuses.get(item.item_key, "new"),
        )
        for item in spec.items
    ]
    result = compose_experience(
        metadata,
        policy=_experience_policy(cfg),
        seed=cfg.seed,
        scope=spec.import_key,
    )
    by_key = {item.item_key: item for item in spec.items}
    spec.items = [by_key[item.item_key] for item in result.ordered]
    spec.diagnostics = result.diagnostics
    return spec


@dataclass
class CompiledCourse:
    meta: CourseMeta
    tl_course: TLCourse
    curriculum: Curriculum
    graph: ConceptGraph
    banks: dict[str, LessonBank]
    problems: list[str]
    skipped_modules: list[str]
    cfg: CompileConfig
    unit_counts: dict[str, int]  # units per kind: "lesson"/"l1".."l3"/"checkpoint"/"final_review"
    notes: list[str]
    sequence_quality: SequenceQualityReport
    relaxations_by_unit: dict[str, tuple[ConstraintRelaxation, ...]]
    # Immutable-at-compile hash bindings.  ``write_bundle`` re-hashes a deep
    # snapshot and rejects any post-compile mutation, including changes which
    # happen to remain schema-valid.
    snapshot_sha256: dict[str, str]


def _relaxations_payload(
    relaxations_by_unit: dict[str, tuple[ConstraintRelaxation, ...]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        unit_key: [asdict(relaxation) for relaxation in relaxations]
        for unit_key, relaxations in sorted(relaxations_by_unit.items())
    }


def compile_workspace(course_dir: str | Path) -> CompiledCourse:
    """Assemble the TL course from curriculum + banks per compile.yaml.
    Modules without any bank content (e.g. authored modules before CMS support
    lands) are skipped and reported, not fatal."""
    ws = Workspace(course_dir).require()
    # Capture one coherent canonical snapshot.  Compilation itself is pure and
    # can release the lock once all typed inputs have been loaded.
    with ws.publication_lock():
        meta = ws.load_meta()
        curriculum = ws.load_curriculum()
        graph = ws.load_graph()
        cfg = ws.load_compile_config()
        bank_entries = list(ws.iter_banks())
        compile_trace = inspect_publication_readiness(ws.root).trace
    concepts_by_id = graph.by_id()

    banks: dict[str, LessonBank] = {}
    duplicate_bank_lessons: set[str] = set()
    for bank in bank_entries:
        if bank.lesson in banks:
            duplicate_bank_lessons.add(bank.lesson)
        banks[bank.lesson] = bank
    owners = {
        item.item_key: (bank.module, bank.lesson)
        for bank in banks.values()
        for item in bank.items
    }

    seen: set[str] = set()  # item_keys already placed in an earlier unit — "unseen" preference keys on this
    notes: list[str] = []
    unit_counts: Counter[str] = Counter()
    modules: list[TLModule] = []
    skipped: list[str] = []
    relaxations_by_unit: dict[str, tuple[ConstraintRelaxation, ...]] = {}
    for module in curriculum.modules:
        units: list[TLUnit] = []
        for lesson in module.lessons:
            bank = banks.get(lesson.key)
            if bank is None:
                continue
            if cfg.levels <= 1:
                # Phase-1 flat shape: one unit per lesson and the full active
                # bank. The shared scheduler below determines presentation order.
                spec = _UnitSpec(
                    import_key=lesson.key,
                    title=lesson.title,
                    slo=lesson.slo,
                    kind="lesson",
                    items=_active_items(bank),
                    flashcards=list(bank.flashcards),
                    item_statuses={item.item_key: "new" for item in _active_items(bank)},
                )
                seen.update(it.item_key for it in spec.items)
                specs = [spec]
            else:
                specs, lesson_notes = _compose_lesson_levels(lesson, bank, concepts_by_id, cfg, seen)
                notes.extend(lesson_notes)
            for spec in specs:
                _schedule_spec(spec, cfg=cfg, owners=owners)
                relaxations_by_unit[spec.import_key] = (
                    spec.diagnostics.relaxations if spec.diagnostics else ()
                )
                units.append(_emit_unit(spec, owners=owners, cfg=cfg))
                unit_counts[spec.kind] += 1
        if units and cfg.checkpoints == "per_module":
            checkpoint = _compose_checkpoint(module, banks, concepts_by_id, cfg, seen)
            if checkpoint is not None:
                _schedule_spec(checkpoint, cfg=cfg, owners=owners)
                relaxations_by_unit[checkpoint.import_key] = (
                    checkpoint.diagnostics.relaxations if checkpoint.diagnostics else ()
                )
                units.append(_emit_unit(checkpoint, owners=owners, cfg=cfg))
                unit_counts[checkpoint.kind] += 1
        if not units:
            skipped.append(module.key)
            continue
        modules.append(TLModule(import_key=module.key, title=module.title, lessons=units))

    if cfg.final_review and modules:
        review = _compose_final_review(meta.id, curriculum, banks, concepts_by_id, cfg, seen)
        if review is not None:
            _schedule_spec(review, cfg=cfg, owners=owners)
            relaxations_by_unit[review.import_key] = (
                review.diagnostics.relaxations if review.diagnostics else ()
            )
            modules.append(
                TLModule(
                    import_key=f"{meta.id}-review",
                    title="Course Review",
                    lessons=[_emit_unit(review, owners=owners, cfg=cfg)],
                )
            )
            unit_counts[review.kind] += 1

    tl_course = TLCourse(
        import_key=meta.id,
        title=meta.title,
        difficulty=meta.difficulty,
        source_summary=None,
        modules=modules,
    )
    problems = validate_techlingo_course(tl_course)
    problems.extend(
        f"duplicate bank lesson identity: {lesson!r}"
        for lesson in sorted(duplicate_bank_lessons)
    )
    sequence_quality = validate_tl_course(
        tl_course,
        policy=_sequence_quality_policy(cfg),
        relaxations_by_unit=relaxations_by_unit,
    )
    if cfg.sequence_quality.block_on_errors:
        problems.extend(
            f"sequence quality [{issue.code}] {issue.unit_path}: {issue.message} "
            f"(observed={issue.observed!r}, configured={issue.configured!r}; "
            f"items={list(issue.item_paths)!r})"
            for issue in sequence_quality.issues
            if issue.severity == "error"
        )
    return CompiledCourse(
        meta=meta,
        tl_course=tl_course,
        curriculum=curriculum,
        graph=graph,
        banks=banks,
        problems=problems,
        skipped_modules=skipped,
        cfg=cfg,
        unit_counts=dict(unit_counts),
        notes=notes,
        sequence_quality=sequence_quality,
        relaxations_by_unit=relaxations_by_unit,
        snapshot_sha256={
            "source_set_sha256": compile_trace.source_set_sha256,
            "validation_set_sha256": compile_trace.validation_set_sha256,
            "workflow_config_sha256": compile_trace.workflow_config_sha256,
            "course_meta_sha256": hash_data(meta),
            "curriculum_sha256": hash_data(curriculum),
            "concept_graph_sha256": hash_data(graph),
            "compile_config_sha256": hash_data(cfg),
            "bank_sha256": banks_sha256(banks),
            "artifact_sha256": hash_data(tl_course),
            "sequence_quality_sha256": hash_data(sequence_quality.to_dict()),
            "relaxations_sha256": hash_data(_relaxations_payload(relaxations_by_unit)),
        },
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


_BUNDLE_COMPONENT_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?$"
)
_WINDOWS_RESERVED_COMPONENTS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _require_safe_bundle_component(value: str, *, label: str) -> str:
    """Reject untrusted identities which could alter a bundle path."""

    if not isinstance(value, str) or not value:
        raise PublicationSafetyError([f"{label}: bundle path component is empty"])
    if len(value.encode("utf-8")) > 128 or _BUNDLE_COMPONENT_RE.fullmatch(value) is None:
        raise PublicationSafetyError(
            [
                f"{label}: unsafe bundle path component {value!r}; use only letters, "
                "digits, '.', '_' and '-' without leading/trailing dots"
            ]
        )
    if value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_COMPONENTS:
        raise PublicationSafetyError(
            [f"{label}: reserved bundle path component {value!r}"]
        )
    return value


def _safe_bundle_relative_path(root: Path, rel_path: str) -> Path:
    """Resolve one relative output path and prove it remains under ``root``."""

    relative = Path(rel_path)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PublicationSafetyError([f"bundle path is not a safe relative path: {rel_path!r}"])
    resolved_root = root.resolve(strict=True)
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise PublicationSafetyError(
            [f"bundle path escapes its staging root: {rel_path!r}"]
        ) from exc
    return candidate


def _require_safe_dist_root(ws: Workspace) -> Path:
    """Create/return ``dist`` only when it is a real in-workspace directory."""

    workspace_root = ws.root.resolve(strict=True)
    dist_dir = ws.dist_dir
    if dist_dir.is_symlink():
        raise PublicationSafetyError(["dist: symbolic-link output roots are not publishable"])
    if dist_dir.exists() and not dist_dir.is_dir():
        raise PublicationSafetyError(["dist: output root is not a directory"])
    try:
        dist_dir.resolve(strict=False).relative_to(workspace_root)
    except ValueError as exc:
        raise PublicationSafetyError(["dist: output root escapes the workspace"]) from exc
    dist_dir.mkdir(parents=True, exist_ok=True)
    if dist_dir.is_symlink():
        raise PublicationSafetyError(["dist: symbolic-link output roots are not publishable"])
    try:
        dist_dir.resolve(strict=True).relative_to(workspace_root)
    except ValueError as exc:
        raise PublicationSafetyError(["dist: output root escapes the workspace"]) from exc
    return dist_dir


def _require_unique_bundle_keys(values: list[str], *, label: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        portable = value.casefold()
        previous = seen.get(portable)
        if previous is not None:
            raise PublicationSafetyError(
                [f"{label}: duplicate/colliding import keys {previous!r} and {value!r}"]
            )
        seen[portable] = value


def _validate_bundle_identities(compiled: CompiledCourse) -> None:
    """Validate every untrusted identity before creating ``dist`` or staging."""

    meta_id = _require_safe_bundle_component(compiled.meta.id, label="course.id")
    course_id = _require_safe_bundle_component(
        compiled.tl_course.import_key, label="compiled course import_key"
    )
    if meta_id != course_id:
        raise PublicationSafetyError(
            ["compiled course import_key does not match the workspace course id"]
        )

    module_keys: list[str] = []
    unit_keys: list[str] = []
    for module in compiled.tl_course.modules:
        module_keys.append(
            _require_safe_bundle_component(module.import_key, label="module import_key")
        )
        for unit in module.lessons:
            unit_keys.append(
                _require_safe_bundle_component(unit.import_key, label="unit import_key")
            )
    _require_unique_bundle_keys(module_keys, label="modules")
    # Unit keys are global filenames, not merely module-local identities.
    _require_unique_bundle_keys(unit_keys, label="units")

    curriculum_module_keys: list[str] = []
    curriculum_lesson_keys: list[str] = []
    for module in compiled.curriculum.modules:
        curriculum_module_keys.append(
            _require_safe_bundle_component(module.key, label="curriculum module key")
        )
        for lesson in module.lessons:
            curriculum_lesson_keys.append(
                _require_safe_bundle_component(lesson.key, label="curriculum lesson key")
            )
    _require_unique_bundle_keys(curriculum_module_keys, label="curriculum modules")
    _require_unique_bundle_keys(curriculum_lesson_keys, label="curriculum lessons")

    bank_keys: list[str] = []
    for lesson_key, bank in compiled.banks.items():
        safe_key = _require_safe_bundle_component(lesson_key, label="bank mapping key")
        safe_lesson = _require_safe_bundle_component(bank.lesson, label="bank lesson key")
        _require_safe_bundle_component(bank.module, label="bank module key")
        if safe_key != safe_lesson:
            raise PublicationSafetyError(
                [
                    f"bank/{lesson_key}: mapping key does not match declared lesson "
                    f"{bank.lesson!r}"
                ]
            )
        bank_keys.append(safe_key)
    _require_unique_bundle_keys(bank_keys, label="banks")


def _validated_compiled_snapshot(compiled: CompiledCourse) -> CompiledCourse:
    """Deep-copy, re-hash, and revalidate the exact object that would ship."""

    snapshot = copy.deepcopy(compiled)
    expected = snapshot.snapshot_sha256
    actual = {
        "course_meta_sha256": hash_data(snapshot.meta),
        "curriculum_sha256": hash_data(snapshot.curriculum),
        "concept_graph_sha256": hash_data(snapshot.graph),
        "compile_config_sha256": hash_data(snapshot.cfg),
        "bank_sha256": banks_sha256(snapshot.banks),
        "artifact_sha256": hash_data(snapshot.tl_course),
        "sequence_quality_sha256": hash_data(snapshot.sequence_quality.to_dict()),
        "relaxations_sha256": hash_data(
            _relaxations_payload(snapshot.relaxations_by_unit)
        ),
    }
    mutation_blockers = [
        f"compiled artifact: {name.removesuffix('_sha256').replace('_', ' ')} changed after compilation"
        for name, value in actual.items()
        if expected.get(name) != value
    ]

    problems = validate_techlingo_course(snapshot.tl_course)
    if not snapshot.tl_course.modules:
        problems.append("course.modules must contain at least one module.")
    for module_index, module in enumerate(snapshot.tl_course.modules):
        if not module.lessons:
            problems.append(
                f"modules[{module_index}].lessons must contain at least one unit."
            )
        for unit_index, unit in enumerate(module.lessons):
            if not unit.exercises:
                problems.append(
                    f"modules[{module_index}].lessons[{unit_index}].exercises "
                    "must contain at least one question."
                )
    sequence_quality = validate_tl_course(
        snapshot.tl_course,
        policy=_sequence_quality_policy(snapshot.cfg),
        relaxations_by_unit=snapshot.relaxations_by_unit,
    )
    # ``block_on_errors`` is a diagnostic compile-time convenience only.  A
    # publication boundary never lets configuration downgrade a hard final-
    # artifact invariant.
    problems.extend(
        f"sequence quality [{issue.code}] {issue.unit_path}: {issue.message} "
        f"(observed={issue.observed!r}, configured={issue.configured!r}; "
        f"items={list(issue.item_paths)!r})"
        for issue in sequence_quality.issues
        if issue.severity == "error"
    )
    if hash_data(sequence_quality.to_dict()) != expected.get("sequence_quality_sha256"):
        mutation_blockers.append(
            "compiled artifact: final sequence validation changed after compilation"
        )
    if problems:
        mutation_blockers.extend(f"compiled artifact: {problem}" for problem in problems)
    if mutation_blockers:
        raise PublicationSafetyError(mutation_blockers)

    # Emit the freshly computed report rather than trusting cached diagnostics.
    snapshot.sequence_quality = sequence_quality
    return snapshot


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
    # One lock covers the canonical snapshot, staging preflight, atomic bundle
    # promotion, and build-state commit.  This closes the former final-check /
    # os.replace TOCTOU window and serializes version allocation.
    with ws.publication_lock():
        snapshot = _validated_compiled_snapshot(compiled)
        _validate_bundle_identities(snapshot)

        # Bind this compiled object to the exact publishable workspace snapshot.
        # A caller may compile for diagnostics at any time, but no dist path is
        # created before this authoritative preflight passes.
        base_trace = require_publishable(course_dir)
        trace_bindings = {
            "source_set_sha256": base_trace.source_set_sha256,
            "validation_set_sha256": base_trace.validation_set_sha256,
            "workflow_config_sha256": base_trace.workflow_config_sha256,
            "compile_config_sha256": base_trace.compile_config_sha256,
            "bank_sha256": base_trace.bank_sha256,
            "course_meta_sha256": base_trace.course_meta_sha256,
            "curriculum_sha256": base_trace.curriculum_sha256,
            "concept_graph_sha256": base_trace.concept_graph_sha256,
        }
        stale = [
            name
            for name, value in trace_bindings.items()
            if snapshot.snapshot_sha256.get(name) != value
        ]
        if stale:
            labels = {
                "compile_config_sha256": "compile.yaml: configuration",
                "course_meta_sha256": "course.yaml metadata",
                "curriculum_sha256": "curriculum",
                "concept_graph_sha256": "concept graph",
                "bank_sha256": "bank",
                "source_set_sha256": "source set",
                "validation_set_sha256": "validation evidence",
                "workflow_config_sha256": "workflow configuration",
            }
            raise PublicationSafetyError(
                [
                    f"{labels[name]} changed after the compiled artifact was assembled"
                    for name in stale
                ]
            )
        trace = base_trace.with_artifact(snapshot.tl_course)
        if trace.artifact_sha256 != snapshot.snapshot_sha256.get("artifact_sha256"):
            raise PublicationSafetyError(
                ["compiled artifact: course content changed after compilation"]
            )

        # Qualitative review is opt-in.  Bind every exact compiled unit and its
        # exact current evaluation context to persisted eligible evidence.
        qualitative_required = snapshot.cfg.gauntlet.qualitative_required_for_publication
        gauntlet_artifacts: dict[str, Any] = {}
        gauntlet_contexts: dict[str, Any] = {}
        gauntlet_record_provenance: list[dict[str, Any]] = []
        if qualitative_required:
            from .gauntlet_io import (
                GauntletIOError,
                compiled_unit_artifacts,
                publication_evaluation_contexts,
                qualitative_publication_coverage,
            )

            try:
                gauntlet_artifacts = compiled_unit_artifacts(snapshot)
                gauntlet_contexts = publication_evaluation_contexts(
                    course_dir,
                    snapshot,
                    required_artifacts=gauntlet_artifacts,
                )
            except (GauntletIOError, ValueError) as exc:
                raise PublicationSafetyError(
                    [f"gauntlet: cannot establish exact compiled-unit context: {exc}"]
                ) from exc
            if not gauntlet_artifacts:
                raise PublicationSafetyError(
                    ["gauntlet: qualitative publication requires at least one compiled unit"]
                )
            coverage = qualitative_publication_coverage(
                course_dir,
                compiled_artifact_sha256=trace.artifact_sha256 or "",
                required_artifacts=gauntlet_artifacts,
                expected_contexts=gauntlet_contexts,
            )
            if not coverage.ok:
                raise PublicationSafetyError(coverage.blockers)
            gauntlet_record_provenance = coverage.provenance()

        meta = snapshot.meta
        dist_dir = _require_safe_dist_root(ws)
        version = next_bundle_version(dist_dir, meta.id)
        bundle_dir = _safe_bundle_relative_path(dist_dir, f"{meta.id}-v{version}")
        if bundle_dir.exists():  # defensive; version allocation is lock-protected
            raise PublicationSafetyError([f"bundle target already exists: {bundle_dir.name}"])
        staging_dir = Path(
            tempfile.mkdtemp(prefix=f".{meta.id}-v{version}.staging-", dir=dist_dir)
        )
        _safe_bundle_relative_path(dist_dir, staging_dir.name)
        promoted = False
        entities: list[dict[str, Any]] = []
        emitted_paths: dict[str, str] = {}

        def write_entity(rel_path: str, kind: str, key: str, data: Any) -> None:
            portable_path = rel_path.casefold()
            previous = emitted_paths.get(portable_path)
            if previous is not None:
                raise PublicationSafetyError(
                    [f"bundle entity path collision: {previous!r} and {rel_path!r}"]
                )
            emitted_paths[portable_path] = rel_path
            text = _dump_json(data)
            path = _safe_bundle_relative_path(staging_dir, rel_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Re-resolve after mkdir so a replaced/symlinked parent cannot turn
            # a checked relative path into an escape.
            path = _safe_bundle_relative_path(staging_dir, rel_path)
            path.write_text(text, encoding="utf-8")
            entities.append(
                {
                    "kind": kind,
                    "key": key,
                    "path": rel_path,
                    "sha256": sha256_text(text),
                }
            )

        try:
            # Per-unit files — the incremental-import granularity.
            for module in snapshot.tl_course.modules:
                for unit in module.lessons:
                    write_entity(
                        f"units/{unit.import_key}.json",
                        "unit",
                        unit.import_key,
                        unit.model_dump(mode="json"),
                    )

            # Course tree (keys/titles/order only; content lives in units/).
            tree = {
                "schema_version": snapshot.tl_course.schema_version,
                "import_key": snapshot.tl_course.import_key,
                "title": snapshot.tl_course.title,
                "difficulty": snapshot.tl_course.difficulty,
                "modules": [
                    {
                        "import_key": module.import_key,
                        "title": module.title,
                        "units": [unit.import_key for unit in module.lessons],
                    }
                    for module in snapshot.tl_course.modules
                ],
            }
            write_entity("course.json", "course", snapshot.tl_course.import_key, tree)

            # Concept registry — runtime mastery keys on these ids.
            registry = build_concept_registry(snapshot.graph, snapshot.banks)
            write_entity(
                "concepts.json", "concepts", snapshot.tl_course.import_key, registry
            )

            # Exact learner-facing sequence metrics and every item-level path.
            write_entity(
                "quality_report.json",
                "sequence-quality",
                snapshot.tl_course.import_key,
                snapshot.sequence_quality.to_dict(),
            )

            # Full exercise bank — Phase 3 runtime session composition reads
            # this; older importers simply ignore it.
            for lesson_key, bank in sorted(snapshot.banks.items()):
                write_entity(
                    f"bank/{lesson_key}.json",
                    "bank",
                    lesson_key,
                    bank.model_dump(mode="json"),
                )

            if flat:
                write_entity(
                    "course.flat.json",
                    "flat-course",
                    snapshot.tl_course.import_key,
                    snapshot.tl_course.model_dump(mode="json"),
                )

            manifest_provenance = trace.as_dict()
            manifest_provenance["qualitative_gauntlet"] = {
                "required": qualitative_required,
                "compiled_artifact_sha256": trace.artifact_sha256,
                "covered_unit_count": len(gauntlet_record_provenance),
                "records": gauntlet_record_provenance,
            }
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
                    "levels": snapshot.cfg.levels,
                    "recycle": snapshot.cfg.recycle,
                    "checkpoints": snapshot.cfg.checkpoints,
                    "final_review": snapshot.cfg.final_review,
                    "session_size_hint": snapshot.cfg.session_size_hint,
                    "seed": snapshot.cfg.seed,
                    "experience": snapshot.cfg.experience.model_dump(mode="json"),
                    "sequence_quality": snapshot.cfg.sequence_quality.model_dump(mode="json"),
                    "gauntlet": snapshot.cfg.gauntlet.model_dump(mode="json"),
                },
                "provenance": manifest_provenance,
                "entities_sha256": hash_data(entities),
                "entities": entities,
            }
            manifest_path = _safe_bundle_relative_path(staging_dir, "manifest.json")
            manifest_path.write_text(_dump_json(manifest), encoding="utf-8")

            # Re-run both gates while still holding the same lock, immediately
            # before the atomic directory promotion and state commit.
            _validated_compiled_snapshot(snapshot)
            latest_trace = require_publishable(course_dir)
            if latest_trace != base_trace:
                raise PublicationSafetyError(
                    ["workspace changed while the bundle was being staged; compile again"]
                )
            if qualitative_required:
                from .gauntlet_io import qualitative_publication_coverage

                latest_coverage = qualitative_publication_coverage(
                    course_dir,
                    compiled_artifact_sha256=trace.artifact_sha256 or "",
                    required_artifacts=gauntlet_artifacts,
                    expected_contexts=gauntlet_contexts,
                )
                if not latest_coverage.ok:
                    raise PublicationSafetyError(latest_coverage.blockers)
                if latest_coverage.provenance() != gauntlet_record_provenance:
                    raise PublicationSafetyError(
                        [
                            "gauntlet: qualitative records changed while the bundle was "
                            "staged; compile again"
                        ]
                    )
            os.replace(staging_dir, bundle_dir)
            promoted = True

            state = ws.load_build_state()
            state.last_compilation = CompilationPublication(
                source_set_sha256=trace.source_set_sha256,
                validation_set_sha256=trace.validation_set_sha256,
                workflow_config_sha256=trace.workflow_config_sha256,
                compile_config_sha256=trace.compile_config_sha256,
                bank_sha256=trace.bank_sha256,
                artifact_sha256=trace.artifact_sha256 or "",
                course_meta_sha256=trace.course_meta_sha256,
                curriculum_sha256=trace.curriculum_sha256,
                concept_graph_sha256=trace.concept_graph_sha256,
                bundle_version=version,
                bundle_path=str(bundle_dir.relative_to(ws.root)),
                published_at=utc_now_iso(),
            )
            state.bank_sha256 = trace.bank_sha256
            ws.save_build_state(state)
        except BaseException as publication_error:
            cleanup_errors: list[str] = []
            if staging_dir.exists():
                try:
                    shutil.rmtree(staging_dir)
                except BaseException as cleanup_error:  # pragma: no cover - filesystem failure
                    cleanup_errors.append(f"staging {staging_dir}: {cleanup_error}")
            if promoted and bundle_dir.exists():
                # State recording is part of publication.  If it fails, remove
                # only this challenger; older versioned bundles remain LKG.
                try:
                    shutil.rmtree(bundle_dir)
                except BaseException as cleanup_error:  # pragma: no cover - filesystem failure
                    cleanup_errors.append(f"bundle {bundle_dir}: {cleanup_error}")
            if cleanup_errors:
                raise RuntimeError(
                    "publication failed and rollback was incomplete: "
                    + "; ".join(cleanup_errors)
                ) from publication_error
            raise

        flat_path = bundle_dir / "course.flat.json" if flat else None
        return BundleOutput(bundle_dir=bundle_dir, flat_path=flat_path, version=version)
