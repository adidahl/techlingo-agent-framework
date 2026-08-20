"""Deterministic learner-experience selection, scheduling, and reporting."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from techlingo_workflow.experience import (
    CONSTRAINT_MECHANICS_WINDOW,
    CONSTRAINT_MECHANIC_STREAK,
    CONSTRAINT_TF_ANSWER_STREAK,
    CONSTRAINT_UI_FAMILY_STREAK,
    ConstraintRelaxation,
    ExperienceItem,
    ExperiencePolicy,
    compose_experience,
    relaxation_attestation_errors,
    relaxation_violation_proven_unavoidable,
    select_variants,
)
from techlingo_workflow.sequence_quality import (
    OrderedUnit,
    SequenceQualityPolicy,
    validate_ordered_unit,
    validate_ordered_units,
    validate_tl_course,
)
from techlingo_workflow.techlingo_models import TLCourse, TLModule, TLQuestion, TLUnit


def _item(
    key: str,
    mechanic: str,
    *,
    concept: str | None = None,
    rung: int = 1,
    variant: int = 1,
    answer: bool | None = None,
    position: tuple[int, ...] = (),
    prompt: str = "",
) -> ExperienceItem:
    return ExperienceItem(
        item_key=key,
        concept_id=concept or key,
        rung=rung,
        variant=variant,
        mechanic=mechanic,
        true_false_answer=answer,
        correct_option_indexes=position,
        prompt=prompt or f"Prompt for {key}",
        payload_hash=f"hash:{key}",
    )


def _max_run(values):
    best = run = 0
    previous = object()
    for value in values:
        run = run + 1 if value == previous else 1
        previous = value
        best = max(best, run)
    return best


def test_same_seed_is_reproducible_and_preserves_identity_and_content():
    items = [
        *[_item(f"sc-{i}", "single_choice", rung=1) for i in range(6)],
        *[_item(f"mc-{i}", "multi_choice", rung=2) for i in range(5)],
        *[_item(f"tf-{i}", "true_false", rung=2, answer=bool(i % 2)) for i in range(5)],
        *[_item(f"fg-{i}", "fill_gaps", rung=3) for i in range(4)],
    ]
    first = compose_experience(items, seed=901, scope="lesson")
    second = compose_experience(items, seed=901, scope="lesson")
    assert [item.item_key for item in first.ordered] == [item.item_key for item in second.ordered]
    assert Counter(item.item_key for item in first.ordered) == Counter(item.item_key for item in items)
    assert {item.item_key: item.payload_hash for item in first.ordered} == {
        item.item_key: item.payload_hash for item in items
    }
    assert first.diagnostics.relaxations == ()


def test_feasible_pool_interleaves_mechanics_windows_and_concepts():
    items = [
        _item(f"a-{i}", "single_choice", concept=f"concept-{i}", rung=1)
        for i in range(8)
    ] + [
        _item(f"b-{i}", "multi_choice", concept=f"other-{i}", rung=2)
        for i in range(5)
    ] + [
        _item(f"c-{i}", "fill_gaps", concept=f"third-{i}", rung=3)
        for i in range(4)
    ]
    result = compose_experience(items, seed=17)
    mechanics = [item.mechanic for item in result.ordered]
    assert _max_run(mechanics) <= 2
    assert all(
        len(set(mechanics[start : start + 6])) >= 3
        for start in range(len(mechanics) - 5)
    )
    assert all(
        left.concept_id != right.concept_id
        for left, right in zip(result.ordered, result.ordered[1:])
    )


def test_dominant_mechanic_reserves_separators_instead_of_false_relaxation():
    # 10 A and 4 B are exactly feasible at max streak 2: A,A,B repeated.
    items = [
        *[_item(f"a-{i}", "single_choice", concept=f"a-{i}") for i in range(10)],
        *[_item(f"b-{i}", "fill_gaps", concept=f"b-{i}", rung=3) for i in range(4)],
    ]
    policy = ExperiencePolicy(min_mechanics_per_window=2)
    result = compose_experience(items, policy=policy, seed=4)
    assert _max_run([item.mechanic for item in result.ordered]) <= 2
    assert CONSTRAINT_MECHANIC_STREAK not in result.diagnostics.relaxed_constraints


def test_impossible_pool_relaxes_predictably_and_reports_evidence():
    items = [
        _item(f"tf-{i}", "true_false", concept=f"c-{i}", rung=2, answer=True)
        for i in range(7)
    ]
    result = compose_experience(items, seed=2)
    assert result.diagnostics.relaxed_constraints == (
        CONSTRAINT_TF_ANSWER_STREAK,
        CONSTRAINT_UI_FAMILY_STREAK,
        CONSTRAINT_MECHANIC_STREAK,
    )
    assert all(relaxation.violation_observed for relaxation in result.diagnostics.relaxations)
    assert all(relaxation.item_keys for relaxation in result.diagnostics.relaxations)
    assert [relaxation.configured for relaxation in result.diagnostics.relaxations] == [2, 2, 2]
    assert all(
        not relaxation_attestation_errors(relaxation, result.ordered, ExperiencePolicy())
        for relaxation in result.diagnostics.relaxations
    )
    assert all(
        relaxation_violation_proven_unavoidable(
            relaxation, result.ordered, ExperiencePolicy()
        )
        for relaxation in result.diagnostics.relaxations
    )


def test_ui_family_is_hard_even_when_original_choice_mechanics_alternate():
    items = [
        *[_item(f"sc-{i}", "single_choice") for i in range(4)],
        *[_item(f"mc-{i}", "multi_choice") for i in range(4)],
        *[_item(f"fg-{i}", "fill_gaps", rung=3) for i in range(4)],
    ]
    result = compose_experience(items, seed=7)
    ui_families = [
        "multiple_choice"
        if item.mechanic in {"single_choice", "multi_choice"}
        else "fill_blank"
        for item in result.ordered
    ]
    assert _max_run(ui_families) <= 2
    assert _max_run([item.mechanic for item in result.ordered]) <= 2
    assert result.diagnostics.relaxations == ()


def test_ui_relaxes_before_original_mechanic_and_keeps_nonwaiver_history():
    # All five questions render as multiple choice, but the 3/2 split means the
    # original mechanics themselves can still satisfy a max streak of two.
    items = [
        *[_item(f"sc-{i}", "single_choice") for i in range(3)],
        *[_item(f"mc-{i}", "multi_choice") for i in range(2)],
    ]
    result = compose_experience(items, seed=19, scope="ui-impossible")
    assert result.diagnostics.relaxed_constraints == (
        CONSTRAINT_TF_ANSWER_STREAK,
        CONSTRAINT_UI_FAMILY_STREAK,
    )
    tf_decision, ui_waiver = result.diagnostics.relaxations
    assert not tf_decision.violation_observed
    assert tf_decision.item_keys == ()
    assert tf_decision.proof_kind == "scheduler-profile-exhaustive-v1"
    assert ui_waiver.violation_observed
    assert ui_waiver.item_keys
    assert ui_waiver.attestation_sha256
    assert CONSTRAINT_MECHANIC_STREAK not in result.diagnostics.relaxed_constraints

    report = validate_ordered_unit(
        OrderedUnit(
            "unit/ui-impossible",
            result.ordered,
            relaxations=result.diagnostics.relaxations,
        )
    )
    assert not [
        issue
        for issue in report.issues
        if issue.code == "constraint_relaxation_invalid"
    ]
    ui_issues = [
        issue for issue in report.issues if issue.code == CONSTRAINT_UI_FAMILY_STREAK
    ]
    assert ui_issues and all(issue.severity == "warning" for issue in ui_issues)


def test_same_concept_adjacency_is_prevented_when_feasible():
    items = [
        _item("a1", "single_choice", concept="a"),
        _item("a2", "fill_gaps", concept="a", rung=3),
        _item("b1", "multi_choice", concept="b"),
        _item("c1", "true_false", concept="c", rung=2, answer=False),
        _item("c2", "rearrange", concept="c", rung=3),
    ]
    result = compose_experience(items, seed=12)
    assert all(
        left.concept_id != right.concept_id
        for left, right in zip(result.ordered, result.ordered[1:])
    )


def test_variant_selection_prefers_unseen_and_balances_mechanic_tf_and_position():
    groups = []
    for concept_index in range(4):
        concept = f"c{concept_index}"
        groups.append(
            [
                _item(f"{concept}/r1/v1", "single_choice", concept=concept, variant=1, position=(0,)),
                _item(f"{concept}/r1/v2", "multi_choice", concept=concept, variant=2, position=(1, 2)),
            ]
        )
    for concept_index in range(4, 8):
        concept = f"c{concept_index}"
        groups.append(
            [
                _item(f"{concept}/r2/v1", "true_false", concept=concept, rung=2, variant=1, answer=False),
                _item(f"{concept}/r2/v2", "true_false", concept=concept, rung=2, variant=2, answer=True),
            ]
        )
    selected = select_variants(
        groups,
        seed=901,
        scope="variants",
        seen_item_keys={"c0/r1/v1"},
    )
    assert selected.selected[0].item_key == "c0/r1/v2"
    assert Counter(item.mechanic for item in selected.selected[:4]) == {
        "single_choice": 2,
        "multi_choice": 2,
    }
    assert Counter(item.true_false_answer for item in selected.selected[4:]) == {
        False: 2,
        True: 2,
    }
    assert len(selected.unused_item_keys) == len(groups)


def test_true_false_sequence_avoids_strict_alternation_when_choices_exist():
    items = [
        _item(f"tf-{i}", "true_false", concept=f"c-{i}", rung=2, answer=(i % 2 == 0))
        for i in range(8)
    ]
    result = compose_experience(items, seed=9)
    answers = [item.true_false_answer for item in result.ordered]
    assert _max_run(answers) <= 2
    assert not all(left != right for left, right in zip(answers, answers[1:]))


def test_final_validator_reports_exact_paths_and_ui_family_separately():
    items = tuple(
        [
            _item("q1", "single_choice", concept="a", position=(0,), prompt="Which service is correct now"),
            _item("q2", "single_choice", concept="a", position=(0,), prompt="Which service is correct here"),
            _item("q3", "single_choice", concept="b", position=(0,), prompt="Which service is correct today"),
            _item("q4", "multi_choice", concept="c", position=(0,), prompt="Which service is correct again"),
        ]
    )
    report = validate_ordered_unit(OrderedUnit("modules/m/units/u", items))
    codes = Counter(issue.code for issue in report.issues)
    assert codes["mechanic_streak"] == 1
    assert codes["ui_family_streak"] == 1
    assert codes["concept_adjacency"] == 1
    assert codes["correct_option_position_streak"] == 1
    assert all(issue.item_paths for issue in report.issues)
    assert report.metrics.maximum_mechanic_streak == 3
    assert report.metrics.maximum_ui_family_streak == 4


def test_relaxed_violation_is_warning_but_unexplained_violation_blocks():
    items = tuple(_item(f"q{i}", "true_false", rung=2, answer=True) for i in range(5))
    composed = compose_experience(items, seed=3)
    relaxed_report = validate_ordered_units(
        [OrderedUnit("unit/relaxed", composed.ordered, relaxations=composed.diagnostics.relaxations)]
    )
    unexplained_report = validate_ordered_units([OrderedUnit("unit/broken", items)])
    assert relaxed_report.ok
    assert not unexplained_report.ok
    assert all(issue.severity == "warning" for issue in relaxed_report.issues)


def test_final_validator_rejects_forged_relaxation_evidence():
    items = tuple(_item(f"q{i}", "true_false", rung=2, answer=True) for i in range(5))
    composed = compose_experience(items, seed=3)
    forged = list(composed.diagnostics.relaxations)
    forged[0] = replace(
        forged[0],
        observed=1,
        item_keys=(),
        violation_observed=False,
    )
    report = validate_ordered_units(
        [OrderedUnit("unit/forged", composed.ordered, relaxations=tuple(forged))]
    )
    assert not report.ok
    invalid = [
        issue for issue in report.issues if issue.code == "constraint_relaxation_invalid"
    ]
    assert invalid and all(issue.severity == "error" for issue in invalid)


def test_exact_looking_manual_relaxation_cannot_waive_a_hard_failure():
    items = tuple(_item(f"q{i}", "true_false", rung=2, answer=True) for i in range(5))
    manual = ConstraintRelaxation(
        constraint=CONSTRAINT_TF_ANSWER_STREAK,
        reason="manual claim that the pool is impossible",
        item_keys=tuple(item.item_key for item in items),
        observed=5,
        configured=2,
        violation_observed=True,
    )
    report = validate_ordered_unit(
        OrderedUnit("unit/manual", items, relaxations=(manual,))
    )
    assert any(
        issue.code == "constraint_relaxation_invalid"
        and "attestation" in issue.message
        for issue in report.issues
    )
    tf_issues = [
        issue for issue in report.issues if issue.code == CONSTRAINT_TF_ANSWER_STREAK
    ]
    assert tf_issues and all(issue.severity == "error" for issue in tf_issues)


def test_search_bound_exhaustion_never_authorizes_relaxation():
    items = [
        _item("a", "single_choice"),
        _item("b", "fill_gaps", rung=3),
        _item("c", "true_false", rung=2, answer=False),
    ]
    policy = ExperiencePolicy(max_search_states=1)
    try:
        compose_experience(items, policy=policy, scope="tiny-bound")
    except RuntimeError as exc:
        message = str(exc)
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("bounded search unexpectedly relaxed a constraint")
    assert "Search exhaustion never authorizes a relaxation" in message
    assert "increase max_search_states" in message


def test_invalid_pinned_prefix_fails_instead_of_hiding_a_streak():
    items = [
        _item("a", "single_choice"),
        _item("b", "multi_choice"),
        _item("c", "single_choice"),
        _item("separator", "fill_gaps", rung=3),
    ]
    try:
        compose_experience(items, pinned_item_keys=("a", "b", "c"))
    except ValueError as exc:
        assert "pinned prefix violates a hard experience constraint" in str(exc)
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("invalid pinned prefix unexpectedly scheduled")


def test_legacy_relaxation_order_inserts_ui_before_original_mechanic():
    policy = ExperiencePolicy(
        relaxation_order=(
            CONSTRAINT_MECHANICS_WINDOW,
            CONSTRAINT_TF_ANSWER_STREAK,
            CONSTRAINT_MECHANIC_STREAK,
            "concept_adjacency",
        )
    )
    assert policy.relaxation_order.index(CONSTRAINT_UI_FAMILY_STREAK) + 1 == (
        policy.relaxation_order.index(CONSTRAINT_MECHANIC_STREAK)
    )


def test_unknown_runtime_mechanics_do_not_create_false_mechanic_failure():
    items = tuple(_item(f"q{i}", "unknown", concept=f"c{i}", rung=(i % 3) + 1) for i in range(8))
    result = compose_experience(items, seed=0)
    assert CONSTRAINT_MECHANIC_STREAK not in result.diagnostics.relaxed_constraints
    report = validate_ordered_unit(OrderedUnit("unit/unknown", result.ordered))
    hard_streak_codes = {
        CONSTRAINT_MECHANIC_STREAK,
        CONSTRAINT_UI_FAMILY_STREAK,
    }
    assert not [issue for issue in report.issues if issue.code in hard_streak_codes]


def test_malformed_emitted_experience_metadata_becomes_structured_errors():
    questions = [
        TLQuestion(
            import_key="bad-answer",
            question_type="multiple_choice",
            question_text="Choose one",
            correct_answer="not-an-index",
            options={
                "item_key": "bad-answer",
                "rung": 1,
                "variant": 1,
                "original_question_type": "single_choice",
                "options": [],
            },
        ),
        TLQuestion(
            import_key="bad-rung",
            question_type="true_false",
            question_text="True or false",
            correct_answer="true",
            options={
                "item_key": "bad-rung",
                "rung": "not-a-rung",
                "variant": 1,
                "original_question_type": "true_false",
            },
        ),
        TLQuestion(
            import_key="bad-status",
            question_type="true_false",
            question_text="True or false",
            correct_answer="false",
            options={
                "item_key": "bad-status",
                "rung": 2,
                "variant": 1,
                "original_question_type": "true_false",
                "learning_status": "invented",
            },
        ),
    ]
    course = TLCourse(
        import_key="course",
        title="Course",
        modules=[
            TLModule(
                import_key="module",
                title="Module",
                lessons=[
                    TLUnit(
                        import_key="unit",
                        title="Unit",
                        slo="Learn",
                        exercises=questions,
                    )
                ],
            )
        ],
    )
    report = validate_tl_course(course)
    metadata_issues = [
        issue
        for issue in report.issues
        if issue.code == "experience_metadata_invalid"
    ]
    assert not report.ok
    assert len(metadata_issues) == 3
    assert all(issue.severity == "error" for issue in metadata_issues)
    assert all(issue.item_paths for issue in metadata_issues)
    assert report.summary["questions"] == 3
    assert report.units[0].issues[:3] == tuple(metadata_issues)
