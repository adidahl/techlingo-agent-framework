"""Independent qualitative critic, targeted repair, and champion retention.

This module is intentionally independent of the course compiler and workspace.
Callers adapt their exact final learner artifact into :class:`ArtifactSnapshot`
and supply an authoritative deterministic hard-gate callback.  No qualitative
backend can waive that callback's result.
"""

from __future__ import annotations

import inspect
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .backends import BackendTimeoutError, split_backend_label
from .gauntlet_models import (
    ALL_QUALITY_DIMENSIONS,
    ArtifactSnapshot,
    BlindArtifact,
    BlindComparisonRequest,
    BlindReference,
    BlindVerdict,
    BlindWinner,
    CandidateRole,
    ComparisonDecision,
    CriticBackendResponse,
    CriticEvaluation,
    CriticRequest,
    CriticResult,
    CriticRubric,
    EvaluationContext,
    EvaluationModelProvenance,
    GauntletHistory,
    GauntletOutcome,
    HardGateIssue,
    HardGateResult,
    HumanAction,
    HumanDecision,
    ModelIndependence,
    PairwiseComparisonResult,
    PositionComparisonRecord,
    QualityDimension,
    QualityGap,
    ReferenceSession,
    RepairScope,
    RoundDecision,
    RoundHistory,
    SourceExcerpt,
    StopReason,
    TargetedEditRequest,
    TargetedEditResult,
    UsageRecord,
    add_usage,
    text_sha256,
)
from .references import approved_references


DEFAULT_RUBRIC_CRITERIA: dict[QualityDimension, str] = {
    QualityDimension.factual_fidelity: "Every claim and answer is faithful to the supplied source.",
    QualityDimension.answer_unambiguity: "Each item has one defensible answer under its instructions.",
    QualityDimension.distractor_plausibility: "Distractors are credible without becoming defensible answers.",
    QualityDimension.misconception_quality: "Wrong choices diagnose realistic learner misconceptions.",
    QualityDimension.cognitive_progression: "The ordered session builds understanding at a coherent pace.",
    QualityDimension.mechanic_rhythm_variation: "Mechanics vary naturally without disruptive repetition.",
    QualityDimension.prompt_language_variation: "Prompt stems avoid formulaic repetition.",
    QualityDimension.scenario_authenticity: "Scenarios resemble credible decisions or tasks.",
    QualityDimension.feedback_usefulness: "Feedback explains the governing concept and next step.",
    QualityDimension.difficulty_appropriateness: "Difficulty matches the stated audience and session position.",
    QualityDimension.terminology_consistency: "Terms are used consistently with the source and across items.",
    QualityDimension.overall_learner_experience: "The exact final sequence is coherent, varied, and instructive.",
}

DEFAULT_GAUNTLET_GOAL = (
    "Produce an excellent, source-faithful learner session while preserving "
    "all deterministic correctness, identity, and sequence constraints."
)
SOURCE_FIDELITY_GOAL = (
    "Independently verify the hard-gated edited challenger against the supplied "
    "source material. Reject unsupported facts, answers, explanations, feedback, "
    "or terminology changes; do not edit the artifact."
)
EVALUATOR_PROTOCOL_VERSION = "critic-review-semantics-v6"
EDITOR_PROTECTED_PAYLOAD_FIELDS = frozenset({"question_type"})


MANDATORY_IDENTIFYING_METADATA_KEYS = frozenset(
    {
        "artifact_id",
        "item_key",
        "import_key",
        "path",
        "version_id",
        "builder_model",
        "critic_model",
        "model_id",
        "generated_at",
        "champion",
        "challenger",
        "candidate_role",
    }
)

# Backward-compatible public name.  Callers may add keys but mandatory role and
# stable-identity fields are never removable from blind comparison requests.
DEFAULT_IDENTIFYING_METADATA_KEYS = MANDATORY_IDENTIFYING_METADATA_KEYS


class GauntletConfigurationError(ValueError):
    """The policy cannot be applied safely."""


class EditorScopeError(ValueError):
    """An editor changed content outside its authorized narrow scope."""

    def __init__(self, message: str, *, usage: UsageRecord | None = None) -> None:
        super().__init__(message)
        self.usage = usage or UsageRecord()


class CriticEvidenceError(ValueError):
    """A critic cited an item identity/path absent from the evaluated artifact."""

    def __init__(self, message: str, *, usage: UsageRecord | None = None) -> None:
        super().__init__(message)
        self.usage = usage or UsageRecord()


