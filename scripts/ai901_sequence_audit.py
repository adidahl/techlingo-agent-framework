#!/usr/bin/env python3
"""Read-only, reproducible sequence audit for the AI-901 course workspace.

The raw-bank half intentionally uses only the Python standard library.  The
compiled half calls the same compiler and final sequence validators used by
the product.  Nothing in this module writes a bank or a ``dist/`` artifact.

Run from the repository root with:

    .venv/bin/python scripts/ai901_sequence_audit.py --pretty
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping


AUDIT_SCHEMA_VERSION = "ai901-sequence-audit-v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COURSE_DIR = REPO_ROOT / "courses" / "ai-901"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _maximum_run(values: Iterable[str]) -> int:
    maximum = 0
    previous: object = object()
    current = 0
    for value in values:
        if value == previous:
            current += 1
        else:
            previous = value
            current = 1
        maximum = max(maximum, current)
    return maximum


def _maximum_known_run(values: Iterable[str | None]) -> int:
    """Maximum equal-value run, treating non-choice items as separators."""

    maximum = 0
    previous: str | None = None
    current = 0
    for value in values:
        if value is None:
            previous = None
            current = 0
        elif value == previous:
            current += 1
        else:
            previous = value
            current = 1
        maximum = max(maximum, current)
    return maximum


def _raw_correct_position_key(item: Mapping[str, Any]) -> str | None:
    payload = item.get("payload", {})
    if payload.get("question_type") not in {"single_choice", "multi_choice"}:
        return None
    indexes = [
        index
        for index, option in enumerate(payload.get("options", []))
        if option.get("is_correct") is True
    ]
    return ",".join(str(index) for index in indexes) or "missing"


def _learner_ui_family(mechanic: str) -> str:
    return {
        "single_choice": "multiple_choice",
        "multi_choice": "multiple_choice",
        "fill_gaps": "fill_blank",
        "rearrange": "arrange_sentence",
    }.get(mechanic, mechanic)


def audit_raw_banks(course_dir: str | Path) -> dict[str, Any]:
    """Measure literal active-item order in ``bank/*.json`` with stdlib only."""

    bank_dir = Path(course_dir) / "bank"
    files = sorted(bank_dir.glob("*.json"))
    per_bank: list[dict[str, Any]] = []
    active_total = 0
    correct_positions: dict[str, Counter[str]] = {
        "single_choice": Counter(),
        "multi_choice": Counter(),
    }
    for path in files:
        document = json.loads(path.read_text(encoding="utf-8"))
        active = [item for item in document.get("items", []) if item.get("status") != "retired"]
        mechanics = [str(item.get("payload", {}).get("question_type", "unknown")) for item in active]
        maximum = _maximum_run(mechanics)
        maximum_ui_family = _maximum_run(map(_learner_ui_family, mechanics))
        position_keys = [_raw_correct_position_key(item) for item in active]
        maximum_correct_position = _maximum_known_run(position_keys)
        for item, position_key in zip(active, position_keys):
            mechanic = str(item.get("payload", {}).get("question_type", "unknown"))
            if position_key is not None and mechanic in correct_positions:
                correct_positions[mechanic][position_key] += 1
        active_total += len(active)
        per_bank.append(
            {
                "bank": path.name,
                "active_items": len(active),
                "maximum_same_mechanic_streak": maximum,
                "maximum_same_ui_family_streak": maximum_ui_family,
                "maximum_same_correct_option_position_streak": maximum_correct_position,
            }
        )

    streak_distribution = Counter(entry["maximum_same_mechanic_streak"] for entry in per_bank)
    ui_family_streak_distribution = Counter(
        entry["maximum_same_ui_family_streak"] for entry in per_bank
    )
    item_count_distribution = Counter(entry["active_items"] for entry in per_bank)
    position_streak_distribution = Counter(
        entry["maximum_same_correct_option_position_streak"] for entry in per_bank
    )
    threshold = 8
    below = [
        {
            "bank": entry["bank"],
            "maximum_same_mechanic_streak": entry["maximum_same_mechanic_streak"],
        }
        for entry in per_bank
        if entry["maximum_same_mechanic_streak"] < threshold
    ]
    return {
        "definition": {
            "source": "bank/*.json",
            "active_item_predicate": "item.status != 'retired'",
            "ordering": "literal items[] order within each bank file",
            "streak_dimension": "payload.question_type (single_choice and multi_choice remain distinct)",
            "ui_family_normalization": {
                "single_choice": "multiple_choice",
                "multi_choice": "multiple_choice",
                "fill_gaps": "fill_blank",
                "rearrange": "arrange_sentence",
                "true_false": "true_false",
            },
            "qualifying_threshold": threshold,
        },
        "bank_files": len(files),
        "active_items": active_total,
        "active_items_per_bank_distribution": {
            str(count): banks for count, banks in sorted(item_count_distribution.items())
        },
        "maximum_same_mechanic_streak": max(streak_distribution, default=0),
        "maximum_streak_distribution": {
            str(streak): banks for streak, banks in sorted(streak_distribution.items())
        },
        "maximum_same_ui_family_streak": max(ui_family_streak_distribution, default=0),
        "maximum_ui_family_streak_distribution": {
            str(streak): banks
            for streak, banks in sorted(ui_family_streak_distribution.items())
        },
        "choice_correct_option_position_distribution": {
            mechanic: dict(sorted(distribution.items()))
            for mechanic, distribution in sorted(correct_positions.items())
        },
        "maximum_same_correct_option_position_streak": max(
            position_streak_distribution, default=0
        ),
        "maximum_correct_option_position_streak_distribution": {
            str(streak): banks
            for streak, banks in sorted(position_streak_distribution.items())
        },
        "banks_with_ui_family_streak_at_least_8": sum(
            entry["maximum_same_ui_family_streak"] >= threshold for entry in per_bank
        ),
        "banks_with_streak_at_least_8": sum(
            entry["maximum_same_mechanic_streak"] >= threshold for entry in per_bank
        ),
        "banks_below_8": below,
    }


def _load_product_apis() -> dict[str, Any]:
    """Load repository APIs only after the stdlib raw audit is available."""

    source_dir = str(REPO_ROOT / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)

    from techlingo_workflow.compiler import compile_workspace
    from techlingo_workflow.experience import ExperiencePolicy
    from techlingo_workflow.sequence_quality import SequenceQualityPolicy, validate_tl_course
    from techlingo_workflow.validate_techlingo import validate_techlingo_course

    return {
        "compile_workspace": compile_workspace,
        "ExperiencePolicy": ExperiencePolicy,
        "SequenceQualityPolicy": SequenceQualityPolicy,
        "validate_tl_course": validate_tl_course,
        "validate_techlingo_course": validate_techlingo_course,
    }


def _quality_policy(compiled: Any, apis: Mapping[str, Any]) -> Any:
    experience = compiled.cfg.experience
    sequence = compiled.cfg.sequence_quality
    experience_policy = apis["ExperiencePolicy"](
        max_same_mechanic_streak=experience.max_same_mechanic_streak,
        max_same_ui_family_streak=experience.max_same_ui_family_streak,
        max_same_true_false_answer_streak=experience.max_same_true_false_answer_streak,
        mechanics_window_size=experience.mechanics_window_size,
        min_mechanics_per_window=experience.min_mechanics_per_window,
        avoid_adjacent_same_concept=experience.avoid_adjacent_same_concept,
        max_search_states=experience.max_search_states,
        relaxation_order=tuple(experience.relaxation_order),
    )
    return apis["SequenceQualityPolicy"](
        experience=experience_policy,
        max_same_correct_position_streak=sequence.max_same_correct_position_streak,
        prompt_stem_words=sequence.prompt_stem_words,
        max_repeated_prompt_stem=sequence.max_repeated_prompt_stem,
        max_downward_rung_jump=sequence.max_downward_rung_jump,
        max_same_rung_streak=sequence.max_same_rung_streak,
    )


_OPTION_FIELDS = (
    "text",
    "is_correct",
    "error_type",
    "rationale",
    "better_fit",
    "feedback",
)


def _normalized_option(option: Mapping[str, Any]) -> str:
    return _canonical_json({field: option.get(field) for field in _OPTION_FIELDS})


def _decoded_indexes(answer: str) -> tuple[int, ...]:
    decoded = json.loads(answer)
    if isinstance(decoded, int):
        return (decoded,)
    if isinstance(decoded, list) and all(isinstance(index, int) for index in decoded):
        return tuple(decoded)
    raise ValueError(f"not an integer or integer array: {answer!r}")


def _decoded_fill_answers(answer: str) -> list[str]:
    if answer.startswith("["):
        decoded = json.loads(answer)
        if not isinstance(decoded, list) or not all(isinstance(value, str) for value in decoded):
            raise ValueError(f"not a string array: {answer!r}")
        return decoded
    return [answer]


def _answer_semantics_match(payload: Mapping[str, Any], question: Any) -> tuple[bool, str]:
    mechanic = payload.get("question_type")
    emitted_mechanic = question.options.get("original_question_type")
    if emitted_mechanic != mechanic:
        return False, f"mechanic changed from {mechanic!r} to {emitted_mechanic!r}"

    if mechanic in {"single_choice", "multi_choice"}:
        source_options = list(payload.get("options", []))
        emitted_options = list(question.options.get("options", []))
        if Counter(map(_normalized_option, source_options)) != Counter(
            map(_normalized_option, emitted_options)
        ):
            return False, "option content changed during presentation permutation"
        try:
            correct_indexes = _decoded_indexes(question.correct_answer)
            emitted_correct = Counter(
                _normalized_option(emitted_options[index]) for index in correct_indexes
            )
        except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return False, f"emitted choice answer cannot be decoded: {exc}"
        source_correct = Counter(
            _normalized_option(option) for option in source_options if option.get("is_correct") is True
        )
        if emitted_correct != source_correct:
            return False, "emitted correct indexes identify different source options"
        return True, ""

    if mechanic == "true_false":
        emitted = str(question.correct_answer).lower() == "true"
        return (
            (True, "")
            if emitted is payload.get("correct_answer")
            else (False, "true/false answer changed")
        )

    if mechanic == "fill_gaps":
        gaps = [part for part in payload.get("parts", []) if part.get("type") == "gap"]
        source_answers = list(gaps[0].get("accepted_answers", [])) if gaps else []
        try:
            emitted_answers = _decoded_fill_answers(question.correct_answer)
        except (ValueError, json.JSONDecodeError) as exc:
            return False, f"emitted fill answer cannot be decoded: {exc}"
        if emitted_answers != source_answers:
            return False, "accepted fill answers changed"
        return True, ""

    if mechanic == "rearrange":
        source_order = list(payload.get("correct_order", []))
        emitted_order = list(question.options.get("correct_order", []))
        if emitted_order != source_order or question.correct_answer != " ".join(source_order):
            return False, "arrangement correct order changed"
        return True, ""

    return False, f"unsupported source mechanic {mechanic!r}"


def _compiled_units(compiled: Any) -> list[tuple[Any, Any]]:
    return [
        (module, unit)
        for module in compiled.tl_course.modules
        for unit in module.lessons
    ]


def _preservation_report(compiled: Any) -> dict[str, Any]:
    source: dict[str, tuple[Any, Any]] = {}
    duplicate_source_keys: list[str] = []
    for bank in compiled.banks.values():
        for item in bank.items:
            if item.status == "retired":
                continue
            if item.item_key in source:
                duplicate_source_keys.append(item.item_key)
            else:
                source[item.item_key] = (bank, item)

    unknown: list[str] = []
    metadata_mismatches: list[dict[str, str]] = []
    answer_mismatches: list[dict[str, str]] = []
    duplicate_keys_by_unit: dict[str, list[str]] = {}
    placement_keys: list[str] = []
    original_mechanics: Counter[str] = Counter()
    emitted_types: Counter[str] = Counter()

    for module, unit in _compiled_units(compiled):
        unit_keys = [str(question.options.get("item_key", "")) for question in unit.exercises]
        duplicates = sorted(key for key, count in Counter(unit_keys).items() if count > 1)
        if duplicates:
            duplicate_keys_by_unit[unit.import_key] = duplicates
        for index, question in enumerate(unit.exercises):
            path = f"{module.import_key}/{unit.import_key}/questions/{index}"
            item_key = str(question.options.get("item_key", ""))
            placement_keys.append(item_key)
            emitted_types[str(question.question_type)] += 1
            entry = source.get(item_key)
            if entry is None:
                unknown.append(path)
                continue
            bank, item = entry
            original_mechanics[str(item.payload.get("question_type", "unknown"))] += 1
            expected_metadata = {
                "concept_id": item.concept_id,
                "rung": item.rung,
                "variant": item.variant,
                "module_key": bank.module,
                "lesson_key": bank.lesson,
            }
            actual_metadata = {
                key: question.options.get(key) for key in expected_metadata
            }
            if actual_metadata != expected_metadata:
                metadata_mismatches.append(
                    {
                        "path": path,
                        "reason": f"expected {expected_metadata!r}, got {actual_metadata!r}",
                    }
                )
            matches, reason = _answer_semantics_match(item.payload, question)
            if not matches:
                answer_mismatches.append({"path": path, "reason": reason})

    unique_placements = set(placement_keys)
    identity_ok = not (
        duplicate_source_keys
        or unknown
        or metadata_mismatches
        or duplicate_keys_by_unit
    )
    return {
        "source_active_item_keys": len(source),
        "source_duplicate_item_keys": sorted(set(duplicate_source_keys)),
        "placements": len(placement_keys),
        "unique_placed_item_keys": len(unique_placements),
        "repeated_placements_across_units": len(placement_keys) - len(unique_placements),
        "unselected_active_item_keys": len(set(source) - unique_placements),
        "unknown_item_paths": unknown,
        "metadata_mismatches": metadata_mismatches,
        "duplicate_item_keys_within_units": duplicate_keys_by_unit,
        "answer_semantic_mismatches": answer_mismatches,
        "original_mechanic_distribution": dict(sorted(original_mechanics.items())),
        "emitted_question_type_distribution": dict(sorted(emitted_types.items())),
        "all_output_items_traceable": identity_ok,
        "all_answer_semantics_preserved": not answer_mismatches,
    }


def _relaxation_report(compiled: Any) -> dict[str, Any]:
    unit_by_key = {unit.import_key: unit for _module, unit in _compiled_units(compiled)}
    units: list[dict[str, Any]] = []
    record_count = 0
    violation_count = 0
    proven_impossible: list[dict[str, Any]] = []
    ui_family_proven_impossible: list[dict[str, Any]] = []
    decision_constraints: Counter[str] = Counter()
    waiver_constraints: Counter[str] = Counter()
    units_with_actual_waivers: set[str] = set()
    for unit_key in sorted(compiled.relaxations_by_unit):
        raw_relaxations = compiled.relaxations_by_unit[unit_key]
        if not raw_relaxations:
            continue
        records = [asdict(relaxation) for relaxation in raw_relaxations]
        record_count += len(records)
        violation_count += sum(record["violation_observed"] for record in records)
        decision_constraints.update(record["constraint"] for record in records)
        waiver_constraints.update(
            record["constraint"] for record in records if record["violation_observed"]
        )
        if any(record["violation_observed"] for record in records):
            units_with_actual_waivers.add(unit_key)
        units.append({"unit": unit_key, "relaxations": records})

        unit = unit_by_key[unit_key]
        mechanics = Counter(
            str(question.options.get("original_question_type") or question.question_type)
            for question in unit.exercises
        )
        ui_families = Counter(
            _learner_ui_family(mechanic)
            for mechanic, count in mechanics.items()
            for _ in range(count)
        )
        for record in records:
            if not record["violation_observed"] or record["constraint"] not in {
                "mechanic_streak",
                "ui_family_streak",
            }:
                continue
            categories = (
                mechanics if record["constraint"] == "mechanic_streak" else ui_families
            )
            dominant_category, dominant_count = max(
                categories.items(), key=lambda entry: (entry[1], entry[0])
            )
            separators = sum(categories.values()) - dominant_count
            lower_bound = math.ceil(dominant_count / (separators + 1))
            proof = {
                "unit": unit_key,
                (
                    "dominant_mechanic"
                    if record["constraint"] == "mechanic_streak"
                    else "dominant_ui_family"
                ): dominant_category,
                "dominant_count": dominant_count,
                (
                    "other_mechanic_separators"
                    if record["constraint"] == "mechanic_streak"
                    else "other_ui_family_separators"
                ): separators,
                "theoretical_minimum_maximum_streak": lower_bound,
                "configured_maximum_streak": record["configured"],
                "proven_impossible": lower_bound > record["configured"],
            }
            if record["constraint"] == "mechanic_streak":
                proven_impossible.append(proof)
            else:
                ui_family_proven_impossible.append(proof)

    return {
        "semantics": {
            "decision_record": (
                "a signed cumulative scheduler relaxation decision; it can record that a "
                "constraint was searched as relaxed without a final artifact violation"
            ),
            "actual_waiver": "a decision record where violation_observed is true",
        },
        "unit_count": len(units),
        "record_count": record_count,
        "violation_observed_count": violation_count,
        "units_with_decisions": len(units),
        "units_with_actual_waivers": len(units_with_actual_waivers),
        "decision_constraint_distribution": dict(sorted(decision_constraints.items())),
        "actual_waiver_constraint_distribution": dict(sorted(waiver_constraints.items())),
        "units": units,
        "mechanic_streak_feasibility_proofs": proven_impossible,
        "ui_family_streak_feasibility_proofs": ui_family_proven_impossible,
    }


def audit_compiled_course(course_dir: str | Path) -> dict[str, Any]:
    """Compile entirely in memory and audit the exact learner-facing artifact."""

    apis = _load_product_apis()
    compiled = apis["compile_workspace"](Path(course_dir))
    techlingo_problems = apis["validate_techlingo_course"](compiled.tl_course)
    quality = apis["validate_tl_course"](
        compiled.tl_course,
        policy=_quality_policy(compiled, apis),
        relaxations_by_unit=compiled.relaxations_by_unit,
    )
    preservation = _preservation_report(compiled)
    relaxation = _relaxation_report(compiled)
    compiler_quality = compiled.sequence_quality.to_dict()
    revalidated_quality = quality.to_dict()
    issue_counts = Counter(issue.code for issue in quality.issues)
    unit_lengths = Counter(len(unit.exercises) for _module, unit in _compiled_units(compiled))
    artifact = compiled.tl_course.model_dump(mode="json")
    compiled_positions: Counter[str] = Counter()
    for unit_report in quality.units:
        compiled_positions.update(
            unit_report.metrics.correct_option_position_distribution
        )

    validators = {
        "compiler_problem_count": len(compiled.problems),
        "compiler_problems": list(compiled.problems),
        "techlingo_problem_count": len(techlingo_problems),
        "techlingo_problems": techlingo_problems,
        "sequence_quality_ok": quality.ok,
        "sequence_quality_error_count": sum(issue.severity == "error" for issue in quality.issues),
        "revalidation_matches_compiler_report": revalidated_quality == compiler_quality,
    }
    return {
        "artifact_sha256": _sha256_json(artifact),
        "unit_counts": dict(sorted(compiled.unit_counts.items())),
        "unit_length_distribution": {
            str(length): units for length, units in sorted(unit_lengths.items())
        },
        "sequence_quality_schema_version": quality.schema_version,
        "sequence_quality": quality.summary,
        "sequence_issue_counts": dict(sorted(issue_counts.items())),
        "choice_presentation": {
            "correct_option_position_distribution": dict(
                sorted(compiled_positions.items())
            ),
            "maximum_same_correct_option_position_streak": max(
                (
                    unit_report.metrics.maximum_same_correct_position_streak
                    for unit_report in quality.units
                ),
                default=0,
            ),
            "units_with_position_streak_warning": sum(
                issue.code == "correct_option_position_streak"
                for issue in quality.issues
            ),
        },
        "constraint_relaxations": relaxation,
        "preservation": preservation,
        "validators": validators,
        "ok": (
            not compiled.problems
            and not techlingo_problems
            and quality.ok
            and validators["revalidation_matches_compiler_report"]
            and preservation["all_output_items_traceable"]
            and preservation["all_answer_semantics_preserved"]
        ),
    }


def audit_course(course_dir: str | Path = DEFAULT_COURSE_DIR) -> dict[str, Any]:
    course_path = Path(course_dir)
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "course_id": course_path.name,
        "raw_bank_baseline": audit_raw_banks(course_path),
        "compiled": audit_compiled_course(course_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report the raw and in-memory compiled AI-901 sequence metrics as JSON."
    )
    parser.add_argument(
        "course_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_COURSE_DIR,
        help="Course workspace (default: courses/ai-901 in this repository).",
    )
    parser.add_argument("--pretty", action="store_true", help="Indent the JSON output.")
    args = parser.parse_args(argv)
    try:
        report = audit_course(args.course_dir)
    except Exception as exc:  # machine-readable CLI failure without writing artifacts
        print(
            json.dumps(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0 if report["compiled"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
