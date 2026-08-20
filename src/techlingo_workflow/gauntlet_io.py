"""Adapters and persistence at the deterministic/qualitative QA boundary."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .experience import ConstraintRelaxation, ExperiencePolicy
from .gauntlet_models import (
    ArtifactItem,
    ArtifactSnapshot,
    EvaluationContext,
    EvaluationModelProvenance,
    GauntletHistory,
    GauntletOutcome,
    HardGateIssue,
    HardGateResult,
    ReferenceSession,
    ReferenceStatus,
    SourceExcerpt,
    canonical_sha256,
)
from .references import (
    ReferenceError,
    load_reference,
    promote_reference,
    write_reference,
)
from .sequence_quality import SequenceQualityPolicy, validate_tl_course
from .techlingo_models import TLCourse, TLModule, TLQuestion, TLUnit
from .validate_techlingo import validate_techlingo_course
from .workspace import Workspace, _atomic_write_text, sha256_text

GAUNTLET_RECORD_SCHEMA = "techlingo-gauntlet-record-v1"
REFERENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class GauntletIOError(ValueError):
    """A persisted Gauntlet/reference artifact is invalid or ambiguous."""


class GauntletRecord(BaseModel):
    """One unit's auditable outcome bound to an exact compiled course hash."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["techlingo-gauntlet-record-v1"] = GAUNTLET_RECORD_SCHEMA
    compiled_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit_key: str = Field(min_length=1)
    champion: ArtifactSnapshot
    history: GauntletHistory

    @model_validator(mode="after")
    def hashes_match(self) -> "GauntletRecord":
        if self.unit_key != self.champion.session_id:
            raise ValueError("record unit_key does not match champion session_id")
        if self.champion.content_hash() != self.history.final_champion_hash:
            raise ValueError("record champion does not match Gauntlet history final hash")
        return self


class GauntletRecordReference(BaseModel):
    """Manifest-safe pointer to one exact persisted qualitative decision."""

    model_config = ConfigDict(extra="forbid")

    unit_key: str = Field(min_length=1)
    compiled_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    champion_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gauntlet_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_path: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    finished_at: datetime


@dataclass(frozen=True)
class QualitativePublicationCoverage:
    """Exact-unit qualitative coverage at one compiled-course hash."""

    blockers: tuple[str, ...]
    references: tuple[GauntletRecordReference, ...]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def provenance(self) -> list[dict]:
        return [reference.model_dump(mode="json") for reference in self.references]


def artifact_from_tl_unit(
    *,
    course_id: str,
    module_key: str,
    unit: TLUnit,
    unit_path: str,
    relaxations: Iterable[ConstraintRelaxation] = (),
) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        artifact_id=f"{course_id}:{unit.import_key}",
        course_id=course_id,
        session_id=unit.import_key,
        items=[
            ArtifactItem(
                item_key=str(question.options.get("item_key") or question.import_key),
                path=f"{unit_path}/questions/{index}",
                payload=question.model_dump(mode="json"),
            )
            for index, question in enumerate(unit.exercises)
        ],
        metadata={
            "module_key": module_key,
            "unit_title": unit.title,
            "unit_slo": unit.slo,
            "constraint_relaxations": [
                asdict(relaxation)
                for relaxation in relaxations
            ],
        },
    )


def compiled_unit_artifacts(compiled) -> dict[str, ArtifactSnapshot]:
    artifacts: dict[str, ArtifactSnapshot] = {}
    for module_index, module in enumerate(compiled.tl_course.modules):
        for unit_index, unit in enumerate(module.lessons):
            if unit.import_key in artifacts:
                raise ValueError(
                    f"compiled course contains duplicate unit key '{unit.import_key}'"
                )
            unit_path = (
                f"modules/{module_index}:{module.import_key}/units/{unit_index}:{unit.import_key}"
            )
            artifacts[unit.import_key] = artifact_from_tl_unit(
                course_id=compiled.tl_course.import_key,
                module_key=module.import_key,
                unit=unit,
                unit_path=unit_path,
                relaxations=compiled.relaxations_by_unit.get(unit.import_key, ()),
            )
    return artifacts


