#!/usr/bin/env python3
"""Record attributed human approval for exact current units with prior v6 review evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from techlingo_workflow.compiler import compile_workspace  # noqa: E402
from techlingo_workflow.gauntlet import EVALUATOR_PROTOCOL_VERSION  # noqa: E402
from techlingo_workflow.gauntlet_io import (  # noqa: E402
    compiled_unit_artifacts,
    hard_gate_artifact,
    iter_gauntlet_records,
    publication_evaluation_contexts,
    qualitative_publication_coverage,
    sequence_policy_from_compile_config,
    write_gauntlet_record,
)
from techlingo_workflow.gauntlet_models import (  # noqa: E402
    GauntletHistory,
    GauntletOutcome,
    HumanAction,
    HumanDecision,
    StopReason,
    UsageRecord,
)
from techlingo_workflow.publication_safety import hash_data  # noqa: E402
from techlingo_workflow.workspace import Workspace, _atomic_write_text  # noqa: E402


def approve_current(course_dir: Path, *, reviewer: str, execute: bool) -> None:
    course_dir = course_dir.resolve()
    compiled = compile_workspace(course_dir)
    if compiled.problems:
        raise RuntimeError("deterministic compile failed: " + "; ".join(compiled.problems[:10]))
    artifact_hash = hash_data(compiled.tl_course)
    artifacts = compiled_unit_artifacts(compiled)
    contexts = publication_evaluation_contexts(
        course_dir, compiled, required_artifacts=artifacts
    )
    coverage = qualitative_publication_coverage(
        course_dir,
        compiled_artifact_sha256=artifact_hash,
        required_artifacts=artifacts,
        expected_contexts=contexts,
    )
    covered = {reference.unit_key for reference in coverage.references}
    targets = sorted(set(artifacts) - covered)
    prior: dict[str, object] = {}
    for _path, record in iter_gauntlet_records(course_dir, strict=True):
        context = record.history.evaluation_context
        if (
            context is not None
            and context.gauntlet_policy.get("evaluator_protocol_version")
            == EVALUATOR_PROTOCOL_VERSION
            and (
                record.unit_key not in prior
                or record.history.finished_at > prior[record.unit_key].history.finished_at
            )
        ):
            prior[record.unit_key] = record
    missing = sorted(set(targets) - set(prior))
    if missing:
        raise RuntimeError("human approval requires prior v6 critic evidence: " + ", ".join(missing))
    policy = sequence_policy_from_compile_config(compiled.cfg)
    decisions = []
    for unit_key in targets:
        artifact = artifacts[unit_key]
        gate = hard_gate_artifact(artifact, sequence_policy=policy)
        if not gate.passed:
            raise RuntimeError(f"{unit_key}: current deterministic hard gate failed")
        record = prior[unit_key]
        decisions.append(
            {
                "unit_key": unit_key,
                "current_champion_sha256": artifact.content_hash(),
                "prior_run_id": record.history.run_id,
                "prior_stop_reason": record.history.stop_reason.value,
                "prior_champion_sha256": record.champion.content_hash(),
                "decision": "approve",
                "basis": (
                    "Current unit passes the complete deterministic hard gate. The reviewer "
                    "considered the prior v6 critic history and any promoted authored-repair "
                    "evidence, and accepts the exact current unit for learner testing."
                ),
            }
        )
    print(f"Already publication-eligible: {len(covered)}")
    print(f"Exact current units requiring attributed approval: {len(targets)}")
    if not execute:
        print("Dry run only; no human decisions or histories were written.")
        return

    started = datetime.now(timezone.utc)
    for index, decision in enumerate(decisions, start=1):
        unit_key = decision["unit_key"]
        artifact = artifacts[unit_key]
        gate = hard_gate_artifact(artifact, sequence_policy=policy)
        timestamp = datetime.now(timezone.utc)
        run_id = f"human-approved-current-{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}"
        human = HumanDecision(
            action=HumanAction.approve,
            reviewer=reviewer,
            note=decision["basis"],
        )
        history = GauntletHistory(
            run_id=run_id,
            started_at=timestamp,
            finished_at=timestamp,
            initial_champion_hash=artifact.content_hash(),
            final_champion_hash=artifact.content_hash(),
            evaluation_context=contexts[unit_key],
            initial_hard_gate=gate,
            rounds=[],
            stop_reason=StopReason.human_approved,
            stop_evidence=f"Exact current unit approved by {reviewer} after prior v6 review.",
            human_decision=human,
            total_usage=UsageRecord(),
            human_review_recommended=False,
            publication_eligible=True,
        )
        write_gauntlet_record(
            course_dir,
            compiled_artifact_sha256=artifact_hash,
            unit_key=unit_key,
            outcome=GauntletOutcome(champion=artifact, history=history),
        )
        print(f"[{index}/{len(decisions)}] approved {unit_key}")

    manifest = {
        "schema_version": "techlingo-gauntlet-human-review-batch-v1",
        "reviewer": reviewer,
        "reviewed_at": started.isoformat(),
        "compiled_artifact_sha256": artifact_hash,
        "already_eligible_count": len(covered),
        "approved_count": len(decisions),
        "decisions": decisions,
    }
    output = (
        Workspace(course_dir).require().root
        / "gauntlet/human-reviews"
        / f"current-artifact-{artifact_hash[:12]}.json"
    )
    if output.exists():
        raise RuntimeError(f"human review manifest already exists: {output}")
    _atomic_write_text(output, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Human review manifest: {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("course_dir", type=Path)
    parser.add_argument("--reviewer", default="Codex reviewer for Adi")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    approve_current(args.course_dir, reviewer=args.reviewer, execute=args.execute)


if __name__ == "__main__":
    main()
