"""Serializable contracts for qualitative course QA.

The models in this module deliberately describe only observable artifacts,
findings, decisions, and usage.  They contain no field for private reasoning or
agent scratchpads, which keeps persisted Gauntlet history suitable for audit.

The orchestration and backend protocols live in :mod:`techlingo_workflow.gauntlet`;
reference-file operations live in :mod:`techlingo_workflow.references`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


REFERENCE_SCHEMA_VERSION = "techlingo-reference-v1"
GAUNTLET_HISTORY_SCHEMA_VERSION = "techlingo-gauntlet-history-v1"
EVALUATION_CONTEXT_SCHEMA_VERSION = "techlingo-evaluation-context-v1"


def canonical_sha256(value: Any) -> str:
    """Hash JSON-like audit data with one stable, cross-module encoding."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    """Base class for externally persisted/backend-facing contracts."""

    model_config = ConfigDict(extra="forbid")


class QualityDimension(str, Enum):
    factual_fidelity = "factual_fidelity"
    answer_unambiguity = "answer_unambiguity"
    distractor_plausibility = "distractor_plausibility"
    misconception_quality = "misconception_quality"
    cognitive_progression = "cognitive_progression"
    mechanic_rhythm_variation = "mechanic_rhythm_variation"
    prompt_language_variation = "prompt_language_variation"
    scenario_authenticity = "scenario_authenticity"
    feedback_usefulness = "feedback_usefulness"
    difficulty_appropriateness = "difficulty_appropriateness"
    terminology_consistency = "terminology_consistency"
    overall_learner_experience = "overall_learner_experience"


ALL_QUALITY_DIMENSIONS: tuple[QualityDimension, ...] = tuple(QualityDimension)


class RepairScope(str, Enum):
    item = "item"
    session = "session"
    course = "course"


class HumanReviewReason(str, Enum):
    """Evidence-backed reasons that make automatic repair unsafe.

    This is optional for backward compatibility with immutable v1 histories.
    New live critic responses are required to pair it coherently with
    ``human_review_recommended`` before they can influence orchestration.
    """

    insufficient_authoritative_evidence = "insufficient_authoritative_evidence"
    conflicting_authoritative_evidence = "conflicting_authoritative_evidence"
    unresolvable_answer_ambiguity = "unresolvable_answer_ambiguity"
    unsafe_or_unbounded_repair = "unsafe_or_unbounded_repair"
    external_expertise_required = "external_expertise_required"


class ReferenceStatus(str, Enum):
    draft = "draft"
    approved = "approved"


class ModelIndependence(str, Enum):
    independent = "independent"
    unavailable = "unavailable"


class CandidateRole(str, Enum):
    champion = "champion"
    challenger = "challenger"
    tie = "tie"


class BlindWinner(str, Enum):
    a = "A"
    b = "B"
    tie = "tie"


class ComparisonDecision(str, Enum):
    promote = "promote"
    retain = "retain"
    human_review = "human_review"


class RoundDecision(str, Enum):
    promoted = "promoted"
    rejected_hard_gate = "rejected_hard_gate"
    rejected_source_fidelity = "rejected_source_fidelity"
    rejected_comparison = "rejected_comparison"
    retained_budget = "retained_budget"
    retained_human = "retained_human"
    retained_no_repair = "retained_no_repair"


class StopReason(str, Enum):
    success = "success"
    no_actionable_gap = "no_actionable_gap"
    plateau = "plateau"
    repeated_loss = "repeated_loss"
    max_rounds = "max_rounds"
    time_budget = "time_budget"
    token_budget = "token_budget"
    cost_budget = "cost_budget"
    human_approved = "human_approved"
    human_stop = "human_stop"
    human_review_required = "human_review_required"
    initial_hard_gate_failure = "initial_hard_gate_failure"


class HumanAction(str, Enum):
    continue_ = "continue"
    approve = "approve"
    stop = "stop"


class UsageRecord(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    backend_calls: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def add_usage(*records: UsageRecord) -> UsageRecord:
    return UsageRecord(
        input_tokens=sum(record.input_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
        cost_usd=sum(record.cost_usd for record in records),
        backend_calls=sum(record.backend_calls for record in records),
    )


class ArtifactItem(StrictModel):
    """One ordered learner-facing item and its stable audit location."""

    item_key: str = Field(
        min_length=1,
        description="Stable item identity; critics must copy this literal value when citing it.",
    )
    path: str = Field(
        min_length=1,
        description=(
            "Stable learner-artifact path; critics must copy this literal value when citing it, "
            "never a JSON navigation expression or source location."
        ),
    )
    payload: dict[str, Any]


class ArtifactSnapshot(StrictModel):
    """The exact ordered artifact shown to a learner."""

    artifact_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    items: list[ArtifactItem] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_item_identity(self) -> "ArtifactSnapshot":
        keys = [item.item_key for item in self.items]
        paths = [item.path for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("artifact item_key values must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("artifact item paths must be unique")
        return self

    def content_hash(self) -> str:
        """Hash content and order, excluding the human-facing snapshot label."""

        payload = self.model_dump(mode="json", exclude={"artifact_id"})
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class SourceExcerpt(StrictModel):
    source_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    location: str | None = None
    source_hash: str | None = None


class ReferenceContext(StrictModel):
    course_id: str = Field(min_length=1)
    course_title: str | None = None
    module_id: str | None = None
    module_title: str | None = None
    lesson_id: str | None = None
    lesson_title: str | None = None
    audience: str | None = None


class DimensionExpectation(StrictModel):
    minimum_score: float = Field(ge=0.0, le=1.0)
    annotation: str = Field(min_length=1)


class ReferenceApproval(StrictModel):
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    draft_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str | None = None

    @model_validator(mode="after")
    def timestamp_is_unambiguous(self) -> "ReferenceApproval":
        if self.approved_at.tzinfo is None:
            raise ValueError("approved_at must be timezone-aware")
        return self


class ReferenceSession(StrictModel):
    """Versioned, human-curatable exemplary session format.

    ``status='approved'`` is invalid without an explicit approval record.  A
    draft is never silently interpreted as an approved reference.
    """

    schema_version: Literal["techlingo-reference-v1"] = REFERENCE_SCHEMA_VERSION
    reference_id: str = Field(min_length=1)
    status: ReferenceStatus = ReferenceStatus.draft
    context: ReferenceContext
    relevant_sources: list[SourceExcerpt] = Field(min_length=1)
    final_ordered_questions: list[ArtifactItem] = Field(min_length=1)
    annotations: list[str] = Field(min_length=1)
    expected_dimensions: dict[QualityDimension, DimensionExpectation] = Field(
        default_factory=dict
    )
    known_weaknesses: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    approval: ReferenceApproval | None = None

    @model_validator(mode="after")
    def status_matches_approval(self) -> "ReferenceSession":
        if self.status is ReferenceStatus.approved and self.approval is None:
            raise ValueError("approved references require an explicit approval record")
        if self.status is ReferenceStatus.draft and self.approval is not None:
            raise ValueError("draft references cannot carry approval metadata")
        keys = [question.item_key for question in self.final_ordered_questions]
        if len(keys) != len(set(keys)):
            raise ValueError("reference question item_key values must be unique")
        if (
            self.status is ReferenceStatus.approved
            and self.approval is not None
            and self.approval.draft_content_hash != self.content_hash()
        ):
            raise ValueError(
                "approved reference content no longer matches the reviewed draft hash"
            )
        return self

    def content_hash(self) -> str:
        """Hash only the reviewable draft content, not status/approval metadata."""

        payload = self.model_dump(
            mode="json", exclude={"status", "approval", "schema_version"}
        )
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class CriticRubric(StrictModel):
    criteria: dict[QualityDimension, str]

    @model_validator(mode="after")
    def includes_every_dimension(self) -> "CriticRubric":
        present = set(self.criteria)
        expected = set(ALL_QUALITY_DIMENSIONS)
        if present != expected:
            missing = sorted(item.value for item in expected - present)
            extra = sorted(str(item) for item in present - expected)
            raise ValueError(
                f"rubric must define every quality dimension; missing={missing}, extra={extra}"
            )
        if any(not text.strip() for text in self.criteria.values()):
            raise ValueError("rubric criteria must be non-empty")
        return self


class EvaluationModelProvenance(StrictModel):
    """Actual model/backend roles used for one isolated evaluation run."""

    builder_model: str | None = None
    critic_backend: str = Field(min_length=1)
    critic_model: str = Field(min_length=1)
    critic_fresh_context: bool
    editor_backend: str = Field(min_length=1)
    editor_model: str = Field(min_length=1)
    editor_fresh_context: bool
    comparator_backend: str = Field(min_length=1)
    comparator_model: str = Field(min_length=1)
    comparator_fresh_context: bool


class EvaluationInputHash(StrictModel):
    """Stable identity and content hash for source/reference evidence."""

    input_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    location: str | None = None


class EvaluationContext(StrictModel):
    """Canonical policy and evidence context bound to qualitative approval.

    The complete policy snapshot is retained for audit.  Large source and
    reference bodies are represented by stable identities plus canonical
    hashes; the record therefore remains compact while publication can rebuild
    and compare the exact context from the current workspace.
    """

    schema_version: Literal["techlingo-evaluation-context-v1"] = (
        EVALUATION_CONTEXT_SCHEMA_VERSION
    )
    goal: str = Field(min_length=1)
    source_fidelity_goal: str = Field(min_length=1)
    gauntlet_policy: dict[str, Any]
    gauntlet_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric: CriticRubric
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: list[EvaluationInputHash]
    approved_references: list[EvaluationInputHash]
    models: EvaluationModelProvenance
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        goal: str,
        source_fidelity_goal: str,
        gauntlet_policy: Mapping[str, Any],
        rubric: CriticRubric,
        source_material: Sequence[SourceExcerpt],
        approved_reference_sessions: Sequence[ReferenceSession],
        models: EvaluationModelProvenance,
    ) -> "EvaluationContext":
        policy = json.loads(
            json.dumps(dict(gauntlet_policy), ensure_ascii=False, default=str)
        )
        for source in source_material:
            if source.source_hash is not None and source.source_hash != text_sha256(source.text):
                raise ValueError(
                    f"source hash does not match excerpt content: {source.source_id}"
                )
        if any(
            reference.status is not ReferenceStatus.approved
            for reference in approved_reference_sessions
        ):
            raise ValueError("evaluation context may bind only approved references")
        sources = sorted(
            (
                EvaluationInputHash(
                    input_id=source.source_id,
                    sha256=source.source_hash or text_sha256(source.text),
                    location=source.location,
                )
                for source in source_material
            ),
            key=lambda item: (item.input_id, item.location or "", item.sha256),
        )
        references = sorted(
            (
                EvaluationInputHash(
                    input_id=reference.reference_id,
                    sha256=canonical_sha256(reference),
                )
                for reference in approved_reference_sessions
            ),
            key=lambda item: (item.input_id, item.sha256),
        )
        if len({(item.input_id, item.location) for item in sources}) != len(sources):
            raise ValueError("evaluation source identities must be unique")
        if len({item.input_id for item in references}) != len(references):
            raise ValueError("approved reference ids must be unique")
        payload = {
            "schema_version": EVALUATION_CONTEXT_SCHEMA_VERSION,
            "goal": goal,
            "source_fidelity_goal": source_fidelity_goal,
            "gauntlet_policy": policy,
            "gauntlet_policy_sha256": canonical_sha256(policy),
            "rubric": rubric.model_dump(mode="json"),
            "rubric_sha256": canonical_sha256(rubric),
            "sources": [item.model_dump(mode="json") for item in sources],
            "approved_references": [item.model_dump(mode="json") for item in references],
            "models": models.model_dump(mode="json"),
        }
        return cls(**payload, context_sha256=canonical_sha256(payload))

    @model_validator(mode="after")
    def hashes_are_canonical(self) -> "EvaluationContext":
        if self.gauntlet_policy_sha256 != canonical_sha256(self.gauntlet_policy):
            raise ValueError("gauntlet_policy_sha256 does not match the policy snapshot")
        if self.rubric_sha256 != canonical_sha256(self.rubric):
            raise ValueError("rubric_sha256 does not match the rubric snapshot")
        payload = self.model_dump(mode="json", exclude={"context_sha256"})
        if self.context_sha256 != canonical_sha256(payload):
            raise ValueError("context_sha256 does not match the evaluation context")
        return self


class CriticEvidence(StrictModel):
    statement: str = Field(min_length=1)
    item_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Only literal item_key values copied from actual_final_artifact.items; empty for "
            "artifact-wide or source-only evidence."
        ),
    )
    paths: list[str] = Field(
        default_factory=list,
        description=(
            "Only literal path values copied from actual_final_artifact.items; never JSON paths, "
            "array indexes, payload field paths, or source filenames."
        ),
    )


class DimensionAssessment(StrictModel):
    dimension: QualityDimension
    score: float = Field(ge=0.0, le=1.0)
    evidence: list[CriticEvidence] = Field(min_length=1)


class CriticalDefect(StrictModel):
    defect_id: str = Field(min_length=1)
    dimension: QualityDimension
    summary: str = Field(min_length=1)
    evidence: list[CriticEvidence] = Field(min_length=1)
    affected_item_keys: list[str] = Field(
        default_factory=list,
        description="Literal item_key values copied from actual_final_artifact.items.",
    )
    affected_paths: list[str] = Field(
        default_factory=list,
        description="Literal path values copied from actual_final_artifact.items.",
    )
    recommended_scope: RepairScope

    @model_validator(mode="after")
    def identifies_affected_location(self) -> "CriticalDefect":
        if not self.affected_item_keys and not self.affected_paths:
            raise ValueError("critical defects require exact affected item keys or paths")
        return self


class QualityGap(StrictModel):
    dimension: QualityDimension
    summary: str = Field(min_length=1)
    evidence: list[CriticEvidence] = Field(min_length=1)
    affected_item_keys: list[str] = Field(
        default_factory=list,
        description="Literal item_key values copied from actual_final_artifact.items.",
    )
    affected_paths: list[str] = Field(
        default_factory=list,
        description="Literal path values copied from actual_final_artifact.items.",
    )
    recommended_scope: RepairScope
    repair_instruction: str = Field(min_length=1)
    allowed_payload_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def narrow_scope_has_targets(self) -> "QualityGap":
        if not self.affected_item_keys and not self.affected_paths:
            raise ValueError("largest gap requires exact affected item keys or paths")
        if self.recommended_scope in (RepairScope.item, RepairScope.course):
            if not self.affected_item_keys:
                raise ValueError(
                    f"{self.recommended_scope.value} repair requires explicit affected_item_keys"
                )
        if self.recommended_scope is RepairScope.item and not self.allowed_payload_fields:
            raise ValueError("item repair requires explicit allowed_payload_fields")
        return self


class CriticResult(StrictModel):
    dimensions: list[DimensionAssessment]
    critical_defects: list[CriticalDefect] = Field(default_factory=list)
    largest_gap: QualityGap | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    human_review_recommended: bool = False
    human_review_reason: HumanReviewReason | None = None
    concise_summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def exactly_one_assessment_per_dimension(self) -> "CriticResult":
        dimensions = [item.dimension for item in self.dimensions]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("critic result contains duplicate dimension assessments")
        present = set(dimensions)
        expected = set(ALL_QUALITY_DIMENSIONS)
        if present != expected:
            missing = sorted(item.value for item in expected - present)
            raise ValueError(f"critic result is missing dimensions: {missing}")
        return self

    def mean_score(self) -> float:
        return sum(item.score for item in self.dimensions) / len(self.dimensions)

    def scores(self) -> dict[QualityDimension, float]:
        return {item.dimension: item.score for item in self.dimensions}


class CriticRequest(StrictModel):
    """Fresh-context critic input; intentionally excludes builder history."""

    goal: str = Field(min_length=1)
    rubric: CriticRubric
    source_material: list[SourceExcerpt]
    approved_references: list[ReferenceSession]
    actual_final_artifact: ArtifactSnapshot
    reference_confidence_reduced: bool
    validation_feedback: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_are_really_approved(self) -> "CriticRequest":
        if any(ref.status is not ReferenceStatus.approved for ref in self.approved_references):
            raise ValueError("critic requests may include only approved references")
        if self.reference_confidence_reduced != (len(self.approved_references) == 0):
            raise ValueError(
                "reference_confidence_reduced must be true exactly when no approved reference exists"
            )
        return self


class CriticBackendResponse(StrictModel):
    result: CriticResult
    usage: UsageRecord = Field(default_factory=lambda: UsageRecord(backend_calls=1))


class CriticEvaluation(StrictModel):
    result: CriticResult
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluation_context_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    goal_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    critic_backend: str = Field(min_length=1)
    critic_model: str = Field(min_length=1)
    fresh_context: bool | None = None
    model_independence: ModelIndependence
    approved_reference_count: int = Field(ge=0)
    reference_confidence_reduced: bool
    usage: UsageRecord


class TargetedEditRequest(StrictModel):
    champion: ArtifactSnapshot
    directive: QualityGap
    source_material: list[SourceExcerpt]
    validation_feedback: list[str] = Field(default_factory=list)


class TargetedEditResult(StrictModel):
    challenger: ArtifactSnapshot
    change_summary: str = Field(min_length=1)
    touched_item_keys: list[str] = Field(default_factory=list)
    touched_payload_fields: dict[str, list[str]] = Field(default_factory=dict)
    order_changed: bool = False
    constraint_relaxations: list[str] = Field(default_factory=list)
    usage: UsageRecord = Field(default_factory=lambda: UsageRecord(backend_calls=1))


class BlindArtifact(StrictModel):
    """Candidate content visible to a comparison backend.

    Stable keys, paths, artifact ids, and champion/challenger labels are omitted.
    Item order remains significant.
    """

    ordered_items: list[dict[str, Any]] = Field(min_length=1)


class BlindReference(StrictModel):
    ordered_items: list[dict[str, Any]] = Field(min_length=1)
    annotations: list[str]
    expected_dimensions: dict[QualityDimension, DimensionExpectation]
    known_weaknesses: list[str]
    exceptions: list[str]


class BlindComparisonRequest(StrictModel):
    comparison_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    rubric: CriticRubric
    source_material: list[SourceExcerpt]
    approved_reference_standard: list[BlindReference]
    candidate_a: BlindArtifact
    candidate_b: BlindArtifact


class PairDimensionScore(StrictModel):
    dimension: QualityDimension
    score_a: float = Field(ge=0.0, le=1.0)
    score_b: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)


class BlindVerdict(StrictModel):
    winner: BlindWinner
    confidence: float = Field(ge=0.0, le=1.0)
    margin: float = Field(ge=0.0, le=1.0)
    dimensions: list[PairDimensionScore]
    evidence: list[str] = Field(min_length=1)
    human_review_recommended: bool = False
    usage: UsageRecord = Field(default_factory=lambda: UsageRecord(backend_calls=1))

    @model_validator(mode="after")
    def exactly_one_score_per_dimension(self) -> "BlindVerdict":
        dimensions = [item.dimension for item in self.dimensions]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("blind verdict contains duplicate dimension scores")
        if set(dimensions) != set(ALL_QUALITY_DIMENSIONS):
            missing = sorted(
                item.value for item in set(ALL_QUALITY_DIMENSIONS) - set(dimensions)
            )
            raise ValueError(f"blind verdict is missing dimensions: {missing}")
        return self


class PositionComparisonRecord(StrictModel):
    order_index: Literal[1, 2]
    seed: int
    a_role: Literal[CandidateRole.champion, CandidateRole.challenger]
    b_role: Literal[CandidateRole.champion, CandidateRole.challenger]
    mapped_winner: CandidateRole
    verdict: BlindVerdict


class PairwiseComparisonResult(StrictModel):
    champion_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    challenger_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluation_context_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    comparator_backend: str | None = None
    comparator_model: str | None = None
    comparator_fresh_context: bool | None = None
    records: list[PositionComparisonRecord] = Field(min_length=2, max_length=2)
    position_sensitive: bool
    stable: bool
    decision: ComparisonDecision
    protected_regressions: list[QualityDimension] = Field(default_factory=list)
    minimum_confidence: float = Field(ge=0.0, le=1.0)
    minimum_margin: float = Field(ge=0.0, le=1.0)
    decision_evidence: str = Field(min_length=1)
    usage: UsageRecord

    @model_validator(mode="after")
    def records_are_position_swapped(self) -> "PairwiseComparisonResult":
        first, second = self.records
        if (first.order_index, second.order_index) != (1, 2):
            raise ValueError("comparison records must be stored in order 1, 2")
        if first.a_role is not second.b_role or first.b_role is not second.a_role:
            raise ValueError("comparison records must use inverse A/B assignments")
        if first.seed != second.seed:
            raise ValueError("comparison records must use the same deterministic seed")
        observed_sensitive = first.mapped_winner is not second.mapped_winner
        if self.position_sensitive != observed_sensitive:
            raise ValueError("position_sensitive does not match the mapped verdicts")
        for record in self.records:
            expected_winner = (
                record.a_role
                if record.verdict.winner is BlindWinner.a
                else record.b_role
                if record.verdict.winner is BlindWinner.b
                else CandidateRole.tie
            )
            if record.mapped_winner is not expected_winner:
                raise ValueError("mapped comparison winner does not match the blind verdict")
        if self.minimum_confidence != min(record.verdict.confidence for record in self.records):
            raise ValueError("minimum_confidence does not match comparison records")
        if self.minimum_margin != min(record.verdict.margin for record in self.records):
            raise ValueError("minimum_margin does not match comparison records")
        if len(self.protected_regressions) != len(set(self.protected_regressions)):
            raise ValueError("protected_regressions cannot contain duplicates")
        if self.stable and (
            self.position_sensitive
            or any(record.verdict.human_review_recommended for record in self.records)
        ):
            raise ValueError("stable comparison cannot be position-sensitive or request human review")
        if self.usage != add_usage(*(record.verdict.usage for record in self.records)):
            raise ValueError("comparison usage does not match its two verdicts")
        return self


class HardGateIssue(StrictModel):
    code: str = Field(min_length=1)
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)


class HardGateResult(StrictModel):
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    passed: bool
    issues: list[HardGateIssue] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    constraint_relaxations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def passing_gate_has_no_issues(self) -> "HardGateResult":
        if self.passed and self.issues:
            raise ValueError("a passing hard gate cannot contain blocking issues")
        if not self.passed and not self.issues:
            raise ValueError("a failed hard gate must record at least one blocking issue")
        return self


class HumanDecision(StrictModel):
    action: HumanAction = HumanAction.continue_
    reviewer: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def human_actions_are_attributed(self) -> "HumanDecision":
        if self.action is not HumanAction.continue_ and not self.reviewer:
            raise ValueError("approve/stop decisions require a reviewer")
        return self


class RoundHistory(StrictModel):
    round_number: int = Field(ge=1)
    champion_hash_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    critic: CriticEvaluation | None = None
    critic_failure: str | None = Field(default=None, min_length=1)
    repair_scope: RepairScope | None = None
    repair_summary: str | None = None
    editor_backend: str | None = None
    editor_model: str | None = None
    editor_fresh_context: bool | None = None
    edit_usage: UsageRecord | None = None
    validation_retry_usage: UsageRecord = Field(default_factory=UsageRecord)
    touched_item_keys: list[str] = Field(default_factory=list)
    challenger_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Optional so immutable v1-v6 histories remain readable. New promoted
    # rounds persist both exact snapshots; hashes alone cannot reconstruct a
    # proposal after more than one promotion in the same run.
    promoted_champion_before: ArtifactSnapshot | None = None
    promoted_challenger: ArtifactSnapshot | None = None
    hard_gate: HardGateResult | None = None
    source_fidelity_critic: CriticEvaluation | None = None
    source_fidelity_gate: HardGateResult | None = None
    comparison: PairwiseComparisonResult | None = None
    decision: RoundDecision
    decision_evidence: str = Field(min_length=1)
    champion_hash_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    constraint_relaxations: list[str] = Field(default_factory=list)
    cumulative_usage: UsageRecord


class GauntletHistory(StrictModel):
    schema_version: Literal["techlingo-gauntlet-history-v1"] = (
        GAUNTLET_HISTORY_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    initial_champion_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_champion_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_context: EvaluationContext | None = None
    initial_hard_gate: HardGateResult
    rounds: list[RoundHistory] = Field(default_factory=list)
    stop_reason: StopReason
    stop_evidence: str = Field(min_length=1)
    human_decision: HumanDecision | None = None
    total_usage: UsageRecord
    human_review_recommended: bool = False
    publication_eligible: bool = False

    @staticmethod
    def _usage_not_less(current: UsageRecord, previous: UsageRecord) -> bool:
        return (
            current.input_tokens >= previous.input_tokens
            and current.output_tokens >= previous.output_tokens
            and current.cost_usd >= previous.cost_usd
            and current.backend_calls >= previous.backend_calls
        )

    def _critic_passes_bound_policy(self, critic: CriticEvaluation) -> bool:
        if self.evaluation_context is None:
            return False
        policy = self.evaluation_context.gauntlet_policy
        thresholds = policy.get("quality_thresholds")
        if not isinstance(thresholds, Mapping):
            return False
        try:
            confidence_threshold = float(policy["confidence_threshold"])
            expected = {
                QualityDimension(str(key)): float(value)
                for key, value in thresholds.items()
            }
        except (KeyError, TypeError, ValueError):
            return False
        result = critic.result
        scores = result.scores()
        return (
            not result.critical_defects
            and not result.human_review_recommended
            and result.confidence >= confidence_threshold
            and set(expected) == set(ALL_QUALITY_DIMENSIONS)
            and all(scores[dimension] >= threshold for dimension, threshold in expected.items())
            and critic.fresh_context is True
        )

    def _comparison_policy_result(
        self, comparison: PairwiseComparisonResult
    ) -> tuple[ComparisonDecision | None, set[QualityDimension], bool]:
        """Recompute a blind-comparison decision from persisted raw verdicts."""

        if self.evaluation_context is None:
            return None, set(), False
        policy = self.evaluation_context.gauntlet_policy
        try:
            confidence = float(policy["confidence_threshold"])
            margin = float(policy["minimum_improvement_margin"])
        except (KeyError, TypeError, ValueError):
            return None, set(), False
        try:
            protected = {
                QualityDimension(value)
                for value in policy.get("protected_dimensions", [])
            }
        except ValueError:
            return None, set(), False
        observed_regressions: set[QualityDimension] = set()
        for record in comparison.records:
            scores = {item.dimension: item for item in record.verdict.dimensions}
            for dimension in protected:
                pair = scores[dimension]
                challenger_score, champion_score = (
                    (pair.score_a, pair.score_b)
                    if record.a_role is CandidateRole.challenger
                    else (pair.score_b, pair.score_a)
                )
                if challenger_score < champion_score:
                    observed_regressions.add(dimension)
        backend_requested_human = any(
            record.verdict.human_review_recommended for record in comparison.records
        )
        stable = (
            not comparison.position_sensitive
            and not backend_requested_human
            and comparison.minimum_confidence >= confidence
        )
        if comparison.position_sensitive:
            decision = (
                ComparisonDecision.human_review
                if bool(policy.get("unstable_comparison_requires_human_review", True))
                else ComparisonDecision.retain
            )
        elif backend_requested_human or comparison.minimum_confidence < confidence:
            decision = ComparisonDecision.human_review
        elif comparison.records[0].mapped_winner is not CandidateRole.challenger:
            decision = ComparisonDecision.retain
        elif comparison.minimum_margin < margin or observed_regressions:
            decision = ComparisonDecision.retain
        else:
            decision = ComparisonDecision.promote
        return decision, observed_regressions, stable

    def _comparison_can_promote(self, comparison: PairwiseComparisonResult) -> bool:
        decision, observed_regressions, stable = self._comparison_policy_result(comparison)
        return bool(
            decision is ComparisonDecision.promote
            and comparison.decision is decision
            and comparison.stable is stable
            and set(comparison.protected_regressions) == observed_regressions
            and comparison.comparator_fresh_context is True
        )

    def _source_fidelity_passes(self, critic: CriticEvaluation) -> bool:
        if self.evaluation_context is None:
            return False
        thresholds = self.evaluation_context.gauntlet_policy.get("quality_thresholds")
        if not isinstance(thresholds, Mapping):
            return False
        try:
            threshold = float(thresholds[QualityDimension.factual_fidelity.value])
            confidence_threshold = float(
                self.evaluation_context.gauntlet_policy["confidence_threshold"]
            )
        except (KeyError, TypeError, ValueError):
            return False
        result = critic.result
        return (
            result.scores()[QualityDimension.factual_fidelity] >= threshold
            and result.confidence >= confidence_threshold
            and not any(
                defect.dimension is QualityDimension.factual_fidelity
                for defect in result.critical_defects
            )
            and not result.human_review_recommended
            and critic.fresh_context is True
        )

    def coherence_errors(self) -> list[str]:
        """Return structural audit failures without making legacy files unreadable."""

        errors: list[str] = []
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            errors.append("history timestamps must be timezone-aware")
        elif self.finished_at < self.started_at:
            errors.append("history finished_at precedes started_at")
        if not self.initial_hard_gate.passed and self.stop_reason is not StopReason.initial_hard_gate_failure:
            errors.append("failed initial hard gate requires initial_hard_gate_failure stop")
        if self.evaluation_context is not None and (
            self.initial_hard_gate.artifact_sha256 != self.initial_champion_hash
        ):
            errors.append("initial hard gate is not bound to the initial champion")

        expected_before = self.initial_champion_hash
        previous_usage = UsageRecord()
        for index, round_ in enumerate(self.rounds, start=1):
            prefix = f"round {index}"
            if round_.round_number != index:
                errors.append(f"{prefix}: round_number is not contiguous")
            if round_.champion_hash_before != expected_before:
                errors.append(f"{prefix}: champion hash chain is broken")
            if round_.critic is None:
                if round_.critic_failure is None:
                    errors.append(f"{prefix}: critic evaluation is missing")
                elif round_.decision is not RoundDecision.retained_human:
                    errors.append(
                        f"{prefix}: critic failure requires a retained_human decision"
                    )
            elif round_.critic_failure is not None:
                errors.append(f"{prefix}: critic evaluation and failure are both present")
            elif self.evaluation_context is not None:
                if round_.critic.artifact_sha256 != round_.champion_hash_before:
                    errors.append(f"{prefix}: critic evaluation is not bound to the champion")
                if (
                    round_.critic.evaluation_context_sha256
                    != self.evaluation_context.context_sha256
                    or round_.critic.goal_sha256
                    != text_sha256(self.evaluation_context.goal)
                ):
                    errors.append(
                        f"{prefix}: critic evaluation is not bound to the evaluation context/goal"
                    )
                models = self.evaluation_context.models
                if (
                    round_.critic.critic_backend != models.critic_backend
                    or round_.critic.critic_model != models.critic_model
                    or round_.critic.fresh_context != models.critic_fresh_context
                ):
                    errors.append(f"{prefix}: critic provenance does not match evaluation context")
                if round_.critic.approved_reference_count != len(
                    self.evaluation_context.approved_references
                ):
                    errors.append(f"{prefix}: approved reference count does not match context")
                if round_.critic.reference_confidence_reduced != (
                    len(self.evaluation_context.approved_references) == 0
                ):
                    errors.append(f"{prefix}: reference confidence flag does not match context")
            if round_.source_fidelity_critic is not None and self.evaluation_context is not None:
                if round_.source_fidelity_critic.artifact_sha256 != round_.challenger_hash:
                    errors.append(f"{prefix}: source-fidelity critic is not bound to the challenger")
                if (
                    round_.source_fidelity_critic.evaluation_context_sha256
                    != self.evaluation_context.context_sha256
                    or round_.source_fidelity_critic.goal_sha256
                    != text_sha256(self.evaluation_context.source_fidelity_goal)
                ):
                    errors.append(
                        f"{prefix}: source-fidelity critic is not bound to its context/goal"
                    )
                models = self.evaluation_context.models
                if (
                    round_.source_fidelity_critic.critic_backend != models.critic_backend
                    or round_.source_fidelity_critic.critic_model != models.critic_model
                    or round_.source_fidelity_critic.fresh_context
                    != models.critic_fresh_context
                ):
                    errors.append(f"{prefix}: source-fidelity critic provenance does not match context")
                if round_.source_fidelity_critic.approved_reference_count != len(
                    self.evaluation_context.approved_references
                ):
                    errors.append(f"{prefix}: source-fidelity reference count does not match context")
                if round_.source_fidelity_critic.reference_confidence_reduced != (
                    len(self.evaluation_context.approved_references) == 0
                ):
                    errors.append(
                        f"{prefix}: source-fidelity reference confidence flag does not match context"
                    )
            has_challenger = round_.challenger_hash is not None
            promoted_snapshots = (
                round_.promoted_champion_before,
                round_.promoted_challenger,
            )
            if any(snapshot is not None for snapshot in promoted_snapshots):
                if any(snapshot is None for snapshot in promoted_snapshots):
                    errors.append(f"{prefix}: promoted snapshot evidence is incomplete")
                elif round_.decision is not RoundDecision.promoted:
                    errors.append(f"{prefix}: non-promoted round contains promoted snapshots")
                else:
                    if (
                        round_.promoted_champion_before.content_hash()
                        != round_.champion_hash_before
                    ):
                        errors.append(
                            f"{prefix}: promoted before snapshot does not match champion hash"
                        )
                    if (
                        round_.promoted_challenger.content_hash()
                        != round_.challenger_hash
                    ):
                        errors.append(
                            f"{prefix}: promoted challenger snapshot does not match challenger hash"
                        )
            repair_values = (
                round_.repair_scope,
                round_.repair_summary,
                round_.editor_backend,
                round_.editor_model,
                round_.editor_fresh_context,
                round_.edit_usage,
            )
            if has_challenger and any(value is None for value in repair_values):
                errors.append(f"{prefix}: challenger lacks complete editor provenance/usage")
            elif not has_challenger and any(value is not None for value in repair_values):
                errors.append(f"{prefix}: editor state exists without a challenger")
            if has_challenger and round_.critic is not None:
                directive = round_.critic.result.largest_gap
                if directive is None or directive.recommended_scope is not round_.repair_scope:
                    errors.append(f"{prefix}: repair scope is not bound to the critic directive")
            if has_challenger and self.evaluation_context is not None:
                models = self.evaluation_context.models
                if (
                    round_.editor_backend != models.editor_backend
                    or round_.editor_model != models.editor_model
                    or round_.editor_fresh_context != models.editor_fresh_context
                ):
                    errors.append(f"{prefix}: editor provenance does not match evaluation context")
            if (round_.source_fidelity_critic is None) != (
                round_.source_fidelity_gate is None
            ):
                errors.append(f"{prefix}: source-fidelity critic/gate evidence is incomplete")
            if round_.hard_gate is not None and self.evaluation_context is not None:
                if round_.hard_gate.artifact_sha256 != round_.challenger_hash:
                    errors.append(f"{prefix}: challenger hard gate is not bound to the challenger")
            if round_.source_fidelity_gate is not None and self.evaluation_context is not None:
                if round_.source_fidelity_gate.artifact_sha256 != round_.challenger_hash:
                    errors.append(f"{prefix}: source-fidelity gate is not bound to the challenger")
            if round_.comparison is not None and self.evaluation_context is not None:
                if (
                    round_.comparison.champion_sha256 != round_.champion_hash_before
                    or round_.comparison.challenger_sha256 != round_.challenger_hash
                ):
                    errors.append(f"{prefix}: comparison is not bound to champion/challenger hashes")
                if (
                    round_.comparison.evaluation_context_sha256
                    != self.evaluation_context.context_sha256
                ):
                    errors.append(f"{prefix}: comparison is not bound to evaluation context")
                try:
                    expected_seed = int(
                        self.evaluation_context.gauntlet_policy["comparison_seed"]
                    ) + round_.round_number - 1
                except (KeyError, TypeError, ValueError):
                    errors.append(f"{prefix}: comparison has no valid bound seed policy")
                else:
                    if any(
                        record.seed != expected_seed
                        for record in round_.comparison.records
                    ):
                        errors.append(f"{prefix}: comparison seed does not match bound policy")
                models = self.evaluation_context.models
                if (
                    round_.comparison.comparator_backend != models.comparator_backend
                    or round_.comparison.comparator_model != models.comparator_model
                    or round_.comparison.comparator_fresh_context
                    != models.comparator_fresh_context
                ):
                    errors.append(
                        f"{prefix}: comparator provenance does not match evaluation context"
                    )
                expected_decision, expected_regressions, expected_stable = (
                    self._comparison_policy_result(round_.comparison)
                )
                if (
                    round_.comparison.decision is not expected_decision
                    or set(round_.comparison.protected_regressions)
                    != expected_regressions
                    or round_.comparison.stable is not expected_stable
                ):
                    errors.append(
                        f"{prefix}: comparison decision does not match its raw verdicts and policy"
                    )
            if round_.comparison is not None and (
                round_.hard_gate is None or not round_.hard_gate.passed
            ):
                errors.append(f"{prefix}: comparison ran without a passing challenger hard gate")

            if self.evaluation_context is not None and round_.critic is not None:
                expected_usage = add_usage(
                    previous_usage,
                    round_.critic.usage,
                    round_.edit_usage or UsageRecord(),
                    round_.validation_retry_usage,
                    (
                        round_.source_fidelity_critic.usage
                        if round_.source_fidelity_critic is not None
                        else UsageRecord()
                    ),
                    (
                        round_.comparison.usage
                        if round_.comparison is not None
                        else UsageRecord()
                    ),
                )
                if round_.cumulative_usage != expected_usage:
                    errors.append(f"{prefix}: cumulative usage does not match recorded calls")
            elif round_.critic_failure is not None:
                expected_usage = add_usage(
                    previous_usage,
                    round_.validation_retry_usage,
                )
                if round_.cumulative_usage != expected_usage:
                    errors.append(
                        f"{prefix}: failed-critic usage does not match recorded calls"
                    )
            elif not self._usage_not_less(round_.cumulative_usage, previous_usage):
                errors.append(f"{prefix}: cumulative usage decreased")

            unchanged = round_.champion_hash_after == round_.champion_hash_before
            if round_.decision is RoundDecision.promoted:
                if round_.challenger_hash is None or round_.champion_hash_after != round_.challenger_hash:
                    errors.append(f"{prefix}: promoted decision is not bound to the challenger hash")
                if round_.hard_gate is None or not round_.hard_gate.passed:
                    errors.append(f"{prefix}: promoted challenger lacks a passing hard gate")
                if round_.comparison is None or not self._comparison_can_promote(round_.comparison):
                    errors.append(f"{prefix}: promoted decision lacks a promoting comparison")
                if round_.source_fidelity_gate is not None and not round_.source_fidelity_gate.passed:
                    errors.append(f"{prefix}: promoted challenger failed source fidelity")
                if round_.touched_item_keys and (
                    round_.source_fidelity_critic is None
                    or round_.source_fidelity_gate is None
                    or not round_.source_fidelity_gate.passed
                ):
                    errors.append(f"{prefix}: content-edited promotion lacks source-fidelity evidence")
                elif round_.source_fidelity_critic is not None and (
                    not self._source_fidelity_passes(round_.source_fidelity_critic)
                    or round_.source_fidelity_gate is None
                    or not round_.source_fidelity_gate.passed
                ):
                    errors.append(f"{prefix}: source-fidelity gate disagrees with critic evidence")
            elif round_.decision is RoundDecision.rejected_hard_gate:
                if round_.challenger_hash is None or round_.hard_gate is None or round_.hard_gate.passed:
                    errors.append(f"{prefix}: hard-gate rejection is incoherent")
                if round_.comparison is not None or not unchanged:
                    errors.append(f"{prefix}: rejected hard-gate challenger changed the champion")
            elif round_.decision is RoundDecision.rejected_source_fidelity:
                if (
                    round_.challenger_hash is None
                    or round_.hard_gate is None
                    or not round_.hard_gate.passed
                    or round_.source_fidelity_critic is None
                    or round_.source_fidelity_gate is None
                    or round_.source_fidelity_gate.passed
                ):
                    errors.append(f"{prefix}: source-fidelity rejection is incoherent")
                elif self._source_fidelity_passes(round_.source_fidelity_critic):
                    errors.append(f"{prefix}: source-fidelity rejection contradicts critic evidence")
                if round_.comparison is not None or not unchanged:
                    errors.append(f"{prefix}: source-rejected challenger changed the champion")
            elif round_.decision is RoundDecision.rejected_comparison:
                if (
                    round_.challenger_hash is None
                    or round_.hard_gate is None
                    or not round_.hard_gate.passed
                    or round_.comparison is None
                    or round_.comparison.decision is not ComparisonDecision.retain
                ):
                    errors.append(f"{prefix}: comparison rejection is incoherent")
                if not unchanged:
                    errors.append(f"{prefix}: rejected comparison changed the champion")
                if round_.touched_item_keys and (
                    round_.source_fidelity_critic is None
                    or round_.source_fidelity_gate is None
                    or not round_.source_fidelity_gate.passed
                ):
                    errors.append(f"{prefix}: compared content edit lacks passing source fidelity")
                elif round_.source_fidelity_critic is not None and not self._source_fidelity_passes(
                    round_.source_fidelity_critic
                ):
                    errors.append(f"{prefix}: comparison used failed source-fidelity evidence")
            elif round_.decision is RoundDecision.retained_human:
                if round_.comparison is not None and round_.comparison.decision is not ComparisonDecision.human_review:
                    errors.append(f"{prefix}: retained_human comparison is incoherent")
                if not unchanged:
                    errors.append(f"{prefix}: human-review decision changed the champion")
                if round_.comparison is not None and round_.touched_item_keys and (
                    round_.source_fidelity_critic is None
                    or round_.source_fidelity_gate is None
                    or not round_.source_fidelity_gate.passed
                ):
                    errors.append(f"{prefix}: compared content edit lacks passing source fidelity")
                elif round_.comparison is not None and round_.source_fidelity_critic is not None and not self._source_fidelity_passes(
                    round_.source_fidelity_critic
                ):
                    errors.append(f"{prefix}: comparison used failed source-fidelity evidence")
            elif round_.decision is RoundDecision.retained_no_repair:
                if any(
                    value is not None
                    for value in (
                        round_.challenger_hash,
                        round_.hard_gate,
                        round_.source_fidelity_critic,
                        round_.source_fidelity_gate,
                        round_.comparison,
                    )
                ) or not unchanged:
                    errors.append(f"{prefix}: no-repair decision contains challenger state")
            elif round_.decision is RoundDecision.retained_budget and not unchanged:
                errors.append(f"{prefix}: budget stop changed the champion")

            expected_before = round_.champion_hash_after
            previous_usage = round_.cumulative_usage

        if expected_before != self.final_champion_hash:
            errors.append("final champion hash does not close the round hash chain")
        if self.rounds:
            if self.rounds[-1].cumulative_usage != self.total_usage:
                errors.append("total usage does not match the final cumulative usage")
        elif self.total_usage != UsageRecord():
            errors.append("history without rounds cannot report backend usage")

        if self.stop_reason is StopReason.initial_hard_gate_failure:
            if self.initial_hard_gate.passed or self.rounds:
                errors.append("initial_hard_gate_failure stop is inconsistent")
        elif self.stop_reason is StopReason.success:
            if not self.rounds or self.rounds[-1].decision is not RoundDecision.retained_no_repair:
                errors.append("success stop requires a final no-repair critic round")
            elif self.rounds[-1].critic is None or not self._critic_passes_bound_policy(
                self.rounds[-1].critic
            ):
                errors.append("success stop lacks a threshold-passing fresh critic")
        elif self.stop_reason is StopReason.no_actionable_gap:
            if not self.rounds or self.rounds[-1].decision is not RoundDecision.retained_no_repair:
                errors.append("no_actionable_gap stop requires a final no-repair critic round")
            elif (
                self.rounds[-1].critic is None
                or self.rounds[-1].critic.result.largest_gap is not None
                or self._critic_passes_bound_policy(self.rounds[-1].critic)
            ):
                errors.append("no_actionable_gap stop contradicts final critic evidence")
        elif self.stop_reason is StopReason.plateau:
            if not self.rounds or self.rounds[-1].decision is not RoundDecision.retained_no_repair:
                errors.append("plateau stop requires a final retained round")
            elif self.evaluation_context is not None:
                try:
                    required_plateau = int(
                        self.evaluation_context.gauntlet_policy["plateau_rounds"]
                    )
                    margin = float(
                        self.evaluation_context.gauntlet_policy[
                            "minimum_improvement_margin"
                        ]
                    )
                except (KeyError, TypeError, ValueError):
                    errors.append("plateau stop has no valid bound plateau policy")
                else:
                    scores = [
                        round_.critic.result.mean_score()
                        for round_ in self.rounds
                        if round_.critic is not None
                    ]
                    observed_plateau = 0
                    for before, after in zip(scores, scores[1:]):
                        observed_plateau = (
                            observed_plateau + 1
                            if after - before < margin
                            else 0
                        )
                    if observed_plateau < required_plateau:
                        errors.append("plateau stop occurred before the bound plateau limit")
        elif self.stop_reason is StopReason.repeated_loss:
            if not self.rounds or self.rounds[-1].decision not in {
                RoundDecision.rejected_hard_gate,
                RoundDecision.rejected_comparison,
            }:
                errors.append("repeated_loss stop requires a rejected challenger")
            elif self.evaluation_context is not None:
                try:
                    required_losses = int(
                        self.evaluation_context.gauntlet_policy["repeated_loss_rounds"]
                    )
                except (KeyError, TypeError, ValueError):
                    errors.append("repeated_loss stop has no valid bound loss policy")
                else:
                    observed_losses = 0
                    for round_ in reversed(self.rounds):
                        if round_.decision not in {
                            RoundDecision.rejected_hard_gate,
                            RoundDecision.rejected_comparison,
                        }:
                            break
                        observed_losses += 1
                    if observed_losses < required_losses:
                        errors.append("repeated_loss stop occurred before the bound loss limit")
        elif self.stop_reason is StopReason.human_review_required:
            if not self.rounds or self.rounds[-1].decision not in {
                RoundDecision.retained_human,
                RoundDecision.rejected_source_fidelity,
            }:
                errors.append("human_review_required stop lacks a human-review round")
            if not self.human_review_recommended:
                errors.append("human_review_required stop omits the human-review flag")
        elif self.stop_reason is StopReason.max_rounds and self.evaluation_context is not None:
            try:
                configured_rounds = int(
                    self.evaluation_context.gauntlet_policy["max_rounds"]
                )
            except (KeyError, TypeError, ValueError):
                errors.append("max_rounds stop has no valid bound round policy")
            else:
                if len(self.rounds) != configured_rounds:
                    errors.append("max_rounds stop does not match the bound round limit")
        elif self.stop_reason in {StopReason.token_budget, StopReason.cost_budget}:
            if self.evaluation_context is not None:
                policy = self.evaluation_context.gauntlet_policy
                key = (
                    "max_tokens"
                    if self.stop_reason is StopReason.token_budget
                    else "max_cost_usd"
                )
                observed = (
                    self.total_usage.total_tokens
                    if self.stop_reason is StopReason.token_budget
                    else self.total_usage.cost_usd
                )
                try:
                    configured = float(policy[key])
                except (KeyError, TypeError, ValueError):
                    errors.append(f"{self.stop_reason.value} stop has no valid bound budget")
                else:
                    if observed < configured:
                        errors.append(
                            f"{self.stop_reason.value} stop occurred before its bound budget"
                        )
        elif self.stop_reason is StopReason.time_budget and self.evaluation_context is not None:
            try:
                float(self.evaluation_context.gauntlet_policy["max_time_seconds"])
            except (KeyError, TypeError, ValueError):
                errors.append("time_budget stop has no valid bound budget")
        elif self.stop_reason in {StopReason.human_approved, StopReason.human_stop}:
            wanted = (
                HumanAction.approve
                if self.stop_reason is StopReason.human_approved
                else HumanAction.stop
            )
            if self.human_decision is None or self.human_decision.action is not wanted:
                errors.append("terminal human stop lacks its structured attributed decision")

        if (
            self.stop_reason not in {StopReason.human_approved, StopReason.human_stop}
            and self.human_decision is not None
        ):
            errors.append("human decision is present for a non-human stop")

        return errors

    def derived_publication_eligible(
        self,
        *,
        expected_context: EvaluationContext | None = None,
    ) -> bool:
        """Derive eligibility from evidence; never trust the stored boolean."""

        if self.evaluation_context is None or self.coherence_errors():
            return False
        if expected_context is not None and (
            self.evaluation_context.context_sha256 != expected_context.context_sha256
        ):
            return False
        if not self.initial_hard_gate.passed:
            return False
        if self.stop_reason is StopReason.human_approved:
            return bool(
                self.human_decision is not None
                and self.human_decision.action is HumanAction.approve
                and self.human_decision.reviewer
            )
        if self.stop_reason is not StopReason.success or not self.rounds:
            return False
        if not self.evaluation_context.sources:
            return False
        final_critic = self.rounds[-1].critic
        return bool(
            final_critic is not None
            and self.evaluation_context.models.critic_fresh_context
            and self._critic_passes_bound_policy(final_critic)
        )

    def publication_blockers(
        self,
        *,
        expected_context: EvaluationContext | None,
    ) -> list[str]:
        blockers = self.coherence_errors()
        if self.evaluation_context is None:
            blockers.append("history has no bound evaluation context")
        elif expected_context is None:
            blockers.append("current evaluation context was not supplied")
        elif self.evaluation_context.context_sha256 != expected_context.context_sha256:
            blockers.append("history evaluation context does not match current policy/evidence")
        derived = self.derived_publication_eligible(expected_context=expected_context)
        if not derived:
            blockers.append("history evidence does not derive qualitative publication eligibility")
        if self.publication_eligible != derived:
            blockers.append("stored publication_eligible flag does not match derived evidence")
        return list(dict.fromkeys(blockers))


class GauntletOutcome(StrictModel):
    champion: ArtifactSnapshot
    history: GauntletHistory