def _artifact_course(artifact: ArtifactSnapshot) -> TLCourse:
    questions = [TLQuestion.model_validate(item.payload) for item in artifact.items]
    unit = TLUnit(
        import_key=artifact.session_id,
        title=str(artifact.metadata.get("unit_title") or artifact.session_id),
        slo=str(artifact.metadata.get("unit_slo") or "Qualitative QA candidate."),
        exercises=questions,
    )
    return TLCourse(
        import_key=artifact.course_id,
        title=artifact.course_id,
        modules=[
            TLModule(
                import_key=str(artifact.metadata.get("module_key") or "gauntlet"),
                title="Gauntlet",
                lessons=[unit],
            )
        ],
    )


def hard_gate_artifact(
    artifact: ArtifactSnapshot,
    *,
    experience_policy: ExperiencePolicy = ExperiencePolicy(),
    sequence_policy: SequenceQualityPolicy | None = None,
) -> HardGateResult:
    """Authoritative schema, answer, identity, and final-sequence gate."""

    artifact_sha256 = artifact.content_hash()
    issues: list[HardGateIssue] = []
    try:
        course = _artifact_course(artifact)
    except ValueError as exc:
        return HardGateResult(
            artifact_sha256=artifact_sha256,
            passed=False,
            issues=[HardGateIssue(code="schema", path=artifact.session_id, message=str(exc))],
        )

    for item, question in zip(artifact.items, course.modules[0].lessons[0].exercises):
        emitted_key = question.options.get("item_key")
        if emitted_key != item.item_key:
            issues.append(
                HardGateIssue(
                    code="identity",
                    path=item.path,
                    message="artifact item_key is missing from or does not match question metadata",
                )
            )
    issues.extend(
        HardGateIssue(code="techlingo_schema", path=artifact.session_id, message=problem)
        for problem in validate_techlingo_course(course)
    )
    raw_relaxations = artifact.metadata.get("constraint_relaxations", [])
    try:
        relaxations = tuple(ConstraintRelaxation(**value) for value in raw_relaxations)
    except (TypeError, ValueError) as exc:
        issues.append(
            HardGateIssue(
                code="relaxation_schema",
                path=artifact.session_id,
                message=f"invalid constraint relaxation metadata: {exc}",
            )
        )
        relaxations = ()
    quality = validate_tl_course(
        course,
        policy=sequence_policy or SequenceQualityPolicy(experience=experience_policy),
        relaxations_by_unit={artifact.session_id: relaxations},
    )
    issues.extend(
        HardGateIssue(
            code=f"sequence:{issue.code}",
            path=issue.item_paths[0] if issue.item_paths else issue.unit_path,
            message=issue.message,
        )
        for issue in quality.issues
        if issue.severity == "error"
    )
    return HardGateResult(
        artifact_sha256=artifact_sha256,
        passed=not issues,
        issues=issues,
        checks={
            "schema_and_answers": not any(
                issue.code in {"schema", "techlingo_schema"} for issue in issues
            ),
            "identity": not any(issue.code == "identity" for issue in issues),
            "final_sequence": not any(
                issue.code.startswith("sequence:") for issue in issues
            ),
        },
        constraint_relaxations=[
            f"{relaxation.constraint}: {relaxation.reason}" for relaxation in relaxations
        ],
    )


def sequence_policy_from_compile_config(config) -> SequenceQualityPolicy:
    """Map the YAML-facing compile policy to the authoritative validator policy."""

    experience = config.experience
    quality = config.sequence_quality
    return SequenceQualityPolicy(
        experience=ExperiencePolicy(
            max_same_mechanic_streak=experience.max_same_mechanic_streak,
            max_same_ui_family_streak=experience.max_same_ui_family_streak,
            max_same_true_false_answer_streak=experience.max_same_true_false_answer_streak,
            mechanics_window_size=experience.mechanics_window_size,
            min_mechanics_per_window=experience.min_mechanics_per_window,
            avoid_adjacent_same_concept=experience.avoid_adjacent_same_concept,
            max_search_states=experience.max_search_states,
            relaxation_order=tuple(experience.relaxation_order),
        ),
        max_same_correct_position_streak=quality.max_same_correct_position_streak,
        prompt_stem_words=quality.prompt_stem_words,
        max_repeated_prompt_stem=quality.max_repeated_prompt_stem,
        max_downward_rung_jump=quality.max_downward_rung_jump,
        max_same_rung_streak=quality.max_same_rung_streak,
    )


