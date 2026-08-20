"""Deterministic publication eligibility and provenance hashing.

Compilation can be useful as a read-only diagnostic even when a workspace is
in progress.  Publication is different: every current source must have passed
the hard pipeline validation for its exact bytes and workflow configuration,
and the canonical bank must still be the one recorded at promotion time.

This module deliberately has no LLM dependencies and does not inspect or
alter compiler ordering.  It is shared by the course CLI, bundle writer, and
tests so there is one authoritative publication gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .workspace import (
    BUILD_STATE_SCHEMA,
    LessonBank,
    Workspace,
    WorkspaceError,
    canonical_json,
    sha256_text,
)


class PublicationSafetyError(WorkspaceError):
    """Raised when an operation would publish an unvalidated workspace."""

    def __init__(self, blockers: Iterable[str]) -> None:
        self.blockers = list(blockers)
        preview = "; ".join(self.blockers[:5])
        if len(self.blockers) > 5:
            preview += f"; and {len(self.blockers) - 5} more"
        super().__init__(f"workspace is not publishable: {preview}")


@dataclass(frozen=True)
class PublicationTrace:
    """Hashes which bind a build and bundle to their exact inputs."""

    source_hashes: dict[str, str]
    source_set_sha256: str
    validation_report_hashes: dict[str, str | None]
    validation_set_sha256: str
    workflow_config_sha256: str
    compile_config_sha256: str
    bank_sha256: str
    course_meta_sha256: str
    curriculum_sha256: str
    concept_graph_sha256: str
    artifact_sha256: str | None = None

    def with_artifact(self, artifact: Any) -> "PublicationTrace":
        return PublicationTrace(
            source_hashes=dict(self.source_hashes),
            source_set_sha256=self.source_set_sha256,
            validation_report_hashes=dict(self.validation_report_hashes),
            validation_set_sha256=self.validation_set_sha256,
            workflow_config_sha256=self.workflow_config_sha256,
            compile_config_sha256=self.compile_config_sha256,
            bank_sha256=self.bank_sha256,
            course_meta_sha256=self.course_meta_sha256,
            curriculum_sha256=self.curriculum_sha256,
            concept_graph_sha256=self.concept_graph_sha256,
            artifact_sha256=hash_data(artifact),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_hashes": dict(self.source_hashes),
            "source_set_sha256": self.source_set_sha256,
            "validation_report_hashes": dict(self.validation_report_hashes),
            "validation_set_sha256": self.validation_set_sha256,
            "workflow_config_sha256": self.workflow_config_sha256,
            "compile_config_sha256": self.compile_config_sha256,
            "bank_sha256": self.bank_sha256,
            "course_meta_sha256": self.course_meta_sha256,
            "curriculum_sha256": self.curriculum_sha256,
            "concept_graph_sha256": self.concept_graph_sha256,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass
class PublicationReadiness:
    trace: PublicationTrace
    blockers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def require(self) -> PublicationTrace:
        if self.blockers:
            raise PublicationSafetyError(self.blockers)
        return self.trace


def hash_data(data: Any) -> str:
    """Hash JSON-like data using the workspace's canonical serializer."""

    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    return sha256_text(canonical_json(data))


def banks_sha256(banks: Mapping[str, LessonBank] | Iterable[LessonBank]) -> str:
    """Hash an exact, path-addressed bank set independent of filesystem order."""

    if isinstance(banks, Mapping):
        pairs = banks.items()
    else:
        pairs = ((bank.lesson, bank) for bank in banks)
    payload = [
        {"lesson": lesson, "bank": bank.model_dump(mode="json")}
        for lesson, bank in sorted(pairs, key=lambda pair: pair[0])
    ]
    return hash_data(payload)


def source_set_sha256(source_hashes: Mapping[str, str]) -> str:
    return hash_data(dict(sorted(source_hashes.items())))


def inspect_publication_readiness(course_dir: str | Path) -> PublicationReadiness:
    """Return all blocking publication problems plus the current input trace.

    Legacy build-state-v1 files remain loadable for diagnostics and migration,
    but are not publication evidence.  Publication fails closed unless every
    current source has the complete v2 source/config/report/LKG binding and the
    current canonical bank set matches its recorded promotion hash.
    """

    ws = Workspace(course_dir).require()
    with ws.publication_lock():
        return _inspect_publication_readiness_locked(ws)


