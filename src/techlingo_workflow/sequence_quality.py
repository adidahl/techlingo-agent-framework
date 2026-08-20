"""Deterministic validation of the exact ordered artifact a learner plays."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .experience import (
    CONSTRAINT_CONCEPT_ADJACENCY,
    CONSTRAINT_MECHANICS_WINDOW,
    CONSTRAINT_MECHANIC_STREAK,
    CONSTRAINT_TF_ANSWER_STREAK,
    CONSTRAINT_UI_FAMILY_STREAK,
    ConstraintRelaxation,
    ExperienceItem,
    ExperiencePolicy,
    learner_ui_family,
    relaxation_attestation_errors,
)

QUALITY_REPORT_SCHEMA = "sequence-quality-v1"

_KNOWN_EXPERIENCE_MECHANICS = {
    "single_choice",
    "multi_choice",
    "true_false",
    "fill_gaps",
    "fill_blank",
    "rearrange",
    "arrange_sentence",
}
_KNOWN_LEARNING_STATUSES = {"new", "review", "warmup", "mistake"}


@dataclass(frozen=True)
class SequenceQualityPolicy:
    experience: ExperiencePolicy = field(default_factory=ExperiencePolicy)
    max_same_correct_position_streak: int = 2
    prompt_stem_words: int = 5
    max_repeated_prompt_stem: int = 2
    max_downward_rung_jump: int = 1
    max_same_rung_streak: int = 6

    def __post_init__(self) -> None:
        if self.max_same_correct_position_streak < 1:
            raise ValueError("max_same_correct_position_streak must be >= 1")
        if self.prompt_stem_words < 1:
            raise ValueError("prompt_stem_words must be >= 1")
        if self.max_repeated_prompt_stem < 1:
            raise ValueError("max_repeated_prompt_stem must be >= 1")
        if self.max_downward_rung_jump < 0:
            raise ValueError("max_downward_rung_jump must be >= 0")
        if self.max_same_rung_streak < 1:
            raise ValueError("max_same_rung_streak must be >= 1")


@dataclass(frozen=True)
class SequenceIssue:
    code: str
    severity: str
    message: str
    unit_path: str
    item_paths: tuple[str, ...]
    observed: Any = None
    configured: Any = None
    relaxed: bool = False


@dataclass(frozen=True)
class RollingWindowMetric:
    start: int
    end: int
    mechanics: tuple[str, ...]
    distinct_mechanics: int
    item_paths: tuple[str, ...]


@dataclass(frozen=True)
class UnitSequenceMetrics:
    unit_path: str
    question_count: int
    mechanic_distribution: dict[str, int]
    ui_family_distribution: dict[str, int]
    maximum_mechanic_streak: int
    maximum_ui_family_streak: int
    maximum_same_answer_true_false_streak: int
    rolling_windows: tuple[RollingWindowMetric, ...]
    adjacent_concept_collisions: int
    correct_option_position_distribution: dict[str, int]
    maximum_same_correct_position_streak: int
    rung_sequence: tuple[int, ...]
    maximum_same_rung_streak: int
    largest_downward_rung_jump: int
    nondecreasing_transition_ratio: float
    repeated_prompt_stems: dict[str, int]
    constraint_relaxations: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class UnitSequenceReport:
    metrics: UnitSequenceMetrics
    issues: tuple[SequenceIssue, ...]


@dataclass(frozen=True)
class SequenceQualityReport:
    schema_version: str
    units: tuple[UnitSequenceReport, ...]
    issues: tuple[SequenceIssue, ...]
    summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrderedUnit:
    unit_path: str
    items: tuple[ExperienceItem, ...]
    item_paths: tuple[str, ...] = ()
    relaxations: tuple[ConstraintRelaxation, ...] = ()

    def resolved_paths(self) -> tuple[str, ...]:
        if self.item_paths:
            if len(self.item_paths) != len(self.items):
                raise ValueError(f"item_paths length does not match items for {self.unit_path}")
            return self.item_paths
        return tuple(
            f"{self.unit_path}/questions/{index}:{item.item_key}"
            for index, item in enumerate(self.items)
        )


def _max_run(
    values: Sequence[Any],
    paths: Sequence[str],
    *,
    break_on_none: bool = False,
    break_on_unknown: bool = False,
) -> tuple[int, tuple[str, ...]]:
    maximum = 0
    maximum_paths: tuple[str, ...] = ()
    run_value: Any = object()
    run_paths: list[str] = []
    for value, path in zip(values, paths):
        if (break_on_none and value is None) or (
            break_on_unknown and value == "unknown"
        ):
            run_value = object()
            run_paths = []
            continue
        if value == run_value:
            run_paths.append(path)
        else:
            run_value = value
            run_paths = [path]
        if len(run_paths) > maximum:
            maximum = len(run_paths)
            maximum_paths = tuple(run_paths)
    return maximum, maximum_paths

def _stem(prompt: str, words: int) -> str:
    tokens = re.findall(r"[a-z0-9]+", prompt.lower())
    return " ".join(tokens[:words])


def _position_key(indexes: tuple[int, ...]) -> str:
    return ",".join(str(index) for index in indexes)


def _relaxation_map(relaxations: Sequence[ConstraintRelaxation]) -> dict[str, ConstraintRelaxation]:
    return {relaxation.constraint: relaxation for relaxation in relaxations}


def _issue(
    issues: list[SequenceIssue],
    *,
    code: str,
    message: str,
    unit_path: str,
    item_paths: Sequence[str],
    observed: Any,
    configured: Any,
    relaxation: Optional[ConstraintRelaxation] = None,
    soft: bool = False,
) -> None:
    issues.append(
        SequenceIssue(
            code=code,
            severity="warning" if soft or relaxation is not None else "error",
            message=message,
            unit_path=unit_path,
            item_paths=tuple(item_paths),
            observed=observed,
            configured=configured,
            relaxed=relaxation is not None,
        )
    )


def validate_ordered_unit(
    unit: OrderedUnit,
    *,
    policy: SequenceQualityPolicy = SequenceQualityPolicy(),
) -> UnitSequenceReport:
    items = list(unit.items)
    paths = list(unit.resolved_paths())
    experience = policy.experience
    # Claims do not become waivers until their evidence, deterministic
    # scheduler attestation, and independent feasibility proof are verified
    # below.  Hard violations are initially recorded as errors.
    relaxed: dict[str, ConstraintRelaxation] = {}
    issues: list[SequenceIssue] = []

    mechanics = [item.mechanic or "unknown" for item in items]
    ui_families = [learner_ui_family(mechanic) for mechanic in mechanics]
    mechanic_run, mechanic_run_paths = _max_run(
        mechanics, paths, break_on_unknown=True
    )
    ui_run, ui_run_paths = _max_run(
        ui_families, paths, break_on_unknown=True
    )
    tf_answers = [item.true_false_answer for item in items]
    tf_run, tf_run_paths = _max_run(tf_answers, paths, break_on_none=True)

    if mechanic_run > experience.max_same_mechanic_streak:
        _issue(
            issues,
            code=CONSTRAINT_MECHANIC_STREAK,
            message="same learner mechanic exceeds the configured local streak",
            unit_path=unit.unit_path,
            item_paths=mechanic_run_paths,
            observed=mechanic_run,
            configured=experience.max_same_mechanic_streak,
            relaxation=relaxed.get(CONSTRAINT_MECHANIC_STREAK),
        )
    if ui_run > experience.max_same_ui_family_streak:
        _issue(
            issues,
            code=CONSTRAINT_UI_FAMILY_STREAK,
            message="same learner interaction family exceeds the configured local streak",
            unit_path=unit.unit_path,
            item_paths=ui_run_paths,
            observed=ui_run,
            configured=experience.max_same_ui_family_streak,
            relaxation=relaxed.get(CONSTRAINT_UI_FAMILY_STREAK),
        )
    if tf_run > experience.max_same_true_false_answer_streak:
        _issue(
            issues,
            code=CONSTRAINT_TF_ANSWER_STREAK,
            message="identical true/false answers exceed the configured local streak",
            unit_path=unit.unit_path,
            item_paths=tf_run_paths,
            observed=tf_run,
            configured=experience.max_same_true_false_answer_streak,
            relaxation=relaxed.get(CONSTRAINT_TF_ANSWER_STREAK),
        )

    rolling: list[RollingWindowMetric] = []
    mechanic_pool = set(mechanics)
    rolling_applicable = (
        len(items) >= experience.mechanics_window_size
        and "unknown" not in mechanic_pool
        and len(mechanic_pool) >= experience.min_mechanics_per_window
    )
    if rolling_applicable:
        size = experience.mechanics_window_size
        for start in range(len(items) - size + 1):
            values = tuple(mechanics[start : start + size])
            window_paths = tuple(paths[start : start + size])
            metric = RollingWindowMetric(
                start=start,
                end=start + size - 1,
                mechanics=values,
                distinct_mechanics=len(set(values)),
                item_paths=window_paths,
            )
            rolling.append(metric)
            if metric.distinct_mechanics < experience.min_mechanics_per_window:
                _issue(
                    issues,
                    code=CONSTRAINT_MECHANICS_WINDOW,
                    message="rolling window contains too few learner mechanics",
                    unit_path=unit.unit_path,
                    item_paths=window_paths,
                    observed=metric.distinct_mechanics,
                    configured=experience.min_mechanics_per_window,
                    relaxation=relaxed.get(CONSTRAINT_MECHANICS_WINDOW),
                )

    collisions: list[tuple[str, str]] = []
    for index in range(1, len(items)):
        if items[index].concept_id is not None and items[index].concept_id == items[index - 1].concept_id:
            collision = (paths[index - 1], paths[index])
            collisions.append(collision)
            if experience.avoid_adjacent_same_concept:
                _issue(
                    issues,
                    code=CONSTRAINT_CONCEPT_ADJACENCY,
                    message="adjacent questions test the same concept",
                    unit_path=unit.unit_path,
                    item_paths=collision,
                    observed=items[index].concept_id,
                    configured="non-adjacent",
                    relaxation=relaxed.get(CONSTRAINT_CONCEPT_ADJACENCY),
                )

    positions = [item.correct_option_indexes or None for item in items]
    position_run, position_run_paths = _max_run(positions, paths, break_on_none=True)
    position_distribution = Counter(_position_key(value) for value in positions if value)
    if position_run > policy.max_same_correct_position_streak:
        _issue(
            issues,
            code="correct_option_position_streak",
            message="choice questions repeat the same correct-option position",
            unit_path=unit.unit_path,
            item_paths=position_run_paths,
            observed=position_run,
            configured=policy.max_same_correct_position_streak,
            soft=True,
        )

    rungs = [item.rung for item in items]
    same_rung_run, same_rung_paths = _max_run(rungs, paths)
    transitions = list(zip(rungs, rungs[1:]))
    largest_drop = max((left - right for left, right in transitions), default=0)
    nondecreasing = (
        sum(right >= left for left, right in transitions) / len(transitions)
        if transitions
        else 1.0
    )
    if largest_drop > policy.max_downward_rung_jump:
        drop_index = next(
            index
            for index, (left, right) in enumerate(transitions)
            if left - right == largest_drop
        )
        _issue(
            issues,
            code="difficulty_downward_jump",
            message="difficulty drops abruptly inside the learner-facing sequence",
            unit_path=unit.unit_path,
            item_paths=paths[drop_index : drop_index + 2],
            observed=largest_drop,
            configured=policy.max_downward_rung_jump,
            soft=True,
        )
    if same_rung_run > policy.max_same_rung_streak:
        _issue(
            issues,
            code="difficulty_monotony",
            message="too many consecutive questions remain on the same difficulty rung",
            unit_path=unit.unit_path,
            item_paths=same_rung_paths,
            observed=same_rung_run,
            configured=policy.max_same_rung_streak,
            soft=True,
        )

    stems: dict[str, list[str]] = {}
    for item, path in zip(items, paths):
        stem = _stem(item.prompt, policy.prompt_stem_words)
        if stem:
            stems.setdefault(stem, []).append(path)
    repeated_stems = {
        stem: len(stem_paths)
        for stem, stem_paths in stems.items()
        if len(stem_paths) > policy.max_repeated_prompt_stem
    }
    for stem, count in repeated_stems.items():
        _issue(
            issues,
            code="repeated_prompt_stem",
            message=f"prompt stem repeats across the unit: {stem!r}",
            unit_path=unit.unit_path,
            item_paths=stems[stem],
            observed=count,
            configured=policy.max_repeated_prompt_stem,
            soft=True,
        )

    # A relaxation is an auditable proof record, not a free-form waiver.  The
    # final-artifact validator independently recomputes its configured value,
    # observed value, violation flag, and exact evidence keys.  Stale, forged,
    # duplicate, or unknown records therefore fail closed.
    path_to_key = {path: item.item_key for path, item in zip(paths, items)}
    violating_windows = [
        window
        for window in rolling
        if window.distinct_mechanics < experience.min_mechanics_per_window
    ]
    expected_relaxations: dict[
        str, tuple[bool, Any, Any, tuple[str, ...]]
    ] = {
        CONSTRAINT_MECHANIC_STREAK: (
            mechanic_run > experience.max_same_mechanic_streak,
            mechanic_run,
            experience.max_same_mechanic_streak,
            tuple(path_to_key[path] for path in mechanic_run_paths)
            if mechanic_run > experience.max_same_mechanic_streak
            else (),
        ),
        CONSTRAINT_UI_FAMILY_STREAK: (
            ui_run > experience.max_same_ui_family_streak,
            ui_run,
            experience.max_same_ui_family_streak,
            tuple(path_to_key[path] for path in ui_run_paths)
            if ui_run > experience.max_same_ui_family_streak
            else (),
        ),
        CONSTRAINT_TF_ANSWER_STREAK: (
            tf_run > experience.max_same_true_false_answer_streak,
            tf_run,
            experience.max_same_true_false_answer_streak,
            tuple(path_to_key[path] for path in tf_run_paths)
            if tf_run > experience.max_same_true_false_answer_streak
            else (),
        ),
        CONSTRAINT_CONCEPT_ADJACENCY: (
            bool(collisions),
            1 if collisions else 0,
            0,
            tuple(path_to_key[path] for path in collisions[0]) if collisions else (),
        ),
        CONSTRAINT_MECHANICS_WINDOW: (
            bool(violating_windows),
            violating_windows[0].distinct_mechanics if violating_windows else None,
            experience.min_mechanics_per_window,
            tuple(path_to_key[path] for path in violating_windows[0].item_paths)
            if violating_windows
            else (),
        ),
    }
    relaxation_counts = Counter(
        relaxation.constraint for relaxation in unit.relaxations
    )
    valid_relaxations: list[ConstraintRelaxation] = []
    valid_waivers: dict[str, ConstraintRelaxation] = {}
    for constraint, count in sorted(relaxation_counts.items()):
        if count > 1:
            issues.append(
                SequenceIssue(
                    code="constraint_relaxation_invalid",
                    severity="error",
                    message=f"constraint relaxation {constraint!r} is duplicated",
                    unit_path=unit.unit_path,
                    item_paths=(),
                    observed=count,
                    configured=1,
                )
            )
    for relaxation in unit.relaxations:
        expected_record = expected_relaxations.get(relaxation.constraint)
        errors: list[str] = []
        if expected_record is None:
            errors.append("constraint is unknown")
        else:
            expected_violation, expected_observed, expected_configured, expected_keys = (
                expected_record
            )
            if relaxation.configured != expected_configured:
                errors.append(
                    f"configured={relaxation.configured!r}, expected {expected_configured!r}"
                )
            if relaxation.observed != expected_observed:
                errors.append(
                    f"observed={relaxation.observed!r}, expected {expected_observed!r}"
                )
            if relaxation.violation_observed is not expected_violation:
                errors.append(
                    "violation_observed does not match the final artifact"
                )
            if relaxation.item_keys != expected_keys:
                errors.append(
                    f"item_keys={relaxation.item_keys!r}, expected {expected_keys!r}"
                )
        if not relaxation.reason.strip():
            errors.append("reason is empty")
        errors.extend(
            relaxation_attestation_errors(relaxation, items, experience)
        )
        if errors:
            issues.append(
                SequenceIssue(
                    code="constraint_relaxation_invalid",
                    severity="error",
                    message=(
                        f"invalid {relaxation.constraint!r} relaxation: "
                        + "; ".join(errors)
                    ),
                    unit_path=unit.unit_path,
                    item_paths=tuple(
                        paths[[item.item_key for item in items].index(key)]
                        for key in relaxation.item_keys
                        if key in {item.item_key for item in items}
                    ),
                    observed=relaxation.observed,
                    configured=relaxation.configured,
                    relaxed=False,
                )
            )
        else:
            valid_relaxations.append(relaxation)
            if relaxation.violation_observed:
                valid_waivers[relaxation.constraint] = relaxation

    # Only a verified record that describes an actual, independently
    # unavoidable final violation can downgrade the corresponding hard error.
    # Valid no-violation records remain auditable search decisions, not waivers.
    issues = [
        replace(issue, severity="warning", relaxed=True)
        if issue.severity == "error" and issue.code in valid_waivers
        else issue
        for issue in issues
    ]

    for relaxation in valid_relaxations:
        issues.append(
            SequenceIssue(
                code="constraint_relaxation",
                severity="warning",
                message=f"{relaxation.constraint}: {relaxation.reason}",
                unit_path=unit.unit_path,
                item_paths=tuple(
                    paths[[item.item_key for item in items].index(key)]
                    for key in relaxation.item_keys
                    if key in {item.item_key for item in items}
                ),
                observed=relaxation.observed,
                configured=relaxation.configured,
                relaxed=True,
            )
        )

    metrics = UnitSequenceMetrics(
        unit_path=unit.unit_path,
        question_count=len(items),
        mechanic_distribution=dict(sorted(Counter(mechanics).items())),
        ui_family_distribution=dict(sorted(Counter(ui_families).items())),
        maximum_mechanic_streak=mechanic_run,
        maximum_ui_family_streak=ui_run,
        maximum_same_answer_true_false_streak=tf_run,
        rolling_windows=tuple(rolling),
        adjacent_concept_collisions=len(collisions),
        correct_option_position_distribution=dict(sorted(position_distribution.items())),
        maximum_same_correct_position_streak=position_run,
        rung_sequence=tuple(rungs),
        maximum_same_rung_streak=same_rung_run,
        largest_downward_rung_jump=largest_drop,
        nondecreasing_transition_ratio=round(nondecreasing, 6),
        repeated_prompt_stems=repeated_stems,
        constraint_relaxations=tuple(asdict(relaxation) for relaxation in unit.relaxations),
    )
    return UnitSequenceReport(metrics=metrics, issues=tuple(issues))


def validate_ordered_units(
    units: Iterable[OrderedUnit],
    *,
    policy: SequenceQualityPolicy = SequenceQualityPolicy(),
    additional_issues: Iterable[SequenceIssue] = (),
) -> SequenceQualityReport:
    raw_reports = tuple(validate_ordered_unit(unit, policy=policy) for unit in units)
    extra = tuple(additional_issues)
    known_paths = {report.metrics.unit_path for report in raw_reports}
    unit_reports = tuple(
        replace(
            report,
            issues=(
                *(issue for issue in extra if issue.unit_path == report.metrics.unit_path),
                *report.issues,
            ),
        )
        for report in raw_reports
    )
    issues = (
        *(issue for report in unit_reports for issue in report.issues),
        *(issue for issue in extra if issue.unit_path not in known_paths),
    )
    maximum_mechanic = max(
        (report.metrics.maximum_mechanic_streak for report in unit_reports), default=0
    )
    maximum_ui_family = max(
        (report.metrics.maximum_ui_family_streak for report in unit_reports), default=0
    )
    summary = {
        "ok": not any(issue.severity == "error" for issue in issues),
        "units": len(unit_reports),
        "questions": sum(report.metrics.question_count for report in unit_reports),
        "errors": sum(issue.severity == "error" for issue in issues),
        "warnings": sum(issue.severity == "warning" for issue in issues),
        "units_with_relaxations": sum(bool(report.metrics.constraint_relaxations) for report in unit_reports),
        "maximum_mechanic_streak": maximum_mechanic,
        "maximum_ui_family_streak": maximum_ui_family,
        "maximum_same_answer_true_false_streak": max(
            (report.metrics.maximum_same_answer_true_false_streak for report in unit_reports),
            default=0,
        ),
        "adjacent_concept_collisions": sum(
            report.metrics.adjacent_concept_collisions for report in unit_reports
        ),
        "rolling_windows_below_minimum": sum(
            window.distinct_mechanics < policy.experience.min_mechanics_per_window
            for report in unit_reports
            for window in report.metrics.rolling_windows
        ),
    }
    return SequenceQualityReport(
        schema_version=QUALITY_REPORT_SCHEMA,
        units=unit_reports,
        issues=issues,
        summary=summary,
    )


def experience_item_from_tl_question(
    question: Any,
    *,
    module_key: str,
    lesson_key: str,
    learning_status: str = "new",
) -> ExperienceItem:
    """Derive metadata from an emitted TLQuestion without duplicating schema."""

    options: Mapping[str, Any] = question.options
    if not isinstance(options, Mapping):
        raise ValueError("options must be an object")

    def integer_metadata(name: str, default: int) -> int:
        raw_value = options.get(name, default)
        if isinstance(raw_value, bool):
            raise ValueError(f"{name} must be an integer, not a boolean")
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, str) and re.fullmatch(r"[+-]?\d+", raw_value.strip()):
            return int(raw_value)
        raise ValueError(f"{name} must be an integer, got {raw_value!r}")

    raw_item_key = options.get("item_key", question.import_key)
    if not isinstance(raw_item_key, str) or not raw_item_key.strip():
        raise ValueError("item_key must be a non-empty string")
    raw_mechanic = options.get("original_question_type", question.question_type)
    if not isinstance(raw_mechanic, str) or raw_mechanic not in _KNOWN_EXPERIENCE_MECHANICS:
        raise ValueError(f"original_question_type is invalid: {raw_mechanic!r}")
    mechanic = raw_mechanic

    concept_id = options.get("concept_id")
    if concept_id is not None and (
        not isinstance(concept_id, str) or not concept_id.strip()
    ):
        raise ValueError("concept_id must be null or a non-empty string")
    blooms_level = options.get("blooms_level")
    if blooms_level is not None and not isinstance(blooms_level, str):
        raise ValueError("blooms_level must be null or a string")

    def text_metadata(name: str, fallback: str) -> str:
        raw_value = options.get(name, fallback)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return raw_value

    resolved_module_key = text_metadata("module_key", module_key)
    resolved_lesson_key = text_metadata("lesson_key", lesson_key)
    resolved_status = text_metadata("learning_status", learning_status)
    if resolved_status not in _KNOWN_LEARNING_STATUSES:
        raise ValueError(f"learning_status is invalid: {resolved_status!r}")

    tf_answer: Optional[bool] = None
    correct_indexes: tuple[int, ...] = ()
    if mechanic == "true_false" or question.question_type == "true_false":
        normalized = str(question.correct_answer).strip().lower()
        if normalized not in {"true", "false"}:
            raise ValueError(
                "true_false correct_answer must be exactly 'true' or 'false'"
            )
        tf_answer = normalized == "true"
    elif question.question_type == "multiple_choice":
        raw = str(question.correct_answer).strip()
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "multiple_choice correct_answer must be an integer index or integer array"
            ) from exc
        if isinstance(decoded, list):
            if not decoded or any(type(index) is not int or index < 0 for index in decoded):
                raise ValueError(
                    "multiple_choice correct_answer array must contain non-negative integers"
                )
            correct_indexes = tuple(decoded)
        elif type(decoded) is int and decoded >= 0:
            correct_indexes = (decoded,)
        else:
            raise ValueError(
                "multiple_choice correct_answer must be a non-negative integer or integer array"
            )
    return ExperienceItem(
        item_key=raw_item_key,
        concept_id=concept_id,
        rung=integer_metadata("rung", 1),
        variant=integer_metadata("variant", 1),
        mechanic=mechanic,
        true_false_answer=tf_answer,
        correct_option_indexes=correct_indexes,
        blooms_level=blooms_level,
        module_key=resolved_module_key,
        lesson_key=resolved_lesson_key,
        learning_status=resolved_status,
        prompt=str(question.question_text),
    )


def _ordered_units_from_tl_course(
    course: Any,
    *,
    relaxations_by_unit: Optional[Mapping[str, Sequence[ConstraintRelaxation]]] = None,
    capture_metadata_errors: bool,
) -> tuple[tuple[OrderedUnit, ...], tuple[SequenceIssue, ...]]:
    relaxations_by_unit = relaxations_by_unit or {}
    units: list[OrderedUnit] = []
    conversion_issues: list[SequenceIssue] = []
    for module_index, module in enumerate(course.modules):
        for unit_index, unit in enumerate(module.lessons):
            unit_path = (
                f"modules/{module_index}:{module.import_key}/units/{unit_index}:{unit.import_key}"
            )
            items: list[ExperienceItem] = []
            paths: list[str] = []
            for question_index, question in enumerate(unit.exercises):
                try:
                    item = experience_item_from_tl_question(
                        question,
                        module_key=module.import_key,
                        lesson_key=unit.import_key,
                    )
                except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                    if not capture_metadata_errors:
                        raise
                    raw_question_key = getattr(
                        question, "import_key", f"question-{question_index}"
                    )
                    question_path = (
                        f"{unit_path}/questions/{question_index}:{raw_question_key}"
                    )
                    conversion_issues.append(
                        SequenceIssue(
                            code="experience_metadata_invalid",
                            severity="error",
                            message=(
                                "cannot derive learner-experience metadata from "
                                f"the emitted question: {exc}"
                            ),
                            unit_path=unit_path,
                            item_paths=(question_path,),
                            observed={
                                "error_type": type(exc).__name__,
                                "detail": str(exc),
                            },
                            configured=(
                                "valid item identity, rung/variant, mechanic, "
                                "learning status, and answer metadata"
                            ),
                        )
                    )
                    item = ExperienceItem(
                        item_key=(
                            f"invalid-metadata:{module_index}:{unit_index}:"
                            f"{question_index}"
                        ),
                        concept_id=None,
                        rung=1,
                        mechanic="unknown",
                        module_key=str(module.import_key),
                        lesson_key=str(unit.import_key),
                        prompt=str(getattr(question, "question_text", "")),
                    )
                else:
                    question_path = (
                        f"{unit_path}/questions/{question_index}:{item.item_key}"
                    )
                items.append(item)
                paths.append(question_path)
            units.append(
                OrderedUnit(
                    unit_path=unit_path,
                    items=tuple(items),
                    item_paths=tuple(paths),
                    relaxations=tuple(relaxations_by_unit.get(unit.import_key, ())),
                )
            )
    return tuple(units), tuple(conversion_issues)


def ordered_units_from_tl_course(
    course: Any,
    *,
    relaxations_by_unit: Optional[Mapping[str, Sequence[ConstraintRelaxation]]] = None,
) -> tuple[OrderedUnit, ...]:
    """Convert a valid emitted course, raising on malformed metadata.

    Final validation uses the tolerant internal form below so malformed input
    becomes a structured hard error.  Direct conversion remains strict to
    prevent callers from accidentally consuming placeholder metadata.
    """

    units, _issues = _ordered_units_from_tl_course(
        course,
        relaxations_by_unit=relaxations_by_unit,
        capture_metadata_errors=False,
    )
    return units


def validate_tl_course(
    course: Any,
    *,
    policy: SequenceQualityPolicy = SequenceQualityPolicy(),
    relaxations_by_unit: Optional[Mapping[str, Sequence[ConstraintRelaxation]]] = None,
) -> SequenceQualityReport:
    units, metadata_issues = _ordered_units_from_tl_course(
        course,
        relaxations_by_unit=relaxations_by_unit,
        capture_metadata_errors=True,
    )
    return validate_ordered_units(
        units,
        policy=policy,
        additional_issues=metadata_issues,
    )


def write_quality_report(path: str | Path, report: SequenceQualityReport) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def concise_quality_summary(report: SequenceQualityReport) -> str:
    summary = report.summary
    return (
        f"{summary['units']} units / {summary['questions']} questions; "
        f"mechanic streak {summary['maximum_mechanic_streak']}, "
        f"UI-family streak {summary['maximum_ui_family_streak']}, "
        f"T/F answer streak {summary['maximum_same_answer_true_false_streak']}; "
        f"{summary['errors']} errors, {summary['warnings']} warnings, "
        f"{summary['units_with_relaxations']} units relaxed"
    )


__all__ = [
    "OrderedUnit",
    "QUALITY_REPORT_SCHEMA",
    "RollingWindowMetric",
    "SequenceIssue",
    "SequenceQualityPolicy",
    "SequenceQualityReport",
    "UnitSequenceMetrics",
    "UnitSequenceReport",
    "concise_quality_summary",
    "experience_item_from_tl_question",
    "ordered_units_from_tl_course",
    "validate_ordered_unit",
    "validate_ordered_units",
    "validate_tl_course",
    "write_quality_report",
]
