"""Focused corpus regression for the deterministic AI-901 learner sequence."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COURSE_DIR = REPO_ROOT / "courses" / "ai-901"
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ai901_sequence_audit import audit_compiled_course, audit_course  # noqa: E402


def _bank_fingerprint() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((COURSE_DIR / "bank").glob("*.json"))
    }


def test_ai901_sequence_audit_regression() -> None:
    banks_before = _bank_fingerprint()
    dist_existed_before = (COURSE_DIR / "dist").exists()

    first = audit_course(COURSE_DIR)
    raw = first["raw_bank_baseline"]
    assert raw["bank_files"] == 72
    assert raw["active_items"] == 2160
    assert raw["active_items_per_bank_distribution"] == {"30": 72}
    assert raw["maximum_same_mechanic_streak"] == 8
    assert raw["maximum_streak_distribution"] == {"3": 2, "4": 1, "8": 69}
    assert raw["banks_with_streak_at_least_8"] == 69
    assert raw["maximum_same_ui_family_streak"] == 12
    assert raw["maximum_ui_family_streak_distribution"] == {"12": 72}
    assert raw["banks_with_ui_family_streak_at_least_8"] == 72
    assert raw["choice_correct_option_position_distribution"] == {
        "multi_choice": {"0,1": 126, "0,1,2": 409, "0,1,3": 40, "0,2": 1},
        "single_choice": {"0": 574, "1": 1, "2": 1},
    }
    assert raw["maximum_same_correct_option_position_streak"] == 8
    assert raw["maximum_correct_option_position_streak_distribution"] == {
        "3": 3,
        "5": 1,
        "8": 68,
    }
    assert raw["banks_below_8"] == [
        {
            "bank": "ai-agents-and-teams-of-agents.json",
            "maximum_same_mechanic_streak": 4,
        },
        {
            "bank": "from-ocr-text-to-fields.json",
            "maximum_same_mechanic_streak": 3,
        },
        {
            "bank": "where-speech-is-used.json",
            "maximum_same_mechanic_streak": 3,
        },
    ]

    compiled = first["compiled"]
    # A second independent in-memory compilation must yield identical course,
    # metrics, relaxations, and preservation evidence.
    assert audit_compiled_course(COURSE_DIR) == compiled
    assert compiled["artifact_sha256"] == (
        "cc8560b1f00025246f51f446a71f0e8f2b619446e3ae9d4269f2a102761a3609"
    )
    assert compiled["unit_counts"] == {
        "checkpoint": 12,
        "final_review": 1,
        "l1": 72,
        "l2": 72,
        "l3": 72,
    }

    quality = compiled["sequence_quality"]
    assert quality["ok"] is True
    assert quality["units"] == 229
    assert quality["questions"] == 2348
    assert quality["errors"] == 0
    assert quality["warnings"] == 147
    assert quality["units_with_relaxations"] == 12
    assert quality["maximum_mechanic_streak"] == 3
    assert quality["maximum_ui_family_streak"] == 4
    assert quality["maximum_same_answer_true_false_streak"] == 2
    assert quality["adjacent_concept_collisions"] == 0
    assert quality["rolling_windows_below_minimum"] == 0

    preservation = compiled["preservation"]
    assert preservation["source_active_item_keys"] == 2160
    assert preservation["placements"] == 2348
    assert preservation["unique_placed_item_keys"] == 1982
    assert preservation["unknown_item_paths"] == []
    assert preservation["metadata_mismatches"] == []
    assert preservation["duplicate_item_keys_within_units"] == {}
    assert preservation["answer_semantic_mismatches"] == []
    assert preservation["all_output_items_traceable"] is True
    assert preservation["all_answer_semantics_preserved"] is True

    validators = compiled["validators"]
    assert validators["compiler_problem_count"] == 0
    assert validators["techlingo_problem_count"] == 0
    assert validators["sequence_quality_error_count"] == 0
    assert validators["sequence_quality_ok"] is True
    assert validators["revalidation_matches_compiler_report"] is True
    assert compiled["ok"] is True

    assert compiled["sequence_issue_counts"] == {
        "constraint_relaxation": 36,
        "difficulty_downward_jump": 87,
        "mechanic_streak": 2,
        "repeated_prompt_stem": 10,
        "ui_family_streak": 12,
    }
    assert compiled["choice_presentation"] == {
        "correct_option_position_distribution": {
            "0": 164,
            "0,1": 21,
            "0,1,2": 110,
            "0,1,3": 121,
            "0,2": 24,
            "0,2,3": 111,
            "0,3": 28,
            "1": 166,
            "1,2": 24,
            "1,2,3": 112,
            "1,3": 29,
            "2": 161,
            "2,3": 23,
            "3": 158,
        },
        "maximum_same_correct_option_position_streak": 1,
        "units_with_position_streak_warning": 0,
    }

    # Decision records are not silently equated with actual violations.  The
    # scheduler records every cumulative relaxation decision, and only the 14
    # signed records with violation_observed=true are artifact waivers.
    relaxations = compiled["constraint_relaxations"]
    assert relaxations["unit_count"] == 12
    assert relaxations["record_count"] == 36
    assert relaxations["violation_observed_count"] == 14
    assert relaxations["units_with_decisions"] == 12
    assert relaxations["units_with_actual_waivers"] == 12
    assert relaxations["decision_constraint_distribution"] == {
        "mechanic_streak": 2,
        "mechanics_window": 10,
        "true_false_answer_streak": 12,
        "ui_family_streak": 12,
    }
    assert relaxations["actual_waiver_constraint_distribution"] == {
        "mechanic_streak": 2,
        "ui_family_streak": 12,
    }
    assert {entry["unit"] for entry in relaxations["units"]} == {
        "before-adding-speech-l3",
        "choose-audio-processing-options-l3",
        "choosing-an-extraction-approach-l3",
        "choosing-and-cleaning-recognition-text-l3",
        "client-apps-and-the-python-sdk-l1",
        "common-business-scenarios-l3",
        "foundry-workspaces-and-portals-l3",
        "from-ocr-text-to-fields-l3",
        "generative-ai-at-work-l3",
        "what-computer-vision-does-l3",
        "what-text-analysis-does-l1",
        "where-speech-is-used-l3",
    }
    for entry in relaxations["units"]:
        records = entry["relaxations"]
        assert all(record["reason"] for record in records)
        assert all(isinstance(record["violation_observed"], bool) for record in records)
        assert all(record["attestation_sha256"] for record in records)
        assert all(record["artifact_sha256"] for record in records)
        assert all(record["policy_sha256"] for record in records)
        violations = [record for record in records if record["violation_observed"]]
        expected_constraints = {"ui_family_streak"}
        if entry["unit"] in {
            "client-apps-and-the-python-sdk-l1",
            "what-text-analysis-does-l1",
        }:
            expected_constraints.add("mechanic_streak")
        assert {record["constraint"] for record in violations} == expected_constraints
        assert all(record["observed"] > record["configured"] == 2 for record in violations)
        assert all(record["item_keys"] for record in violations)

    # Five true/false items and one separator have a theoretical best authored-
    # mechanic streak of ceil(5 / (1 + 1)) == 3.  These are the exact two
    # proven-impossible mechanic cases; the test intentionally permits them.
    assert relaxations["mechanic_streak_feasibility_proofs"] == [
        {
            "unit": "client-apps-and-the-python-sdk-l1",
            "dominant_mechanic": "true_false",
            "dominant_count": 5,
            "other_mechanic_separators": 1,
            "theoretical_minimum_maximum_streak": 3,
            "configured_maximum_streak": 2,
            "proven_impossible": True,
        },
        {
            "unit": "what-text-analysis-does-l1",
            "dominant_mechanic": "true_false",
            "dominant_count": 5,
            "other_mechanic_separators": 1,
            "theoretical_minimum_maximum_streak": 3,
            "configured_maximum_streak": 2,
            "proven_impossible": True,
        },
    ]
    ui_proofs = relaxations["ui_family_streak_feasibility_proofs"]
    assert len(ui_proofs) == 12
    assert {proof["unit"] for proof in ui_proofs} == {
        entry["unit"] for entry in relaxations["units"]
    }
    assert {
        proof["unit"]: proof["dominant_ui_family"] for proof in ui_proofs
    } == {
        entry["unit"]: (
            "true_false"
            if entry["unit"]
            in {"client-apps-and-the-python-sdk-l1", "what-text-analysis-does-l1"}
            else "multiple_choice"
        )
        for entry in relaxations["units"]
    }
    assert all(proof["configured_maximum_streak"] == 2 for proof in ui_proofs)
    assert all(proof["proven_impossible"] is True for proof in ui_proofs)

    # The audit path is deliberately in-memory: no bank or bundle side effect.
    assert _bank_fingerprint() == banks_before
    assert (COURSE_DIR / "dist").exists() is dist_existed_before


def _run_all() -> None:
    test_ai901_sequence_audit_regression()
    print("AI-901 sequence audit regression passed.")


if __name__ == "__main__":
    _run_all()
