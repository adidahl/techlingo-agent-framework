"""Shared deterministic learner-experience selection and sequencing.

The content factory owns question correctness and the compiler/runtime own which
questions are selected.  This module sits at their common, final ordering
boundary.  It never edits question content: it accepts immutable metadata and
returns the same objects in a deterministic order.

The scheduler uses bounded backtracking with deterministic candidate scoring.
It first searches with every experience constraint enabled, then follows the
documented relaxation order when the pool cannot satisfy them.  Identity,
content, answer data, and coverage are invariants and are never relaxed.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Iterable, Optional, Sequence


CONSTRAINT_MECHANICS_WINDOW = "mechanics_window"
CONSTRAINT_TF_ANSWER_STREAK = "true_false_answer_streak"
CONSTRAINT_MECHANIC_STREAK = "mechanic_streak"
CONSTRAINT_UI_FAMILY_STREAK = "ui_family_streak"
CONSTRAINT_CONCEPT_ADJACENCY = "concept_adjacency"

SCHEDULER_ATTESTATION_VERSION = "experience-scheduler-attestation-v1"
PROOF_CATEGORICAL_CAPACITY = "categorical-run-capacity-v1"
PROOF_ROLLING_WINDOW_EXHAUSTIVE = "rolling-window-exhaustive-v1"
PROOF_SEARCH_PROFILE_EXHAUSTIVE = "scheduler-profile-exhaustive-v1"

DEFAULT_RELAXATION_ORDER = (
    CONSTRAINT_MECHANICS_WINDOW,
    CONSTRAINT_TF_ANSWER_STREAK,
    CONSTRAINT_UI_FAMILY_STREAK,
    CONSTRAINT_MECHANIC_STREAK,
    CONSTRAINT_CONCEPT_ADJACENCY,
)

LEGACY_RELAXATION_ORDER = (
    CONSTRAINT_MECHANICS_WINDOW,
    CONSTRAINT_TF_ANSWER_STREAK,
    CONSTRAINT_MECHANIC_STREAK,
    CONSTRAINT_CONCEPT_ADJACENCY,
)


@dataclass(frozen=True)
class ExperienceItem:
    """Learner-facing metadata required by both compiler and runtime.

    ``learning_status`` is contextual.  Compiler levels normally use ``new``
    or ``review``; runtime sessions additionally use ``warmup`` and
    ``mistake``.  It is metadata only and cannot affect answer correctness.
    """

    item_key: str
    concept_id: Optional[str]
    rung: int
    variant: int = 1
    mechanic: str = "unknown"
    true_false_answer: Optional[bool] = None
    correct_option_indexes: tuple[int, ...] = ()
    blooms_level: Optional[str] = None
    module_key: Optional[str] = None
    lesson_key: Optional[str] = None
    learning_status: str = "new"
    prompt: str = ""
    payload_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.item_key:
            raise ValueError("experience item_key must be non-empty")
        if not 1 <= self.rung <= 5:
            raise ValueError(f"invalid rung {self.rung!r} for {self.item_key}")
        if self.variant < 1:
            raise ValueError(f"invalid variant {self.variant!r} for {self.item_key}")


@dataclass(frozen=True)
class ExperiencePolicy:
    """Configurable final-session quality policy.

    The first three local constraints are enforced whenever a valid ordering
    can be found within ``max_search_states``.  Rolling-window diversity is
    attempted only when the pool exposes enough fully-described mechanics.
    """

    max_same_mechanic_streak: int = 2
    max_same_ui_family_streak: int = 2
    max_same_true_false_answer_streak: int = 2
    mechanics_window_size: int = 6
    min_mechanics_per_window: int = 3
    avoid_adjacent_same_concept: bool = True
    max_search_states: int = 100_000
    relaxation_order: tuple[str, ...] = DEFAULT_RELAXATION_ORDER

    def __post_init__(self) -> None:
        if self.max_same_mechanic_streak < 1:
            raise ValueError("max_same_mechanic_streak must be >= 1")
        if self.max_same_ui_family_streak < 1:
            raise ValueError("max_same_ui_family_streak must be >= 1")
        if self.max_same_true_false_answer_streak < 1:
            raise ValueError("max_same_true_false_answer_streak must be >= 1")
        if self.mechanics_window_size < 1:
            raise ValueError("mechanics_window_size must be >= 1")
        if not 1 <= self.min_mechanics_per_window <= self.mechanics_window_size:
            raise ValueError("min_mechanics_per_window must be between 1 and mechanics_window_size")
        if self.max_search_states < 1:
            raise ValueError("max_search_states must be >= 1")
        known = {
            CONSTRAINT_MECHANICS_WINDOW,
            CONSTRAINT_TF_ANSWER_STREAK,
            CONSTRAINT_MECHANIC_STREAK,
            CONSTRAINT_UI_FAMILY_STREAK,
            CONSTRAINT_CONCEPT_ADJACENCY,
        }
        order = tuple(self.relaxation_order)
        if set(order) == set(LEGACY_RELAXATION_ORDER) and len(order) == len(
            LEGACY_RELAXATION_ORDER
        ):
            # compile-v1 configurations written before UI-family enforcement
            # remain loadable.  UI-family is the coarser/stronger run cap, so it
            # is relaxed immediately *before* original mechanic.  Doing the
            # reverse would disable a still-useful fine-grained constraint
            # while the coarser one continues to reject the same candidates.
            insertion = order.index(CONSTRAINT_MECHANIC_STREAK)
            order = (
                *order[:insertion],
                CONSTRAINT_UI_FAMILY_STREAK,
                *order[insertion:],
            )
            object.__setattr__(self, "relaxation_order", order)
        if set(order) != known or len(order) != len(known):
            raise ValueError(f"relaxation_order must contain each known constraint once: {sorted(known)}")


@dataclass(frozen=True)
class ConstraintRelaxation:
    constraint: str
    reason: str
    item_keys: tuple[str, ...] = ()
    observed: Optional[int] = None
    configured: Optional[int] = None
    violation_observed: bool = False
    proof_kind: Optional[str] = None
    search_relaxed_before: tuple[str, ...] = ()
    scheduler_version: Optional[str] = None
    scheduler_seed: Optional[int] = None
    scheduler_scope: Optional[str] = None
    scheduler_pinned_item_keys: tuple[str, ...] = ()
    artifact_sha256: Optional[str] = None
    policy_sha256: Optional[str] = None
    attestation_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        # JSON persistence naturally decodes tuples as lists.  Normalize them
        # here so an otherwise valid scheduler record survives a round trip and
        # evidence comparisons remain exact.
        object.__setattr__(self, "item_keys", tuple(self.item_keys))
        object.__setattr__(
            self, "search_relaxed_before", tuple(self.search_relaxed_before)
        )
        object.__setattr__(
            self,
            "scheduler_pinned_item_keys",
            tuple(self.scheduler_pinned_item_keys),
        )


@dataclass(frozen=True)
class CompositionDiagnostics:
    seed: int
    search_states: int
    attempts: int
    relaxations: tuple[ConstraintRelaxation, ...] = ()
    rolling_window_applicable: bool = False

    @property
    def relaxed_constraints(self) -> tuple[str, ...]:
        return tuple(r.constraint for r in self.relaxations)


@dataclass(frozen=True)
class CompositionResult:
    ordered: tuple[ExperienceItem, ...]
    diagnostics: CompositionDiagnostics


@dataclass(frozen=True)
class VariantSelectionResult:
    selected: tuple[ExperienceItem, ...]
    unused_item_keys: tuple[str, ...]


def _stable_rank(seed: int, scope: str, key: str) -> int:
    material = f"{seed}:{scope}:{key}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _mechanic(item: ExperienceItem) -> str:
    return item.mechanic or "unknown"


def learner_ui_family(mechanic: str) -> str:
    """Normalize source mechanics to the interaction a learner actually sees."""

    return {
        "single_choice": "multiple_choice",
        "multi_choice": "multiple_choice",
        "fill_gaps": "fill_blank",
        "rearrange": "arrange_sentence",
    }.get(mechanic or "unknown", mechanic or "unknown")


def _ui_family(item: ExperienceItem) -> str:
    return learner_ui_family(_mechanic(item))


def _category(item: ExperienceItem) -> str:
    if item.rung >= 4:
        return "scenario"
    if item.rung == 3 or _mechanic(item) in {"fill_gaps", "fill_blank", "rearrange", "arrange_sentence"}:
        return "production"
    return "recognition"


def _tf_suffix(path: Sequence[ExperienceItem]) -> list[bool]:
    suffix: list[bool] = []
    for item in reversed(path):
        if item.true_false_answer is None:
            break
        suffix.append(item.true_false_answer)
    suffix.reverse()
    return suffix


def _choice_suffix(path: Sequence[ExperienceItem]) -> list[tuple[int, ...]]:
    suffix: list[tuple[int, ...]] = []
    for item in reversed(path):
        if not item.correct_option_indexes:
            break
        suffix.append(item.correct_option_indexes)
    suffix.reverse()
    return suffix


def _continues_strict_alternation(values: Sequence[object], candidate: object) -> bool:
    combined = [*values, candidate]
    if len(combined) < 4 or len(set(combined[-4:])) != 2:
        return False
    tail = combined[-4:]
    return tail[0] == tail[2] and tail[1] == tail[3] and tail[0] != tail[1]


def select_variants(
    candidate_groups: Sequence[Sequence[ExperienceItem]],
    *,
    seed: int,
    scope: str,
    seen_item_keys: Iterable[str] = (),
    context: Sequence[ExperienceItem] = (),
) -> VariantSelectionResult:
    """Choose one variant from each requested concept/rung cell.

    Unseen variants are authoritative.  Among equally unseen candidates the
    selector balances mechanics, true/false answers, and correct-option
    positions.  Lowest variant remains the final semantic tie-break, preserving
    backward compatibility when variants do not differ on experience metadata.
    Unselected variants are returned explicitly for recycling/review.
    """

    seen = set(seen_item_keys)
    selected = list(context)
    chosen: list[ExperienceItem] = []
    unused: list[str] = []

    for group_index, raw_group in enumerate(candidate_groups):
        group = list(raw_group)
        if not group:
            raise ValueError(f"variant group {group_index} is empty")
        keys = [item.item_key for item in group]
        if len(keys) != len(set(keys)):
            raise ValueError(f"variant group {group_index} contains duplicate item keys")
        cells = {(item.concept_id, item.rung) for item in group}
        if len(cells) != 1:
            raise ValueError(f"variant group {group_index} spans multiple concept/rung cells: {sorted(cells)!r}")

        mechanic_counts = Counter(_mechanic(item) for item in selected)
        tf_counts = Counter(item.true_false_answer for item in selected if item.true_false_answer is not None)
        position_counts = Counter(item.correct_option_indexes for item in selected if item.correct_option_indexes)
        tf_suffix = _tf_suffix(selected)
        choice_suffix = _choice_suffix(selected)

        def score(item: ExperienceItem) -> tuple:
            projected_tf_imbalance = 0
            local_tf_repeat = 0
            tf_alternation = 0
            if item.true_false_answer is not None:
                yes = tf_counts[True] + (1 if item.true_false_answer else 0)
                no = tf_counts[False] + (0 if item.true_false_answer else 1)
                projected_tf_imbalance = abs(yes - no)
                if tf_suffix and tf_suffix[-1] == item.true_false_answer:
                    local_tf_repeat = 1
                tf_alternation = int(_continues_strict_alternation(tf_suffix, item.true_false_answer))

            position_repeat = 0
            position_alternation = 0
            projected_position_count = 0
            if item.correct_option_indexes:
                position_repeat = int(bool(choice_suffix) and choice_suffix[-1] == item.correct_option_indexes)
                position_alternation = int(
                    _continues_strict_alternation(choice_suffix, item.correct_option_indexes)
                )
                projected_position_count = position_counts[item.correct_option_indexes] + 1

            return (
                item.item_key in seen,
                mechanic_counts[_mechanic(item)],
                local_tf_repeat,
                projected_tf_imbalance,
                tf_alternation,
                position_repeat,
                projected_position_count,
                position_alternation,
                item.variant,
                _stable_rank(seed, f"{scope}:variant:{group_index}", item.item_key),
                item.item_key,
            )

        pick = min(group, key=score)
        chosen.append(pick)
        selected.append(pick)
        unused.extend(item.item_key for item in group if item.item_key != pick.item_key)

    return VariantSelectionResult(selected=tuple(chosen), unused_item_keys=tuple(unused))


def _mechanic_run(path: Sequence[ExperienceItem]) -> int:
    if not path:
        return 0
    value = _mechanic(path[-1])
    run = 0
    for item in reversed(path):
        if _mechanic(item) != value:
            break
        run += 1
    return run


def _ui_family_run(path: Sequence[ExperienceItem]) -> int:
    if not path:
        return 0
    value = _ui_family(path[-1])
    if value == "unknown":
        return 0
    run = 0
    for item in reversed(path):
        if _ui_family(item) != value:
            break
        run += 1
    return run


def _equal_suffix_run(values: Sequence[object]) -> int:
    if not values:
        return 0
    run = 0
    value = values[-1]
    for entry in reversed(values):
        if entry != value:
            break
        run += 1
    return run


def _rolling_applicable(items: Sequence[ExperienceItem], policy: ExperiencePolicy) -> bool:
    mechanics = [_mechanic(item) for item in items]
    return (
        len(items) >= policy.mechanics_window_size
        and all(mechanic != "unknown" for mechanic in mechanics)
        and len(set(mechanics)) >= policy.min_mechanics_per_window
    )


def _initially_relaxed_constraints(
    items: Sequence[ExperienceItem], policy: ExperiencePolicy
) -> set[str]:
    """Constraints disabled by policy/applicability rather than search."""

    relaxed: set[str] = set()
    if not _rolling_applicable(items, policy):
        relaxed.add(CONSTRAINT_MECHANICS_WINDOW)
    if not policy.avoid_adjacent_same_concept:
        relaxed.add(CONSTRAINT_CONCEPT_ADJACENCY)
    return relaxed


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _policy_fingerprint(policy: ExperiencePolicy) -> str:
    return _sha256_json(
        {
            "max_same_mechanic_streak": policy.max_same_mechanic_streak,
            "max_same_ui_family_streak": policy.max_same_ui_family_streak,
            "max_same_true_false_answer_streak": policy.max_same_true_false_answer_streak,
            "mechanics_window_size": policy.mechanics_window_size,
            "min_mechanics_per_window": policy.min_mechanics_per_window,
            "avoid_adjacent_same_concept": policy.avoid_adjacent_same_concept,
            "max_search_states": policy.max_search_states,
            "relaxation_order": list(policy.relaxation_order),
        }
    )


def _artifact_fingerprint(items: Sequence[ExperienceItem]) -> str:
    """Bind proof to stable scheduling metadata in exact learner order.

    Prompt text, payload hashes, and raw correct-option positions are excluded:
    deterministic emission rewrites fill-gap display text and intentionally
    permutes choice positions after scheduling.  Every hard-constraint input is
    included, as are stable identity and ownership fields.
    """

    return _sha256_json(
        [
            {
                "item_key": item.item_key,
                "concept_id": item.concept_id,
                "rung": item.rung,
                "variant": item.variant,
                "mechanic": _mechanic(item),
                "ui_family": _ui_family(item),
                "true_false_answer": item.true_false_answer,
                "blooms_level": item.blooms_level,
                "module_key": item.module_key,
                "lesson_key": item.lesson_key,
                "learning_status": item.learning_status,
            }
            for item in items
        ]
    )


def _proof_kind(constraint: str, *, violation_observed: bool) -> str:
    if not violation_observed:
        return PROOF_SEARCH_PROFILE_EXHAUSTIVE
    return (
        PROOF_ROLLING_WINDOW_EXHAUSTIVE
        if constraint == CONSTRAINT_MECHANICS_WINDOW
        else PROOF_CATEGORICAL_CAPACITY
    )


def _categorical_run_unavoidable(
    items: Sequence[ExperienceItem],
    *,
    extractor,
    maximum: int,
) -> bool:
    """Prove the configured run cap impossible from categorical capacity.

    For a value occurring ``count`` times, the other items create
    ``other + 1`` gaps of ``maximum`` slots.  Exceeding that capacity is both a
    necessary and sufficient proof that some run of the value must be longer.
    Unknown/None values are not constrained and remain valid separators.
    """

    values = [extractor(item) for item in items]
    constrained = [value for value in values if value is not None and value != "unknown"]
    counts = Counter(constrained)
    total = len(values)
    return any(count > maximum * (total - count + 1) for count in counts.values())


class _ProofSearchLimit(RuntimeError):
    pass


def _rolling_window_has_solution(
    items: Sequence[ExperienceItem], policy: ExperiencePolicy
) -> Optional[bool]:
    """Exactly decide rolling-window feasibility over the mechanic multiset.

    ``None`` means the configured proof-state bound was reached.  Callers must
    fail closed in that case; an incomplete proof can never authorize a waiver.
    """

    if not _rolling_applicable(items, policy):
        return True
    mechanics = sorted({_mechanic(item) for item in items})
    index = {mechanic: position for position, mechanic in enumerate(mechanics)}
    initial = tuple(Counter(_mechanic(item) for item in items)[value] for value in mechanics)
    size = policy.mechanics_window_size
    minimum = policy.min_mechanics_per_window
    states = 0

    @lru_cache(maxsize=None)
    def visit(counts: tuple[int, ...], suffix: tuple[int, ...]) -> bool:
        nonlocal states
        states += 1
        if states > policy.max_search_states:
            raise _ProofSearchLimit
        if not any(counts):
            return True
        for candidate, count in enumerate(counts):
            if count == 0:
                continue
            window = (*suffix, candidate)
            if len(window) >= size and len(set(window[-size:])) < minimum:
                continue
            remaining = list(counts)
            remaining[candidate] -= 1
            next_suffix = window[-(size - 1) :] if size > 1 else ()
            if visit(tuple(remaining), tuple(next_suffix)):
                return True
        return False

    try:
        # ``index`` documents and fixes the deterministic mechanic ordering;
        # the initial tuple above uses that same ordering.
        assert len(index) == len(initial)
        return visit(initial, ())
    except _ProofSearchLimit:
        return None


def relaxation_violation_proven_unavoidable(
    relaxation: ConstraintRelaxation,
    items: Sequence[ExperienceItem],
    policy: ExperiencePolicy,
) -> bool:
    """Independently prove that an observed constraint violation is unavoidable."""

    if not relaxation.violation_observed:
        return False
    if relaxation.constraint == CONSTRAINT_MECHANIC_STREAK:
        return _categorical_run_unavoidable(
            items, extractor=_mechanic, maximum=policy.max_same_mechanic_streak
        )
    if relaxation.constraint == CONSTRAINT_UI_FAMILY_STREAK:
        return _categorical_run_unavoidable(
            items,
            extractor=_ui_family,
            maximum=policy.max_same_ui_family_streak,
        )
    if relaxation.constraint == CONSTRAINT_TF_ANSWER_STREAK:
        return _categorical_run_unavoidable(
            items,
            extractor=lambda item: item.true_false_answer,
            maximum=policy.max_same_true_false_answer_streak,
        )
    if relaxation.constraint == CONSTRAINT_CONCEPT_ADJACENCY:
        return _categorical_run_unavoidable(
            items, extractor=lambda item: item.concept_id, maximum=1
        )
    if relaxation.constraint == CONSTRAINT_MECHANICS_WINDOW:
        feasible = _rolling_window_has_solution(items, policy)
        return feasible is False
    return False


def _unsigned_attestation_payload(relaxation: ConstraintRelaxation) -> dict[str, object]:
    return {
        "constraint": relaxation.constraint,
        "reason": relaxation.reason,
        "item_keys": list(relaxation.item_keys),
        "observed": relaxation.observed,
        "configured": relaxation.configured,
        "violation_observed": relaxation.violation_observed,
        "proof_kind": relaxation.proof_kind,
        "search_relaxed_before": list(relaxation.search_relaxed_before),
        "scheduler_version": relaxation.scheduler_version,
        "scheduler_seed": relaxation.scheduler_seed,
        "scheduler_scope": relaxation.scheduler_scope,
        "scheduler_pinned_item_keys": list(
            relaxation.scheduler_pinned_item_keys
        ),
        "artifact_sha256": relaxation.artifact_sha256,
        "policy_sha256": relaxation.policy_sha256,
    }


def relaxation_attestation_errors(
    relaxation: ConstraintRelaxation,
    items: Sequence[ExperienceItem],
    policy: ExperiencePolicy,
) -> tuple[str, ...]:
    """Return why a purported scheduler-issued relaxation is not authentic."""

    errors: list[str] = []
    expected_artifact = _artifact_fingerprint(items)
    expected_policy = _policy_fingerprint(policy)
    expected_proof = _proof_kind(
        relaxation.constraint,
        violation_observed=relaxation.violation_observed,
    )
    if relaxation.scheduler_version != SCHEDULER_ATTESTATION_VERSION:
        errors.append("scheduler attestation version is missing or unsupported")
    if type(relaxation.scheduler_seed) is not int:
        errors.append("scheduler seed is missing or invalid")
    if not relaxation.scheduler_scope:
        errors.append("scheduler scope is missing")
    ordered_keys = tuple(item.item_key for item in items)
    pinned_keys = relaxation.scheduler_pinned_item_keys
    if len(pinned_keys) != len(set(pinned_keys)):
        errors.append("scheduler_pinned_item_keys contains duplicates")
    if ordered_keys[: len(pinned_keys)] != pinned_keys:
        errors.append(
            "scheduler_pinned_item_keys is not the exact prefix of the final order"
        )
    if relaxation.artifact_sha256 != expected_artifact:
        errors.append("artifact fingerprint does not match the final ordered items")
    if relaxation.policy_sha256 != expected_policy:
        errors.append("policy fingerprint does not match the active policy")
    if relaxation.proof_kind != expected_proof:
        errors.append(f"proof_kind must be {expected_proof!r}")

    try:
        constraint_index = policy.relaxation_order.index(relaxation.constraint)
    except ValueError:
        errors.append("constraint is absent from the active relaxation order")
    else:
        initially_relaxed = _initially_relaxed_constraints(items, policy)
        expected_before = tuple(
            constraint
            for constraint in policy.relaxation_order[:constraint_index]
            if constraint not in initially_relaxed
        )
        if relaxation.search_relaxed_before != expected_before:
            errors.append(
                "search_relaxed_before is not the cumulative stricter profile"
            )

    expected_attestation = _sha256_json(_unsigned_attestation_payload(relaxation))
    if relaxation.attestation_sha256 != expected_attestation:
        errors.append("attestation hash is missing or does not match the record")
    if not errors:
        decision_error = _search_decision_proof_error(relaxation, items, policy)
        if decision_error is not None:
            errors.append(decision_error)
    if (
        relaxation.violation_observed
        and not relaxation_violation_proven_unavoidable(relaxation, items, policy)
    ):
        errors.append(
            "the observed violation is not independently proven unavoidable"
        )
    return tuple(errors)


def _attest_relaxation(
    relaxation: ConstraintRelaxation,
    *,
    ordered: Sequence[ExperienceItem],
    policy: ExperiencePolicy,
    seed: int,
    scope: str,
    relaxed_by_search: set[str],
    pinned_item_keys: Sequence[str],
) -> ConstraintRelaxation:
    if (
        relaxation.violation_observed
        and not relaxation_violation_proven_unavoidable(relaxation, ordered, policy)
    ):
        raise RuntimeError(
            f"cannot safely relax {relaxation.constraint!r} for {scope!r}: "
            "the final violation is not independently proven unavoidable"
        )
    constraint_index = policy.relaxation_order.index(relaxation.constraint)
    before = tuple(
        constraint
        for constraint in policy.relaxation_order[:constraint_index]
        if constraint in relaxed_by_search
    )
    issued = replace(
        relaxation,
        proof_kind=_proof_kind(
            relaxation.constraint,
            violation_observed=relaxation.violation_observed,
        ),
        search_relaxed_before=before,
        scheduler_version=SCHEDULER_ATTESTATION_VERSION,
        scheduler_seed=seed,
        scheduler_scope=scope,
        scheduler_pinned_item_keys=tuple(pinned_item_keys),
        artifact_sha256=_artifact_fingerprint(ordered),
        policy_sha256=_policy_fingerprint(policy),
    )
    decision_error = _search_decision_proof_error(issued, ordered, policy)
    if decision_error is not None:
        raise RuntimeError(
            f"cannot attest relaxation of {relaxation.constraint!r} for "
            f"{scope!r}: {decision_error}"
        )
    return replace(
        issued,
        attestation_sha256=_sha256_json(_unsigned_attestation_payload(issued)),
    )


def _allowed(
    path: Sequence[ExperienceItem],
    item: ExperienceItem,
    *,
    policy: ExperiencePolicy,
    relaxed: set[str],
    rolling_applicable: bool,
) -> bool:
    if (
        CONSTRAINT_CONCEPT_ADJACENCY not in relaxed
        and policy.avoid_adjacent_same_concept
        and path
        and item.concept_id is not None
        and path[-1].concept_id == item.concept_id
    ):
        return False

    if (
        CONSTRAINT_MECHANIC_STREAK not in relaxed
        and path
        and _mechanic(item) != "unknown"
        and _mechanic(path[-1]) == _mechanic(item)
        and _mechanic_run(path) >= policy.max_same_mechanic_streak
    ):
        return False

    if (
        CONSTRAINT_UI_FAMILY_STREAK not in relaxed
        and path
        and _ui_family(item) != "unknown"
        and _ui_family(path[-1]) == _ui_family(item)
        and _ui_family_run(path) >= policy.max_same_ui_family_streak
    ):
        return False

    if CONSTRAINT_TF_ANSWER_STREAK not in relaxed and item.true_false_answer is not None:
        suffix = _tf_suffix(path)
        if (
            suffix
            and suffix[-1] == item.true_false_answer
            and _equal_suffix_run(suffix) >= policy.max_same_true_false_answer_streak
        ):
            return False

    if CONSTRAINT_MECHANICS_WINDOW not in relaxed and rolling_applicable:
        size = policy.mechanics_window_size
        candidate_window = [*path[-(size - 1) :], item] if size > 1 else [item]
        if len(candidate_window) == size:
            mechanics = {_mechanic(entry) for entry in candidate_window}
            if len(mechanics) < policy.min_mechanics_per_window:
                return False
    return True


def _candidate_score(
    path: Sequence[ExperienceItem],
    item: ExperienceItem,
    *,
    position: int,
    total: int,
    min_rung: int,
    max_rung: int,
    seed: int,
    scope: str,
    policy: ExperiencePolicy,
    future_slack: int,
    relaxed_best_effort: tuple[int, int, int, int],
    lower_same_concept_waiting: bool,
) -> tuple:
    recent = path[-max(1, policy.mechanics_window_size - 1) :]
    recent_mechanic = sum(_mechanic(entry) == _mechanic(item) for entry in recent)
    recent_ui_family = sum(_ui_family(entry) == _ui_family(item) for entry in recent)
    recent_category = sum(_category(entry) == _category(item) for entry in recent[-3:])

    # Integer arithmetic keeps ordering identical across Python versions.
    denominator = max(1, total - 1)
    target_scaled = min_rung * denominator + (max_rung - min_rung) * position
    distance_scaled = abs(item.rung * denominator - target_scaled)
    downward_jump = max(0, (path[-1].rung - item.rung) - 1) if path else 0

    tf_suffix = _tf_suffix(path)
    tf_history = [
        entry.true_false_answer
        for entry in path
        if entry.true_false_answer is not None
    ]
    tf_alternation = int(
        item.true_false_answer is not None
        and _continues_strict_alternation(tf_history, item.true_false_answer)
    )
    tf_repeat = int(
        item.true_false_answer is not None
        and bool(tf_suffix)
        and tf_suffix[-1] == item.true_false_answer
    )

    choice_suffix = _choice_suffix(path)
    choice_history = [
        entry.correct_option_indexes
        for entry in path
        if entry.correct_option_indexes
    ]
    choice_repeat = int(
        bool(item.correct_option_indexes)
        and bool(choice_suffix)
        and choice_suffix[-1] == item.correct_option_indexes
    )
    choice_alternation = int(
        bool(item.correct_option_indexes)
        and _continues_strict_alternation(choice_history, item.correct_option_indexes)
    )

    return (
        *relaxed_best_effort,
        int(lower_same_concept_waiting),
        -future_slack,
        downward_jump,
        distance_scaled,
        recent_ui_family,
        recent_mechanic,
        {"mistake": 0, "review": 1, "new": 2, "warmup": -1}.get(item.learning_status, 3),
        tf_alternation,
        choice_alternation,
        choice_repeat,
        tf_repeat,
        recent_category,
        _stable_rank(seed, f"{scope}:order:{position}", item.item_key),
        item.item_key,
    )


def _maximum_value_run(items: Sequence[ExperienceItem], extractor) -> int:
    maximum = 0
    previous: object = object()
    current = 0
    for item in items:
        value = extractor(item)
        if value is None or value == "unknown":
            previous = object()
            current = 0
        elif value == previous:
            current += 1
        else:
            previous = value
            current = 1
        maximum = max(maximum, current)
    return maximum


def _minimum_achievable_max_run(
    path: Sequence[ExperienceItem],
    remaining: Sequence[ExperienceItem],
    *,
    extractor,
) -> int:
    """Lower bound the best final streak after a proposed placement.

    This is used only after a streak constraint has been explicitly relaxed.
    It keeps the fallback a best-effort schedule: a scarce separator is held
    until it minimizes the unavoidable maximum run instead of being consumed
    as soon as the configured (now impossible) limit is approached.
    """

    known_remaining = [
        item
        for item in remaining
        if (value := extractor(item)) is not None and value != "unknown"
    ]
    values = {extractor(item) for item in known_remaining}
    if path:
        trailing = extractor(path[-1])
        if trailing is not None and trailing != "unknown":
            values.add(trailing)
    existing = _maximum_value_run(path, extractor)
    if not values:
        return existing

    upper = max(existing, len(path) + len(known_remaining), 1)
    for limit in range(max(existing, 1), upper + 1):
        feasible = True
        for value in values:
            count = sum(extractor(item) == value for item in known_remaining)
            if count == 0:
                continue
            separators = len(known_remaining) - count
            suffix = _trailing_value_run(path, value, extractor)
            first_gap = limit - suffix if suffix else limit
            if first_gap + limit * separators < count:
                feasible = False
                break
        if feasible:
            return limit
    return upper


def _trailing_value_run(
    path: Sequence[ExperienceItem], value: object, extractor
) -> int:
    run = 0
    for entry in reversed(path):
        if extractor(entry) != value:
            break
        run += 1
    return run


def _dimension_slack(
    path: Sequence[ExperienceItem],
    remaining: Sequence[ExperienceItem],
    *,
    values: Iterable[object],
    extractor,
    max_run: int,
) -> int:
    """Smallest remaining placement capacity for one streak dimension.

    For a value with ``count`` remaining, every differently-valued item creates
    another gap of ``max_run`` slots.  When the current suffix already has this
    value, the first gap has only ``max_run - suffix_run`` slots.  A negative
    result proves that no continuation can satisfy the constraint.
    """

    worst: Optional[int] = None
    for value in values:
        count = sum(extractor(item) == value for item in remaining)
        if count == 0:
            continue
        separators = len(remaining) - count
        suffix = _trailing_value_run(path, value, extractor)
        first_gap = max_run - suffix if suffix else max_run
        capacity = first_gap + max_run * separators
        slack = capacity - count
        worst = slack if worst is None else min(worst, slack)
    return worst if worst is not None else len(remaining)


def _future_slack(
    path: Sequence[ExperienceItem],
    remaining: Sequence[ExperienceItem],
    *,
    policy: ExperiencePolicy,
    relaxed: set[str],
) -> int:
    slacks: list[int] = []
    if CONSTRAINT_UI_FAMILY_STREAK not in relaxed:
        known_ui_families = {
            _ui_family(item) for item in remaining if _ui_family(item) != "unknown"
        }
        slacks.append(
            _dimension_slack(
                path,
                remaining,
                values=known_ui_families,
                extractor=_ui_family,
                max_run=policy.max_same_ui_family_streak,
            )
        )
    if CONSTRAINT_MECHANIC_STREAK not in relaxed:
        known_mechanics = {
            _mechanic(item) for item in remaining if _mechanic(item) != "unknown"
        }
        slacks.append(
            _dimension_slack(
                path,
                remaining,
                values=known_mechanics,
                extractor=_mechanic,
                max_run=policy.max_same_mechanic_streak,
            )
        )
    if CONSTRAINT_CONCEPT_ADJACENCY not in relaxed and policy.avoid_adjacent_same_concept:
        slacks.append(
            _dimension_slack(
                path,
                remaining,
                values={item.concept_id for item in remaining if item.concept_id is not None},
                extractor=lambda item: item.concept_id,
                max_run=1,
            )
        )
    if CONSTRAINT_TF_ANSWER_STREAK not in relaxed:
        slacks.append(
            _dimension_slack(
                path,
                remaining,
                values={True, False},
                extractor=lambda item: item.true_false_answer,
                max_run=policy.max_same_true_false_answer_streak,
            )
        )
    return min(slacks, default=len(remaining))


def _state_key(path: Sequence[ExperienceItem], remaining_mask: int, policy: ExperiencePolicy) -> tuple:
    if not path:
        return (remaining_mask, None, None, 0, None, 0, None, 0, ())
    mechanic = _mechanic(path[-1])
    mechanic_run = 0
    for entry in reversed(path):
        if _mechanic(entry) != mechanic:
            break
        mechanic_run += 1
    ui_family = _ui_family(path[-1])
    ui_family_run = _ui_family_run(path)
    tf_suffix = _tf_suffix(path)
    tf_answer = tf_suffix[-1] if tf_suffix else None
    tf_run = 0
    if tf_suffix:
        for answer in reversed(tf_suffix):
            if answer != tf_answer:
                break
            tf_run += 1
    window = tuple(_mechanic(entry) for entry in path[-max(0, policy.mechanics_window_size - 1) :])
    return (
        remaining_mask,
        path[-1].concept_id,
        mechanic,
        min(mechanic_run, policy.max_same_mechanic_streak),
        ui_family,
        min(ui_family_run, policy.max_same_ui_family_streak),
        tf_answer,
        min(tf_run, policy.max_same_true_false_answer_streak),
        window,
    )


@dataclass
class _Search:
    items: Sequence[ExperienceItem]
    prefix: list[ExperienceItem]
    policy: ExperiencePolicy
    seed: int
    scope: str
    relaxed: set[str]
    rolling_applicable: bool
    states: int = 0
    exhausted: bool = False
    dead: set[tuple] = field(default_factory=set)
    mechanics_dead: set[tuple] = field(default_factory=set)

    def _mechanics_continuation_feasible(
        self,
        path: Sequence[ExperienceItem],
        remaining: Sequence[ExperienceItem],
    ) -> bool:
        """Prove that the remaining mechanic multiset can complete ``path``.

        The main search operates on item identities because concept adjacency
        and deterministic scoring distinguish otherwise similar questions.
        Rolling-window feasibility does not: it depends only on the recent
        mechanic suffix and the counts still available.  Solving that smaller
        exact problem here prevents the item-level search from enumerating
        permutations which all have the same impossible mechanic future.

        Returning ``False`` is therefore a sound prune.  Returning ``True`` is
        only a necessary-condition result; the main search still enforces all
        other constraints and preserves every item identity and payload.
        """

        window_active = (
            self.rolling_applicable
            and CONSTRAINT_MECHANICS_WINDOW not in self.relaxed
        )
        streak_active = CONSTRAINT_MECHANIC_STREAK not in self.relaxed
        if not window_active and not streak_active:
            return True

        window_size = self.policy.mechanics_window_size
        suffix_size = max(
            max(0, window_size - 1) if window_active else 0,
            self.policy.max_same_mechanic_streak if streak_active else 0,
        )
        history = tuple(_mechanic(item) for item in path[-suffix_size:])
        counts = Counter(_mechanic(item) for item in remaining)
        mechanics = tuple(sorted(counts))

        def visit(
            recent: tuple[str, ...],
            remaining_counts: tuple[int, ...],
        ) -> bool:
            state = (
                window_active,
                streak_active,
                recent,
                mechanics,
                remaining_counts,
            )
            if state in self.mechanics_dead:
                return False
            if not any(remaining_counts):
                return True

            for index, mechanic in enumerate(mechanics):
                if remaining_counts[index] == 0:
                    continue
                if streak_active and mechanic != "unknown":
                    run = 0
                    for previous in reversed(recent):
                        if previous != mechanic:
                            break
                        run += 1
                    if run >= self.policy.max_same_mechanic_streak:
                        continue

                candidate_window = (*recent, mechanic)
                if (
                    window_active
                    and len(candidate_window) >= window_size
                    and len(set(candidate_window[-window_size:]))
                    < self.policy.min_mechanics_per_window
                ):
                    continue

                next_counts = list(remaining_counts)
                next_counts[index] -= 1
                next_recent = candidate_window[-suffix_size:]
                if visit(next_recent, tuple(next_counts)):
                    return True

            self.mechanics_dead.add(state)
            return False

        return visit(history, tuple(counts[mechanic] for mechanic in mechanics))

    def run(self) -> Optional[list[ExperienceItem]]:
        remaining_mask = (1 << len(self.items)) - 1
        return self._visit(list(self.prefix), remaining_mask)

    def _visit(self, path: list[ExperienceItem], remaining_mask: int) -> Optional[list[ExperienceItem]]:
        if remaining_mask == 0:
            return list(path)
        self.states += 1
        if self.states > self.policy.max_search_states:
            self.exhausted = True
            return None
        state = _state_key(path, remaining_mask, self.policy)
        if state in self.dead:
            return None

        indexes = [i for i in range(len(self.items)) if remaining_mask & (1 << i)]
        allowed_with_slack: list[tuple[int, int, tuple[int, int, int, int]]] = []
        for i in indexes:
            if not _allowed(
                path,
                self.items[i],
                policy=self.policy,
                relaxed=self.relaxed,
                rolling_applicable=self.rolling_applicable,
            ):
                continue
            future = [self.items[j] for j in indexes if j != i]
            proposed = [*path, self.items[i]]
            if not self._mechanics_continuation_feasible(proposed, future):
                continue
            slack = _future_slack(
                proposed, future, policy=self.policy, relaxed=self.relaxed
            )
            if slack >= 0:
                # A relaxed constraint remains an optimization target.  These
                # exact lower bounds are needed only in fallback profiles, so
                # the common strict search stays inexpensive.
                ui_family_best = (
                    max(
                        0,
                        _minimum_achievable_max_run(
                            proposed, future, extractor=_ui_family
                        )
                        - self.policy.max_same_ui_family_streak,
                    )
                    if CONSTRAINT_UI_FAMILY_STREAK in self.relaxed
                    else 0
                )
                mechanic_best = (
                    max(
                        0,
                        _minimum_achievable_max_run(
                            proposed, future, extractor=_mechanic
                        )
                        - self.policy.max_same_mechanic_streak,
                    )
                    if CONSTRAINT_MECHANIC_STREAK in self.relaxed
                    else 0
                )
                tf_best = (
                    max(
                        0,
                        _minimum_achievable_max_run(
                            proposed,
                            future,
                            extractor=lambda entry: entry.true_false_answer,
                        )
                        - self.policy.max_same_true_false_answer_streak,
                    )
                    if CONSTRAINT_TF_ANSWER_STREAK in self.relaxed
                    else 0
                )
                concept_best = (
                    max(
                        0,
                        _minimum_achievable_max_run(
                            proposed,
                            future,
                            extractor=lambda entry: entry.concept_id,
                        )
                        - 1,
                    )
                    if CONSTRAINT_CONCEPT_ADJACENCY in self.relaxed
                    and self.policy.avoid_adjacent_same_concept
                    else 0
                )
                allowed_with_slack.append(
                    (
                        i,
                        slack,
                        (ui_family_best, mechanic_best, tf_best, concept_best),
                    )
                )
        min_rung = min((entry.rung for entry in [*path, *(self.items[i] for i in indexes)]), default=1)
        max_rung = max((entry.rung for entry in [*path, *(self.items[i] for i in indexes)]), default=5)
        allowed_with_slack.sort(
            key=lambda candidate: _candidate_score(
                path,
                self.items[candidate[0]],
                position=len(path),
                total=len(self.prefix) + len(self.items),
                min_rung=min_rung,
                max_rung=max_rung,
                seed=self.seed,
                scope=self.scope,
                policy=self.policy,
                future_slack=candidate[1],
                relaxed_best_effort=candidate[2],
                lower_same_concept_waiting=any(
                    j != candidate[0]
                    and self.items[j].concept_id is not None
                    and self.items[j].concept_id == self.items[candidate[0]].concept_id
                    and self.items[j].rung < self.items[candidate[0]].rung
                    for j in indexes
                ),
            )
        )
        for index, _slack, _best_effort in allowed_with_slack:
            path.append(self.items[index])
            result = self._visit(path, remaining_mask & ~(1 << index))
            if result is not None:
                return result
            path.pop()
            if self.exhausted:
                break
        self.dead.add(state)
        return None


def _search_decision_proof_error(
    relaxation: ConstraintRelaxation,
    items: Sequence[ExperienceItem],
    policy: ExperiencePolicy,
) -> Optional[str]:
    """Recompute the exhaustive no-solution decision before one relaxation.

    Pinned prefixes are deliberately *not* applied to this proof.  A pin may
    narrow the scheduler's choices, but it cannot manufacture authority to
    waive a hard learner-experience invariant when the complete item pool has
    a compliant order.  This also makes a persisted record independently
    verifiable without trusting caller-supplied pin metadata.
    """

    try:
        constraint_index = policy.relaxation_order.index(relaxation.constraint)
    except ValueError:
        return "constraint is absent from the active relaxation order"
    expected_before = tuple(
        constraint
        for constraint in policy.relaxation_order[:constraint_index]
        if constraint not in _initially_relaxed_constraints(items, policy)
    )
    if relaxation.search_relaxed_before != expected_before:
        return "search profile is not the deterministic cumulative predecessor"

    search = _Search(
        items=list(items),
        prefix=[],
        policy=policy,
        seed=relaxation.scheduler_seed if type(relaxation.scheduler_seed) is int else 0,
        scope=relaxation.scheduler_scope or "attestation-verification",
        relaxed={
            *_initially_relaxed_constraints(items, policy),
            *relaxation.search_relaxed_before,
        },
        rolling_applicable=_rolling_applicable(items, policy),
    )
    solution = search.run()
    if search.exhausted:
        return (
            "verification hit max_search_states before proving the stricter "
            "profile infeasible; increase the bound"
        )
    if solution is not None:
        return "the stricter pre-relaxation profile has a valid ordering"
    return None


def _max_run(
    items: Sequence[ExperienceItem],
    *,
    mechanic: bool = False,
    ui_family: bool = False,
    tf: bool = False,
) -> tuple[int, tuple[str, ...]]:
    best = 0
    best_keys: tuple[str, ...] = ()
    run: list[ExperienceItem] = []
    sentinel = object()
    previous: object = sentinel
    for item in items:
        if mechanic:
            value: object = _mechanic(item)
        elif ui_family:
            value = _ui_family(item)
        elif tf:
            value = item.true_false_answer if item.true_false_answer is not None else sentinel
        else:  # pragma: no cover - private misuse guard
            raise AssertionError("a run dimension is required")
        if value is sentinel or ((mechanic or ui_family) and value == "unknown"):
            run = []
            previous = sentinel
            continue
        if value == previous:
            run.append(item)
        else:
            run = [item]
            previous = value
        if len(run) > best:
            best = len(run)
            best_keys = tuple(entry.item_key for entry in run)
    return best, best_keys


def _adjacent_collisions(items: Sequence[ExperienceItem]) -> list[tuple[str, str]]:
    return [
        (left.item_key, right.item_key)
        for left, right in zip(items, items[1:])
        if left.concept_id is not None and left.concept_id == right.concept_id
    ]


def _window_violations(
    items: Sequence[ExperienceItem], policy: ExperiencePolicy
) -> list[tuple[str, ...]]:
    size = policy.mechanics_window_size
    return [
        tuple(entry.item_key for entry in items[start : start + size])
        for start in range(0, max(0, len(items) - size + 1))
        if len({_mechanic(entry) for entry in items[start : start + size]})
        < policy.min_mechanics_per_window
    ]


def _actual_relaxations(
    ordered: Sequence[ExperienceItem],
    *,
    policy: ExperiencePolicy,
    relaxed_by_search: set[str],
    rolling_applicable: bool,
    reason: str,
    seed: int,
    scope: str,
    pinned_item_keys: Sequence[str],
) -> tuple[ConstraintRelaxation, ...]:
    diagnostics: list[ConstraintRelaxation] = []
    mechanic_run, mechanic_keys = _max_run(ordered, mechanic=True)
    ui_family_run, ui_family_keys = _max_run(ordered, ui_family=True)
    tf_run, tf_keys = _max_run(ordered, tf=True)
    collisions = _adjacent_collisions(ordered)
    windows = _window_violations(ordered, policy) if rolling_applicable else []
    for constraint in policy.relaxation_order:
        if constraint not in relaxed_by_search:
            continue
        if constraint == CONSTRAINT_MECHANICS_WINDOW:
            keys = windows[0] if windows else ()
            by_key = {item.item_key: item for item in ordered}
            observed = len({_mechanic(by_key[key]) for key in keys}) if keys else None
            diagnostics.append(
                ConstraintRelaxation(
                    constraint=constraint,
                    reason=reason,
                    item_keys=keys,
                    observed=observed,
                    configured=policy.min_mechanics_per_window,
                    violation_observed=bool(keys),
                )
            )
        elif constraint == CONSTRAINT_TF_ANSWER_STREAK:
            violated = tf_run > policy.max_same_true_false_answer_streak
            diagnostics.append(
                ConstraintRelaxation(
                    constraint=constraint,
                    reason=reason,
                    item_keys=tf_keys if violated else (),
                    observed=tf_run,
                    configured=policy.max_same_true_false_answer_streak,
                    violation_observed=violated,
                )
            )
        elif constraint == CONSTRAINT_UI_FAMILY_STREAK:
            violated = ui_family_run > policy.max_same_ui_family_streak
            diagnostics.append(
                ConstraintRelaxation(
                    constraint=constraint,
                    reason=reason,
                    item_keys=ui_family_keys if violated else (),
                    observed=ui_family_run,
                    configured=policy.max_same_ui_family_streak,
                    violation_observed=violated,
                )
            )
        elif constraint == CONSTRAINT_MECHANIC_STREAK:
            violated = mechanic_run > policy.max_same_mechanic_streak
            diagnostics.append(
                ConstraintRelaxation(
                    constraint=constraint,
                    reason=reason,
                    item_keys=mechanic_keys if violated else (),
                    observed=mechanic_run,
                    configured=policy.max_same_mechanic_streak,
                    violation_observed=violated,
                )
            )
        elif constraint == CONSTRAINT_CONCEPT_ADJACENCY:
            diagnostics.append(
                ConstraintRelaxation(
                    constraint=constraint,
                    reason=reason,
                    item_keys=collisions[0] if collisions else (),
                    observed=1 if collisions else 0,
                    configured=0,
                    violation_observed=bool(collisions),
                )
            )
    return tuple(
        _attest_relaxation(
            relaxation,
            ordered=ordered,
            policy=policy,
            seed=seed,
            scope=scope,
            relaxed_by_search=relaxed_by_search,
            pinned_item_keys=pinned_item_keys,
        )
        for relaxation in diagnostics
    )


def compose_experience(
    items: Sequence[ExperienceItem],
    *,
    policy: ExperiencePolicy = ExperiencePolicy(),
    seed: int = 0,
    scope: str = "session",
    pinned_item_keys: Sequence[str] = (),
) -> CompositionResult:
    """Return a reproducible learner-facing order of exactly ``items``.

    ``pinned_item_keys`` are kept first in the provided order (used by the
    runtime confidence opener).  All other items remain freely schedulable.
    Duplicate or unknown keys fail closed rather than silently losing content.
    """

    original = list(items)
    keys = [item.item_key for item in original]
    if len(keys) != len(set(keys)):
        duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
        raise ValueError(f"session contains duplicate item keys: {duplicates}")
    if len(pinned_item_keys) != len(set(pinned_item_keys)):
        raise ValueError("pinned_item_keys contains duplicates")
    by_key = {item.item_key: item for item in original}
    unknown = [key for key in pinned_item_keys if key not in by_key]
    if unknown:
        raise ValueError(f"pinned item keys are not present in the session: {unknown}")
    prefix = [by_key[key] for key in pinned_item_keys]
    pinned = set(pinned_item_keys)
    remainder = [item for item in original if item.item_key not in pinned]
    rolling_applicable = _rolling_applicable(original, policy)
    initially_relaxed = _initially_relaxed_constraints(original, policy)

    checked_prefix: list[ExperienceItem] = []
    for item in prefix:
        if not _allowed(
            checked_prefix,
            item,
            policy=policy,
            relaxed=initially_relaxed,
            rolling_applicable=rolling_applicable,
        ):
            raise ValueError(
                f"pinned prefix violates a hard experience constraint at "
                f"{item.item_key!r}; pin fewer items or change the policy"
            )
        checked_prefix.append(item)
    if not remainder:
        return CompositionResult(
            ordered=tuple(prefix),
            diagnostics=CompositionDiagnostics(
                seed=seed,
                search_states=0,
                attempts=1,
                rolling_window_applicable=rolling_applicable,
            ),
        )

    profiles: list[set[str]] = [set(initially_relaxed)]
    for constraint in policy.relaxation_order:
        if constraint not in profiles[-1]:
            profiles.append({*profiles[-1], constraint})

    total_states = 0
    for attempt, relaxed in enumerate(profiles, start=1):
        search = _Search(
            items=remainder,
            prefix=prefix,
            policy=policy,
            seed=seed,
            scope=scope,
            relaxed=relaxed,
            rolling_applicable=rolling_applicable,
        )
        ordered = search.run()
        total_states += search.states
        if search.exhausted:
            active = [
                constraint
                for constraint in policy.relaxation_order
                if constraint not in relaxed
            ]
            raise RuntimeError(
                f"experience scheduling for {scope!r} hit "
                f"max_search_states={policy.max_search_states} before proving "
                f"the current profile infeasible (active={active}); increase "
                "max_search_states. Search exhaustion never authorizes a relaxation"
            )
        if ordered is None:
            continue

        # Identity/content preservation is checked independently of search.
        if Counter(entry.item_key for entry in ordered) != Counter(keys):
            raise AssertionError("experience composer lost or duplicated an item")
        original_hashes = {entry.item_key: entry.payload_hash for entry in original}
        if any(original_hashes[entry.item_key] != entry.payload_hash for entry in ordered):
            raise AssertionError("experience composer changed item content")

        reason = (
            "enabled by the configured cumulative relaxation order after "
            "exhaustive deterministic search proved the stricter profile had "
            "no solution; violation_observed states whether the final order "
            "exceeds this limit"
        )
        actual = _actual_relaxations(
            ordered,
            policy=policy,
            relaxed_by_search=relaxed - initially_relaxed,
            rolling_applicable=rolling_applicable,
            reason=reason,
            seed=seed,
            scope=scope,
            pinned_item_keys=pinned_item_keys,
        )
        return CompositionResult(
            ordered=tuple(ordered),
            diagnostics=CompositionDiagnostics(
                seed=seed,
                search_states=total_states,
                attempts=attempt,
                relaxations=actual,
                rolling_window_applicable=rolling_applicable,
            ),
        )

    # The fully-relaxed search always has a solution.  Reaching here indicates
    # an internal scheduler invariant failure (bound exhaustion returned above).
    raise RuntimeError(
        f"experience scheduling found no ordering for fully-relaxed scope {scope!r}"
    )


__all__ = [
    "CONSTRAINT_CONCEPT_ADJACENCY",
    "CONSTRAINT_MECHANICS_WINDOW",
    "CONSTRAINT_MECHANIC_STREAK",
    "CONSTRAINT_TF_ANSWER_STREAK",
    "CONSTRAINT_UI_FAMILY_STREAK",
    "CompositionDiagnostics",
    "CompositionResult",
    "ConstraintRelaxation",
    "ExperienceItem",
    "ExperiencePolicy",
    "VariantSelectionResult",
    "compose_experience",
    "learner_ui_family",
    "relaxation_attestation_errors",
    "relaxation_violation_proven_unavoidable",
    "select_variants",
]
