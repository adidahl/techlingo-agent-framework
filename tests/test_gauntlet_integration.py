"""Focused adapter, persistence, and CLI tests for qualitative QA integration.

No test invokes a live subscription CLI or mutates canonical course banks.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from techlingo_workflow.cli_course import course_app
from techlingo_workflow.gauntlet import (
    BlindComparator,
    DEFAULT_GAUNTLET_GOAL,
    GauntletConfig,
    IndependentCritic,
    QualitativeGauntlet,
    SOURCE_FIDELITY_GOAL,
    TargetedEditor,
    default_critic_rubric,
)
from techlingo_workflow.gauntlet_backends import (
    FreshCLIComparisonBackend,
    FreshCLICriticBackend,
    FreshCLIEditorBackend,
)
from techlingo_workflow.gauntlet_io import (
    GauntletIOError,
    artifact_from_tl_unit,
    hard_gate_artifact,
    iter_course_references,
    iter_gauntlet_records,
    load_gauntlet_record,
    reference_path,
    source_excerpts_for_artifact,
    write_course_reference,
    write_gauntlet_record,
)
from techlingo_workflow.experience import ConstraintRelaxation
from techlingo_workflow.techlingo_models import TLQuestion, TLUnit
from techlingo_workflow.gauntlet_models import (
    ArtifactItem,
    ArtifactSnapshot,
    EvaluationContext,
    EvaluationModelProvenance,
    GauntletHistory,
    GauntletOutcome,
    HardGateResult,
    HumanAction,
    HumanDecision,
    ReferenceContext,
    ReferenceStatus,
    SourceExcerpt,
    StopReason,
    UsageRecord,
)
from techlingo_workflow.references import create_reference_draft, promote_reference
from techlingo_workflow.workspace import init_workspace


def _workspace(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("# Source\nA grounded fact.", encoding="utf-8")
    return init_workspace(
        tmp_path / "course",
        course_id="course",
        title="Course",
        source_files=[source],
    )


def _artifact(session_id: str = "unit") -> ArtifactSnapshot:
    return ArtifactSnapshot(
        artifact_id=f"course:{session_id}",
        course_id="course",
        session_id=session_id,
        items=[
            ArtifactItem(
                item_key="item-1",
                path=f"units/{session_id}/questions/0",
                payload={"prompt": "A grounded question?"},
            )
        ],
    )


def _outcome(session_id: str = "unit", run_id: str = "run-1") -> GauntletOutcome:
    artifact = _artifact(session_id)
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    config = GauntletConfig(critic_backend="codex", critic_model="codex:o3")
    context = EvaluationContext.create(
        goal=DEFAULT_GAUNTLET_GOAL,
        source_fidelity_goal=SOURCE_FIDELITY_GOAL,
        gauntlet_policy=config.to_mapping(),
        rubric=default_critic_rubric(),
        source_material=[
            SourceExcerpt(source_id="source.md", text="A grounded fact.")
        ],
        approved_reference_sessions=[],
        models=EvaluationModelProvenance(
            critic_backend="codex",
            critic_model="codex:o3",
            critic_fresh_context=True,
            editor_backend="codex",
            editor_model="codex:o3",
            editor_fresh_context=True,
            comparator_backend="codex",
            comparator_model="codex:o3",
            comparator_fresh_context=True,
        ),
    )
    gate = HardGateResult(
        artifact_sha256=artifact.content_hash(),
        passed=True,
        checks={"deterministic": True},
    )
    human = HumanDecision(
        action=HumanAction.approve,
        reviewer="Integration Reviewer",
        note="Reviewed exact artifact.",
    )
    history = GauntletHistory(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        initial_champion_hash=artifact.content_hash(),
        final_champion_hash=artifact.content_hash(),
        evaluation_context=context,
        initial_hard_gate=gate,
        stop_reason=StopReason.human_approved,
        stop_evidence="Reviewed exact artifact.",
        human_decision=human,
        total_usage=UsageRecord(),
        publication_eligible=True,
    )
    return GauntletOutcome(champion=artifact, history=history)


def test_fresh_backend_protocol_name_matches_config_and_clients_keep_role_names():
    calls = []

    def factory(model_label, client_name):
        client = object()
        calls.append((model_label, client_name, client))
        return client

    critic_backend = FreshCLICriticBackend("codex:o3", client_factory=factory)
    editor_backend = FreshCLIEditorBackend("codex:o3", client_factory=factory)
    comparison_backend = FreshCLIComparisonBackend("codex:o3", client_factory=factory)
    assert critic_backend.name == editor_backend.name == comparison_backend.name == "codex"

    first = critic_backend._client()
    second = critic_backend._client()
    assert first is not second
    assert [call[1] for call in calls] == [
        "TechLingoGauntletCritic",
        "TechLingoGauntletCritic",
    ]

    config = GauntletConfig(critic_backend="codex", critic_model="codex:o3")
    # Construction performs the core's strict backend/model compatibility check.
    QualitativeGauntlet(
        config=config,
        critic=IndependentCritic(critic_backend),
        editor=TargetedEditor(editor_backend),
        comparator=BlindComparator(comparison_backend, config),
        hard_gate=lambda _artifact: HardGateResult(passed=True),
    )


def test_hard_gate_requires_stable_item_key_inside_emitted_question_metadata():
    question = TLQuestion(
        import_key="unit-q1",
        question_type="true_false",
        question_text="The source states a grounded fact.",
        options={"original_question_type": "true_false", "rung": 2},
        correct_answer="true",
        explanation="This follows directly from the source.",
    )
    artifact = ArtifactSnapshot(
        artifact_id="course:unit",
        course_id="course",
        session_id="unit",
        items=[
            ArtifactItem(
                item_key="stable-item-key",
                path="modules/0/units/0/questions/0",
                payload=question.model_dump(mode="json"),
            )
        ],
    )
    gate = hard_gate_artifact(artifact)
    assert not gate.passed
    assert gate.artifact_sha256 == artifact.content_hash()
    assert any(issue.code == "identity" for issue in gate.issues)
    malformed = _artifact()
    schema_gate = hard_gate_artifact(malformed)
    assert not schema_gate.passed
    assert schema_gate.artifact_sha256 == malformed.content_hash()
    assert schema_gate.issues[0].code == "schema"


def test_artifact_adapter_preserves_complete_relaxation_attestation():
    question = TLQuestion(
        import_key="unit-q1",
        question_type="true_false",
        question_text="The source states a grounded fact.",
        options={
            "original_question_type": "true_false",
            "rung": 2,
            "item_key": "stable-item-key",
        },
        correct_answer="true",
        explanation="This follows directly from the source.",
    )
    relaxation = ConstraintRelaxation(
        constraint="ui_family_streak",
        reason="Proven unavoidable under pinned order.",
        item_keys=("stable-item-key",),
        observed=3,
        configured=2,
        violation_observed=True,
        proof_kind="search-profile-exhaustive-v1",
        search_relaxed_before=("mechanics_window",),
        scheduler_version="experience-scheduler-attestation-v1",
        scheduler_seed=901,
        scheduler_scope="course/unit",
        scheduler_pinned_item_keys=("stable-item-key",),
        artifact_sha256="a" * 64,
        policy_sha256="b" * 64,
        attestation_sha256="c" * 64,
    )
    artifact = artifact_from_tl_unit(
        course_id="course",
        module_key="module",
        unit=TLUnit(
            import_key="unit",
            title="Unit",
            slo="Understand the fact.",
            exercises=[question],
        ),
        unit_path="modules/0/units/0",
        relaxations=[relaxation],
    )
    stored = artifact.metadata["constraint_relaxations"][0]
    assert stored["proof_kind"] == relaxation.proof_kind
    assert stored["scheduler_pinned_item_keys"] == ("stable-item-key",)
    assert stored["artifact_sha256"] == "a" * 64
    assert stored["policy_sha256"] == "b" * 64
    assert stored["attestation_sha256"] == "c" * 64


def test_workspace_reference_catalog_keeps_draft_and_approved_files_separate(tmp_path):
    ws = _workspace(tmp_path)
    draft = create_reference_draft(
        reference_id="strong-unit",
        context=ReferenceContext(course_id="course", lesson_id="unit"),
        relevant_sources=[SourceExcerpt(source_id="source.md", text="A grounded fact.")],
        artifact=_artifact(),
        annotations=["The ordered session is concise and source faithful."],
    )
    draft_path = write_course_reference(ws.root, draft)
    approved = promote_reference(
        draft,
        approved_by="Human Reviewer",
        approved_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    approved_path = write_course_reference(ws.root, approved)

    assert draft_path != approved_path and draft_path.exists() and approved_path.exists()
    entries = list(iter_course_references(ws.root))
    assert [reference.status for _path, reference in entries] == [
        ReferenceStatus.draft,
        ReferenceStatus.approved,
    ]
    with pytest.raises(GauntletIOError, match="reference_id"):
        reference_path(ws.root, "../escape", status=ReferenceStatus.draft)


def test_source_excerpt_resolution_rejects_traversal_and_symlinks(tmp_path):
    root = tmp_path / "course"
    sources = root / "sources"
    sources.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("sensitive outside text", encoding="utf-8")
    artifact = _artifact().model_copy(
        update={"metadata": {"module_key": "module"}}, deep=True
    )

    def fake_workspace(source_file: str):
        return SimpleNamespace(
            root=root,
            sources_dir=sources,
            load_curriculum=lambda: SimpleNamespace(
                modules=[SimpleNamespace(key="module", source_file=source_file)]
            ),
        )

    with pytest.raises(GauntletIOError, match="filename inside sources"):
        source_excerpts_for_artifact(fake_workspace("../outside.md"), artifact)

    link = sources / "linked.md"
    link.symlink_to(outside)
    with pytest.raises(GauntletIOError, match="cannot be a symlink"):
        source_excerpts_for_artifact(fake_workspace("linked.md"), artifact)


def test_history_is_immutable_path_safe_strictly_readable_and_visible_in_cli(tmp_path):
    ws = _workspace(tmp_path)
    outcome = _outcome(session_id="unit/with-slash", run_id="run/one")
    path = write_gauntlet_record(
        ws.root,
        compiled_artifact_sha256="a" * 64,
        unit_key="unit/with-slash",
        outcome=outcome,
    )
    assert path.parent.name == "history" and path.parent.parent.name == "gauntlet"
    assert path.exists() and "/with-slash/" not in path.as_posix()

    runner = CliRunner()
    listed = runner.invoke(course_app, ["gauntlet", "history", "list", str(ws.root)])
    assert listed.exit_code == 0 and "run/one" in listed.output
    shown = runner.invoke(
        course_app,
        ["gauntlet", "history", "show", str(ws.root), "run/one", "--json"],
    )
    assert shown.exit_code == 0
    assert json.loads(shown.output)["unit_key"] == "unit/with-slash"

    with pytest.raises(GauntletIOError, match="immutable"):
        write_gauntlet_record(
            ws.root,
            compiled_artifact_sha256="a" * 64,
            unit_key="unit/with-slash",
            outcome=outcome,
        )
    (path.parent / "malformed.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GauntletIOError, match="could not load"):
        list(iter_gauntlet_records(ws.root, strict=True))
    assert len(list(iter_gauntlet_records(ws.root))) == 1


def test_legacy_unbound_history_remains_readable_but_is_not_publication_evidence(
    tmp_path,
):
    ws = _workspace(tmp_path)
    outcome = _outcome()
    legacy_history = outcome.history.model_copy(
        update={"evaluation_context": None, "publication_eligible": True}, deep=True
    )
    from techlingo_workflow.gauntlet_io import GauntletRecord

    record = GauntletRecord(
        compiled_artifact_sha256="a" * 64,
        unit_key="unit",
        champion=outcome.champion,
        history=legacy_history,
    )
    path = ws.root / "gauntlet" / "history" / "legacy.json"
    path.parent.mkdir(parents=True)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    loaded = load_gauntlet_record(path)
    assert loaded.history.evaluation_context is None
    assert not loaded.history.derived_publication_eligible(
        expected_context=outcome.history.evaluation_context
    )
    assert "history has no bound evaluation context" in loaded.history.publication_blockers(
        expected_context=outcome.history.evaluation_context
    )


def test_gauntlet_cli_is_dry_until_execute_is_explicit(tmp_path):
    artifact = _artifact()
    compiled = SimpleNamespace()
    runner = CliRunner()
    with patch(
        "techlingo_workflow.cli_course._compiled_artifacts_or_die",
        return_value=(object(), compiled, {"unit": artifact}),
    ), patch(
        "techlingo_workflow.gauntlet_backends.FreshCLICriticBackend",
        side_effect=AssertionError("live backend must not be constructed"),
    ):
        result = runner.invoke(
            course_app,
            ["gauntlet", "run", str(tmp_path), "--unit", "unit"],
        )
    assert result.exit_code == 0
    assert "Dry run only" in result.output
    assert "--execute" in result.output


@pytest.mark.parametrize(
    ("issues", "problems", "expected_exit"),
    [
        (
            [
                SimpleNamespace(
                    severity="warning",
                    code="mechanic_streak",
                    unit_path="units/u",
                    message="declared relaxation",
                    relaxed=True,
                )
            ],
            [],
            0,
        ),
        (
            [
                SimpleNamespace(
                    severity="error",
                    code="concept_adjacency",
                    unit_path="units/u",
                    message="unexplained collision",
                    relaxed=False,
                )
            ],
            ["sequence quality [concept_adjacency] units/u: unexplained collision"],
            1,
        ),
        ([], ["modules[0].lessons[0].exercises[0]: invalid answer"], 1),
    ],
)
def test_quality_cli_writes_machine_json_and_only_errors_fail(
    tmp_path, issues, problems, expected_exit
):
    report = SimpleNamespace(
        issues=tuple(issues),
        units=(
            SimpleNamespace(
                metrics=SimpleNamespace(constraint_relaxations=({"constraint": "x"},))
            ),
        ),
        to_dict=lambda: {"schema_version": "sequence-quality-v1", "issues": []},
    )
    compiled = SimpleNamespace(
        tl_course=SimpleNamespace(import_key="course"),
        sequence_quality=report,
        problems=problems,
    )
    output = tmp_path / f"quality-{expected_exit}-{len(issues)}.json"
    with patch("techlingo_workflow.compiler.compile_workspace", return_value=compiled):
        result = CliRunner().invoke(
            course_app,
            ["quality", str(tmp_path), "--output", str(output)],
        )
    assert result.exit_code == expected_exit
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is (expected_exit == 0)
    assert "Course quality:" in result.output
