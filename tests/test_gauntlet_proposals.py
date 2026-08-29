from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from techlingo_workflow.cli_course import course_app
from techlingo_workflow.gauntlet_models import HumanReviewReason
from techlingo_workflow.gauntlet_proposals import (
    GauntletAuthoredProposal,
    GauntletProposalError,
    apply_approved_proposal_to_banks,
    approve_authored_proposal,
    list_authored_proposals,
    list_authored_rebuild_queue,
    load_authored_proposal,
    load_proposal_approval,
    promote_validated_authored_repair,
    write_authored_proposal,
    write_authored_proposal_review,
    write_proposal_approval,
)
from techlingo_workflow.workspace import Workspace, payload_hash
from techlingo_workflow.publication_safety import inspect_publication_readiness


ROOT = Path(__file__).resolve().parents[1]
AI901 = ROOT / "courses" / "ai-901"
PROPOSAL_ID = "ai-agents-working-together-l2-62f2678a974f-round-2"
ITEM_KEY = "ai-agents-working-together/agent-llm/r4/v1"
PROPOSAL_FIXTURE = ROOT / "documents" / "ai-901" / "GAUNTLET_QUESTION_PROPOSAL.json"


def _proposal(course_dir: Path = AI901) -> GauntletAuthoredProposal:
    if course_dir == AI901:
        return load_authored_proposal(PROPOSAL_FIXTURE)
    proposals = list_authored_proposals(course_dir)
    assert [proposal.proposal_id for proposal in proposals] == [PROPOSAL_ID]
    return proposals[0]


