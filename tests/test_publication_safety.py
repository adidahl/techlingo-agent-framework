"""Regression tests for build/publication safety (deterministic, no LLM).

Run without pytest:  PYTHONPATH=src python3 tests/test_publication_safety.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from techlingo_workflow.compiler import (
    _relaxations_payload,
    _sequence_quality_policy,
    compile_workspace,
    write_bundle,
)
from techlingo_workflow.course_build import (
    _promote_generated_state,
    build_course,
    config_hash,
    plan_build,
    resolve_build_config,
)
from techlingo_workflow.models import (
    BloomsLevel,
    Course,
    Module,
    ValidationIssue,
    ValidationReport,
    WorkflowRunResult,
)
from techlingo_workflow.gauntlet_io import (
    compiled_unit_artifacts,
    publication_evaluation_contexts,
    qualitative_publication_blockers,
    qualitative_publication_coverage,
    write_gauntlet_record,
)
from techlingo_workflow.gauntlet_models import (
    ALL_QUALITY_DIMENSIONS,
    ArtifactSnapshot,
    CriticEvaluation,
    CriticEvidence,
    CriticResult,
    DimensionAssessment,
    EvaluationContext,
    GauntletHistory,
    GauntletOutcome,
    HardGateResult,
    ModelIndependence,
    RoundDecision,
    RoundHistory,
    StopReason,
    UsageRecord,
    text_sha256,
)
from techlingo_workflow.publication_safety import (
    PublicationSafetyError,
    banks_sha256,
    hash_data,
    inspect_publication_readiness,
)
from techlingo_workflow.sequence_quality import validate_tl_course
from techlingo_workflow.workspace import (
    BankItem,
    BuildState,
    CompileConfig,
    Concept,
    ConceptGraph,
    Curriculum,
    CurriculumLesson,
    CurriculumModule,
    LessonBank,
    SourcePublication,
    SourceState,
    Workspace,
    init_workspace,
    payload_hash,
    sha256_text,
    utc_now_iso,
)


def _workspace(tmp: Path) -> Workspace:
    docs = tmp / "docs"
    docs.mkdir()
    source = docs / "1. Source.md"
    source.write_text("# Source\nGrounded fact.", encoding="utf-8")
    ws = init_workspace(
        tmp / "course",
        course_id="safe-course",
        title="Safe Course",
        source_files=[source],
    )
    ws.save_compile_config(CompileConfig(levels=1, checkpoints="none", final_review=False))
    ws.save_graph(
        ConceptGraph(
            concepts=[
                Concept(
                    id="grounded-fact",
                    label="Grounded fact",
                    summary="A fact supported by the source.",
                    source={"file": source.name},
                    lessons=["lesson"],
                )
            ]
        )
    )
    ws.save_curriculum(
        Curriculum(
            modules=[
                CurriculumModule(
                    key="module",
                    title="Module",
                    source_file=source.name,
                    lessons=[
                        CurriculumLesson(
                            key="lesson",
                            title="Lesson",
                            slo="Recall the grounded fact.",
                            concepts=["grounded-fact"],
                        )
                    ],
                )
            ]
        )
    )
    payload = {
        "blooms_level": BloomsLevel.understanding.value,
        "question_type": "true_false",
        "prompt": "True or false?",
        "concept_id": "grounded-fact",
        "statement": "This statement is grounded in the source.",
        "correct_answer": True,
        "feedback_for_correct": "Correct.",
        "feedback_for_incorrect": {
            "intrinsic": "The source directly supports it.",
            "instructional": "Review the grounded fact.",
        },
    }
    ws.save_bank(
        LessonBank(
            lesson="lesson",
            module="module",
            items=[
                BankItem(
                    item_key="lesson/grounded-fact/r2/v1",
                    concept_id="grounded-fact",
                    rung=2,
                    variant=1,
                    payload=payload,
                    payload_hash=payload_hash(payload),
                    source_hash=ws.source_hash(ws.sources_dir / source.name),
                )
            ],
        )
    )
    _mark_valid(ws)
    return ws


def _mark_valid(ws: Workspace) -> None:
    source = next(ws.iter_sources())
    meta = ws.load_meta()
    workflow_hash = config_hash(resolve_build_config(meta.workflow, meta.difficulty))
    source_banks = list(ws.iter_banks())
    report_hash = sha256_text("valid-report")
    promoted = utc_now_iso()
    ws.save_build_state(
        BuildState(
            workflow_config_hash=workflow_hash,
            bank_sha256=banks_sha256(source_banks),
            sources={
                source.name: SourceState(
                    sha256=ws.source_hash(source),
                    status="ok",
                    built_at=promoted,
                    module_keys=["module"],
                    validation_ok=True,
                    config_sha256=workflow_hash,
                    validation_report_sha256=report_hash,
                    last_known_good=SourcePublication(
                        source_sha256=ws.source_hash(source),
                        config_sha256=workflow_hash,
                        bank_sha256=banks_sha256(source_banks),
                        validation_report_sha256=report_hash,
                        promoted_at=promoted,
                        module_keys=["module"],
                    ),
                )
            },
        )
    )


def _canonical_bytes(ws: Workspace) -> dict[str, bytes]:
    paths = [ws.curriculum_path, ws.graph_path, *sorted(ws.bank_dir.glob("*.json"))]
    return {str(path.relative_to(ws.root)): path.read_bytes() for path in paths}


def _require_qualitative_gauntlet(ws: Workspace) -> None:
    cfg = ws.load_compile_config()
    cfg.gauntlet.critic_backend = "codex"
    cfg.gauntlet.critic_model = "codex:test"
    cfg.gauntlet.qualitative_required_for_publication = True
    ws.save_compile_config(cfg)


def _add_second_unit(ws: Workspace) -> None:
    curriculum = ws.load_curriculum()
    curriculum.modules[0].lessons.append(
        CurriculumLesson(
            key="lesson-two",
            title="Lesson Two",
            slo="Apply the grounded fact again.",
            concepts=["grounded-fact"],
        )
    )
    ws.save_curriculum(curriculum)
    graph = ws.load_graph()
    graph.concepts[0].lessons.append("lesson-two")
    ws.save_graph(graph)
    bank = ws.load_bank("lesson").model_copy(deep=True)
    bank.lesson = "lesson-two"
    bank.items[0].item_key = "lesson-two/grounded-fact/r2/v1"
    ws.save_bank(bank)
    _mark_valid(ws)


def _gauntlet_outcome(
    artifact: ArtifactSnapshot,
    *,
    context: EvaluationContext,
    run_id: str,
    eligible: bool = True,
    human_review_recommended: bool = False,
) -> GauntletOutcome:
    started = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    champion_hash = artifact.content_hash()
    ineligible = not eligible or human_review_recommended
    result = CriticResult(
        dimensions=[
            DimensionAssessment(
                dimension=dimension,
                score=0.2 if ineligible else 0.95,
                evidence=[CriticEvidence(statement=f"Observed {dimension.value}.")],
            )
            for dimension in ALL_QUALITY_DIMENSIONS
        ],
        largest_gap=None,
        confidence=0.95,
        human_review_recommended=human_review_recommended,
        concise_summary=(
            "Manual review is required."
            if ineligible
            else "Every configured qualitative threshold passes."
        ),
    )
    usage = UsageRecord(backend_calls=1)
    critic = CriticEvaluation(
        result=result,
        artifact_sha256=champion_hash,
        evaluation_context_sha256=context.context_sha256,
        goal_sha256=text_sha256(context.goal),
        critic_backend=context.models.critic_backend,
        critic_model=context.models.critic_model,
        fresh_context=context.models.critic_fresh_context,
        model_independence=ModelIndependence.unavailable,
        approved_reference_count=len(context.approved_references),
        reference_confidence_reduced=not context.approved_references,
        usage=usage,
    )
    round_ = RoundHistory(
        round_number=1,
        champion_hash_before=champion_hash,
        critic=critic,
        decision=(
            RoundDecision.retained_human
            if ineligible
            else RoundDecision.retained_no_repair
        ),
        decision_evidence=(
            "Quality did not pass and no safe repair was supplied."
            if ineligible
            else "All configured qualitative thresholds pass."
        ),
        champion_hash_after=champion_hash,
        cumulative_usage=usage,
    )
    return GauntletOutcome(
        champion=artifact,
        history=GauntletHistory(
            run_id=run_id,
            started_at=started,
            finished_at=started + timedelta(seconds=1),
            initial_champion_hash=champion_hash,
            final_champion_hash=champion_hash,
            evaluation_context=context,
            initial_hard_gate=HardGateResult(
                artifact_sha256=champion_hash,
                passed=True,
                checks={"all": True},
            ),
            rounds=[round_],
            stop_reason=(
                StopReason.human_review_required if ineligible else StopReason.success
            ),
            stop_evidence=(
                "Manual review is required."
                if ineligible
                else "The exact final artifact passed qualitative review."
            ),
            total_usage=usage,
            human_review_recommended=ineligible,
            publication_eligible=not ineligible,
        ),
    )


def _write_exact_gauntlet_records(
    ws: Workspace,
    compiled,
) -> dict[str, Path]:
    compiled_hash = hash_data(compiled.tl_course)
    artifacts = compiled_unit_artifacts(compiled)
    contexts = publication_evaluation_contexts(
        ws.root, compiled, required_artifacts=artifacts
    )
    paths: dict[str, Path] = {}
    for index, (unit_key, artifact) in enumerate(sorted(artifacts.items())):
        paths[unit_key] = write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256=compiled_hash,
            unit_key=unit_key,
            outcome=_gauntlet_outcome(
                artifact,
                context=contexts[unit_key],
                run_id=f"exact-{index}",
            ),
        )
    return paths


def test_invalid_challenger_never_replaces_last_known_good_workspace():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        before = _canonical_bytes(ws)
        report = ValidationReport(
            ok=False,
            issues=[ValidationIssue(severity="error", path="modules[0]", message="hard failure")],
        )
        result = WorkflowRunResult(
            run_id="invalid",
            run_dir="unused",
            course=Course(title="Invalid challenger", modules=[Module(title="Bad", lessons=[])]),
            validation_report=report,
        )

        async def fake_stream(*_args, **_kwargs):
            return result

        fake_workflow_module = types.ModuleType("techlingo_workflow.workflow")
        fake_workflow_module.build_techlingo_workflow = lambda: object()
        source = next(ws.iter_sources())
        with patch.dict(sys.modules, {"techlingo_workflow.workflow": fake_workflow_module}), patch(
            "techlingo_workflow.course_build.stream_pipeline", fake_stream
        ):
            outcomes = build_course(
                ws.root,
                model_label="test:model",
                force=True,
                echo=lambda _msg: None,
            )

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.ok is False and outcome.validation_ok is False
        assert "blocking validation failed" in (outcome.error or "")
        assert _canonical_bytes(ws) == before
        recorded = ws.load_build_state().sources[source.name]
        assert recorded.status == "failed" and recorded.validation_ok is False
        assert recorded.last_known_good is not None


def test_failed_validation_blocks_new_bundle_and_keeps_previous_bundle():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        first = write_bundle(ws.root, compile_workspace(ws.root))
        state = ws.load_build_state()
        source = next(ws.iter_sources())
        previous_lkg = state.sources[source.name].last_known_good
        state.sources[source.name] = SourceState(
            sha256=ws.source_hash(source),
            status="failed",
            validation_ok=False,
            config_sha256=state.workflow_config_hash,
            error="blocking validation failed",
            last_known_good=previous_lkg,
        )
        ws.save_build_state(state)

        try:
            write_bundle(ws.root, compile_workspace(ws.root))
            assert False, "expected publication refusal"
        except PublicationSafetyError as error:
            assert any("hard validation did not pass" in blocker for blocker in error.blockers)
        assert first.bundle_dir.exists()
        assert not (ws.dist_dir / "safe-course-v2").exists()


def test_source_and_bank_hash_drift_are_blocking():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        source = next(ws.iter_sources())
        original = source.read_text(encoding="utf-8")
        source.write_text(original + "\nchanged", encoding="utf-8")
        report = inspect_publication_readiness(ws.root)
        assert any("content changed" in blocker for blocker in report.blockers)

        source.write_text(original, encoding="utf-8")
        bank = ws.load_bank("lesson")
        bank.items[0].payload["statement"] = "Tampered after validation."
        bank.items[0].payload_hash = payload_hash(bank.items[0].payload)
        ws.save_bank(bank)
        report = inspect_publication_readiness(ws.root)
        assert any("promoted bank content changed" in blocker for blocker in report.blockers)
        assert any("canonical bank hash differs" in blocker for blocker in report.blockers)


def test_workflow_configuration_drift_is_blocking():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        meta = ws.load_meta()
        meta.difficulty = "advanced"
        ws.save_meta(meta)
        report = inspect_publication_readiness(ws.root)
        assert any("current workflow configuration" in blocker for blocker in report.blockers)


def test_exact_worksheet_item_policy_is_a_publication_gate():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        graph = ws.load_graph()
        graph.concepts[0].depth = "fact"
        ws.save_graph(graph)
        meta = ws.load_meta()
        meta.workflow = {"worksheet_items_per_lesson": 3}
        ws.save_meta(meta)
        _mark_valid(ws)
        report = inspect_publication_readiness(ws.root)
        assert any(
            "exact worksheet policy requires 3 active items, got 1" in blocker
            for blocker in report.blockers
        ), report.blockers

        bank = ws.load_bank("lesson")
        for variant in (2, 3):
            extra = bank.items[0].model_copy(deep=True)
            extra.item_key = f"lesson/grounded-fact/r2/v{variant}"
            extra.variant = variant
            bank.items.append(extra)
        ws.save_bank(bank)
        _mark_valid(ws)  # all hashes genuinely bind the three-item bank
        assert inspect_publication_readiness(ws.root).ok

        bank = ws.load_bank("lesson")
        extra = bank.items[0].model_copy(deep=True)
        extra.item_key = "lesson/grounded-fact/r2/v4"
        extra.variant = 4
        bank.items.append(extra)
        ws.save_bank(bank)
        _mark_valid(ws)  # all hashes genuinely bind the four-item bank

        report = inspect_publication_readiness(ws.root)
        assert any(
            "exact worksheet policy requires 3 active items, got 4" in blocker
            for blocker in report.blockers
        ), report.blockers


def test_infeasible_worksheet_item_budget_is_a_publication_gate():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        graph = ws.load_graph()
        graph.concepts[0].depth = "fact"
        ws.save_graph(graph)
        meta = ws.load_meta()
        meta.workflow = {"worksheet_items_per_lesson": 1}
        ws.save_meta(meta)
        _mark_valid(ws)

        report = inspect_publication_readiness(ws.root)
        assert any(
            "worksheet item budget 1 is infeasible" in blocker
            and "at least 3 rows" in blocker
            and "at most 5" in blocker
            for blocker in report.blockers
        ), report.blockers


def test_exact_worksheet_policy_blocks_missing_concept_refs_and_still_checks_count():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        curriculum = ws.load_curriculum()
        curriculum.modules[0].lessons[0].concepts = []
        ws.save_curriculum(curriculum)
        meta = ws.load_meta()
        meta.workflow = {"worksheet_items_per_lesson": 3}
        ws.save_meta(meta)
        _mark_valid(ws)

        report = inspect_publication_readiness(ws.root)

        assert any(
            "exact worksheet policy requires 3 active items, got 1" in blocker
            for blocker in report.blockers
        ), report.blockers
        assert any(
            "worksheet" in blocker.lower()
            and "concept" in blocker.lower()
            and "missing" in blocker.lower()
            for blocker in report.blockers
        ), report.blockers


def test_exact_worksheet_policy_blocks_unresolved_concept_refs_and_still_checks_count():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        curriculum = ws.load_curriculum()
        curriculum.modules[0].lessons[0].concepts = ["unresolved-concept"]
        ws.save_curriculum(curriculum)
        meta = ws.load_meta()
        meta.workflow = {"worksheet_items_per_lesson": 3}
        ws.save_meta(meta)
        _mark_valid(ws)

        report = inspect_publication_readiness(ws.root)

        assert any(
            "exact worksheet policy requires 3 active items, got 1" in blocker
            for blocker in report.blockers
        ), report.blockers
        assert any(
            "worksheet" in blocker.lower()
            and "unresolved" in blocker.lower()
            and "unresolved-concept" in blocker
            for blocker in report.blockers
        ), report.blockers


def test_exact_worksheet_policy_blocks_depthless_concepts_and_still_checks_count():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))  # grounded-fact intentionally has depth=None
        meta = ws.load_meta()
        meta.workflow = {"worksheet_items_per_lesson": 3}
        ws.save_meta(meta)
        _mark_valid(ws)

        report = inspect_publication_readiness(ws.root)

        assert any(
            "exact worksheet policy requires 3 active items, got 1" in blocker
            for blocker in report.blockers
        ), report.blockers
        assert any(
            "worksheet" in blocker.lower()
            and "grounded-fact" in blocker
            and "depth" in blocker.lower()
            for blocker in report.blockers
        ), report.blockers


def test_stale_compiled_object_is_rejected_after_compile_config_change():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        compiled = compile_workspace(ws.root)
        cfg = ws.load_compile_config()
        cfg.seed += 1
        ws.save_compile_config(cfg)
        try:
            write_bundle(ws.root, compiled)
            assert False, "expected stale compile refusal"
        except PublicationSafetyError as error:
            assert any("configuration changed" in blocker for blocker in error.blockers)
        assert not ws.dist_dir.exists() or not list(ws.dist_dir.glob("safe-course-v*"))


def test_bundle_manifest_and_build_state_capture_exact_trace_hashes():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        compiled = compile_workspace(ws.root)
        out = write_bundle(ws.root, compiled)
        manifest = json.loads((out.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        provenance = manifest["provenance"]
        assert provenance["artifact_sha256"] == hash_data(compiled.tl_course)
        assert provenance["bank_sha256"] == banks_sha256(compiled.banks)
        assert provenance["source_hashes"] == {
            source.name: ws.source_hash(source) for source in ws.iter_sources()
        }
        assert provenance["source_set_sha256"]
        assert provenance["validation_report_hashes"]
        assert provenance["validation_set_sha256"] == hash_data(
            provenance["validation_report_hashes"]
        )
        assert provenance["workflow_config_sha256"]
        assert provenance["compile_config_sha256"]
        assert provenance["course_meta_sha256"] == hash_data(ws.load_meta())
        assert provenance["curriculum_sha256"] == hash_data(ws.load_curriculum())
        assert provenance["concept_graph_sha256"] == hash_data(ws.load_graph())
        assert manifest["entities_sha256"] == hash_data(manifest["entities"])
        qualitative = provenance["qualitative_gauntlet"]
        assert qualitative == {
            "required": False,
            "compiled_artifact_sha256": provenance["artifact_sha256"],
            "covered_unit_count": 0,
            "records": [],
        }

        recorded = ws.load_build_state().last_compilation
        assert recorded is not None
        assert recorded.artifact_sha256 == provenance["artifact_sha256"]
        assert recorded.validation_set_sha256 == provenance["validation_set_sha256"]
        assert recorded.bank_sha256 == provenance["bank_sha256"]
        assert recorded.course_meta_sha256 == provenance["course_meta_sha256"]
        assert recorded.curriculum_sha256 == provenance["curriculum_sha256"]
        assert recorded.concept_graph_sha256 == provenance["concept_graph_sha256"]
        assert recorded.bundle_version == out.version


def test_required_qualitative_gauntlet_fails_closed_without_exact_records():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        try:
            write_bundle(ws.root, compiled)
            assert False, "expected missing qualitative coverage to block publication"
        except PublicationSafetyError as error:
            assert any("no publication-eligible record matches" in item for item in error.blockers)
        assert not ws.dist_dir.exists() or not list(ws.dist_dir.glob("safe-course-v*"))


def test_qualitative_coverage_reuses_exact_unit_across_unrelated_course_hash_change():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        artifacts = compiled_unit_artifacts(compiled)
        contexts = publication_evaluation_contexts(
            ws.root, compiled, required_artifacts=artifacts
        )
        unit_key, artifact = next(iter(artifacts.items()))
        compiled_hash = hash_data(compiled.tl_course)

        write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256="0" * 64,
            unit_key=unit_key,
            outcome=_gauntlet_outcome(
                artifact, context=contexts[unit_key], run_id="wrong-course"
            ),
        )
        changed = artifact.model_copy(deep=True)
        changed.items[0].payload["question_text"] += " Changed after review."
        write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256=compiled_hash,
            unit_key=unit_key,
            outcome=_gauntlet_outcome(
                changed, context=contexts[unit_key], run_id="wrong-champion"
            ),
        )

        coverage = qualitative_publication_coverage(
            ws.root,
            compiled_artifact_sha256=compiled_hash,
            required_artifacts={unit_key: artifact},
            expected_contexts={unit_key: contexts[unit_key]},
        )
        assert coverage.ok
        assert len(coverage.references) == 1
        reference = coverage.references[0]
        assert reference.compiled_artifact_sha256 == compiled_hash
        assert reference.record_compiled_artifact_sha256 == "0" * 64
        assert reference.champion_artifact_sha256 == artifact.content_hash()


def test_qualitative_coverage_never_reuses_changed_unit_content():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        artifacts = compiled_unit_artifacts(compiled)
        contexts = publication_evaluation_contexts(
            ws.root, compiled, required_artifacts=artifacts
        )
        unit_key, reviewed = next(iter(artifacts.items()))
        write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256="0" * 64,
            unit_key=unit_key,
            outcome=_gauntlet_outcome(
                reviewed, context=contexts[unit_key], run_id="reviewed-before-change"
            ),
        )
        changed = reviewed.model_copy(deep=True)
        changed.items[0].payload["question_text"] += " Changed after review."
        coverage = qualitative_publication_coverage(
            ws.root,
            compiled_artifact_sha256=hash_data(compiled.tl_course),
            required_artifacts={unit_key: changed},
            expected_contexts={unit_key: contexts[unit_key]},
        )
        assert not coverage.ok
        assert changed.content_hash() in coverage.blockers[0]


def test_qualitative_record_must_be_publication_eligible_without_human_review_flag():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        artifacts = compiled_unit_artifacts(compiled)
        contexts = publication_evaluation_contexts(
            ws.root, compiled, required_artifacts=artifacts
        )
        unit_key, artifact = next(iter(artifacts.items()))
        compiled_hash = hash_data(compiled.tl_course)
        write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256=compiled_hash,
            unit_key=unit_key,
            outcome=_gauntlet_outcome(
                artifact,
                context=contexts[unit_key],
                run_id="not-eligible",
                eligible=False,
            ),
        )
        write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256=compiled_hash,
            unit_key=unit_key,
            outcome=_gauntlet_outcome(
                artifact,
                context=contexts[unit_key],
                run_id="human-review",
                human_review_recommended=True,
            ),
        )
        coverage = qualitative_publication_coverage(
            ws.root,
            compiled_artifact_sha256=compiled_hash,
            required_artifacts={unit_key: artifact},
            expected_contexts={unit_key: contexts[unit_key]},
        )
        assert not coverage.ok and coverage.references == ()


def test_unit_key_only_qualitative_helper_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        blockers = qualitative_publication_blockers(
            ws.root,
            compiled_artifact_sha256="0" * 64,
            required_unit_keys=["lesson"],
        )
        assert blockers == [
            "gauntlet/lesson: exact current champion artifact is required for coverage"
        ]


def test_every_exact_compiled_unit_requires_qualitative_coverage():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _add_second_unit(ws)
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        artifacts = compiled_unit_artifacts(compiled)
        contexts = publication_evaluation_contexts(
            ws.root, compiled, required_artifacts=artifacts
        )
        assert len(artifacts) == 2
        unit_key, artifact = next(iter(sorted(artifacts.items())))
        write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256=hash_data(compiled.tl_course),
            unit_key=unit_key,
            outcome=_gauntlet_outcome(
                artifact, context=contexts[unit_key], run_id="only-one-unit"
            ),
        )
        try:
            write_bundle(ws.root, compiled)
            assert False, "one qualitative result cannot cover two compiled units"
        except PublicationSafetyError as error:
            assert len([item for item in error.blockers if item.startswith("gauntlet/")]) == 1


def test_exact_qualitative_records_publish_and_are_hash_referenced_in_manifest():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _add_second_unit(ws)
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        record_paths = _write_exact_gauntlet_records(ws, compiled)
        out = write_bundle(ws.root, compiled)
        manifest = json.loads((out.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        qualitative = manifest["provenance"]["qualitative_gauntlet"]
        assert qualitative["required"] is True
        assert qualitative["compiled_artifact_sha256"] == hash_data(compiled.tl_course)
        assert qualitative["covered_unit_count"] == len(record_paths) == 2
        references = {record["unit_key"]: record for record in qualitative["records"]}
        artifacts = compiled_unit_artifacts(compiled)
        assert set(references) == set(artifacts)
        for unit_key, record_path in record_paths.items():
            reference = references[unit_key]
            assert reference["champion_artifact_sha256"] == artifacts[unit_key].content_hash()
            assert reference["record_sha256"] == sha256_text(
                record_path.read_text(encoding="utf-8")
            )
            assert len(reference["evaluation_context_sha256"]) == 64
            assert len(reference["gauntlet_policy_sha256"]) == 64
            assert ws.root / reference["record_path"] == record_path


def test_qualitative_policy_drift_invalidates_bound_history_context():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        _write_exact_gauntlet_records(ws, compiled)

        cfg = ws.load_compile_config()
        cfg.gauntlet.confidence_threshold = 0.75
        ws.save_compile_config(cfg)
        current = compile_workspace(ws.root)
        artifacts = compiled_unit_artifacts(current)
        contexts = publication_evaluation_contexts(
            ws.root, current, required_artifacts=artifacts
        )
        coverage = qualitative_publication_coverage(
            ws.root,
            compiled_artifact_sha256=hash_data(current.tl_course),
            required_artifacts=artifacts,
            expected_contexts=contexts,
        )
        assert not coverage.ok
        assert any(
            "evaluation context does not match current policy/evidence" in blocker
            for blocker in coverage.blockers
        )


def test_qualitative_record_change_during_staging_aborts_atomic_promotion():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        _require_qualitative_gauntlet(ws)
        compiled = compile_workspace(ws.root)
        record_path = next(iter(_write_exact_gauntlet_records(ws, compiled).values()))
        original_write_text = Path.write_text
        changed = False

        def mutate_record_after_manifest(path: Path, *args, **kwargs):
            nonlocal changed
            result = original_write_text(path, *args, **kwargs)
            if ".staging-" in str(path) and path.name == "manifest.json" and not changed:
                changed = True
                original_write_text(
                    record_path,
                    record_path.read_text(encoding="utf-8") + "\n",
                    encoding="utf-8",
                )
            return result

        try:
            with patch.object(Path, "write_text", mutate_record_after_manifest):
                write_bundle(ws.root, compiled)
            assert False, "changed qualitative evidence must abort publication"
        except PublicationSafetyError as error:
            assert "qualitative records changed" in str(error)
        assert not (ws.dist_dir / "safe-course-v1").exists()
        assert not list(ws.dist_dir.glob(".*.staging-*"))


def test_build_state_v1_loads_and_is_upgraded_on_atomic_save():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        source = next(ws.iter_sources())
        legacy = {
            "schema_version": "build-state-v1",
            "workflow_config_hash": config_hash(resolve_build_config({}, "beginner")),
            "sources": {
                source.name: {
                    "sha256": ws.source_hash(source),
                    "status": "ok",
                    "validation_ok": True,
                    "module_keys": ["module"],
                }
            },
        }
        ws.build_state_path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = ws.load_build_state()
        assert loaded.schema_version == "build-state-v1"
        assert loaded.sources[source.name].validation_ok is True
        ws.save_build_state(loaded)
        saved = json.loads(ws.build_state_path.read_text(encoding="utf-8"))
        assert saved["schema_version"] == "build-state-v2"


def test_interrupted_bundle_staging_preserves_last_known_good_bundle():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        first = write_bundle(ws.root, compile_workspace(ws.root))
        original_write_text = Path.write_text

        def interrupted_write(path: Path, *args, **kwargs):
            if ".staging-" in str(path) and path.name == "course.json":
                raise KeyboardInterrupt("simulated interruption")
            return original_write_text(path, *args, **kwargs)

        try:
            with patch.object(Path, "write_text", interrupted_write):
                write_bundle(ws.root, compile_workspace(ws.root))
            assert False, "expected simulated interruption"
        except KeyboardInterrupt:
            pass

        assert first.bundle_dir.exists()
        assert not (ws.dist_dir / "safe-course-v2").exists()
        assert not list(ws.dist_dir.glob(".*.staging-*"))


def test_interrupted_generated_state_promotion_rolls_back_all_files():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        before = _canonical_bytes(ws)
        bank = ws.load_bank("lesson")
        bank.items[0].payload["statement"] = "Valid new challenger."
        bank.items[0].payload_hash = payload_hash(bank.items[0].payload)
        curriculum = ws.load_curriculum()
        curriculum.modules[0].title = "New title"
        graph = ws.load_graph()
        graph.concepts[0].summary = "New valid summary."

        try:
            with patch.object(ws, "save_graph", side_effect=KeyboardInterrupt("simulated interruption")):
                _promote_generated_state(
                    ws,
                    curriculum=curriculum,
                    graph=graph,
                    banks=[bank],
                    stale_lesson_keys=set(),
                )
            assert False, "expected simulated interruption"
        except KeyboardInterrupt:
            pass
        assert _canonical_bytes(ws) == before


def test_failed_build_state_checkpoint_rolls_back_promoted_content():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        before = _canonical_bytes(ws)
        state_before = ws.build_state_path.read_bytes()
        bank = ws.load_bank("lesson")
        bank.items[0].payload["statement"] = "Validated replacement."
        bank.items[0].payload_hash = payload_hash(bank.items[0].payload)
        curriculum = ws.load_curriculum()
        curriculum.modules[0].title = "Replacement module"
        graph = ws.load_graph()
        graph.concepts[0].summary = "Replacement summary."
        candidate_state = ws.load_build_state().model_copy(deep=True)
        candidate_state.bank_sha256 = banks_sha256([bank])

        try:
            with patch.object(
                ws,
                "save_build_state",
                side_effect=KeyboardInterrupt("simulated checkpoint interruption"),
            ):
                _promote_generated_state(
                    ws,
                    curriculum=curriculum,
                    graph=graph,
                    banks=[bank],
                    stale_lesson_keys=set(),
                    build_state=candidate_state,
                )
            assert False, "expected simulated interruption"
        except KeyboardInterrupt:
            pass

        assert _canonical_bytes(ws) == before
        assert ws.build_state_path.read_bytes() == state_before


def test_post_compile_correct_answer_corruption_is_revalidated_before_any_dist_write():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        compiled = compile_workspace(ws.root)
        compiled.tl_course.modules[0].lessons[0].exercises[0].correct_answer = (
            "invalid-answer"
        )

        try:
            write_bundle(ws.root, compiled)
            assert False, "post-compile corruption must not publish"
        except PublicationSafetyError as error:
            assert any(
                "changed after compilation" in blocker or "correct_answer" in blocker
                for blocker in error.blockers
            )
        assert not ws.dist_dir.exists()


def test_meta_curriculum_and_graph_are_exact_compile_time_bindings():
    mutations = (
        (
            "metadata",
            lambda ws: _mutate_and_save(
                ws.load_meta(), lambda value: setattr(value, "title", "Changed"), ws.save_meta
            ),
        ),
        (
            "curriculum",
            lambda ws: _mutate_and_save(
                ws.load_curriculum(),
                lambda value: setattr(value.modules[0], "title", "Changed"),
                ws.save_curriculum,
            ),
        ),
        (
            "concept graph",
            lambda ws: _mutate_and_save(
                ws.load_graph(),
                lambda value: setattr(value.concepts[0], "summary", "Changed"),
                ws.save_graph,
            ),
        ),
    )
    for expected_label, mutate in mutations:
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td))
            compiled = compile_workspace(ws.root)
            mutate(ws)
            try:
                write_bundle(ws.root, compiled)
                assert False, f"stale {expected_label} must not publish"
            except PublicationSafetyError as error:
                assert any(expected_label in blocker for blocker in error.blockers)
            assert not ws.dist_dir.exists()


def _mutate_and_save(value, mutate, save) -> None:
    mutate(value)
    save(value)


def test_legacy_state_loads_but_fails_closed_and_plans_a_full_evidence_rebuild():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        source = next(ws.iter_sources())
        legacy = {
            "schema_version": "build-state-v1",
            "workflow_config_hash": config_hash(resolve_build_config({}, "beginner")),
            "sources": {
                source.name: {
                    "sha256": ws.source_hash(source),
                    "status": "ok",
                    "validation_ok": True,
                    "module_keys": ["module"],
                }
            },
        }
        ws.build_state_path.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = ws.load_build_state()
        assert loaded.schema_version == "build-state-v1"
        readiness = inspect_publication_readiness(ws.root)
        assert not readiness.ok
        assert any("legacy publication evidence" in item for item in readiness.blockers)
        assert any("validation report hash is missing" in item for item in readiness.blockers)
        assert any("last-known-good" in item for item in readiness.blockers)
        plan = plan_build(
            ws,
            loaded,
            config_hash(resolve_build_config({}, "beginner")),
        )
        assert [(path.name, reason) for path, reason in plan.dirty] == [
            (source.name, "legacy publication evidence requires rebuild")
        ]
        try:
            write_bundle(ws.root, compile_workspace(ws.root))
            assert False, "untraceable legacy state must not publish"
        except PublicationSafetyError:
            pass
        assert not ws.dist_dir.exists()


def test_workspace_publication_lock_is_reentrant_and_serializes_canonical_writes():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        attempted = threading.Event()
        completed = threading.Event()

        def mutate_meta() -> None:
            attempted.set()
            meta = ws.load_meta()
            meta.title = "Concurrent mutation"
            ws.save_meta(meta)
            completed.set()

        with ws.publication_lock():
            # Nested acquisition through a distinct Workspace object proves the
            # per-root lock context is genuinely re-entrant.
            with Workspace(ws.root).publication_lock():
                worker = threading.Thread(target=mutate_meta)
                worker.start()
                assert attempted.wait(1)
                assert not completed.wait(0.05)
        worker.join(timeout=2)
        assert completed.is_set()
        assert ws.load_meta().title == "Concurrent mutation"


def test_bundle_writer_rejects_duplicate_unit_paths_before_creating_dist():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        curriculum = ws.load_curriculum()
        curriculum.modules[0].lessons.append(
            curriculum.modules[0].lessons[0].model_copy(deep=True)
        )
        ws.save_curriculum(curriculum)
        _mark_valid(ws)
        compiled = compile_workspace(ws.root)
        assert len(compiled.tl_course.modules[0].lessons) == 2

        try:
            write_bundle(ws.root, compiled)
            assert False, "duplicate unit filenames must not publish"
        except PublicationSafetyError as error:
            assert any("duplicate/colliding import keys" in item for item in error.blockers)
        assert not ws.dist_dir.exists()


def test_bundle_writer_rejects_traversal_keys_and_unsafe_course_ids():
    for unsafe_kind in ("unit", "course"):
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td))
            if unsafe_kind == "unit":
                curriculum = ws.load_curriculum()
                curriculum.modules[0].lessons[0].key = "../escape"
                ws.save_curriculum(curriculum)
                graph = ws.load_graph()
                graph.concepts[0].lessons = ["../escape"]
                ws.save_graph(graph)
                bank_path = next(ws.bank_dir.glob("*.json"))
                raw_bank = json.loads(bank_path.read_text(encoding="utf-8"))
                raw_bank["lesson"] = "../escape"
                bank_path.write_text(json.dumps(raw_bank), encoding="utf-8")
            else:
                meta = ws.load_meta()
                meta.id = "../escape"
                ws.save_meta(meta)
            _mark_valid(ws)
            compiled = compile_workspace(ws.root)

            try:
                write_bundle(ws.root, compiled)
                assert False, f"unsafe {unsafe_kind} identity must not publish"
            except PublicationSafetyError as error:
                assert any("unsafe bundle path component" in item for item in error.blockers)
            assert not ws.dist_dir.exists()
            assert not (Path(td) / "escape").exists()


def test_bundle_writer_rejects_symlinked_dist_root():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws = _workspace(root)
        outside = root / "outside"
        outside.mkdir()
        ws.dist_dir.symlink_to(outside, target_is_directory=True)

        try:
            write_bundle(ws.root, compile_workspace(ws.root))
            assert False, "symlinked dist must not publish"
        except PublicationSafetyError as error:
            assert any("symbolic-link output roots" in item for item in error.blockers)
        assert list(outside.iterdir()) == []


def test_publication_rejects_sequence_errors_even_when_compile_diagnostics_do_not_block():
    with tempfile.TemporaryDirectory() as td:
        ws = _workspace(Path(td))
        bank = ws.load_bank("lesson")
        original = bank.items[0]
        for variant in (2, 3):
            item = original.model_copy(deep=True)
            item.variant = variant
            item.item_key = f"lesson/grounded-fact/r2/v{variant}"
            bank.items.append(item)
        ws.save_bank(bank)
        cfg = ws.load_compile_config()
        cfg.sequence_quality.max_same_rung_streak = 1
        cfg.sequence_quality.block_on_errors = False
        ws.save_compile_config(cfg)
        _mark_valid(ws)
        compiled = compile_workspace(ws.root)
        assert compiled.problems == []
        assert compiled.relaxations_by_unit["lesson"]

        # Simulate a caller stripping the scheduler's required waiver record
        # while retaining a self-consistent diagnostic snapshot.  With
        # block_on_errors=false, the old publication boundary silently allowed
        # these freshly detected hard errors.
        compiled.relaxations_by_unit["lesson"] = ()
        compiled.sequence_quality = validate_tl_course(
            compiled.tl_course,
            policy=_sequence_quality_policy(compiled.cfg),
            relaxations_by_unit=compiled.relaxations_by_unit,
        )
        compiled.snapshot_sha256["relaxations_sha256"] = hash_data(
            _relaxations_payload(compiled.relaxations_by_unit)
        )
        compiled.snapshot_sha256["sequence_quality_sha256"] = hash_data(
            compiled.sequence_quality.to_dict()
        )
        assert any(issue.severity == "error" for issue in compiled.sequence_quality.issues)

        try:
            write_bundle(ws.root, compiled)
            assert False, "publication must not downgrade hard sequence errors"
        except PublicationSafetyError as error:
            assert any("sequence quality" in item for item in error.blockers)
        assert not ws.dist_dir.exists()


def test_publication_rejects_empty_course_and_empty_unit():
    for empty_kind in ("course", "unit"):
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td))
            if empty_kind == "course":
                ws.save_curriculum(Curriculum())
                ws.delete_bank("lesson")
            else:
                bank = ws.load_bank("lesson")
                bank.items = []
                ws.save_bank(bank)
            _mark_valid(ws)
            compiled = compile_workspace(ws.root)

            try:
                write_bundle(ws.root, compiled)
                assert False, f"empty {empty_kind} must not publish"
            except PublicationSafetyError as error:
                expected = "course.modules" if empty_kind == "course" else ".exercises"
                assert any(expected in item for item in error.blockers)
            assert not ws.dist_dir.exists()


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {type(error).__name__}: {error}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