def _inspect_publication_readiness_locked(ws: Workspace) -> PublicationReadiness:
    """Inspect one lock-protected, internally consistent workspace snapshot."""

    meta = ws.load_meta()
    curriculum = ws.load_curriculum()
    graph = ws.load_graph()
    state = ws.load_build_state()
    sources = list(ws.iter_sources())
    source_hashes = {source.name: ws.source_hash(source) for source in sources}
    bank_entries = list(ws.iter_banks())
    banks: dict[str, LessonBank] = {}
    duplicate_bank_lessons: set[str] = set()
    for bank in bank_entries:
        if bank.lesson in banks:
            duplicate_bank_lessons.add(bank.lesson)
        else:
            banks[bank.lesson] = bank

    # Deferred to avoid a module cycle: course_build imports workspace, while
    # publication preflight needs the same authoritative config resolver.
    from .course_build import config_hash, resolve_build_config

    workflow_hash = config_hash(resolve_build_config(meta.workflow, meta.difficulty))
    compile_hash = hash_data(ws.load_compile_config())
    current_bank_hash = banks_sha256(banks)
    validation_report_hashes = {
        source.name: (
            state.sources[source.name].validation_report_sha256
            if source.name in state.sources
            else None
        )
        for source in sources
    }
    blockers: list[str] = []

    if state.schema_version != BUILD_STATE_SCHEMA:
        blockers.append(
            "build_state.json: legacy publication evidence must be rebuilt before publication"
        )
    if state.workflow_config_hash is None:
        blockers.append("build_state.json: promoted workflow configuration hash is missing")
    elif state.workflow_config_hash != workflow_hash:
        blockers.append(
            "build_state.json: promoted workflow configuration differs from the current configuration"
        )
    for lesson in sorted(duplicate_bank_lessons):
        blockers.append(f"bank/{lesson}: duplicate bank lesson identity")

    current_names = set(source_hashes)
    recorded_names = set(state.sources)
    if not sources:
        blockers.append("sources: no source documents are present")
    for stale in sorted(recorded_names - current_names):
        blockers.append(f"sources/{stale}: build state/content remains for a removed source")

    for source in sources:
        path = f"sources/{source.name}"
        source_state = state.sources.get(source.name)
        if source_state is None:
            blockers.append(f"{path}: has no completed validation record")
            continue
        if source_state.sha256 != source_hashes[source.name]:
            blockers.append(f"{path}: content changed after its last build attempt")
        if source_state.status != "ok":
            detail = f" ({source_state.error})" if source_state.error else ""
            blockers.append(f"{path}: latest build failed{detail}")
        if source_state.validation_ok is not True:
            blockers.append(f"{path}: hard validation did not pass")
        source_config_hash = source_state.config_sha256
        if source_config_hash is None:
            blockers.append(f"{path}: validation is not traceable to a workflow configuration")
        elif source_config_hash != workflow_hash:
            blockers.append(f"{path}: was not validated with the current workflow configuration")

        if source_state.validation_report_sha256 is None:
            blockers.append(f"{path}: validation report hash is missing")

        lkg = source_state.last_known_good
        if lkg is None:
            blockers.append(f"{path}: last-known-good publication binding is missing")
        else:
            if lkg.source_sha256 != source_hashes[source.name]:
                blockers.append(f"{path}: last-known-good source hash does not match")
            if lkg.config_sha256 != workflow_hash:
                blockers.append(f"{path}: last-known-good configuration hash does not match")
            if lkg.validation_report_sha256 != source_state.validation_report_sha256:
                blockers.append(f"{path}: last-known-good validation report hash does not match")
            if lkg.module_keys != source_state.module_keys:
                blockers.append(f"{path}: last-known-good module ownership does not match")
            source_banks = {key: bank for key, bank in banks.items() if bank.module in lkg.module_keys}
            if banks_sha256(source_banks) != lkg.bank_sha256:
                blockers.append(f"{path}: promoted bank content changed after validation")

    curriculum_lessons: dict[str, str] = {}
    for module in curriculum.modules:
        if module.source_file is not None and module.source_file not in current_names:
            blockers.append(
                f"curriculum/modules/{module.key}: references missing source '{module.source_file}'"
            )
        for lesson in module.lessons:
            curriculum_lessons[lesson.key] = module.key
            if lesson.key not in banks and not module.authored:
                blockers.append(f"curriculum/modules/{module.key}/lessons/{lesson.key}: bank is missing")
            elif lesson.key in banks and banks[lesson.key].module != module.key:
                blockers.append(
                    f"bank/{lesson.key}: declares module '{banks[lesson.key].module}', expected '{module.key}'"
                )
    for orphan in sorted(set(banks) - set(curriculum_lessons)):
        blockers.append(f"bank/{orphan}: is not referenced by curriculum")

    if state.bank_sha256 is None:
        blockers.append("bank: promoted workspace hash is missing")
    elif state.bank_sha256 != current_bank_hash:
        blockers.append("bank: canonical bank hash differs from the last promoted build state")

    trace = PublicationTrace(
        source_hashes=source_hashes,
        source_set_sha256=source_set_sha256(source_hashes),
        validation_report_hashes=validation_report_hashes,
        validation_set_sha256=hash_data(validation_report_hashes),
        workflow_config_sha256=workflow_hash,
        compile_config_sha256=compile_hash,
        bank_sha256=current_bank_hash,
        course_meta_sha256=hash_data(meta),
        curriculum_sha256=hash_data(curriculum),
        concept_graph_sha256=hash_data(graph),
    )
    return PublicationReadiness(trace=trace, blockers=blockers)


def require_publishable(course_dir: str | Path) -> PublicationTrace:
    """Raise with actionable paths unless every hard publication gate passes."""

    return inspect_publication_readiness(course_dir).require()