def _approval(proposal: GauntletAuthoredProposal):
    return approve_authored_proposal(
        proposal,
        approved_by="Human Reviewer",
        exact_proposal_sha256=proposal.proposal_sha256,
        exact_history_sha256=proposal.history_sha256,
        exact_compiled_artifact_sha256=proposal.compiled_artifact_sha256,
        exact_champion_after_sha256=proposal.champion_after_sha256,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_ai901(tmp_path: Path) -> Path:
    target = tmp_path / "ai-901"
    shutil.copytree(
        AI901,
        target,
        ignore=shutil.ignore_patterns("build", "dist", "*.pyc", "__pycache__"),
    )
    workspace = Workspace(target)
    approvals = {}
    for path in (target / "gauntlet/proposals/approved").glob("*.json"):
        try:
            approval = load_proposal_approval(path)
        except GauntletProposalError:
            continue
        approvals[approval.approval_sha256] = approval
    state = workspace.load_build_state()
    while state.authored_repairs:
        promoted = state.authored_repairs[-1]
        approval = approvals[promoted.approval_sha256]
        change = approval.proposal.authored_changes[0]
        bank = workspace.load_bank(Path(change.bank_path).stem)
        item = next(item for item in bank.items if item.item_key == change.item_key)
        item.payload = change.payload_before
        item.payload_hash = payload_hash(item.payload)
        item.provenance = change.provenance_before
        item.pinned = change.pinned_before
        workspace.save_bank(bank)
        state.bank_sha256 = promoted.previous_bank_sha256
        state.sources[promoted.source_file].last_known_good.bank_sha256 = (
            promoted.previous_source_bank_sha256
        )
        state.authored_repairs.pop()
        workspace.save_build_state(state)
    return target


def _amend_current_item(course_dir: Path) -> None:
    amendment = json.loads(
        (
            course_dir
            / "gauntlet/proposals/approved/ai-agents-working-together-l2-single-choice-amendment.json"
        ).read_text(encoding="utf-8")
    )
    workspace = Workspace(course_dir)
    bank = workspace.load_bank("ai-agents-working-together")
    item = next(item for item in bank.items if item.item_key == ITEM_KEY)
    item.payload = amendment["payload_after"]
    item.payload_hash = payload_hash(item.payload)
    item.provenance = "human-edited"
    item.pinned = True
    workspace.save_bank(bank)


def test_reviewed_promoted_edit_fixture_exports_exact_delta_and_required_evidence():
    proposal = _proposal()
    assert proposal.compiled_artifact_sha256 == (
        "69c9d18537cc04d4d24f6ffad549e8d9c17ee1b85b2ba8dc167de6c8be33ae73"
    )
    assert proposal.champion_after_sha256 == (
        "5ee21dc24c8d8779a5d7ed51346e330d7e3c08729c54e9e1cff25b6fd6c29202"
    )
    assert {change.field for change in proposal.emitted_changes} == {
        "question_text",
        "options",
        "correct_answer",
        "explanation",
    }
    assert {change.item_key for change in proposal.emitted_changes} == {ITEM_KEY}
    assert proposal.evidence.hard_gate.passed is True
    assert proposal.evidence.source_fidelity_gate.passed is True
    assert proposal.evidence.comparison.stable is True
    assert len(proposal.evidence.comparison.records) == 2
    authored = proposal.authored_changes[0]
    assert authored.bank_path == "bank/ai-agents-working-together.json"
    assert authored.source_file == "2. Introduction to generative AI and agents.md"
    assert authored.payload_before["question_type"] == authored.payload_after["question_type"]
    assert authored.payload_before["concept_id"] == authored.payload_after["concept_id"]
    assert authored.changed_payload_fields == ["feedback_for_correct", "options", "prompt"]


def test_applied_history_is_not_reoffered_for_the_new_compiled_artifact():
    proposal_ids = {proposal.proposal_id for proposal in list_authored_proposals(AI901)}
    assert PROPOSAL_ID not in proposal_ids


def test_export_is_review_only_and_refuses_overwrite(tmp_path):
    proposal = _proposal()
    bank = AI901 / "bank" / "ai-agents-working-together.json"
    before = _sha256(bank)
    output = tmp_path / "proposal.json"
    assert write_authored_proposal(proposal, output) == output
    assert load_authored_proposal(output) == proposal
    assert _sha256(bank) == before
    with pytest.raises(GauntletProposalError, match="refusing to overwrite"):
        write_authored_proposal(proposal, output)


def test_human_review_page_shows_before_after_answers_without_app(tmp_path):
    proposal = _proposal()
    output = write_authored_proposal_review(proposal, tmp_path / "review.html")
    page = output.read_text(encoding="utf-8")
    assert "Current question" in page
    assert "Proposed question" in page
    assert "A large language model" in page
    assert "Generative AI for natural-language reasoning" in page
    assert "You are reviewing one question only—not a course rebuild" in page
    assert "No Terra generation or course-wide rebuild is involved" in page


def test_approval_requires_all_exact_hashes_and_detects_tampering(tmp_path):
    proposal = _proposal()
    with pytest.raises(GauntletProposalError, match="approval is not exact"):
        approve_authored_proposal(
            proposal,
            approved_by="Human Reviewer",
            exact_proposal_sha256="0" * 64,
            exact_history_sha256=proposal.history_sha256,
            exact_compiled_artifact_sha256=proposal.compiled_artifact_sha256,
            exact_champion_after_sha256=proposal.champion_after_sha256,
        )
    approval = _approval(proposal)
    path = write_proposal_approval(AI901, approval, tmp_path / "approval.json")
    assert load_proposal_approval(path) == approval
    tampered = path.read_text(encoding="utf-8").replace(
        "Human Reviewer", "Different Reviewer"
    )
    path.write_text(tampered, encoding="utf-8")
    with pytest.raises(GauntletProposalError, match="approval_sha256"):
        load_proposal_approval(path)


def test_exact_approval_updates_only_authoritative_bank_and_preserves_contract(tmp_path):
    course_dir = _copy_ai901(tmp_path)
    proposal = _proposal(course_dir)
    approval = _approval(proposal)
    source_files = apply_approved_proposal_to_banks(course_dir, approval)
    assert source_files == ["2. Introduction to generative AI and agents.md"]

    bank = Workspace(course_dir).load_bank("ai-agents-working-together")
    item = next(item for item in bank.items if item.item_key == ITEM_KEY)
    authored = proposal.authored_changes[0]
    assert item.payload == authored.payload_after
    assert item.payload["question_type"] == authored.payload_before["question_type"]
    assert item.payload["concept_id"] == authored.payload_before["concept_id"]
    assert item.source_hash is not None
    assert item.provenance == "human-edited"
    assert item.pinned is True
    assert not (course_dir / "dist").exists()


def test_incorporate_is_dry_without_execute(tmp_path):
    course_dir = _copy_ai901(tmp_path)
    proposal = _proposal(course_dir)
    approval = _approval(proposal)
    approval_path = write_proposal_approval(
        course_dir, approval, tmp_path / "dry-approval.json"
    )
    bank = course_dir / "bank" / "ai-agents-working-together.json"
    before = _sha256(bank)
    result = CliRunner().invoke(
        course_app,
        ["gauntlet", "proposal", "incorporate", str(course_dir), str(approval_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Dry run only" in result.output
    assert _sha256(bank) == before


def test_failed_deterministic_incorporation_restores_exact_previous_bank(tmp_path):
    course_dir = _copy_ai901(tmp_path)
    proposal = _proposal(course_dir)
    approval = _approval(proposal)
    approval_path = write_proposal_approval(
        course_dir, approval, tmp_path / "execute-approval.json"
    )
    bank = course_dir / "bank" / "ai-agents-working-together.json"
    before = _sha256(bank)
    result = CliRunner().invoke(
        course_app,
        [
            "gauntlet",
            "proposal",
            "incorporate",
            str(course_dir),
            str(approval_path),
            "--execute",
        ],
    )
    assert result.exit_code == 1
    assert "exact previous bank item was restored" in result.output
    assert _sha256(bank) == before


def test_unsafe_unbounded_findings_are_kept_in_a_separate_queue(tmp_path):
    entries = list_authored_rebuild_queue(_copy_ai901(tmp_path))
    unsafe = [
        entry
        for entry in entries
        if entry.unit_key == "how-computers-represent-images-l3"
    ]
    assert len(unsafe) == 1
    assert unsafe[0].reason is HumanReviewReason.unsafe_or_unbounded_repair
    assert all(entry.unit_key != "ai-agents-working-together-l2" for entry in entries)


def test_unmappable_promoted_edits_do_not_hide_safe_proposals(tmp_path):
    course_dir = _copy_ai901(tmp_path)
    proposals = list_authored_proposals(course_dir)
    entries = list_authored_rebuild_queue(course_dir)
    assert isinstance(proposals, list)
    assert isinstance(entries, list)
    assert {proposal.proposal_id for proposal in proposals}.isdisjoint(
        {entry.queue_id for entry in entries}
    )


def test_validated_authored_repair_promotes_exact_delta_and_receipt(tmp_path, monkeypatch):
    course_dir = _copy_ai901(tmp_path)
    _amend_current_item(course_dir)
    monkeypatch.chdir(tmp_path)
    course_dir = Path("ai-901")
    approval_path = (
        course_dir
        / "gauntlet/proposals/approved/ai-agents-working-together-l2-62f2678a974f-round-2.json"
    )
    record = promote_validated_authored_repair(
        course_dir,
        load_proposal_approval(approval_path),
        amendment_path=course_dir
        / "gauntlet/proposals/approved/ai-agents-working-together-l2-single-choice-amendment.json",
        receipt_path=course_dir
        / "gauntlet/proposals/applied/ai-agents-working-together-l2-single-choice.json",
    )
    assert record.item_key == ITEM_KEY
    assert record.artifact_sha256 == (
        "2fa356d363b3a7f13d4d399d67361e952b387b4b6502e0aa169d3bb7f3169d2b"
    )
    assert inspect_publication_readiness(course_dir).ok


def test_promoted_authored_repair_receipt_tampering_blocks_publication(tmp_path):
    course_dir = _copy_ai901(tmp_path)
    _amend_current_item(course_dir)
    approval_path = (
        course_dir
        / "gauntlet/proposals/approved/ai-agents-working-together-l2-62f2678a974f-round-2.json"
    )
    receipt_path = (
        course_dir
        / "gauntlet/proposals/applied/ai-agents-working-together-l2-single-choice.json"
    )
    promote_validated_authored_repair(
        course_dir,
        load_proposal_approval(approval_path),
        amendment_path=course_dir
        / "gauntlet/proposals/approved/ai-agents-working-together-l2-single-choice-amendment.json",
        receipt_path=receipt_path,
    )
    receipt_path.write_text(receipt_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    report = inspect_publication_readiness(course_dir)
    assert not report.ok
    assert any("receipt changed after promotion" in blocker for blocker in report.blockers)