@dataclass(frozen=True)
class GauntletConfig:
    """Policy and bounded-resource configuration.

    ``from_mapping`` accepts JSON/YAML-shaped values, including string keys for
    quality dimensions.  ``critic_backend`` and ``critic_model`` are optional
    only to preserve backward compatibility when qualitative QA is disabled;
    when supplied, the runner verifies them against the isolated critic.
    """

    critic_backend: str | None = None
    critic_model: str | None = None
    builder_model: str | None = None
    evaluator_protocol_version: str = EVALUATOR_PROTOCOL_VERSION
    max_rounds: int = 4
    plateau_rounds: int = 2
    repeated_loss_rounds: int = 2
    minimum_improvement_margin: float = 0.02
    confidence_threshold: float = 0.65
    human_review_threshold: float = 0.40
    max_time_seconds: float | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    qualitative_required_for_publication: bool = False
    quality_thresholds: Mapping[QualityDimension, float] = field(
        default_factory=lambda: {dimension: 0.80 for dimension in ALL_QUALITY_DIMENSIONS}
    )
    comparison_seed: int = 0
    protected_dimensions: tuple[QualityDimension, ...] = (
        QualityDimension.factual_fidelity,
        QualityDimension.answer_unambiguity,
    )
    unstable_comparison_requires_human_review: bool = True
    identifying_metadata_keys: frozenset[str] = DEFAULT_IDENTIFYING_METADATA_KEYS

    def __post_init__(self) -> None:
        if not self.evaluator_protocol_version.strip():
            raise GauntletConfigurationError("evaluator_protocol_version cannot be blank")
        if self.critic_backend is not None and not self.critic_backend.strip():
            raise GauntletConfigurationError("critic_backend cannot be blank")
        if self.critic_model is not None and not self.critic_model.strip():
            raise GauntletConfigurationError("critic_model cannot be blank")
        if self.qualitative_required_for_publication and (
            self.critic_backend is None or self.critic_model is None
        ):
            raise GauntletConfigurationError(
                "critic_backend and critic_model are required when qualitative QA gates publication"
            )
        if self.max_rounds < 1:
            raise GauntletConfigurationError("max_rounds must be at least 1")
        if self.plateau_rounds < 1:
            raise GauntletConfigurationError("plateau_rounds must be at least 1")
        if self.repeated_loss_rounds < 1:
            raise GauntletConfigurationError("repeated_loss_rounds must be at least 1")
        if not 0.0 <= self.minimum_improvement_margin <= 1.0:
            raise GauntletConfigurationError(
                "minimum_improvement_margin must be between 0 and 1"
            )
        if not 0.0 <= self.human_review_threshold <= self.confidence_threshold <= 1.0:
            raise GauntletConfigurationError(
                "require 0 <= human_review_threshold <= confidence_threshold <= 1"
            )
        for name, value in (
            ("max_time_seconds", self.max_time_seconds),
            ("max_tokens", self.max_tokens),
            ("max_cost_usd", self.max_cost_usd),
        ):
            if value is not None and value < 0:
                raise GauntletConfigurationError(f"{name} cannot be negative")

        normalized_thresholds: dict[QualityDimension, float] = {}
        for key, value in self.quality_thresholds.items():
            dimension = key if isinstance(key, QualityDimension) else QualityDimension(key)
            score = float(value)
            if not 0.0 <= score <= 1.0:
                raise GauntletConfigurationError(
                    f"quality threshold for {dimension.value} must be between 0 and 1"
                )
            normalized_thresholds[dimension] = score
        missing = set(ALL_QUALITY_DIMENSIONS) - set(normalized_thresholds)
        if missing:
            raise GauntletConfigurationError(
                "quality_thresholds missing: "
                + ", ".join(sorted(dimension.value for dimension in missing))
            )
        normalized_protected = tuple(
            item if isinstance(item, QualityDimension) else QualityDimension(item)
            for item in self.protected_dimensions
        )
        if len(normalized_protected) != len(set(normalized_protected)):
            raise GauntletConfigurationError("protected_dimensions cannot contain duplicates")
        normalized_keys = frozenset(str(key).casefold() for key in self.identifying_metadata_keys)
        normalized_keys |= MANDATORY_IDENTIFYING_METADATA_KEYS
        object.__setattr__(self, "quality_thresholds", normalized_thresholds)
        object.__setattr__(self, "protected_dimensions", normalized_protected)
        object.__setattr__(self, "identifying_metadata_keys", normalized_keys)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "GauntletConfig":
        """Construct from a plain JSON/YAML mapping with strict field names."""

        data = dict(values)
        if "quality_thresholds" in data:
            data["quality_thresholds"] = {
                QualityDimension(key): value
                for key, value in dict(data["quality_thresholds"]).items()
            }
        if "protected_dimensions" in data:
            data["protected_dimensions"] = tuple(
                QualityDimension(value) for value in data["protected_dimensions"]
            )
        if "identifying_metadata_keys" in data:
            data["identifying_metadata_keys"] = frozenset(data["identifying_metadata_keys"])
        try:
            return cls(**data)
        except TypeError as exc:
            raise GauntletConfigurationError(str(exc)) from exc

    def to_mapping(self) -> dict[str, Any]:
        data = asdict(self)
        data["quality_thresholds"] = {
            key.value: value for key, value in self.quality_thresholds.items()
        }
        data["protected_dimensions"] = [item.value for item in self.protected_dimensions]
        data["identifying_metadata_keys"] = sorted(self.identifying_metadata_keys)
        return data


def default_critic_rubric() -> CriticRubric:
    return CriticRubric(criteria=dict(DEFAULT_RUBRIC_CRITERIA))


@runtime_checkable
class CriticBackend(Protocol):
    """Fresh, isolated critic invocation over a complete structured request."""

    name: str
    model_label: str

    async def evaluate(
        self, request: CriticRequest
    ) -> CriticBackendResponse | CriticResult | Mapping[str, Any]: ...


@runtime_checkable
class EditorBackend(Protocol):
    """A repair backend that proposes one challenger and reports exact touches."""

    name: str
    model_label: str

    async def repair(
        self, request: TargetedEditRequest
    ) -> TargetedEditResult | Mapping[str, Any]: ...


@runtime_checkable
class ComparisonBackend(Protocol):
    """A blind A/B judge; it never receives champion/challenger labels."""

    name: str
    model_label: str

    async def compare(
        self, request: BlindComparisonRequest
    ) -> BlindVerdict | Mapping[str, Any]: ...


HardGateCallback = Callable[
    [ArtifactSnapshot], HardGateResult | Mapping[str, Any] | Awaitable[HardGateResult | Mapping[str, Any]]
]
HumanControlCallback = Callable[
    [int, ArtifactSnapshot, Sequence[RoundHistory]],
    HumanDecision | Mapping[str, Any] | Awaitable[HumanDecision | Mapping[str, Any]],
]


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _qualified_model_identity(label: str | None) -> tuple[str, str] | None:
    if not label:
        return None
    try:
        return split_backend_label(label)
    except ValueError:
        # An unqualified or unknown alias is not enough evidence to assert
        # independence from the builder.
        return None


def _validate_critic_locations(
    result: CriticResult, artifact: ArtifactSnapshot
) -> None:
    """Reject hallucinated critic targets before they can authorize a repair.

    Evidence statements may be artifact-wide and therefore cite no location,
    but every item key or item path that *is* supplied must resolve exactly in
    the artifact evaluated by this fresh call.
    """

    valid_keys = {item.item_key for item in artifact.items}
    valid_paths = {item.path for item in artifact.items}
    errors: list[str] = []

    def check_locations(label: str, item_keys: Sequence[str], paths: Sequence[str]) -> None:
        unknown_keys = sorted(set(item_keys) - valid_keys)
        unknown_paths = sorted(set(paths) - valid_paths)
        if unknown_keys:
            errors.append(f"{label} cites unknown item_key(s): {', '.join(unknown_keys)}")
        if unknown_paths:
            errors.append(f"{label} cites unknown path(s): {', '.join(unknown_paths)}")

    def check_evidence(label: str, evidence) -> None:
        for index, item in enumerate(evidence):
            check_locations(
                f"{label} evidence[{index}]", item.item_keys, item.paths
            )

    for assessment in result.dimensions:
        check_evidence(f"dimension {assessment.dimension.value}", assessment.evidence)
    for defect in result.critical_defects:
        check_locations(
            f"critical defect {defect.defect_id}",
            defect.affected_item_keys,
            defect.affected_paths,
        )
        check_evidence(f"critical defect {defect.defect_id}", defect.evidence)
    if result.largest_gap is not None:
        check_locations(
            "largest gap",
            result.largest_gap.affected_item_keys,
            result.largest_gap.affected_paths,
        )
        check_evidence("largest gap", result.largest_gap.evidence)
        if (
            result.largest_gap.recommended_scope is RepairScope.session
            and result.largest_gap.allowed_payload_fields
        ):
            errors.append(
                "session repair may only reorder existing items and therefore requires "
                "allowed_payload_fields=[]; use item or course scope for payload edits"
            )
        protected_fields = (
            set(result.largest_gap.allowed_payload_fields)
            & EDITOR_PROTECTED_PAYLOAD_FIELDS
        )
        if protected_fields:
            errors.append(
                "automatic Gauntlet repair cannot authorize protected payload field(s): "
                + ", ".join(sorted(protected_fields))
                + "; request human review when the defect requires an authored mechanic change"
            )
    if result.human_review_recommended != (result.human_review_reason is not None):
        errors.append(
            "human_review_recommended must be true exactly when a blocking "
            "human_review_reason is supplied"
        )
    if result.human_review_reason is not None and result.largest_gap is not None:
        errors.append(
            "a blocking human-review result cannot also authorize an automatic largest_gap repair"
        )
    if errors:
        raise CriticEvidenceError("; ".join(errors))


