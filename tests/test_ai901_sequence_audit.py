"""Focused corpus regression for the deterministic AI-901 learner sequence."""

from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
COURSE_DIR = REPO_ROOT / "courses" / "ai-901"
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ai901_sequence_audit import audit_compiled_course, audit_course  # noqa: E402


_LOWERCASE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _bank_fingerprint() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((COURSE_DIR / "bank").glob("*.json"))
    }


def _assert_sha256(value: object) -> None:
    assert isinstance(value, str)
    assert _LOWERCASE_SHA256.fullmatch(value)


def _assert_relaxations_are_proven(relaxations: dict[str, Any]) -> None:
    """Require signed, independently revalidated proof for every actual waiver."""

    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    all_records: list[dict[str, Any]] = []
    actual_waivers: list[tuple[str, dict[str, Any]]] = []
    units_with_actual_waivers: set[str] = set()

    for entry in relaxations["units"]:
        unit = entry["unit"]
        records = entry["relaxations"]
        assert records
        for record in records:
            key = (unit, record["constraint"])
            assert key not in records_by_key
            records_by_key[key] = record
            all_records.append(record)

            assert record["reason"].strip()
            assert type(record["violation_observed"]) is bool
            _assert_sha256(record["attestation_sha256"])
            _assert_sha256(record["artifact_sha256"])
            _assert_sha256(record["policy_sha256"])

            if not record["violation_observed"]:
                continue
            actual_waivers.append((unit, record))
            units_with_actual_waivers.add(unit)
            assert record["item_keys"]
            expected_proof_kind = (
                "rolling_window_exhaustive"
                if record["constraint"] == "mechanics_window"
                else "categorical_capacity"
            )
            assert record["proof_kind"] == expected_proof_kind

    # These are report-consistency assertions, not corpus snapshots: the
    # number and location of relaxation decisions may legitimately change
    # whenever the rebuilt source questions change.
    assert relaxations["unit_count"] == len(relaxations["units"])
    assert relaxations["units_with_decisions"] == len(relaxations["units"])
    assert relaxations["record_count"] == len(all_records)
    assert relaxations["violation_observed_count"] == len(actual_waivers)
    assert relaxations["units_with_actual_waivers"] == len(units_with_actual_waivers)
    assert relaxations["decision_constraint_distribution"] == dict(
        sorted(Counter(record["constraint"] for record in all_records).items())
    )
    assert relaxations["actual_waiver_constraint_distribution"] == dict(
        sorted(Counter(record["constraint"] for _unit, record in actual_waivers).items())
    )

    # The audit exposes an additional capacity calculation for the two
    # categorical run dimensions.  Match every such actual waiver to exactly
    # one independently calculated, proven-impossible result without pinning
    # the corpus to particular units or totals.
    exposed_proofs: list[tuple[str, str, dict[str, Any]]] = [
        (proof["unit"], "mechanic_streak", proof)
        for proof in relaxations["mechanic_streak_feasibility_proofs"]
    ]
    exposed_proofs.extend(
        (proof["unit"], "ui_family_streak", proof)
        for proof in relaxations["ui_family_streak_feasibility_proofs"]
    )
    expected_exposed_keys = Counter(
        (unit, record["constraint"])
        for unit, record in actual_waivers
        if record["constraint"] in {"mechanic_streak", "ui_family_streak"}
    )
    assert Counter((unit, constraint) for unit, constraint, _proof in exposed_proofs) == (
        expected_exposed_keys
    )
    for unit, constraint, proof in exposed_proofs:
        record = records_by_key[(unit, constraint)]
        assert proof["proven_impossible"] is True
        assert proof["configured_maximum_streak"] == record["configured"]
        separators_key = (
            "other_mechanic_separators"
            if constraint == "mechanic_streak"
            else "other_ui_family_separators"
        )
        dominant_count = proof["dominant_count"]
        separators = proof[separators_key]
        theoretical_minimum = (dominant_count + separators) // (separators + 1)
        assert proof["theoretical_minimum_maximum_streak"] == theoretical_minimum
        assert theoretical_minimum > proof["configured_maximum_streak"]


def test_ai901_sequence_audit_regression() -> None:
    banks_before = _bank_fingerprint()
    dist_existed_before = (COURSE_DIR / "dist").exists()

    first = audit_course(COURSE_DIR)
    raw = first["raw_bank_baseline"]
    assert raw["bank_files"] == 72
    assert raw["active_items"] == 2160
    assert raw["active_items_per_bank_distribution"] == {"30": 72}

    compiled = first["compiled"]
    # A second independent in-memory compilation must yield byte-identical
    # artifact identity, metrics, relaxations, and preservation evidence.
    assert audit_compiled_course(COURSE_DIR) == compiled
    _assert_sha256(compiled["artifact_sha256"])
    assert compiled["unit_counts"] == {
        "checkpoint": 12,
        "final_review": 1,
        "l1": 72,
        "l2": 72,
        "l3": 72,
    }

    quality = compiled["sequence_quality"]
    assert quality["ok"] is True
    assert quality["units"] == sum(compiled["unit_counts"].values())
    assert quality["questions"] == compiled["preservation"]["placements"]
    assert quality["errors"] == 0
    assert quality["adjacent_concept_collisions"] == 0
    assert quality["rolling_windows_below_minimum"] == 0

    preservation = compiled["preservation"]
    assert preservation["source_active_item_keys"] == 2160
    assert preservation["source_duplicate_item_keys"] == []
    assert preservation["unknown_item_paths"] == []
    assert preservation["metadata_mismatches"] == []
    assert preservation["duplicate_item_keys_within_units"] == {}
    assert preservation["answer_semantic_mismatches"] == []
    assert preservation["all_output_items_traceable"] is True
    assert preservation["all_answer_semantics_preserved"] is True
    assert 0 < preservation["unique_placed_item_keys"] <= 2160
    assert preservation["placements"] >= preservation["unique_placed_item_keys"]
    assert preservation["repeated_placements_across_units"] == (
        preservation["placements"] - preservation["unique_placed_item_keys"]
    )
    assert preservation["unselected_active_item_keys"] == (
        2160 - preservation["unique_placed_item_keys"]
    )

    validators = compiled["validators"]
    assert validators["compiler_problem_count"] == 0
    assert validators["compiler_problems"] == []
    assert validators["techlingo_problem_count"] == 0
    assert validators["techlingo_problems"] == []
    assert validators["sequence_quality_error_count"] == 0
    assert validators["sequence_quality_ok"] is True
    assert validators["revalidation_matches_compiler_report"] is True
    assert "constraint_relaxation_invalid" not in compiled["sequence_issue_counts"]
    assert compiled["ok"] is True

    relaxations = compiled["constraint_relaxations"]
    assert quality["units_with_relaxations"] == relaxations["unit_count"]
    _assert_relaxations_are_proven(relaxations)

    # Adjacent-concept and rolling-window failures are never accepted, even
    # when the scheduler has decision records for other proven constraints.
    assert not any(
        record["violation_observed"]
        and record["constraint"] in {"concept_adjacency", "mechanics_window"}
        for entry in relaxations["units"]
        for record in entry["relaxations"]
    )

    # The audit path is deliberately in-memory: no bank or bundle side effect.
    assert _bank_fingerprint() == banks_before
    assert (COURSE_DIR / "dist").exists() is dist_existed_before


def _run_all() -> None:
    test_ai901_sequence_audit_regression()
    print("AI-901 sequence audit regression passed.")


if __name__ == "__main__":
    _run_all()