def source_excerpts_for_artifact(
    ws: Workspace,
    artifact: ArtifactSnapshot,
) -> list[SourceExcerpt]:
    curriculum = ws.load_curriculum()
    module_keys = {
        str(item.payload.get("options", {}).get("module_key") or artifact.metadata.get("module_key") or "")
        for item in artifact.items
    }
    source_names = {
        module.source_file
        for module in curriculum.modules
        if module.key in module_keys and module.source_file
    }
    excerpts: list[SourceExcerpt] = []
    if ws.sources_dir.is_symlink():
        raise GauntletIOError("workspace sources/ directory cannot be a symlink")
    try:
        source_root = ws.sources_dir.resolve(strict=True)
        source_root.relative_to(ws.root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise GauntletIOError(
            "workspace sources/ directory is missing or escapes the workspace"
        ) from exc
    for name in sorted(source_names):
        relative = Path(name)
        if (
            relative.is_absolute()
            or len(relative.parts) != 1
            or relative.name in {"", ".", ".."}
        ):
            raise GauntletIOError(
                f"source_file must be a filename inside sources/: {name!r}"
            )
        path = ws.sources_dir / relative
        # Reject symlinks even when their current target happens to remain
        # inside sources/: later retargeting must not turn an approved context
        # into an exfiltration path.
        if path.is_symlink():
            raise GauntletIOError(f"source_file cannot be a symlink: {name!r}")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(source_root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise GauntletIOError(
                f"source_file is missing or escapes sources/: {name!r}"
            ) from exc
        if not resolved.is_file():
            raise GauntletIOError(f"source_file is not a regular file: {name!r}")
        text = resolved.read_text(encoding="utf-8")
        excerpts.append(
            SourceExcerpt(
                source_id=name,
                text=text,
                location=str(path.relative_to(ws.root)),
                source_hash=sha256_text(text),
            )
        )
    return excerpts


def _publication_gauntlet_config(settings) -> object:
    """Normalize YAML-facing critic identity exactly as the CLI does."""

    from .backends import resolve_backend_name, split_backend_label
    from .gauntlet import GauntletConfig

    values = settings.model_dump(mode="python")
    raw_backend = values.get("critic_backend")
    raw_model = values.get("critic_model")
    if not raw_backend or not raw_model:
        raise GauntletIOError(
            "qualitative publication requires configured critic_backend and critic_model"
        )
    backend = resolve_backend_name(str(raw_backend))
    try:
        model_backend, _model = split_backend_label(str(raw_model))
    except ValueError:
        model_label = f"{backend}:{raw_model}"
    else:
        if model_backend != backend:
            raise GauntletIOError(
                "critic_backend conflicts with backend-qualified critic_model"
            )
        model_label = str(raw_model)
    values.update({"critic_backend": backend, "critic_model": model_label})
    try:
        return GauntletConfig.from_mapping(values)
    except ValueError as exc:
        raise GauntletIOError(f"invalid Gauntlet publication policy: {exc}") from exc


def publication_evaluation_contexts(
    course_dir: str | Path,
    compiled,
    *,
    required_artifacts: Optional[Mapping[str, ArtifactSnapshot]] = None,
) -> dict[str, EvaluationContext]:
    """Rebuild the exact current qualitative context for bundle publication.

    This is the compiler-facing integration API.  It deliberately mirrors the
    production CLI roles: fresh isolated critic/editor/comparator clients using
    the configured critic backend/model, the default rubric/goals, current
    source bytes, and the complete current approved-reference set for the
    course.  Any context drift invalidates prior evidence.
    """

    from .gauntlet import (
        DEFAULT_GAUNTLET_GOAL,
        SOURCE_FIDELITY_GOAL,
        default_critic_rubric,
    )

    ws = Workspace(course_dir).require()
    artifacts = dict(required_artifacts or compiled_unit_artifacts(compiled))
    config = _publication_gauntlet_config(compiled.cfg.gauntlet)
    approved = [
        reference
        for _path, reference in iter_course_references(
            course_dir, status=ReferenceStatus.approved
        )
        if reference.context.course_id == compiled.tl_course.import_key
    ]
    models = EvaluationModelProvenance(
        builder_model=config.builder_model,
        critic_backend=config.critic_backend,
        critic_model=config.critic_model,
        critic_fresh_context=True,
        editor_backend=config.critic_backend,
        editor_model=config.critic_model,
        editor_fresh_context=True,
        comparator_backend=config.critic_backend,
        comparator_model=config.critic_model,
        comparator_fresh_context=True,
    )
    contexts: dict[str, EvaluationContext] = {}
    for unit_key, artifact in sorted(artifacts.items()):
        if artifact.session_id != unit_key:
            raise GauntletIOError(
                f"artifact key {unit_key!r} does not match session {artifact.session_id!r}"
            )
        sources = source_excerpts_for_artifact(ws, artifact)
        if not sources:
            raise GauntletIOError(
                f"gauntlet/{unit_key}: no relevant source material could be resolved"
            )
        contexts[unit_key] = EvaluationContext.create(
            goal=DEFAULT_GAUNTLET_GOAL,
            source_fidelity_goal=SOURCE_FIDELITY_GOAL,
            gauntlet_policy=config.to_mapping(),
            rubric=default_critic_rubric(),
            source_material=sources,
            approved_reference_sessions=approved,
            models=models,
        )
    return contexts


def write_gauntlet_record(
    course_dir: str | Path,
    *,
    compiled_artifact_sha256: str,
    unit_key: str,
    outcome: GauntletOutcome,
) -> Path:
    ws = Workspace(course_dir).require()
    with ws.publication_lock():
        if outcome.champion.session_id != unit_key:
            raise GauntletIOError(
                f"record unit_key {unit_key!r} does not match champion session "
                f"{outcome.champion.session_id!r}"
            )
        context = outcome.history.evaluation_context
        if context is None:
            raise GauntletIOError(
                "new Gauntlet history must bind a canonical evaluation context"
            )
        coherence = outcome.history.coherence_errors()
        if coherence:
            raise GauntletIOError(
                "Gauntlet history is internally inconsistent: " + "; ".join(coherence)
            )
        derived = outcome.history.derived_publication_eligible(
            expected_context=context
        )
        if outcome.history.publication_eligible != derived:
            raise GauntletIOError(
                "stored publication_eligible does not match the derived history evidence"
            )
        record = GauntletRecord(
            compiled_artifact_sha256=compiled_artifact_sha256,
            unit_key=unit_key,
            champion=outcome.champion,
            history=outcome.history,
        )
        unit_file = _safe_filename_component(unit_key)
        run_file = _safe_filename_component(outcome.history.run_id)
        history_root = _safe_gauntlet_directory(ws, "history")
        path = history_root / f"{unit_file}-{run_file}.json"
        if path.exists():
            raise GauntletIOError(f"Gauntlet history is immutable and already exists: {path}")
        _atomic_write_text(path, record.model_dump_json(indent=2) + "\n")
        return path


def load_gauntlet_record(path: str | Path) -> GauntletRecord:
    path = Path(path)
    try:
        return GauntletRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GauntletIOError(f"could not load Gauntlet record {path}: {exc}") from exc


def iter_gauntlet_records(
    course_dir: str | Path,
    *,
    strict: bool = False,
) -> Iterable[tuple[Path, GauntletRecord]]:
    ws = Workspace(course_dir).require()
    root = _safe_gauntlet_directory(ws, "history")
    if not root.exists():
        return iter(())
    records: list[tuple[Path, GauntletRecord]] = []
    for path in sorted(root.glob("*.json")):
        try:
            if path.is_symlink():
                raise GauntletIOError(f"Gauntlet history cannot be a symlink: {path}")
            records.append((path, load_gauntlet_record(path)))
        except GauntletIOError:
            if strict:
                raise
            continue
    return iter(records)


def reference_path(
    course_dir: str | Path,
    reference_id: str,
    *,
    status: ReferenceStatus,
) -> Path:
    """Return the canonical workspace path for a safe reference id."""

    if not REFERENCE_ID_PATTERN.fullmatch(reference_id):
        raise GauntletIOError(
            "reference_id must start with an alphanumeric character and contain "
            "only letters, digits, '.', '_' or '-'"
        )
    ws = Workspace(course_dir).require()
    directory = "approved" if status is ReferenceStatus.approved else "drafts"
    root = _safe_gauntlet_directory(ws, "references", directory)
    return root / f"{reference_id}.json"


def write_course_reference(
    course_dir: str | Path,
    reference: ReferenceSession,
) -> Path:
    ws = Workspace(course_dir).require()
    with ws.publication_lock():
        path = reference_path(ws.root, reference.reference_id, status=reference.status)
        try:
            write_reference(path, reference)
        except ReferenceError as exc:
            raise GauntletIOError(str(exc)) from exc
        return path


def promote_course_reference(
    course_dir: str | Path,
    reference_id: str,
    *,
    approved_by: str,
    note: str | None = None,
) -> tuple[Path, Path, ReferenceSession]:
    """Load, approve, and persist one draft under the publication lock."""

    ws = Workspace(course_dir).require()
    with ws.publication_lock():
        draft_path = reference_path(
            ws.root, reference_id, status=ReferenceStatus.draft
        )
        approved_path = reference_path(
            ws.root, reference_id, status=ReferenceStatus.approved
        )
        if draft_path.is_symlink():
            raise GauntletIOError(f"reference draft cannot be a symlink: {draft_path}")
        try:
            draft = load_reference(draft_path)
            approved = promote_reference(
                draft, approved_by=approved_by, note=note
            )
            write_reference(approved_path, approved)
        except ReferenceError as exc:
            raise GauntletIOError(str(exc)) from exc
        return draft_path, approved_path, approved


def iter_course_references(
    course_dir: str | Path,
    *,
    status: ReferenceStatus | None = None,
) -> Iterable[tuple[Path, ReferenceSession]]:
    ws = Workspace(course_dir).require()
    statuses = (status,) if status is not None else (
        ReferenceStatus.draft,
        ReferenceStatus.approved,
    )
    entries: list[tuple[Path, ReferenceSession]] = []
    for selected in statuses:
        directory = "approved" if selected is ReferenceStatus.approved else "drafts"
        root = _safe_gauntlet_directory(ws, "references", directory)
        if not root.exists():
            continue
        for path in sorted(root.glob("*.json")):
            if path.is_symlink():
                raise GauntletIOError(f"reference cannot be a symlink: {path}")
            try:
                reference = load_reference(path)
            except ReferenceError as exc:
                raise GauntletIOError(str(exc)) from exc
            if reference.status is not selected:
                raise GauntletIOError(
                    f"reference {path} declares {reference.status.value!r} inside {directory!r}"
                )
            entries.append((path, reference))
    return iter(entries)


def _safe_filename_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.") or "record"
    if safe == value:
        return safe
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{suffix}"


def _safe_gauntlet_directory(ws: Workspace, *parts: str) -> Path:
    """Return one lexical Gauntlet directory after rejecting symlink escapes."""

    current = ws.root
    for component in ("gauntlet", *parts):
        current = current / component
        if current.is_symlink():
            raise GauntletIOError(
                f"Gauntlet storage directory cannot be a symlink: {current}"
            )
        if current.exists() and not current.is_dir():
            raise GauntletIOError(
                f"Gauntlet storage path is not a directory: {current}"
            )
    try:
        current.resolve(strict=False).relative_to(ws.root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise GauntletIOError(
            f"Gauntlet storage directory escapes the workspace: {current}"
        ) from exc
    return current


def _canonical_model_sha256(model: BaseModel) -> str:
    return canonical_sha256(model)


def qualitative_publication_coverage(
    course_dir: str | Path,
    *,
    compiled_artifact_sha256: str,
    required_artifacts: Mapping[str, ArtifactSnapshot],
    expected_contexts: Optional[Mapping[str, EvaluationContext]] = None,
) -> QualitativePublicationCoverage:
    """Match every current unit to an exact publication-eligible record.

    Course hash and unit key are necessary but not sufficient: the persisted
    champion must have the same content/order hash as the artifact which will
    actually ship.  When several records cover the same exact unit, the most
    recently finished result is referenced deterministically.
    """

    ws = Workspace(course_dir).require()
    records = list(iter_gauntlet_records(course_dir))
    blockers: list[str] = []
    references: list[GauntletRecordReference] = []
    for unit_key, artifact in sorted(required_artifacts.items()):
        if artifact.session_id != unit_key:
            blockers.append(
                f"gauntlet/{unit_key}: current artifact session_id is "
                f"'{artifact.session_id}', so exact coverage cannot be established"
            )
            continue
        expected_context = (
            expected_contexts.get(unit_key) if expected_contexts is not None else None
        )
        if expected_context is None:
            blockers.append(
                f"gauntlet/{unit_key}: exact current evaluation context is required for coverage"
            )
            continue
        expected_champion_hash = artifact.content_hash()
        content_matches = [
            (path, record)
            for path, record in records
            if record.compiled_artifact_sha256 == compiled_artifact_sha256
            and record.unit_key == unit_key
            and record.champion.content_hash() == expected_champion_hash
        ]
        matches = [
            (path, record)
            for path, record in content_matches
            if not record.history.publication_blockers(
                expected_context=expected_context
            )
        ]
        if not matches:
            detail = ""
            if content_matches:
                evidence_errors = content_matches[-1][1].history.publication_blockers(
                    expected_context=expected_context
                )
                detail = "; evidence rejected: " + "; ".join(evidence_errors[:3])
            blockers.append(
                f"gauntlet/{unit_key}: no publication-eligible record matches "
                f"compiled artifact {compiled_artifact_sha256} and champion "
                f"{expected_champion_hash} under evaluation context "
                f"{expected_context.context_sha256}{detail}"
            )
            continue
        path, record = max(
            matches,
            key=lambda pair: (
                pair[1].history.finished_at.isoformat(),
                pair[1].history.run_id,
                pair[0].as_posix(),
            ),
        )
        references.append(
            GauntletRecordReference(
                unit_key=unit_key,
                compiled_artifact_sha256=compiled_artifact_sha256,
                champion_artifact_sha256=expected_champion_hash,
                evaluation_context_sha256=expected_context.context_sha256,
                gauntlet_policy_sha256=expected_context.gauntlet_policy_sha256,
                record_sha256=sha256_text(path.read_text(encoding="utf-8")),
                history_sha256=_canonical_model_sha256(record.history),
                record_path=str(path.relative_to(ws.root)),
                run_id=record.history.run_id,
                finished_at=record.history.finished_at,
            )
        )
    return QualitativePublicationCoverage(
        blockers=tuple(blockers),
        references=tuple(references),
    )


def qualitative_publication_blockers(
    course_dir: str | Path,
    *,
    compiled_artifact_sha256: str,
    required_unit_keys: Iterable[str] = (),
    required_artifacts: Optional[Mapping[str, ArtifactSnapshot]] = None,
    expected_contexts: Optional[Mapping[str, EvaluationContext]] = None,
) -> list[str]:
    """Backward-compatible blocker facade; exact artifacts are authoritative.

    A unit-key-only request cannot establish that the reviewed champion is the
    artifact being published, so it fails closed instead of preserving the old
    unsafe approximation.
    """

    if required_artifacts is None:
        return [
            f"gauntlet/{unit_key}: exact current champion artifact is required for coverage"
            for unit_key in required_unit_keys
        ]
    required_keys = set(required_unit_keys)
    missing_artifacts = sorted(required_keys - set(required_artifacts))
    blockers = [
        f"gauntlet/{unit_key}: exact current champion artifact is required for coverage"
        for unit_key in missing_artifacts
    ]
    blockers.extend(
        qualitative_publication_coverage(
            course_dir,
            compiled_artifact_sha256=compiled_artifact_sha256,
            required_artifacts=required_artifacts,
            expected_contexts=expected_contexts,
        ).blockers
    )
    return blockers


__all__ = [
    "GAUNTLET_RECORD_SCHEMA",
    "GauntletIOError",
    "GauntletRecord",
    "GauntletRecordReference",
    "QualitativePublicationCoverage",
    "artifact_from_tl_unit",
    "compiled_unit_artifacts",
    "hard_gate_artifact",
    "iter_course_references",
    "iter_gauntlet_records",
    "load_gauntlet_record",
    "publication_evaluation_contexts",
    "promote_course_reference",
    "qualitative_publication_blockers",
    "qualitative_publication_coverage",
    "reference_path",
    "sequence_policy_from_compile_config",
    "source_excerpts_for_artifact",
    "write_course_reference",
    "write_gauntlet_record",
]
