"""Reference implementation of MASTERY_SPEC v1 and SESSION_SPEC v1.

This module is the NORMATIVE reference for the TechLingo app's learning
engine (ARCHITECTURE.md §7). It is pure and dependency-free on purpose: the
app ports it to TypeScript, and both implementations must pass the identical
test vectors (tests/test_learning_specs.py) — the same contract mechanism
GRADING_SPEC v1 shipped with.

Nothing in the generator pipeline imports this at runtime; it lives here so
spec, vectors, and reference evolve in one commit (spec-versioning rule).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .experience import (
    CompositionDiagnostics,
    ExperienceItem,
    ExperiencePolicy,
    compose_experience,
)

MASTERY_SPEC_VERSION = "mastery-v1"
SESSION_SPEC_VERSION = "session-v2"

# ---------------------------------------------------------------------------
# MASTERY_SPEC v1 — per-(user, course, concept) strength with time decay
# ---------------------------------------------------------------------------

# Evidence weight per rung: production/analysis proves more than recognition.
GAIN_BY_RUNG = {1: 0.15, 2: 0.20, 3: 0.30, 4: 0.35, 5: 0.40}

WRONG_STRENGTH_FACTOR = 0.5      # wrong answer halves strength
HALF_LIFE_GROWTH = 1.8           # each correct answer stretches the half-life
HALF_LIFE_SHRINK = 0.5           # each wrong answer shrinks it
INITIAL_HALF_LIFE_DAYS = 1.0     # set on the FIRST correct answer
MIN_HALF_LIFE_DAYS = 0.5
MAX_HALF_LIFE_DAYS = 90.0
DUE_THRESHOLD = 0.55             # effective strength below this ⇒ concept is due
MASTERED_THRESHOLD = 0.80        # informative: crown/legendary gating (post-bridge)


@dataclass
class ConceptMastery:
    """One user_concept_strength row. All numbers rounded to 6 decimals so the
    Python and TypeScript implementations agree bit-for-bit on the vectors."""

    strength: float = 0.0
    half_life_days: float = 0.0   # 0.0 = never answered correctly yet
    last_review_at: Optional[float] = None  # epoch days (client clock)


def _round6(x: float) -> float:
    return round(x, 6)


def effective_strength(m: ConceptMastery, now_days: float) -> float:
    """Strength decayed by time since the last review: s * 2^(-Δt / half_life)."""
    if m.last_review_at is None or m.half_life_days <= 0:
        return 0.0
    elapsed = max(0.0, now_days - m.last_review_at)
    return _round6(m.strength * math.pow(2.0, -elapsed / m.half_life_days))


def apply_answer(m: ConceptMastery, *, rung: int, correct: bool, now_days: float) -> ConceptMastery:
    """Update mastery after one graded answer. `typo` grades count as correct
    (GRADING_SPEC v1). Updates apply to the DECAYED strength, then reset the
    review clock — answering after a long gap starts from what the learner
    actually remembers, not what they once had."""
    gain = GAIN_BY_RUNG.get(rung)
    if gain is None:
        raise ValueError(f"unknown rung {rung!r} (valid: 1-5)")

    current = effective_strength(m, now_days)
    if correct:
        strength = current + (1.0 - current) * gain
        if m.half_life_days <= 0:
            half_life = INITIAL_HALF_LIFE_DAYS
        else:
            half_life = min(m.half_life_days * HALF_LIFE_GROWTH, MAX_HALF_LIFE_DAYS)
    else:
        strength = current * WRONG_STRENGTH_FACTOR
        half_life = max(m.half_life_days * HALF_LIFE_SHRINK, MIN_HALF_LIFE_DAYS)

    return ConceptMastery(
        strength=_round6(strength),
        half_life_days=_round6(half_life),
        last_review_at=now_days,
    )


def is_due(m: ConceptMastery, now_days: float) -> bool:
    """A concept the learner has SEEN is due when it decays below the threshold.
    Never-seen concepts are 'new', not 'due' — the composer treats them separately."""
    if m.last_review_at is None:
        return False
    return effective_strength(m, now_days) < DUE_THRESHOLD


# ---------------------------------------------------------------------------
# SESSION_SPEC v1 — deterministic practice-session composition
# ---------------------------------------------------------------------------

# Bucket shares of the session size (after the warm-up slot).
MISTAKES_SHARE = 0.15
REVIEW_SHARE = 0.25
MAX_ITEMS_PER_CONCEPT = 2
MISTAKE_REDEEMED_AFTER = 2  # correct answers needed to leave the mistake queue


@dataclass
class PoolItem:
    """One candidate exercise from the imported bank (options jsonb carries
    item_key / concept_id / rung; `seen` = the user answered it before)."""

    item_key: str
    concept_id: str
    rung: int
    variant: int = 1
    seen: bool = False
    mechanic: str = "unknown"
    true_false_answer: Optional[bool] = None
    correct_option_indexes: tuple[int, ...] = ()
    blooms_level: Optional[str] = None
    module_key: Optional[str] = None
    lesson_key: Optional[str] = None
    prompt: str = ""
    payload_hash: Optional[str] = None


@dataclass
class UserConceptState:
    concept_id: str
    mastery: ConceptMastery = field(default_factory=ConceptMastery)
    # Highest rung the user has answered correctly at least once (0 = none).
    proven_rung: int = 0


@dataclass
class SessionPlan:
    warmup: list[str] = field(default_factory=list)     # item_keys (0 or 1)
    new: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    mistakes: list[str] = field(default_factory=list)
    final_order: list[str] = field(default_factory=list)  # what the learner actually plays
    composition_diagnostics: Optional[CompositionDiagnostics] = None


def _bucket_counts(session_size: int, *, mistakes_available: int, review_available: int) -> tuple[int, int, int]:
    """(mistakes, review, new) counts after reserving 1 warm-up slot.
    Shares are floors; unused capacity flows to NEW (learning always continues)."""
    remaining = max(0, session_size - 1)
    mistakes = min(mistakes_available, math.floor(remaining * MISTAKES_SHARE))
    review = min(review_available, math.floor(remaining * REVIEW_SHARE))
    new = remaining - mistakes - review
    return mistakes, review, new


def compose_session(
    pool: list[PoolItem],
    user_state: dict[str, UserConceptState],
    mistake_queue: list[str],           # item_keys, oldest first
    *,
    now_days: float,
    session_size: int = 12,
    experience_policy: ExperiencePolicy = ExperiencePolicy(),
    seed: int = 0,
) -> SessionPlan:
    """Deterministic session composition (seeded, with stable SHA-256 tie ranks).

    1. warm-up: the lowest-rung UNSEEN-or-seen item of the user's STRONGEST
       mastered concept (effective >= DUE_THRESHOLD); omitted when none exists.
    2. mistakes: oldest first from the queue (items must still exist in the pool).
    3. review: due concepts, weakest effective strength first (tie: concept_id),
       one item each — the lowest rung already proven (fall back to rung 1),
       preferring UNSEEN variants of that (concept, rung) cell.
    4. new: fill the rest — concepts ordered by curriculum appearance in the
       pool, next unproven rung first (proven_rung + 1, capped at the cell that
       exists), unseen variants first, max MAX_ITEMS_PER_CONCEPT per concept
       across the WHOLE session.
    5. Final order: shared seeded experience scheduler, with warm-up pinned.
       Bucket membership stays intact while concept, authored-mechanic,
       rendered UI-family, local-answer, option-position, rolling-window, and
       difficulty-rhythm policy is applied.
    """
    by_key = {p.item_key: p for p in pool}
    concept_count: dict[str, int] = {}
    chosen: set[str] = set()
    plan = SessionPlan()

    def can_take(item: PoolItem) -> bool:
        return (
            item.item_key not in chosen
            and concept_count.get(item.concept_id, 0) < MAX_ITEMS_PER_CONCEPT
        )

    def take(item: PoolItem, bucket: list[str]) -> None:
        bucket.append(item.item_key)
        chosen.add(item.item_key)
        concept_count[item.concept_id] = concept_count.get(item.concept_id, 0) + 1

    # --- 1. warm-up -----------------------------------------------------------
    strong = [
        (state, effective_strength(state.mastery, now_days))
        for state in user_state.values()
        if state.mastery.last_review_at is not None
        and effective_strength(state.mastery, now_days) >= DUE_THRESHOLD
    ]
    strong.sort(key=lambda t: (-t[1], t[0].concept_id))
    for state, _ in strong:
        candidates = sorted(
            (p for p in pool if p.concept_id == state.concept_id and can_take(p)),
            key=lambda p: (p.rung, p.item_key),
        )
        if candidates:
            take(candidates[0], plan.warmup)
            break

    mistakes_avail = [k for k in mistake_queue if k in by_key and by_key[k].item_key not in chosen]
    due_states = sorted(
        (
            (state, effective_strength(state.mastery, now_days))
            for state in user_state.values()
            if is_due(state.mastery, now_days)
        ),
        key=lambda t: (t[1], t[0].concept_id),
    )
    n_mistakes, n_review, n_new = _bucket_counts(
        session_size, mistakes_available=len(mistakes_avail), review_available=len(due_states)
    )

    # --- 2. mistakes (oldest first) -------------------------------------------
    for key in mistakes_avail:
        if len(plan.mistakes) >= n_mistakes:
            break
        item = by_key[key]
        if can_take(item):
            take(item, plan.mistakes)

    # --- 3. review (weakest due concepts) --------------------------------------
    for state, _eff in due_states:
        if len(plan.review) >= n_review:
            break
        target_rung = max(1, min(state.proven_rung, 5)) if state.proven_rung else 1
        candidates = sorted(
            (p for p in pool if p.concept_id == state.concept_id and can_take(p)),
            key=lambda p: (abs(p.rung - target_rung), p.seen, p.rung, p.item_key),
        )
        if candidates:
            take(candidates[0], plan.review)

    # --- 4. new -----------------------------------------------------------------
    for item in pool:  # pool order = curriculum order
        if len(plan.new) >= n_new:
            break
        if not can_take(item) or item.seen:
            continue
        state = user_state.get(item.concept_id)
        next_rung = (state.proven_rung + 1) if state else 1
        if item.rung > next_rung:
            continue  # too hard for where the learner is on this concept
        take(item, plan.new)
    # Shortfall (pool exhausted at the right rungs): relax the rung gate.
    if len(plan.new) < n_new:
        for item in pool:
            if len(plan.new) >= n_new:
                break
            if can_take(item) and not item.seen:
                take(item, plan.new)

    # --- 5. final learner-facing ordering ---------------------------------------
    roles = {
        **{key: "warmup" for key in plan.warmup},
        **{key: "mistake" for key in plan.mistakes},
        **{key: "review" for key in plan.review},
        **{key: "new" for key in plan.new},
    }
    selected_keys = [*plan.warmup, *plan.mistakes, *plan.review, *plan.new]
    metadata = [
        ExperienceItem(
            item_key=key,
            concept_id=by_key[key].concept_id,
            rung=by_key[key].rung,
            variant=by_key[key].variant,
            mechanic=by_key[key].mechanic,
            true_false_answer=by_key[key].true_false_answer,
            correct_option_indexes=by_key[key].correct_option_indexes,
            blooms_level=by_key[key].blooms_level,
            module_key=by_key[key].module_key,
            lesson_key=by_key[key].lesson_key,
            learning_status=roles[key],
            prompt=by_key[key].prompt,
            payload_hash=by_key[key].payload_hash,
        )
        for key in selected_keys
    ]
    composition = compose_experience(
        metadata,
        policy=experience_policy,
        seed=seed,
        scope="runtime-session",
        pinned_item_keys=plan.warmup,
    )
    ordered = [item.item_key for item in composition.ordered]

    return SessionPlan(
        warmup=plan.warmup,
        new=plan.new,
        review=plan.review,
        mistakes=plan.mistakes,
        final_order=ordered,
        composition_diagnostics=composition.diagnostics,
    )
