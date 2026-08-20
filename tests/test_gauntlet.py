"""Deterministic tests for the isolated qualitative Gauntlet core.

All critic/editor/comparison backends are in-memory fakes.  No live model or
paid API is used.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from techlingo_workflow.gauntlet import (
    BlindComparator,
    CriticEvidenceError,
    EditorScopeError,
    GauntletConfig,
    IndependentCritic,
    QualitativeGauntlet,
    TargetedEditor,
    default_critic_rubric,
)
from techlingo_workflow.gauntlet_models import (
    ALL_QUALITY_DIMENSIONS,
    ArtifactItem,
    ArtifactSnapshot,
    BlindVerdict,
    BlindWinner,
    ComparisonDecision,
    CriticBackendResponse,
    CriticEvidence,
    CriticResult,
    DimensionAssessment,
    DimensionExpectation,
    HardGateIssue,
    HardGateResult,
    HumanAction,
    HumanDecision,
    ModelIndependence,
    PairDimensionScore,
    QualityDimension,
    QualityGap,
    ReferenceContext,
    ReferenceSession,
    ReferenceStatus,
    RepairScope,
    SourceExcerpt,
    StopReason,
    TargetedEditResult,
    UsageRecord,
)
from techlingo_workflow.references import (
    ReferenceError,
    approved_references,
    create_reference_draft,
    load_reference,
    promote_reference,
    promote_reference_file,
    write_reference,
)


def _run(awaitable):
    return asyncio.run(awaitable)


def _artifact(
    *,
    quality: float = 0.6,
    fidelity: float = 0.8,
    prompt: str = "Original prompt",
    invalid: bool = False,
    order: tuple[str, ...] = ("q1", "q2", "q3"),
) -> ArtifactSnapshot:
    payloads = {
        "q1": {
            "prompt": prompt,
            "quality": quality,
            "fidelity": fidelity,
            "invalid": invalid,
            "item_key": "secret-nested-id",
            "nested": {"model_id": "builder-secret", "concept_id": "c1"},
        },
        "q2": {"prompt": "Second prompt", "quality": quality, "fidelity": fidelity},
        "q3": {"prompt": "Third prompt", "quality": quality, "fidelity": fidelity},
    }
    return ArtifactSnapshot(
        artifact_id=f"artifact-{prompt}",
        course_id="course-1",
        session_id="lesson-1",
        items=[
            ArtifactItem(item_key=key, path=f"units[0].items[{key}]", payload=payloads[key])
            for key in order
        ],
        metadata={"audience": "beginner"},
    )


def _source() -> list[SourceExcerpt]:
    return [SourceExcerpt(source_id="source-1", text="Grounded source material.")]


def _gap(
    *,
    scope: RepairScope = RepairScope.item,
    fields: list[str] | None = None,
) -> QualityGap:
    return QualityGap(
        dimension=QualityDimension.answer_unambiguity,
        summary="The first question needs a narrower prompt.",
        evidence=[CriticEvidence(statement="The stem permits two readings.", item_keys=["q1"])],
        affected_item_keys=[] if scope is RepairScope.session else ["q1"],
        affected_paths=["units[0].items[q1]"],
        recommended_scope=scope,
        repair_instruction="Make the smallest safe correction.",
        allowed_payload_fields=(fields if fields is not None else ["prompt", "quality", "fidelity"]),
    )


def _critic_result(
    *,
    score: float = 0.55,
    confidence: float = 0.9,
    gap: QualityGap | None | bool = True,
    human_review: bool = False,
) -> CriticResult:
    if gap is True:
        gap = _gap()
    return CriticResult(
        dimensions=[
            DimensionAssessment(
                dimension=dimension,
                score=score,
                evidence=[CriticEvidence(statement=f"Observed {dimension.value}.")],
            )
            for dimension in ALL_QUALITY_DIMENSIONS
        ],
        largest_gap=gap,
        confidence=confidence,
        human_review_recommended=human_review,
        concise_summary="Concise observable findings only.",
    )


def _verdict(request, *, force_winner: BlindWinner | None = None, usage=None) -> BlindVerdict:
    qa = float(request.candidate_a.ordered_items[0].get("quality", 0.0))
    qb = float(request.candidate_b.ordered_items[0].get("quality", 0.0))
    fa = float(request.candidate_a.ordered_items[0].get("fidelity", qa))
    fb = float(request.candidate_b.ordered_items[0].get("fidelity", qb))
    winner = force_winner
    if winner is None:
        winner = BlindWinner.a if qa > qb else BlindWinner.b if qb > qa else BlindWinner.tie
    return BlindVerdict(
        winner=winner,
        confidence=0.95,
        margin=abs(qa - qb),
        dimensions=[
            PairDimensionScore(
                dimension=dimension,
                score_a=fa if dimension is QualityDimension.factual_fidelity else qa,
                score_b=fb if dimension is QualityDimension.factual_fidelity else qb,
                evidence=f"Compared {dimension.value} in the exact ordered sessions.",
            )
            for dimension in ALL_QUALITY_DIMENSIONS
        ],
        evidence=["Candidate contents were compared without version labels."],
        usage=usage or UsageRecord(backend_calls=1),
    )


class FakeCritic:
    name = "fake-critic"
    model_label = "critic:model"
    fresh_context = True

    def __init__(self, results, usages=None):
        self.results = list(results)
        self.usages = list(usages or [])
        self.requests = []

    async def evaluate(self, request):
        self.requests.append(request)
        result = self.results.pop(0)
        usage = self.usages.pop(0) if self.usages else UsageRecord(backend_calls=1)
        return CriticBackendResponse(result=result, usage=usage)


class FakeEditor:
    name = "fake-editor"
    model_label = "editor:model"
    fresh_context = True

    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    async def repair(self, request):
        self.requests.append(request)
        return self.results.pop(0)


class QualityComparator:
    name = "fake-comparator"
    model_label = "comparator:model"
    fresh_context = True

    def __init__(self, *, always_a: bool = False):
        self.always_a = always_a
        self.requests = []

    async def compare(self, request):
        self.requests.append(request)
        return _verdict(request, force_winner=BlindWinner.a if self.always_a else None)


def _edit(
    champion: ArtifactSnapshot,
    *,
    updates: dict | None = None,
    order: tuple[str, ...] | None = None,
    usage: UsageRecord | None = None,
) -> TargetedEditResult:
    challenger = champion.model_copy(deep=True)
    challenger.artifact_id = "challenger"
    changed_fields: list[str] = []
    if updates:
        item = next(item for item in challenger.items if item.item_key == "q1")
        for key, value in updates.items():
            item.payload[key] = value
        changed_fields = list(updates)
    if order:
        by_key = {item.item_key: item for item in challenger.items}
        challenger.items = [by_key[key] for key in order]
    return TargetedEditResult(
        challenger=challenger,
        change_summary="Applied one bounded repair.",
        touched_item_keys=["q1"] if updates else [],
        touched_payload_fields={"q1": changed_fields} if updates else {},
        order_changed=order is not None,
        usage=usage or UsageRecord(backend_calls=1),
    )


def _hard_gate(calls=None):
    def gate(artifact):
        if calls is not None:
            calls.append(artifact.content_hash())
        invalid = any(item.payload.get("invalid") for item in artifact.items)
        if invalid:
            return HardGateResult(
                passed=False,
                issues=[HardGateIssue(code="invalid", path="q1", message="Invalid answer.")],
            )
        return HardGateResult(passed=True, checks={"schema": True, "answers": True})

    return gate


def _config(**updates) -> GauntletConfig:
    values = {
        "critic_backend": "fake-critic",
        "critic_model": "critic:model",
        "max_rounds": 2,
        "plateau_rounds": 5,
        "repeated_loss_rounds": 2,
        "minimum_improvement_margin": 0.02,
        "confidence_threshold": 0.65,
        "human_review_threshold": 0.4,
    }
    values.update(updates)
    return GauntletConfig(**values)


def _runner(config, critic_backend, editor_backend, comparison_backend, *, gate=None, human=None, monotonic=None):
    critic = IndependentCritic(critic_backend, builder_model=config.builder_model)
    editor = TargetedEditor(editor_backend)
    comparator = BlindComparator(comparison_backend, config)
    kwargs = {}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return QualitativeGauntlet(
        config=config,
        critic=critic,
        editor=editor,
        comparator=comparator,
        hard_gate=gate or _hard_gate(),
        human_control=human,
        **kwargs,
    )


def test_config_round_trips_plain_mapping_and_rejects_unknown_fields():
    config = GauntletConfig.from_mapping(
        {
            "critic_backend": "fake-critic",
            "critic_model": "critic:model",
            "max_rounds": 7,
            "max_tokens": 900,
            "protected_dimensions": ["factual_fidelity"],
            "quality_thresholds": {
                dimension.value: 0.75 for dimension in ALL_QUALITY_DIMENSIONS
            },
        }
    )
    assert config.max_rounds == 7 and config.max_tokens == 900
    assert config.protected_dimensions == (QualityDimension.factual_fidelity,)
    assert GauntletConfig.from_mapping(config.to_mapping()) == config
    with pytest.raises(Exception):
        GauntletConfig.from_mapping({"not_a_field": True})


def test_artifact_hash_tracks_order_and_content_but_not_snapshot_label():
    first = _artifact()
    renamed = first.model_copy(update={"artifact_id": "another-label"}, deep=True)
    reordered = _artifact(order=("q2", "q1", "q3"))
    assert first.content_hash() == renamed.content_hash()
    assert first.content_hash() != reordered.content_hash()


def test_reference_draft_requires_explicit_human_promotion_and_atomic_file_flow():
    draft = create_reference_draft(
        reference_id="ref-1",
        context=ReferenceContext(course_id="course-1", lesson_id="lesson-1"),
        relevant_sources=_source(),
        artifact=_artifact(),
        annotations=["The sequence moves from recognition to application."],
        expected_dimensions={
            QualityDimension.cognitive_progression: DimensionExpectation(
                minimum_score=0.9, annotation="Rungs rise without mechanic blocks."
            )
        },
    )
    assert draft.status is ReferenceStatus.draft
    assert approved_references([draft]) == []
    with pytest.raises(ValidationError):
        ReferenceSession.model_validate(
            {**draft.model_dump(mode="json"), "status": "approved", "approval": None}
        )

    when = datetime(2026, 8, 20, tzinfo=timezone.utc)
    approved = promote_reference(draft, approved_by="Human Reviewer", approved_at=when)
    assert approved.status is ReferenceStatus.approved
    assert approved.approval.draft_content_hash == draft.content_hash()
    assert approved.content_hash() == draft.content_hash()

    with tempfile.TemporaryDirectory() as tmp:
        draft_path = Path(tmp) / "draft.json"
        approved_path = Path(tmp) / "approved.json"
        write_reference(draft_path, draft)
        assert load_reference(draft_path).status is ReferenceStatus.draft
        promoted = promote_reference_file(
            draft_path,
            approved_path,
            approved_by="Human Reviewer",
            approved_at=when,
        )
        assert load_reference(approved_path) == promoted
        with pytest.raises(ReferenceError):
            promote_reference_file(
                draft_path, draft_path, approved_by="Human Reviewer", approved_at=when
            )


def test_critic_is_fresh_structured_and_reports_missing_reference_confidence():
    draft = create_reference_draft(
        reference_id="draft",
        context=ReferenceContext(course_id="course-1"),
        relevant_sources=_source(),
        artifact=_artifact(),
        annotations=["Candidate only."],
    )
    backend = FakeCritic([_critic_result()])
    critic = IndependentCritic(backend, builder_model="critic:model")
    evaluation = _run(
        critic.evaluate(
            goal="Improve the actual session.",
            rubric=default_critic_rubric(),
            source_material=_source(),
            references=[draft],
            artifact=_artifact(),
        )
    )
    request = backend.requests[0]
    assert set(request.model_dump()) == {
        "goal",
        "rubric",
        "source_material",
        "approved_references",
        "actual_final_artifact",
        "reference_confidence_reduced",
    }
    assert request.approved_references == []
    assert evaluation.reference_confidence_reduced is True
    assert evaluation.model_independence is ModelIndependence.unavailable
    assert "history" not in json.dumps(request.model_dump(mode="json"))


def test_critic_output_requires_every_separate_dimension_and_forbids_cot_fields():
    payload = _critic_result().model_dump(mode="json")
    payload["dimensions"].pop()
    with pytest.raises(ValidationError):
        CriticResult.model_validate(payload)
    payload = _critic_result().model_dump(mode="json")
    payload["chain_of_thought"] = "private reasoning"
    with pytest.raises(ValidationError):
        CriticResult.model_validate(payload)


def test_critic_rejects_item_keys_and_paths_absent_from_exact_artifact():
    hallucinated = _gap().model_copy(
        update={
            "affected_item_keys": ["not-in-artifact"],
            "affected_paths": ["units[9].items[missing]"],
        }
    )
    critic = IndependentCritic(FakeCritic([_critic_result(gap=hallucinated)]))
    with pytest.raises(CriticEvidenceError, match="unknown item_key"):
        _run(
            critic.evaluate(
                goal="Evaluate exact evidence.",
                rubric=default_critic_rubric(),
                source_material=_source(),
                references=[],
                artifact=_artifact(),
            )
        )
def test_item_editor_enforces_target_keys_and_payload_field_allow_list():
    champion = _artifact()
    good = _edit(champion, updates={"prompt": "Narrowed prompt"})
    editor = TargetedEditor(FakeEditor([good]))
    result = _run(editor.repair(champion=champion, directive=_gap(fields=["prompt"]), source_material=_source()))
    assert result.challenger.items[0].payload["prompt"] == "Narrowed prompt"

    bad = _edit(champion, updates={"quality": 0.9})
    editor = TargetedEditor(FakeEditor([bad]))
    with pytest.raises(EditorScopeError, match="allow-list"):
        _run(editor.repair(champion=champion, directive=_gap(fields=["prompt"]), source_material=_source()))


def test_session_editor_can_only_reorder_exact_existing_items():
    champion = _artifact()
    reordered = _edit(champion, order=("q2", "q1", "q3"))
    editor = TargetedEditor(FakeEditor([reordered]))
    result = _run(
        editor.repair(
            champion=champion,
            directive=_gap(scope=RepairScope.session),
            source_material=_source(),
        )
    )
    assert [item.item_key for item in result.challenger.items] == ["q2", "q1", "q3"]

    rewritten = _edit(champion, updates={"prompt": "Rewritten"})
    editor = TargetedEditor(FakeEditor([rewritten]))
    with pytest.raises(EditorScopeError, match="cannot rewrite"):
        _run(
            editor.repair(
                champion=champion,
                directive=_gap(scope=RepairScope.session),
                source_material=_source(),
            )
        )


def test_blind_comparison_runs_both_orders_strips_ids_and_promotes_stably():
    config = _config()
    backend = QualityComparator()
    comparator = BlindComparator(backend, config)
    result = _run(
        comparator.compare(
            champion=_artifact(quality=0.5),
            challenger=_artifact(quality=0.9, prompt="better"),
            goal="Prefer the stronger session.",
            rubric=default_critic_rubric(),
            source_material=_source(),
            references=[],
            seed=17,
        )
    )
    assert len(backend.requests) == 2
    assert result.decision is ComparisonDecision.promote
    assert result.position_sensitive is False and result.stable is True
    first, second = backend.requests
    assert first.candidate_a == second.candidate_b
    assert first.candidate_b == second.candidate_a
    rendered = json.dumps(first.model_dump(mode="json"))
    assert "secret-nested-id" not in rendered
    assert "builder-secret" not in rendered
    assert "champion" not in rendered and "challenger" not in rendered


def test_mandatory_blind_keys_cannot_be_removed_by_configuration():
    config = _config(identifying_metadata_keys=frozenset())
    assert {"item_key", "import_key", "path", "candidate_role"} <= set(
        config.identifying_metadata_keys
    )
    backend = QualityComparator()
    _run(
        BlindComparator(backend, config).compare(
            champion=_artifact(quality=0.5),
            challenger=_artifact(quality=0.9, prompt="better"),
            goal="Prefer quality.",
            rubric=default_critic_rubric(),
            source_material=_source(),
            references=[],
            seed=3,
        )
    )
    rendered = json.dumps(backend.requests[0].model_dump(mode="json"))
    assert "secret-nested-id" not in rendered


def test_position_biased_comparison_is_unstable_and_retains_for_human_review():
    config = _config()
    result = _run(
        BlindComparator(QualityComparator(always_a=True), config).compare(
            champion=_artifact(quality=0.5),
            challenger=_artifact(quality=0.9, prompt="better"),
            goal="Prefer quality.",
            rubric=default_critic_rubric(),
            source_material=_source(),
            references=[],
            seed=1,
        )
    )
    assert result.position_sensitive is True
    assert result.decision is ComparisonDecision.human_review


def test_protected_dimension_regression_blocks_otherwise_better_challenger():
    config = _config()
    result = _run(
        BlindComparator(QualityComparator(), config).compare(
            champion=_artifact(quality=0.5, fidelity=0.9),
            challenger=_artifact(quality=0.9, fidelity=0.2, prompt="better overall"),
            goal="Prefer quality without factual regression.",
            rubric=default_critic_rubric(),
            source_material=_source(),
            references=[],
            seed=4,
        )
    )
    assert result.decision is ComparisonDecision.retain
    assert result.protected_regressions == [QualityDimension.factual_fidelity]


def test_loop_stops_immediately_on_qualitative_success():
    critic = FakeCritic([_critic_result(score=0.95, gap=None)])
    editor = FakeEditor([])
    comparator = QualityComparator()
    config = _config(qualitative_required_for_publication=True)
    outcome = _run(
        _runner(config, critic, editor, comparator).run(
            run_id="success",
            champion=_artifact(),
            goal="Excellent session.",
            source_material=_source(),
        )
    )
    assert outcome.history.stop_reason is StopReason.success
    assert outcome.history.publication_eligible is True
    assert outcome.history.coherence_errors() == []
    assert (
        outcome.history.rounds[-1].critic.artifact_sha256
        == outcome.history.final_champion_hash
    )
    replayed = outcome.history.model_copy(deep=True)
    replayed.rounds[-1].critic.goal_sha256 = "0" * 64
    assert any("context/goal" in error for error in replayed.coherence_errors())
    assert editor.requests == [] and comparator.requests == []


def test_optional_gauntlet_does_not_label_low_score_no_gap_as_eligible():
    outcome = _run(
        _runner(
            _config(qualitative_required_for_publication=False),
            FakeCritic([_critic_result(score=0.2, gap=None)]),
            FakeEditor([]),
            QualityComparator(),
        ).run(
            run_id="optional-low-score",
            champion=_artifact(),
            goal="Evaluate honestly.",
            source_material=_source(),
        )
    )
    assert outcome.history.stop_reason is StopReason.human_review_required
    assert outcome.history.publication_eligible is False
    assert outcome.history.human_review_recommended is True


def test_failed_hard_gate_is_authoritative_and_comparison_is_never_called():
    champion = _artifact()
    critic = FakeCritic([_critic_result()])
    edit = _edit(
        champion,
        updates={"prompt": "Broken challenger", "invalid": True},
    )
    editor = FakeEditor([edit])
    comparator = QualityComparator()
    gate_calls = []
    config = _config(repeated_loss_rounds=1)
    gap = _gap(fields=["prompt", "invalid"])
    critic.results = [_critic_result(gap=gap)]
    outcome = _run(
        _runner(config, critic, editor, comparator, gate=_hard_gate(gate_calls)).run(
            run_id="hard-gate",
            champion=champion,
            goal="Repair safely.",
            source_material=_source(),
        )
    )
    assert outcome.champion.content_hash() == champion.content_hash()
    assert outcome.history.stop_reason is StopReason.repeated_loss
    assert outcome.history.rounds[0].decision.value == "rejected_hard_gate"
    assert len(gate_calls) == 2
    assert comparator.requests == []


def test_worse_challenger_never_replaces_champion():
    champion = _artifact(quality=0.6)
    challenger = _edit(champion, updates={"prompt": "Different", "quality": 0.4})
    config = _config(repeated_loss_rounds=1)
    outcome = _run(
        _runner(
            config,
            FakeCritic(
                [_critic_result(), _critic_result(score=0.95, gap=None)]
            ),
            FakeEditor([challenger]),
            QualityComparator(),
        ).run(
            run_id="retain",
            champion=champion,
            goal="Do not regress.",
            source_material=_source(),
        )
    )
    assert outcome.champion.content_hash() == champion.content_hash()
    assert outcome.history.stop_reason is StopReason.repeated_loss


def test_valid_stable_challenger_is_promoted_but_round_cap_still_bounds_loop():
    champion = _artifact(quality=0.5)
    edit = _edit(champion, updates={"prompt": "Better", "quality": 0.9})
    config = _config(max_rounds=1)
    outcome = _run(
        _runner(
            config,
            FakeCritic(
                [_critic_result(), _critic_result(score=0.95, gap=None)]
            ),
            FakeEditor([edit]),
            QualityComparator(),
        ).run(
            run_id="promote",
            champion=champion,
            goal="Improve.",
            source_material=_source(),
        )
    )
    assert outcome.champion.content_hash() == edit.challenger.content_hash()
    assert outcome.history.rounds[0].decision.value == "promoted"
    assert outcome.history.stop_reason is StopReason.max_rounds
    assert outcome.history.coherence_errors() == []
    round_ = outcome.history.rounds[0]
    assert (round_.editor_backend, round_.editor_model, round_.editor_fresh_context) == (
        "fake-editor",
        "editor:model",
        True,
    )
    assert round_.hard_gate.artifact_sha256 == round_.challenger_hash
    assert round_.source_fidelity_critic.artifact_sha256 == round_.challenger_hash
    assert round_.comparison.champion_sha256 == round_.champion_hash_before
    assert round_.comparison.challenger_sha256 == round_.challenger_hash
    assert (
        round_.comparison.comparator_backend,
        round_.comparison.comparator_model,
        round_.comparison.comparator_fresh_context,
    ) == ("fake-comparator", "comparator:model", True)
    replayed = outcome.history.model_copy(deep=True)
    replayed.rounds[0].comparison.challenger_sha256 = "0" * 64
    assert any("comparison is not bound" in error for error in replayed.coherence_errors())


def test_low_confidence_source_fidelity_rejects_content_edit_before_comparison():
    champion = _artifact(quality=0.5)
    edit = _edit(champion, updates={"prompt": "Changed", "quality": 0.9})
    comparator = QualityComparator()
    outcome = _run(
        _runner(
            _config(max_rounds=1),
            FakeCritic(
                [
                    _critic_result(),
                    _critic_result(score=0.95, confidence=0.5, gap=None),
                ]
            ),
            FakeEditor([edit]),
            comparator,
        ).run(
            run_id="low-confidence-fidelity",
            champion=champion,
            goal="Improve safely.",
            source_material=_source(),
        )
    )
    round_ = outcome.history.rounds[0]
    assert round_.decision.value == "rejected_source_fidelity"
    assert outcome.history.stop_reason is StopReason.human_review_required
    assert any(
        issue.code == "source_fidelity_confidence"
        for issue in round_.source_fidelity_gate.issues
    )
    assert comparator.requests == []


def test_plateau_stops_before_repeating_an_unproductive_repair():
    champion = _artifact(quality=0.6)
    low = _edit(champion, updates={"prompt": "No better", "quality": 0.4})
    critic = FakeCritic(
        [
            _critic_result(score=0.5),
            _critic_result(score=0.95, gap=None),
            _critic_result(score=0.5),
        ]
    )
    editor = FakeEditor([low])
    config = _config(max_rounds=4, plateau_rounds=1, repeated_loss_rounds=4)
    outcome = _run(
        _runner(config, critic, editor, QualityComparator()).run(
            run_id="plateau",
            champion=champion,
            goal="Try bounded repair.",
            source_material=_source(),
        )
    )
    assert outcome.history.stop_reason is StopReason.plateau
    assert len(editor.requests) == 1
    assert len(outcome.history.rounds) == 2


def test_repeated_losses_stop_and_retain_best_valid_champion():
    champion = _artifact(quality=0.6)
    low1 = _edit(champion, updates={"prompt": "Attempt one", "quality": 0.4})
    low2 = _edit(champion, updates={"prompt": "Attempt two", "quality": 0.3})
    config = _config(max_rounds=4, plateau_rounds=4, repeated_loss_rounds=2)
    outcome = _run(
        _runner(
            config,
            FakeCritic(
                [
                    _critic_result(),
                    _critic_result(score=0.95, gap=None),
                    _critic_result(),
                    _critic_result(score=0.95, gap=None),
                ]
            ),
            FakeEditor([low1, low2]),
            QualityComparator(),
        ).run(
            run_id="loss",
            champion=champion,
            goal="Try bounded repair.",
            source_material=_source(),
        )
    )
    assert outcome.history.stop_reason is StopReason.repeated_loss
    assert outcome.champion.content_hash() == champion.content_hash()
    assert len(outcome.history.rounds) == 2


@pytest.mark.parametrize(
    ("config_updates", "usage", "expected"),
    [
        ({"max_tokens": 10}, UsageRecord(input_tokens=10, backend_calls=1), StopReason.token_budget),
        ({"max_cost_usd": 0.25}, UsageRecord(cost_usd=0.25, backend_calls=1), StopReason.cost_budget),
    ],
)
def test_token_and_cost_budgets_stop_before_an_editor_call(config_updates, usage, expected):
    critic = FakeCritic([_critic_result()], usages=[usage])
    editor = FakeEditor([])
    config = _config(**config_updates)
    outcome = _run(
        _runner(config, critic, editor, QualityComparator()).run(
            run_id="budget",
            champion=_artifact(),
            goal="Stay bounded.",
            source_material=_source(),
        )
    )
    assert outcome.history.stop_reason is expected
    assert editor.requests == []


def test_time_budget_can_stop_before_any_backend_call():
    critic = FakeCritic([])
    config = _config(max_time_seconds=0)
    outcome = _run(
        _runner(config, critic, FakeEditor([]), QualityComparator()).run(
            run_id="time",
            champion=_artifact(),
            goal="Stay bounded.",
            source_material=_source(),
        )
    )
    assert outcome.history.stop_reason is StopReason.time_budget
    assert critic.requests == []


def test_explicit_human_approval_is_a_terminal_audited_stop():
    async def approve(round_number, champion, rounds):
        assert round_number == 0 and not rounds
        return HumanDecision(
            action=HumanAction.approve,
            reviewer="Course owner",
            note="Reviewed the exact final session.",
        )

    config = _config(qualitative_required_for_publication=True)
    outcome = _run(
        _runner(
            config,
            FakeCritic([]),
            FakeEditor([]),
            QualityComparator(),
            human=approve,
        ).run(
            run_id="human",
            champion=_artifact(),
            goal="Human review.",
            source_material=_source(),
        )
    )
    assert outcome.history.stop_reason is StopReason.human_approved
    assert outcome.history.publication_eligible is True
    assert outcome.history.human_decision.reviewer == "Course owner"
    assert "Reviewed" in outcome.history.stop_evidence