class IndependentCritic:
    """Build a fresh critic request from allow-listed context only."""

    def __init__(
        self,
        backend: CriticBackend,
        *,
        builder_model: str | None = None,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.backend = backend
        self.builder_model = builder_model
        self.max_attempts = max_attempts

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def model_label(self) -> str:
        return self.backend.model_label

    @property
    def fresh_context(self) -> bool:
        return bool(getattr(self.backend, "fresh_context", False))

    async def evaluate(
        self,
        *,
        goal: str,
        rubric: CriticRubric,
        source_material: list[SourceExcerpt],
        references: Sequence[ReferenceSession],
        artifact: ArtifactSnapshot,
    ) -> CriticEvaluation:
        approved = approved_references(references)
        feedback: list[str] = []
        total_usage = UsageRecord()
        response: CriticBackendResponse | None = None
        for attempt in range(1, self.max_attempts + 1):
            request = CriticRequest(
                goal=goal,
                rubric=rubric,
                source_material=source_material,
                approved_references=approved,
                actual_final_artifact=artifact,
                reference_confidence_reduced=not approved,
                validation_feedback=feedback,
            )
            raw = await self.backend.evaluate(request)
            if isinstance(raw, CriticBackendResponse):
                response = raw
            elif isinstance(raw, CriticResult):
                response = CriticBackendResponse(result=raw)
            else:
                # Accept either the full envelope or a bare CriticResult mapping.
                try:
                    response = CriticBackendResponse.model_validate(raw)
                except ValueError:
                    response = CriticBackendResponse(
                        result=CriticResult.model_validate(raw)
                    )
            total_usage = add_usage(total_usage, response.usage)
            try:
                _validate_critic_locations(response.result, artifact)
            except CriticEvidenceError as exc:
                feedback.append(str(exc))
                if attempt == self.max_attempts:
                    raise CriticEvidenceError(
                        f"critic evidence validation failed after {attempt} attempts: {exc}",
                        usage=total_usage,
                    ) from exc
                continue
            break
        assert response is not None
        builder_identity = _qualified_model_identity(self.builder_model)
        critic_identity = _qualified_model_identity(self.model_label)
        independence = (
            ModelIndependence.independent
            if builder_identity is not None
            and critic_identity is not None
            and builder_identity != critic_identity
            else ModelIndependence.unavailable
        )
        return CriticEvaluation(
            result=response.result,
            artifact_sha256=artifact.content_hash(),
            goal_sha256=text_sha256(goal),
            critic_backend=self.backend_name,
            critic_model=self.model_label,
            fresh_context=self.fresh_context,
            model_independence=independence,
            approved_reference_count=len(approved),
            reference_confidence_reduced=not approved,
            usage=total_usage,
        )


def _payload_changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    return {
        key
        for key in set(before) | set(after)
        if before.get(key, object()) != after.get(key, object())
    }


class TargetedEditor:
    """Invoke an editor and enforce item/session/course scope structurally."""

    def __init__(self, backend: EditorBackend, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.backend = backend
        self.max_attempts = max_attempts

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def model_label(self) -> str:
        return self.backend.model_label

    @property
    def fresh_context(self) -> bool:
        return bool(getattr(self.backend, "fresh_context", False))

    async def repair(
        self,
        *,
        champion: ArtifactSnapshot,
        directive: QualityGap,
        source_material: list[SourceExcerpt],
    ) -> TargetedEditResult:
        feedback: list[str] = []
        total_usage = UsageRecord()
        for attempt in range(1, self.max_attempts + 1):
            request = TargetedEditRequest(
                champion=champion,
                directive=directive,
                source_material=source_material,
                validation_feedback=feedback,
            )
            raw = await self.backend.repair(request)
            result = (
                raw
                if isinstance(raw, TargetedEditResult)
                else TargetedEditResult.model_validate(raw)
            )
            total_usage = add_usage(total_usage, result.usage)
            try:
                self._enforce_scope(champion, directive, result)
            except EditorScopeError as exc:
                feedback.append(str(exc))
                if attempt == self.max_attempts:
                    raise EditorScopeError(
                        f"editor scope validation failed after {attempt} attempts: {exc}",
                        usage=total_usage,
                    ) from exc
                continue
            return result.model_copy(update={"usage": total_usage})
        raise AssertionError("unreachable")

    @staticmethod
    def _enforce_scope(
        champion: ArtifactSnapshot,
        directive: QualityGap,
        result: TargetedEditResult,
    ) -> None:
        challenger = result.challenger
        before_keys = [item.item_key for item in champion.items]
        after_keys = [item.item_key for item in challenger.items]
        if set(before_keys) != set(after_keys) or len(before_keys) != len(after_keys):
            raise EditorScopeError("targeted edits cannot add, remove, or duplicate items")
        if (
            champion.course_id != challenger.course_id
            or champion.session_id != challenger.session_id
            or champion.metadata != challenger.metadata
        ):
            raise EditorScopeError("targeted edits cannot replace artifact context or metadata")

        before_by_key = {item.item_key: item for item in champion.items}
        after_by_key = {item.item_key: item for item in challenger.items}
        changed: dict[str, set[str]] = {}
        for item_key in before_keys:
            before = before_by_key[item_key]
            after = after_by_key[item_key]
            if before.path != after.path:
                raise EditorScopeError("targeted edits cannot change stable item paths")
            fields = _payload_changed_fields(before.payload, after.payload)
            protected_fields = fields & EDITOR_PROTECTED_PAYLOAD_FIELDS
            if protected_fields:
                raise EditorScopeError(
                    "targeted edits cannot change protected payload field(s): "
                    + ", ".join(sorted(protected_fields))
                )
            if fields:
                changed[item_key] = fields

        order_changed = before_keys != after_keys
        if result.order_changed != order_changed:
            raise EditorScopeError("editor order_changed report does not match the challenger")
        if len(result.touched_item_keys) != len(set(result.touched_item_keys)):
            raise EditorScopeError("touched_item_keys cannot contain duplicates")
        if set(result.touched_item_keys) != set(changed):
            raise EditorScopeError(
                "editor touched_item_keys report does not match actual payload changes"
            )
        reported_fields = {key: set(value) for key, value in result.touched_payload_fields.items()}
        if reported_fields != changed:
            raise EditorScopeError(
                "editor touched_payload_fields report does not match actual changes: "
                f"reported={dict(sorted((key, sorted(value)) for key, value in reported_fields.items()))}; "
                f"actual={dict(sorted((key, sorted(value)) for key, value in changed.items()))}"
            )

        targets = set(directive.affected_item_keys)
        allowed_fields = set(directive.allowed_payload_fields)
        if directive.recommended_scope is RepairScope.session:
            if changed:
                raise EditorScopeError("session repair may reorder items but cannot rewrite payloads")
            if not order_changed:
                raise EditorScopeError("session repair must propose a changed order")
        else:
            if order_changed:
                raise EditorScopeError(
                    f"{directive.recommended_scope.value} repair cannot reorder the session"
                )
            if not changed:
                raise EditorScopeError("editor returned no observable targeted change")
            if not set(changed) <= targets:
                raise EditorScopeError("editor changed an item outside affected_item_keys")
            if allowed_fields and any(
                not fields <= allowed_fields for fields in changed.values()
            ):
                raise EditorScopeError("editor changed a payload field outside the allow-list")

        if champion.content_hash() == challenger.content_hash():
            raise EditorScopeError("challenger content is identical to the champion")


def _strip_identifying_metadata(value: Any, blocked: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_identifying_metadata(item, blocked)
            for key, item in value.items()
            if str(key).casefold() not in blocked
        }
    if isinstance(value, list):
        return [_strip_identifying_metadata(item, blocked) for item in value]
    if isinstance(value, tuple):
        return [_strip_identifying_metadata(item, blocked) for item in value]
    return value


class BlindComparator:
    """Run deterministic position-swapped A/B judgments and map results safely."""

    def __init__(self, backend: ComparisonBackend, config: GauntletConfig) -> None:
        self.backend = backend
        self.config = config

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def model_label(self) -> str:
        return self.backend.model_label

    @property
    def fresh_context(self) -> bool:
        return bool(getattr(self.backend, "fresh_context", False))

    def _blind_artifact(self, artifact: ArtifactSnapshot) -> BlindArtifact:
        return BlindArtifact(
            ordered_items=[
                _strip_identifying_metadata(item.payload, self.config.identifying_metadata_keys)
                for item in artifact.items
            ]
        )

    def _blind_reference(self, reference: ReferenceSession) -> BlindReference:
        return BlindReference(
            ordered_items=[
                _strip_identifying_metadata(item.payload, self.config.identifying_metadata_keys)
                for item in reference.final_ordered_questions
            ],
            annotations=list(reference.annotations),
            expected_dimensions=dict(reference.expected_dimensions),
            known_weaknesses=list(reference.known_weaknesses),
            exceptions=list(reference.exceptions),
        )

    async def compare(
        self,
        *,
        champion: ArtifactSnapshot,
        challenger: ArtifactSnapshot,
        goal: str,
        rubric: CriticRubric,
        source_material: list[SourceExcerpt],
        references: Sequence[ReferenceSession],
        seed: int,
    ) -> PairwiseComparisonResult:
        blinded = {
            CandidateRole.champion: self._blind_artifact(champion),
            CandidateRole.challenger: self._blind_artifact(challenger),
        }
        rng = random.Random(seed)
        first_a = (
            CandidateRole.champion
            if rng.randrange(2) == 0
            else CandidateRole.challenger
        )
        first_b = (
            CandidateRole.challenger
            if first_a is CandidateRole.champion
            else CandidateRole.champion
        )
        orders = ((first_a, first_b), (first_b, first_a))
        standards = [
            self._blind_reference(reference)
            for reference in approved_references(references)
        ]
        records: list[PositionComparisonRecord] = []
        for index, (a_role, b_role) in enumerate(orders, start=1):
            request = BlindComparisonRequest(
                comparison_id=f"blind-{seed}-{index}",
                goal=goal,
                rubric=rubric,
                source_material=source_material,
                approved_reference_standard=standards,
                candidate_a=blinded[a_role],
                candidate_b=blinded[b_role],
            )
            raw = await self.backend.compare(request)
            verdict = raw if isinstance(raw, BlindVerdict) else BlindVerdict.model_validate(raw)
            if verdict.winner is BlindWinner.a:
                mapped = a_role
            elif verdict.winner is BlindWinner.b:
                mapped = b_role
            else:
                mapped = CandidateRole.tie
            records.append(
                PositionComparisonRecord(
                    order_index=index,
                    seed=seed,
                    a_role=a_role,
                    b_role=b_role,
                    mapped_winner=mapped,
                    verdict=verdict,
                )
            )

        mapped_winners = [record.mapped_winner for record in records]
        position_sensitive = mapped_winners[0] is not mapped_winners[1]
        minimum_confidence = min(record.verdict.confidence for record in records)
        minimum_margin = min(record.verdict.margin for record in records)
        backend_requested_human = any(
            record.verdict.human_review_recommended for record in records
        )

        protected_regressions: set[QualityDimension] = set()
        for record in records:
            by_dimension = {item.dimension: item for item in record.verdict.dimensions}
            for dimension in self.config.protected_dimensions:
                pair = by_dimension[dimension]
                if record.a_role is CandidateRole.challenger:
                    challenger_score, champion_score = pair.score_a, pair.score_b
                else:
                    challenger_score, champion_score = pair.score_b, pair.score_a
                if challenger_score < champion_score:
                    protected_regressions.add(dimension)

        stable = (
            not position_sensitive
            and not backend_requested_human
            and minimum_confidence >= self.config.confidence_threshold
        )
        if position_sensitive:
            decision = (
                ComparisonDecision.human_review
                if self.config.unstable_comparison_requires_human_review
                else ComparisonDecision.retain
            )
            evidence = "Position-swapped judgments disagree; the champion is retained."
        elif backend_requested_human or minimum_confidence < self.config.confidence_threshold:
            decision = ComparisonDecision.human_review
            evidence = "Comparison confidence is insufficient for automatic promotion."
        elif mapped_winners[0] is not CandidateRole.challenger:
            decision = ComparisonDecision.retain
            evidence = "Both position orders did not select the challenger."
        elif minimum_margin < self.config.minimum_improvement_margin:
            decision = ComparisonDecision.retain
            evidence = "Challenger advantage is below the minimum improvement margin."
        elif protected_regressions:
            decision = ComparisonDecision.retain
            evidence = "A protected quality dimension regressed."
        else:
            decision = ComparisonDecision.promote
            evidence = "Both position orders select the hard-gated challenger without protected regression."

        return PairwiseComparisonResult(
            champion_sha256=champion.content_hash(),
            challenger_sha256=challenger.content_hash(),
            comparator_backend=self.backend_name,
            comparator_model=self.model_label,
            comparator_fresh_context=self.fresh_context,
            records=records,
            position_sensitive=position_sensitive,
            stable=stable,
            decision=decision,
            protected_regressions=sorted(protected_regressions, key=lambda item: item.value),
            minimum_confidence=minimum_confidence,
            minimum_margin=minimum_margin,
            decision_evidence=evidence,
            usage=add_usage(*(record.verdict.usage for record in records)),
        )


class QualitativeGauntlet:
    """Bounded champion-retention loop over an exact learner artifact."""

    def __init__(
        self,
        *,
        config: GauntletConfig,
        critic: IndependentCritic,
        editor: TargetedEditor,
        comparator: BlindComparator,
        hard_gate: HardGateCallback,
        human_control: HumanControlCallback | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.critic = critic
        self.editor = editor
        self.comparator = comparator
        self.hard_gate = hard_gate
        self.human_control = human_control
        self.monotonic = monotonic
        self.now = now
        if config.critic_backend is not None and config.critic_backend != critic.backend_name:
            raise GauntletConfigurationError(
                f"configured critic_backend={config.critic_backend!r}, got {critic.backend_name!r}"
            )
        if config.critic_model is not None and config.critic_model != critic.model_label:
            raise GauntletConfigurationError(
                f"configured critic_model={config.critic_model!r}, got {critic.model_label!r}"
            )

    async def _gate(self, artifact: ArtifactSnapshot) -> HardGateResult:
        raw = await _resolve(self.hard_gate(artifact))
        result = raw if isinstance(raw, HardGateResult) else HardGateResult.model_validate(raw)
        return result.model_copy(update={"artifact_sha256": artifact.content_hash()})

    async def _human(
        self,
        round_number: int,
        champion: ArtifactSnapshot,
        rounds: Sequence[RoundHistory],
    ) -> HumanDecision:
        if self.human_control is None:
            return HumanDecision()
        raw = await _resolve(self.human_control(round_number, champion, tuple(rounds)))
        return raw if isinstance(raw, HumanDecision) else HumanDecision.model_validate(raw)

    def _budget_stop(self, *, started: float, usage: UsageRecord) -> StopReason | None:
        if (
            self.config.max_time_seconds is not None
            and self.monotonic() - started >= self.config.max_time_seconds
        ):
            return StopReason.time_budget
        if self.config.max_tokens is not None and usage.total_tokens >= self.config.max_tokens:
            return StopReason.token_budget
        if self.config.max_cost_usd is not None and usage.cost_usd >= self.config.max_cost_usd:
            return StopReason.cost_budget
        return None

    def _quality_passes(self, result: CriticResult) -> bool:
        scores = result.scores()
        return (
            not result.critical_defects
            and not result.human_review_recommended
            and result.confidence >= self.config.confidence_threshold
            and all(
                scores[dimension] >= threshold
                for dimension, threshold in self.config.quality_thresholds.items()
            )
        )

    def _evaluation_context(
        self,
        *,
        goal: str,
        rubric: CriticRubric,
        source_material: Sequence[SourceExcerpt],
        references: Sequence[ReferenceSession],
    ) -> EvaluationContext:
        approved = approved_references(references)
        return EvaluationContext.create(
            goal=goal,
            source_fidelity_goal=SOURCE_FIDELITY_GOAL,
            gauntlet_policy=self.config.to_mapping(),
            rubric=rubric,
            source_material=source_material,
            approved_reference_sessions=approved,
            models=EvaluationModelProvenance(
                builder_model=self.config.builder_model,
                critic_backend=self.critic.backend_name,
                critic_model=self.critic.model_label,
                critic_fresh_context=self.critic.fresh_context,
                editor_backend=self.editor.backend_name,
                editor_model=self.editor.model_label,
                editor_fresh_context=self.editor.fresh_context,
                comparator_backend=self.comparator.backend_name,
                comparator_model=self.comparator.model_label,
                comparator_fresh_context=self.comparator.fresh_context,
            ),
        )

    def _source_fidelity_result(self, evaluation: CriticEvaluation) -> HardGateResult:
        result = evaluation.result
        score = result.scores()[QualityDimension.factual_fidelity]
        threshold = self.config.quality_thresholds[QualityDimension.factual_fidelity]
        factual_defects = [
            defect
            for defect in result.critical_defects
            if defect.dimension is QualityDimension.factual_fidelity
        ]
        issues: list[HardGateIssue] = []
        if score < threshold:
            issues.append(
                HardGateIssue(
                    code="source_fidelity_score",
                    path="artifact",
                    message=(
                        f"factual fidelity score {score:.3f} is below the configured "
                        f"threshold {threshold:.3f}"
                    ),
                )
            )
        if result.confidence < self.config.confidence_threshold:
            issues.append(
                HardGateIssue(
                    code="source_fidelity_confidence",
                    path="artifact",
                    message=(
                        f"source-fidelity confidence {result.confidence:.3f} is below "
                        f"the configured threshold {self.config.confidence_threshold:.3f}"
                    ),
                )
            )
        issues.extend(
            HardGateIssue(
                code="source_fidelity_defect",
                path=(defect.affected_paths[0] if defect.affected_paths else defect.affected_item_keys[0]),
                message=defect.summary,
            )
            for defect in factual_defects
        )
        if result.human_review_recommended:
            issues.append(
                HardGateIssue(
                    code="source_fidelity_human_review",
                    path="artifact",
                    message="source-fidelity critic requested human review",
                )
            )
        if evaluation.fresh_context is not True:
            issues.append(
                HardGateIssue(
                    code="source_fidelity_context",
                    path="artifact",
                    message="source-fidelity evidence was not produced in a fresh context",
                )
            )
        return HardGateResult(
            artifact_sha256=evaluation.artifact_sha256,
            passed=not issues,
            issues=issues,
            checks={
                "factual_fidelity_threshold": score >= threshold,
                "critic_confidence_threshold": (
                    result.confidence >= self.config.confidence_threshold
                ),
                "no_factual_critical_defect": not factual_defects,
                "no_human_review_request": not result.human_review_recommended,
                "fresh_critic_context": evaluation.fresh_context is True,
            },
            metrics={
                "factual_fidelity_score": score,
                "factual_fidelity_threshold": threshold,
                "critic_confidence": result.confidence,
            },
        )

    async def run(
        self,
        *,
        run_id: str,
        champion: ArtifactSnapshot,
        goal: str,
        source_material: list[SourceExcerpt],
        references: Sequence[ReferenceSession] = (),
        rubric: CriticRubric | None = None,
    ) -> GauntletOutcome:
        rubric = rubric or default_critic_rubric()
        evaluation_context = self._evaluation_context(
            goal=goal,
            rubric=rubric,
            source_material=source_material,
            references=references,
        )
        started_at = self.now()
        started = self.monotonic()
        initial_hash = champion.content_hash()
        initial_gate = await self._gate(champion)
        rounds: list[RoundHistory] = []
        total_usage = UsageRecord()
        final_gate = initial_gate
        human_review_recommended = False
        terminal_human_decision: HumanDecision | None = None

        if not initial_gate.passed:
            return self._outcome(
                run_id=run_id,
                started_at=started_at,
                initial_hash=initial_hash,
                champion=champion,
                initial_gate=initial_gate,
                final_gate=initial_gate,
                rounds=rounds,
                stop_reason=StopReason.initial_hard_gate_failure,
                stop_evidence="Initial champion failed the authoritative deterministic hard gate.",
                total_usage=total_usage,
                human_review_recommended=True,
                evaluation_context=evaluation_context,
            )

        human = await self._human(0, champion, rounds)
        if human.action is HumanAction.approve:
            return self._outcome(
                run_id, started_at, initial_hash, champion, initial_gate, final_gate, rounds,
                StopReason.human_approved,
                human.note or f"Approved by {human.reviewer}.",
                total_usage, False, evaluation_context, human,
            )
        if human.action is HumanAction.stop:
            return self._outcome(
                run_id, started_at, initial_hash, champion, initial_gate, final_gate, rounds,
                StopReason.human_stop,
                human.note or f"Stopped by {human.reviewer}.",
                total_usage, False, evaluation_context, human,
            )

        budget_reason = self._budget_stop(started=started, usage=total_usage)
        if budget_reason is not None:
            return self._outcome(
                run_id, started_at, initial_hash, champion, initial_gate, final_gate, rounds,
                budget_reason, "Configured budget was reached before the first critic call.",
                total_usage, False, evaluation_context,
            )

        plateau_count = 0
        consecutive_losses = 0
        previous_critic_score: float | None = None
        stop_reason = StopReason.max_rounds
        stop_evidence = f"Maximum of {self.config.max_rounds} rounds reached."

        for round_number in range(1, self.config.max_rounds + 1):
            champion_snapshot_before = champion.model_copy(deep=True)
            champion_hash_before = champion.content_hash()
            try:
                critic_eval = await self.critic.evaluate(
                    goal=goal,
                    rubric=rubric,
                    source_material=source_material,
                    references=references,
                    artifact=champion,
                )
            except (CriticEvidenceError, BackendTimeoutError) as exc:
                failure_usage = (
                    exc.usage if isinstance(exc, CriticEvidenceError) else UsageRecord()
                )
                total_usage = add_usage(total_usage, failure_usage)
                human_review_recommended = True
                evidence = (
                    str(exc)
                    if isinstance(exc, CriticEvidenceError)
                    else f"critic backend timed out after configured retries: {exc}"
                )
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic_failure=evidence,
                        validation_retry_usage=failure_usage,
                        decision=RoundDecision.retained_human,
                        decision_evidence=evidence,
                        champion_hash_after=champion_hash_before,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason = StopReason.human_review_required
                stop_evidence = evidence
                break
            critic_eval = critic_eval.model_copy(
                update={
                    "evaluation_context_sha256": evaluation_context.context_sha256
                }
            )
            total_usage = add_usage(total_usage, critic_eval.usage)
            score = critic_eval.result.mean_score()
            if previous_critic_score is not None:
                if score - previous_critic_score < self.config.minimum_improvement_margin:
                    plateau_count += 1
                else:
                    plateau_count = 0
            previous_critic_score = score

            budget_reason = self._budget_stop(started=started, usage=total_usage)
            if budget_reason is not None:
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        decision=RoundDecision.retained_budget,
                        decision_evidence="Budget reached after critic evaluation; no repair was attempted.",
                        champion_hash_after=champion_hash_before,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason, stop_evidence = budget_reason, "Configured budget reached."
                break

            if self._quality_passes(critic_eval.result):
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        decision=RoundDecision.retained_no_repair,
                        decision_evidence="All configured qualitative thresholds pass.",
                        champion_hash_after=champion_hash_before,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason, stop_evidence = StopReason.success, "All hard and qualitative gates pass."
                break

            if critic_eval.result.human_review_reason is not None or (
                critic_eval.result.confidence < self.config.human_review_threshold
            ):
                human_review_recommended = True
                if critic_eval.result.human_review_reason is not None:
                    review_evidence = (
                        "Critic supplied blocking human-review reason: "
                        f"{critic_eval.result.human_review_reason.value}."
                    )
                else:
                    review_evidence = (
                        "Critic confidence was below the configured hard human-review threshold."
                    )
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        decision=RoundDecision.retained_human,
                        decision_evidence=review_evidence,
                        champion_hash_after=champion_hash_before,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason = StopReason.human_review_required
                stop_evidence = review_evidence
                break

            if plateau_count >= self.config.plateau_rounds:
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        decision=RoundDecision.retained_no_repair,
                        decision_evidence="Qualitative score improvement plateaued.",
                        champion_hash_after=champion_hash_before,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason, stop_evidence = StopReason.plateau, (
                    f"No meaningful critic-score improvement for {plateau_count} rounds."
                )
                break

            directive = critic_eval.result.largest_gap
            if directive is None:
                if critic_eval.result.critical_defects:
                    human_review_recommended = True
                    reason = StopReason.human_review_required
                    evidence = "Critic reported critical defects without a safe actionable repair scope."
                    decision = RoundDecision.retained_human
                else:
                    human_review_recommended = True
                    reason = StopReason.human_review_required
                    evidence = (
                        "Quality thresholds did not pass, but the critic supplied no actionable gap; "
                        "manual review is required."
                    )
                    decision = RoundDecision.retained_human
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        decision=decision,
                        decision_evidence=evidence,
                        champion_hash_after=champion_hash_before,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason, stop_evidence = reason, evidence
                break

            try:
                edit = await self.editor.repair(
                    champion=champion,
                    directive=directive,
                    source_material=source_material,
                )
            except (EditorScopeError, BackendTimeoutError) as exc:
                failure_usage = (
                    exc.usage if isinstance(exc, EditorScopeError) else UsageRecord()
                )
                total_usage = add_usage(total_usage, failure_usage)
                human_review_recommended = True
                evidence = (
                    str(exc)
                    if isinstance(exc, EditorScopeError)
                    else f"editor backend timed out after configured retries: {exc}"
                )
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        validation_retry_usage=failure_usage,
                        decision=RoundDecision.retained_human,
                        decision_evidence=evidence,
                        champion_hash_after=champion_hash_before,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason = StopReason.human_review_required
                stop_evidence = evidence
                break
            total_usage = add_usage(total_usage, edit.usage)
            challenger = edit.challenger
            challenger_hash = challenger.content_hash()
            budget_reason = self._budget_stop(started=started, usage=total_usage)
            if budget_reason is not None:
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        repair_scope=directive.recommended_scope,
                        repair_summary=edit.change_summary,
                        editor_backend=self.editor.backend_name,
                        editor_model=self.editor.model_label,
                        editor_fresh_context=self.editor.fresh_context,
                        edit_usage=edit.usage,
                        touched_item_keys=edit.touched_item_keys,
                        challenger_hash=challenger_hash,
                        decision=RoundDecision.retained_budget,
                        decision_evidence="Budget reached after edit; ungated challenger was not considered.",
                        champion_hash_after=champion_hash_before,
                        constraint_relaxations=edit.constraint_relaxations,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason, stop_evidence = budget_reason, "Configured budget reached."
                break

            challenger_gate = await self._gate(challenger)
            relaxations = list(
                dict.fromkeys(
                    edit.constraint_relaxations + challenger_gate.constraint_relaxations
                )
            )
            if not challenger_gate.passed:
                consecutive_losses += 1
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        repair_scope=directive.recommended_scope,
                        repair_summary=edit.change_summary,
                        editor_backend=self.editor.backend_name,
                        editor_model=self.editor.model_label,
                        editor_fresh_context=self.editor.fresh_context,
                        edit_usage=edit.usage,
                        touched_item_keys=edit.touched_item_keys,
                        challenger_hash=challenger_hash,
                        hard_gate=challenger_gate,
                        decision=RoundDecision.rejected_hard_gate,
                        decision_evidence="Challenger failed the authoritative hard gate; comparison was skipped.",
                        champion_hash_after=champion_hash_before,
                        constraint_relaxations=relaxations,
                        cumulative_usage=total_usage,
                    )
                )
                if consecutive_losses >= self.config.repeated_loss_rounds:
                    stop_reason, stop_evidence = StopReason.repeated_loss, (
                        f"{consecutive_losses} consecutive challengers failed to beat the champion."
                    )
                    break
                human = await self._human(round_number, champion, rounds)
                if human.action is not HumanAction.continue_:
                    terminal_human_decision = human
                    stop_reason = (
                        StopReason.human_approved
                        if human.action is HumanAction.approve
                        else StopReason.human_stop
                    )
                    stop_evidence = human.note or f"Decision by {human.reviewer}."
                    break
                budget_reason = self._budget_stop(started=started, usage=total_usage)
                if budget_reason is not None:
                    stop_reason, stop_evidence = budget_reason, "Configured budget reached."
                    break
                continue

            source_fidelity_critic: CriticEvaluation | None = None
            source_fidelity_gate: HardGateResult | None = None
            if edit.touched_item_keys:
                try:
                    source_fidelity_critic = await self.critic.evaluate(
                        goal=SOURCE_FIDELITY_GOAL,
                        rubric=rubric,
                        source_material=source_material,
                        references=references,
                        artifact=challenger,
                    )
                except (CriticEvidenceError, BackendTimeoutError) as exc:
                    failure_usage = (
                        exc.usage
                        if isinstance(exc, CriticEvidenceError)
                        else UsageRecord()
                    )
                    total_usage = add_usage(total_usage, failure_usage)
                    human_review_recommended = True
                    evidence = (
                        str(exc)
                        if isinstance(exc, CriticEvidenceError)
                        else f"source-fidelity critic timed out after configured retries: {exc}"
                    )
                    rounds.append(
                        RoundHistory(
                            round_number=round_number,
                            champion_hash_before=champion_hash_before,
                            critic=critic_eval,
                            repair_scope=directive.recommended_scope,
                            repair_summary=edit.change_summary,
                            editor_backend=self.editor.backend_name,
                            editor_model=self.editor.model_label,
                            editor_fresh_context=self.editor.fresh_context,
                            edit_usage=edit.usage,
                            validation_retry_usage=failure_usage,
                            touched_item_keys=edit.touched_item_keys,
                            challenger_hash=challenger_hash,
                            hard_gate=challenger_gate,
                            decision=RoundDecision.retained_human,
                            decision_evidence=evidence,
                            champion_hash_after=champion_hash_before,
                            constraint_relaxations=relaxations,
                            cumulative_usage=total_usage,
                        )
                    )
                    stop_reason = StopReason.human_review_required
                    stop_evidence = evidence
                    break
                source_fidelity_critic = source_fidelity_critic.model_copy(
                    update={
                        "evaluation_context_sha256": evaluation_context.context_sha256
                    }
                )
                total_usage = add_usage(total_usage, source_fidelity_critic.usage)
                source_fidelity_gate = self._source_fidelity_result(
                    source_fidelity_critic
                )
                if not source_fidelity_gate.passed:
                    human_review_recommended = True
                    rounds.append(
                        RoundHistory(
                            round_number=round_number,
                            champion_hash_before=champion_hash_before,
                            critic=critic_eval,
                            repair_scope=directive.recommended_scope,
                            repair_summary=edit.change_summary,
                            editor_backend=self.editor.backend_name,
                            editor_model=self.editor.model_label,
                            editor_fresh_context=self.editor.fresh_context,
                            edit_usage=edit.usage,
                            touched_item_keys=edit.touched_item_keys,
                            challenger_hash=challenger_hash,
                            hard_gate=challenger_gate,
                            source_fidelity_critic=source_fidelity_critic,
                            source_fidelity_gate=source_fidelity_gate,
                            decision=RoundDecision.rejected_source_fidelity,
                            decision_evidence=(
                                "Edited challenger did not pass fresh source-fidelity review; "
                                "the champion is retained for manual review."
                            ),
                            champion_hash_after=champion_hash_before,
                            constraint_relaxations=relaxations,
                            cumulative_usage=total_usage,
                        )
                    )
                    stop_reason = StopReason.human_review_required
                    stop_evidence = (
                        "Content-changing repair failed fresh source-fidelity review."
                    )
                    break
                budget_reason = self._budget_stop(started=started, usage=total_usage)
                if budget_reason is not None:
                    rounds.append(
                        RoundHistory(
                            round_number=round_number,
                            champion_hash_before=champion_hash_before,
                            critic=critic_eval,
                            repair_scope=directive.recommended_scope,
                            repair_summary=edit.change_summary,
                            editor_backend=self.editor.backend_name,
                            editor_model=self.editor.model_label,
                            editor_fresh_context=self.editor.fresh_context,
                            edit_usage=edit.usage,
                            touched_item_keys=edit.touched_item_keys,
                            challenger_hash=challenger_hash,
                            hard_gate=challenger_gate,
                            source_fidelity_critic=source_fidelity_critic,
                            source_fidelity_gate=source_fidelity_gate,
                            decision=RoundDecision.retained_budget,
                            decision_evidence=(
                                "Budget reached after source-fidelity review; comparison was skipped."
                            ),
                            champion_hash_after=champion_hash_before,
                            constraint_relaxations=relaxations,
                            cumulative_usage=total_usage,
                        )
                    )
                    stop_reason, stop_evidence = budget_reason, "Configured budget reached."
                    break

            try:
                comparison = await self.comparator.compare(
                    champion=champion,
                    challenger=challenger,
                    goal=goal,
                    rubric=rubric,
                    source_material=source_material,
                    references=references,
                    seed=self.config.comparison_seed + round_number - 1,
                )
            except BackendTimeoutError as exc:
                human_review_recommended = True
                evidence = f"comparison backend timed out after configured retries: {exc}"
                rounds.append(
                    RoundHistory(
                        round_number=round_number,
                        champion_hash_before=champion_hash_before,
                        critic=critic_eval,
                        repair_scope=directive.recommended_scope,
                        repair_summary=edit.change_summary,
                        editor_backend=self.editor.backend_name,
                        editor_model=self.editor.model_label,
                        editor_fresh_context=self.editor.fresh_context,
                        edit_usage=edit.usage,
                        touched_item_keys=edit.touched_item_keys,
                        challenger_hash=challenger_hash,
                        hard_gate=challenger_gate,
                        source_fidelity_critic=source_fidelity_critic,
                        source_fidelity_gate=source_fidelity_gate,
                        decision=RoundDecision.retained_human,
                        decision_evidence=evidence,
                        champion_hash_after=champion_hash_before,
                        constraint_relaxations=relaxations,
                        cumulative_usage=total_usage,
                    )
                )
                stop_reason = StopReason.human_review_required
                stop_evidence = evidence
                break
            comparison = comparison.model_copy(
                update={
                    "evaluation_context_sha256": evaluation_context.context_sha256
                }
            )
            total_usage = add_usage(total_usage, comparison.usage)
            if comparison.decision is ComparisonDecision.promote:
                champion = challenger
                final_gate = challenger_gate
                consecutive_losses = 0
                round_decision = RoundDecision.promoted
            elif comparison.decision is ComparisonDecision.human_review:
                consecutive_losses += 1
                human_review_recommended = True
                round_decision = RoundDecision.retained_human
            else:
                consecutive_losses += 1
                round_decision = RoundDecision.rejected_comparison

            rounds.append(
                RoundHistory(
                    round_number=round_number,
                    champion_hash_before=champion_hash_before,
                    critic=critic_eval,
                    repair_scope=directive.recommended_scope,
                    repair_summary=edit.change_summary,
                    editor_backend=self.editor.backend_name,
                    editor_model=self.editor.model_label,
                    editor_fresh_context=self.editor.fresh_context,
                    edit_usage=edit.usage,
                    touched_item_keys=edit.touched_item_keys,
                    challenger_hash=challenger_hash,
                    promoted_champion_before=(
                        champion_snapshot_before
                        if round_decision is RoundDecision.promoted
                        else None
                    ),
                    promoted_challenger=(
                        challenger.model_copy(deep=True)
                        if round_decision is RoundDecision.promoted
                        else None
                    ),
                    hard_gate=challenger_gate,
                    source_fidelity_critic=source_fidelity_critic,
                    source_fidelity_gate=source_fidelity_gate,
                    comparison=comparison,
                    decision=round_decision,
                    decision_evidence=comparison.decision_evidence,
                    champion_hash_after=champion.content_hash(),
                    constraint_relaxations=relaxations,
                    cumulative_usage=total_usage,
                )
            )

            if comparison.decision is ComparisonDecision.human_review:
                stop_reason, stop_evidence = (
                    StopReason.human_review_required,
                    comparison.decision_evidence,
                )
                break

            human = await self._human(round_number, champion, rounds)
            if human.action is not HumanAction.continue_:
                terminal_human_decision = human
                stop_reason = (
                    StopReason.human_approved
                    if human.action is HumanAction.approve
                    else StopReason.human_stop
                )
                stop_evidence = human.note or f"Decision by {human.reviewer}."
                break

            budget_reason = self._budget_stop(started=started, usage=total_usage)
            if budget_reason is not None:
                stop_reason, stop_evidence = budget_reason, "Configured budget reached."
                break
            if consecutive_losses >= self.config.repeated_loss_rounds:
                stop_reason, stop_evidence = StopReason.repeated_loss, (
                    f"{consecutive_losses} consecutive challengers failed to beat the champion."
                )
                break

        return self._outcome(
            run_id, started_at, initial_hash, champion, initial_gate, final_gate, rounds,
            stop_reason, stop_evidence, total_usage, human_review_recommended,
            evaluation_context, terminal_human_decision,
        )

    def _outcome(
        self,
        run_id: str,
        started_at: datetime,
        initial_hash: str,
        champion: ArtifactSnapshot,
        initial_gate: HardGateResult,
        final_gate: HardGateResult,
        rounds: list[RoundHistory],
        stop_reason: StopReason,
        stop_evidence: str,
        total_usage: UsageRecord,
        human_review_recommended: bool,
        evaluation_context: EvaluationContext,
        human_decision: HumanDecision | None = None,
    ) -> GauntletOutcome:
        history = GauntletHistory(
            run_id=run_id,
            started_at=started_at,
            finished_at=self.now(),
            initial_champion_hash=initial_hash,
            final_champion_hash=champion.content_hash(),
            evaluation_context=evaluation_context,
            initial_hard_gate=initial_gate,
            rounds=rounds,
            stop_reason=stop_reason,
            stop_evidence=stop_evidence,
            human_decision=human_decision,
            total_usage=total_usage,
            human_review_recommended=human_review_recommended,
            publication_eligible=False,
        )
        eligible = final_gate.passed and history.derived_publication_eligible(
            expected_context=evaluation_context
        )
        history = GauntletHistory.model_validate(
            {**history.model_dump(mode="python"), "publication_eligible": eligible}
        )
        return GauntletOutcome(champion=champion, history=history)


async def run_gauntlet(
    *,
    config: GauntletConfig,
    critic: IndependentCritic,
    editor: TargetedEditor,
    comparator: BlindComparator,
    hard_gate: HardGateCallback,
    run_id: str,
    champion: ArtifactSnapshot,
    goal: str,
    source_material: list[SourceExcerpt],
    references: Sequence[ReferenceSession] = (),
    rubric: CriticRubric | None = None,
    human_control: HumanControlCallback | None = None,
) -> GauntletOutcome:
    """Convenience entry point for the common no-clock-injection case."""

    runner = QualitativeGauntlet(
        config=config,
        critic=critic,
        editor=editor,
        comparator=comparator,
        hard_gate=hard_gate,
        human_control=human_control,
    )
    return await runner.run(
        run_id=run_id,
        champion=champion,
        goal=goal,
        source_material=source_material,
        references=references,
        rubric=rubric,
    )
