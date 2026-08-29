#!/usr/bin/env python3
"""Apply current exact Gauntlet proposals sequentially with publication receipts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from techlingo_workflow.compiler import compile_workspace  # noqa: E402
from techlingo_workflow.gauntlet_models import canonical_sha256  # noqa: E402
from techlingo_workflow.gauntlet_proposals import (  # noqa: E402
    apply_approved_proposal_to_banks,
    approve_authored_proposal,
    list_authored_proposals,
    promote_validated_authored_repair,
    write_proposal_approval,
)
from techlingo_workflow.publication_safety import hash_data  # noqa: E402
from techlingo_workflow.workspace import (  # noqa: E402
    Workspace,
    _atomic_write_text,
    payload_hash,
    sha256_text,
)


def _write_new(path: Path, value: dict) -> None:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite immutable evidence: {path}")
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _amendment(approval, *, reviewer: str) -> tuple[dict, dict]:
    proposal = approval.proposal
    if len(proposal.authored_changes) != 1:
        raise RuntimeError(f"{proposal.proposal_id}: batch requires one authored item")
    change = proposal.authored_changes[0]
    payload_after = json.loads(json.dumps(change.payload_after, ensure_ascii=False))
    correct_count = sum(
        option.get("is_correct") is True for option in payload_after.get("options", [])
    )
    mechanic_changed = payload_after.get("question_type") == "multi_choice" and correct_count == 1
    if mechanic_changed:
        payload_after["question_type"] = "single_choice"
    body = {
        "schema_version": "techlingo-gauntlet-authored-amendment-v1",
        "approved_by": reviewer,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approval_sha256": approval.approval_sha256,
        "proposal_sha256": proposal.proposal_sha256,
        "history_sha256": proposal.history_sha256,
        "item_key": change.item_key,
        "bank_path": change.bank_path,
        "reason": (
            "Reviewer accepted the source-bound promoted edit and corrected its one-answer "
            "mechanic from multi-select to single-choice."
            if mechanic_changed
            else "Reviewer accepted the exact source-bound promoted edit without a mechanic change."
        ),
        "payload_before": change.payload_before,
        "payload_after": payload_after,
    }
    return {"amendment_sha256": hash_data(body), **body}, payload_after


def _set_exact_payload(course_dir: Path, change, payload_after: dict) -> None:
    ws = Workspace(course_dir).require()
    bank = ws.load_bank(Path(change.bank_path).stem)
    matches = [(index, item) for index, item in enumerate(bank.items) if item.item_key == change.item_key]
    if len(matches) != 1:
        raise RuntimeError(f"{change.item_key}: authored item no longer maps uniquely")
    index, item = matches[0]
    updated = item.model_copy(deep=True)
    updated.payload = payload_after
    updated.payload_hash = payload_hash(payload_after)
    updated.provenance = "human-edited"
    updated.pinned = True
    bank.items[index] = updated
    ws.save_bank(bank)


def apply_batch(course_dir: Path, *, reviewer: str, execute: bool) -> None:
    course_dir = course_dir.resolve()
    ws = Workspace(course_dir).require()
    proposals = list_authored_proposals(course_dir)
    item_keys = [change.item_key for proposal in proposals for change in proposal.authored_changes]
    if len(item_keys) != len(set(item_keys)):
        raise RuntimeError("current proposal batch contains overlapping authored items")
    print(f"Current exact proposals: {len(proposals)}")
    if not execute:
        for proposal in proposals:
            print(f"  {proposal.proposal_id}")
        print("Dry run only; no approvals, banks, receipts, state, or bundles changed.")
        return

    approvals = []
    for proposal in proposals:
        approval = approve_authored_proposal(
            proposal,
            approved_by=reviewer,
            exact_proposal_sha256=proposal.proposal_sha256,
            exact_history_sha256=proposal.history_sha256,
            exact_compiled_artifact_sha256=proposal.compiled_artifact_sha256,
            exact_champion_after_sha256=proposal.champion_after_sha256,
        )
        approval_path = ws.root / "gauntlet/proposals/approved" / f"{proposal.proposal_id}.json"
        write_proposal_approval(course_dir, approval, approval_path)
        approvals.append((approval, approval_path))

    for number, (approval, _approval_path) in enumerate(approvals, start=1):
        proposal = approval.proposal
        change = proposal.authored_changes[0]
        bank_path = ws.root / change.bank_path
        state_path = ws.build_state_path
        bank_before = bank_path.read_text(encoding="utf-8")
        state_before = state_path.read_text(encoding="utf-8")
        amendment_path = (
            ws.root / "gauntlet/proposals/approved" / f"{proposal.proposal_id}-amendment.json"
        )
        receipt_path = ws.root / "gauntlet/proposals/applied" / f"{proposal.proposal_id}.json"
        try:
            amendment, payload_after = _amendment(approval, reviewer=reviewer)
            _write_new(amendment_path, amendment)
            previous_artifact = hash_data(compile_workspace(course_dir).tl_course)
            apply_approved_proposal_to_banks(
                course_dir, approval, allow_compiled_artifact_drift=True
            )
            _set_exact_payload(course_dir, change, payload_after)
            compiled = compile_workspace(course_dir)
            if compiled.problems:
                raise RuntimeError("deterministic compile failed: " + "; ".join(compiled.problems[:10]))
            artifact = hash_data(compiled.tl_course)
            receipt = {
                "schema_version": "techlingo-gauntlet-authored-application-v1",
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "applied_by": reviewer,
                "unit_key": proposal.unit_key,
                "item_key": change.item_key,
                "bank_path": change.bank_path,
                "bank_file_sha256": sha256_text(bank_path.read_text(encoding="utf-8")),
                "proposal_sha256": proposal.proposal_sha256,
                "approval_sha256": approval.approval_sha256,
                "history_sha256": proposal.history_sha256,
                "amendment_sha256": amendment["amendment_sha256"],
                "previous_compiled_artifact_sha256": previous_artifact,
                "validated_compiled_artifact_sha256": artifact,
                "application": {
                    "question_type_before": change.payload_before.get("question_type"),
                    "question_type_after": payload_after.get("question_type"),
                    "provenance_after": "human-edited",
                    "pinned_after": True,
                    "authoritative_bank_items_changed": 1,
                    "generated_compiled_units_patched": 0,
                    "source_generation_runs": 0,
                    "model_calls": 0,
                },
                "validation": {
                    "compiler_problem_count": 0,
                    "course_units": len(compiled.tl_course.modules),
                },
            }
            _write_new(receipt_path, receipt)
            promote_validated_authored_repair(
                course_dir,
                approval,
                amendment_path=amendment_path,
                receipt_path=receipt_path,
            )
            print(f"[{number}/{len(approvals)}] promoted {proposal.proposal_id} -> {artifact}")
        except Exception:
            _atomic_write_text(bank_path, bank_before)
            _atomic_write_text(state_path, state_before)
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("course_dir", type=Path)
    parser.add_argument("--reviewer", default="Codex reviewer for Adi")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    apply_batch(args.course_dir, reviewer=args.reviewer, execute=args.execute)


if __name__ == "__main__":
    main()
