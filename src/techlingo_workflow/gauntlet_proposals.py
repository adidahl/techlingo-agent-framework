"""Reviewable, hash-bound Gauntlet proposal incorporation.

Gauntlet histories are immutable evidence.  This module derives review
artifacts from that evidence, translates the exact learner-facing delta back
to its unique authoritative bank item, records explicit human approval, and
applies an approved delta only to the authored bank path. Deterministic
validation and any fresh qualitative evaluation remain explicit steps; source
regeneration is never part of a reviewed item correction.
"""

from __future__ import annotations

import json
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from pydantic import Field, model_validator

from .compiler import compile_workspace
from .emit import emit_question
from .gauntlet_io import (
    GauntletRecord,
    compiled_unit_artifacts,
    iter_gauntlet_records,
    load_gauntlet_record,
)
from .gauntlet_models import (
    CriticEvaluation,
    HardGateResult,
    HumanReviewReason,
    PairwiseComparisonResult,
    QualityDimension,
    RoundDecision,
    StrictModel,
    canonical_sha256,
)
from .publication_safety import banks_sha256, hash_data, inspect_publication_readiness
from .workspace import (
    AuthoredRepairPublication,
    BankItem,
    Workspace,
    _atomic_write_text,
    canonical_json,
    parse_exercise,
    payload_hash,
    sha256_text,
    utc_now_iso,
)


PROPOSAL_SCHEMA = "techlingo-gauntlet-authored-proposal-v1"
APPROVAL_SCHEMA = "techlingo-gauntlet-authored-approval-v1"
QUEUE_SCHEMA = "techlingo-gauntlet-authored-queue-v1"
PROTECTED_EMITTED_FIELDS = frozenset({"import_key", "question_type", "points"})
PROTECTED_OPTION_METADATA = frozenset(
    {
        "blooms_level",
        "original_question_type",
        "concept_id",
        "item_key",
        "rung",
        "variant",
        "module_key",
        "lesson_key",
        "learning_status",
    }
)


class GauntletProposalError(ValueError):
    """A proposal cannot be reconstructed or safely mapped to authored data."""


class ProposalFieldChange(StrictModel):
    item_key: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    field: str = Field(min_length=1)
    before: Any
    after: Any


class AuthoredItemChange(StrictModel):
    item_key: str = Field(min_length=1)
    bank_path: str = Field(min_length=1)
    bank_item_index: int = Field(ge=0)
    source_file: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bank_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_before: str = Field(min_length=1)
    pinned_before: bool
    payload_before: dict[str, Any]
    payload_after: dict[str, Any]
    changed_payload_fields: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def exact_payload_delta(self) -> "AuthoredItemChange":
        changed = sorted(
            key
            for key in set(self.payload_before) | set(self.payload_after)
            if canonical_json(self.payload_before.get(key))
            != canonical_json(self.payload_after.get(key))
        )
        if self.changed_payload_fields != changed:
            raise ValueError("changed_payload_fields does not match authored payload delta")
        if self.bank_payload_sha256 != canonical_sha256(self.payload_before):
            raise ValueError("bank_payload_sha256 does not match payload_before")
        return self


class ProposalEvidence(StrictModel):
    hard_gate: HardGateResult
    source_fidelity_critic: CriticEvaluation
    source_fidelity_gate: HardGateResult
    comparison: PairwiseComparisonResult


class GauntletAuthoredProposal(StrictModel):
    schema_version: Literal["techlingo-gauntlet-authored-proposal-v1"] = PROPOSAL_SCHEMA
    proposal_id: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit_key: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    round_number: int = Field(ge=1)
    record_path: str = Field(min_length=1)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    champion_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    champion_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gauntlet_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_summary: str = Field(min_length=1)
    repair_instruction: str = Field(min_length=1)
    emitted_changes: list[ProposalFieldChange] = Field(min_length=1)
    authored_changes: list[AuthoredItemChange] = Field(min_length=1)
    evidence: ProposalEvidence

    def calculated_sha256(self) -> str:
        return canonical_sha256(
            self.model_dump(mode="json", exclude={"proposal_sha256"})
        )

    @model_validator(mode="after")
    def hashes_are_self_consistent(self) -> "GauntletAuthoredProposal":
        if self.proposal_sha256 != self.calculated_sha256():
            raise ValueError("proposal_sha256 does not match the proposal contents")
        if self.evidence.hard_gate.artifact_sha256 != self.champion_after_sha256:
            raise ValueError("hard gate is not bound to the proposed champion")
        if self.evidence.source_fidelity_gate.artifact_sha256 != self.champion_after_sha256:
            raise ValueError("source-fidelity gate is not bound to the proposed champion")
        comparison = self.evidence.comparison
        if (
            comparison.champion_sha256 != self.champion_before_sha256
            or comparison.challenger_sha256 != self.champion_after_sha256
        ):
            raise ValueError("comparison is not bound to the proposal before/after hashes")
        return self


class GauntletProposalApproval(StrictModel):
    schema_version: Literal["techlingo-gauntlet-authored-approval-v1"] = APPROVAL_SCHEMA
    approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: GauntletAuthoredProposal
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    exact_proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_compiled_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_champion_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def calculated_sha256(self) -> str:
        return canonical_sha256(
            self.model_dump(mode="json", exclude={"approval_sha256"})
        )

    @model_validator(mode="after")
    def exact_hashes_match(self) -> "GauntletProposalApproval":
        if self.approved_at.tzinfo is None:
            raise ValueError("approved_at must be timezone-aware")
        expected = (
            self.proposal.proposal_sha256,
            self.proposal.history_sha256,
            self.proposal.compiled_artifact_sha256,
            self.proposal.champion_after_sha256,
        )
        actual = (
            self.exact_proposal_sha256,
            self.exact_history_sha256,
            self.exact_compiled_artifact_sha256,
            self.exact_champion_after_sha256,
        )
        if actual != expected:
            raise ValueError("approval hashes do not match the embedded exact proposal")
        if self.approval_sha256 != self.calculated_sha256():
            raise ValueError("approval_sha256 does not match the approval contents")
        return self


class AuthoredRebuildQueueEntry(StrictModel):
    schema_version: Literal["techlingo-gauntlet-authored-queue-v1"] = QUEUE_SCHEMA
    queue_id: str = Field(min_length=1)
    unit_key: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    round_number: int = Field(ge=1)
    reason: HumanReviewReason
    summary: str = Field(min_length=1)
    record_path: str = Field(min_length=1)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gauntlet_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _model_sha256(value: StrictModel | GauntletRecord) -> str:
    return canonical_sha256(value.model_dump(mode="json"))


def _changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    return sorted(
        key
        for key in set(before) | set(after)
        if canonical_json(before.get(key)) != canonical_json(after.get(key))
    )


def _choice_answer_indexes(payload: Mapping[str, Any]) -> list[int]:
    raw = payload.get("correct_answer")
    if isinstance(raw, str) and raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GauntletProposalError("choice correct_answer is malformed JSON") from exc
        if not isinstance(parsed, list) or any(not isinstance(value, int) for value in parsed):
            raise GauntletProposalError("choice correct_answer must contain integer indexes")
        return parsed
    try:
        return [int(raw)]
    except (TypeError, ValueError) as exc:
        raise GauntletProposalError("choice correct_answer is not an index") from exc


def _translate_choice_payload(
    bank_payload: Mapping[str, Any],
    emitted_before: Mapping[str, Any],
    emitted_after: Mapping[str, Any],
) -> dict[str, Any]:
    if bank_payload.get("question_type") not in {"single_choice", "multi_choice"}:
        raise GauntletProposalError("emitted multiple_choice does not map to a choice bank item")
    before_options = emitted_before.get("options", {}).get("options")
    after_options = emitted_after.get("options", {}).get("options")
    bank_options = bank_payload.get("options")
    if not all(isinstance(value, list) for value in (before_options, after_options, bank_options)):
        raise GauntletProposalError("choice proposal is missing an options array")
    if len(before_options) != len(after_options) or len(before_options) != len(bank_options):
        raise GauntletProposalError("choice proposal cannot add, remove, or replace options")

    canonical_bank = [canonical_json(value) for value in bank_options]
    if len(canonical_bank) != len(set(canonical_bank)):
        raise GauntletProposalError("authored option mapping is ambiguous because options are duplicated")
    index_by_option = {value: index for index, value in enumerate(canonical_bank)}
    mapped_indexes: list[int] = []
    for option in before_options:
        key = canonical_json(option)
        if key not in index_by_option:
            raise GauntletProposalError(
                "compiled option cannot be mapped literally to the authoritative bank item"
            )
        mapped_indexes.append(index_by_option[key])
    if len(mapped_indexes) != len(set(mapped_indexes)):
        raise GauntletProposalError("compiled option mapping is not one-to-one")

    translated = json.loads(json.dumps(bank_payload, ensure_ascii=False))
    for compiled_index, bank_index in enumerate(mapped_indexes):
        translated["options"][bank_index] = after_options[compiled_index]
    translated["prompt"] = emitted_after["question_text"]
    translated["feedback_for_correct"] = emitted_after.get("explanation")

    correct_from_options = [
        index
        for index, option in enumerate(after_options)
        if option.get("is_correct") is True
    ]
    if _choice_answer_indexes(emitted_after) != correct_from_options:
        raise GauntletProposalError("proposed correct_answer disagrees with proposed option flags")
    if translated["question_type"] == "single_choice" and len(correct_from_options) != 1:
        raise GauntletProposalError("single-choice authored item must retain exactly one answer")
    if translated["question_type"] == "multi_choice" and not correct_from_options:
        raise GauntletProposalError("multi-choice authored item must retain at least one answer")
    return translated


def _translate_to_authored_payload(
    bank_item: BankItem,
    emitted_before: Mapping[str, Any],
    emitted_after: Mapping[str, Any],
    changed_fields: Iterable[str],
) -> dict[str, Any]:
    changed = set(changed_fields)
    if changed & PROTECTED_EMITTED_FIELDS:
        raise GauntletProposalError(
            "proposal changes protected emitted field(s): "
            + ", ".join(sorted(changed & PROTECTED_EMITTED_FIELDS))
        )
    unsupported = changed - {"question_text", "options", "correct_answer", "explanation"}
    if unsupported:
        raise GauntletProposalError(
            "proposal uses unsupported authored mapping field(s): "
            + ", ".join(sorted(unsupported))
        )
    before_options = emitted_before.get("options")
    after_options = emitted_after.get("options")
    if not isinstance(before_options, Mapping) or not isinstance(after_options, Mapping):
        raise GauntletProposalError("proposal options metadata is missing")
    metadata_changes = _changed_fields(before_options, after_options)
    illegal_metadata = set(metadata_changes) & PROTECTED_OPTION_METADATA
    if illegal_metadata:
        raise GauntletProposalError(
            "proposal changes protected identity/mechanic metadata: "
            + ", ".join(sorted(illegal_metadata))
        )
    authored_type = bank_item.payload.get("question_type")
    translated = json.loads(json.dumps(bank_item.payload, ensure_ascii=False))
    if emitted_before.get("question_type") == "multiple_choice":
        translated = _translate_choice_payload(bank_item.payload, emitted_before, emitted_after)
    elif authored_type == "true_false" and emitted_before.get("question_type") == "true_false":
        answer = emitted_after.get("correct_answer")
        if answer not in {"true", "false"}:
            raise GauntletProposalError("true/false proposal has an invalid answer")
        translated["statement"] = emitted_after.get("question_text")
        translated["correct_answer"] = answer == "true"
        translated["feedback_for_correct"] = emitted_after.get("explanation")
        translated["feedback_for_incorrect"] = after_options.get("feedback_for_incorrect")
    elif authored_type == "fill_gaps" and emitted_before.get("question_type") == "fill_blank":
        parts = after_options.get("parts")
        if not isinstance(parts, list):
            raise GauntletProposalError("fill-gap proposal is missing authored parts")
        translated["parts"] = parts
        translated["explanation"] = emitted_after.get("explanation")
        translated["feedback_for_incorrect"] = after_options.get("feedback_for_incorrect")
    elif authored_type == "rearrange" and emitted_before.get("question_type") == "arrange_sentence":
        word_bank = after_options.get("word_bank")
        correct_order = after_options.get("correct_order")
        if not isinstance(word_bank, list) or not isinstance(correct_order, list):
            raise GauntletProposalError("rearrange proposal is missing word/order arrays")
        translated["prompt"] = emitted_after.get("question_text")
        translated["word_bank"] = word_bank
        translated["correct_order"] = correct_order
        translated["interchangeable_groups"] = after_options.get(
            "interchangeable_groups", []
        )
        translated["explanation"] = emitted_after.get("explanation")
        translated["feedback_for_incorrect"] = after_options.get("feedback_for_incorrect")
    else:
        raise GauntletProposalError(
            "emitted mechanic does not map to the authoritative bank item"
        )
    if translated.get("question_type") != bank_item.payload.get("question_type"):
        raise GauntletProposalError("authored translation changed question_type")
    if translated.get("concept_id") != bank_item.payload.get("concept_id"):
        raise GauntletProposalError("authored translation changed concept_id")
    try:
        parse_exercise(translated)
    except ValueError as exc:
        raise GauntletProposalError(f"translated authored payload is invalid: {exc}") from exc

    # Re-emission from the authored shape must reproduce the proposal semantics.
    reemitted = emit_question(parse_exercise(translated), str(emitted_after["import_key"]))
    if reemitted.question_text != emitted_after.get("question_text"):
        raise GauntletProposalError("authored question text does not reproduce the proposal")
    if reemitted.explanation != emitted_after.get("explanation"):
        raise GauntletProposalError("authored explanation does not reproduce the proposal")
    if emitted_after.get("question_type") == "multiple_choice":
        expected_options = emitted_after.get("options", {}).get("options", [])
        actual_options = reemitted.options.get("options", [])
        if sorted(map(canonical_json, expected_options)) != sorted(map(canonical_json, actual_options)):
            raise GauntletProposalError("authored options do not reproduce the proposal")
        expected_correct_count = len(_choice_answer_indexes(emitted_after))
        actual_correct_count = len(_choice_answer_indexes(reemitted.model_dump(mode="json")))
        if actual_correct_count != expected_correct_count:
            raise GauntletProposalError("authored answer semantics do not reproduce the proposal")
    else:
        actual = reemitted.model_dump(mode="json")
        if actual.get("correct_answer") != emitted_after.get("correct_answer"):
            raise GauntletProposalError("authored answer does not reproduce the proposal")
        for key in set(after_options) - PROTECTED_OPTION_METADATA:
            if canonical_json(actual.get("options", {}).get(key)) != canonical_json(after_options.get(key)):
                raise GauntletProposalError(
                    f"authored options field {key!r} does not reproduce the proposal"
                )
    return translated


def _unique_bank_target(
    ws: Workspace,
    item_key: str,
) -> tuple[Path, int, BankItem, str]:
    matches: list[tuple[Path, int, BankItem, str]] = []
    curriculum = ws.load_curriculum()
    source_by_module = {module.key: module.source_file for module in curriculum.modules}
    for path in sorted(ws.bank_dir.glob("*.json")):
        bank = ws.load_bank(path.stem)
        source_file = source_by_module.get(bank.module)
        for index, item in enumerate(bank.items):
            if item.item_key == item_key and source_file:
                matches.append((path, index, item, source_file))
    if not matches:
        raise GauntletProposalError(
            f"item {item_key!r} does not map to an authoritative generated bank/source"
        )
    if len(matches) != 1:
        paths = ", ".join(str(path) for path, _index, _item, _source in matches)
        raise GauntletProposalError(f"item {item_key!r} maps ambiguously: {paths}")
    return matches[0]


def _proposal_for_round(
    ws: Workspace,
    *,
    record_path: Path,
    record: GauntletRecord,
    round_number: int,
    compiled_hash: str,
    current_artifacts: Mapping[str, Any],
) -> GauntletAuthoredProposal:
    round_ = record.history.rounds[round_number - 1]
    if round_.decision is not RoundDecision.promoted or not round_.touched_item_keys:
        raise GauntletProposalError("round is not a promoted content edit")
    if record.compiled_artifact_sha256 != compiled_hash:
        raise GauntletProposalError("record is not bound to the exact current compiled artifact")
    current_artifact = current_artifacts.get(record.unit_key)
    if current_artifact is None:
        raise GauntletProposalError("record unit is absent from the current compiled artifact")
    if current_artifact.content_hash() != round_.champion_hash_before:
        raise GauntletProposalError(
            "promoted round cannot be reconstructed from the exact current champion"
        )
    if round_.promoted_champion_before is not None:
        before_artifact = round_.promoted_champion_before
        after_artifact = round_.promoted_challenger
        if after_artifact is None:
            raise GauntletProposalError("promoted snapshot evidence is incomplete")
    else:
        # Backward-compatible fallback for old single-promotion records whose
        # final persisted champion is the exact promoted challenger.
        before_artifact = current_artifact
        after_artifact = record.champion
        if after_artifact.content_hash() != round_.challenger_hash:
            raise GauntletProposalError(
                "legacy promoted challenger is not the final persisted champion snapshot"
            )
    if round_.hard_gate is None or not round_.hard_gate.passed:
        raise GauntletProposalError("promoted proposal lacks a passing hard gate")
    if round_.source_fidelity_critic is None or round_.source_fidelity_gate is None:
        raise GauntletProposalError("promoted content edit lacks source-fidelity evidence")
    if not round_.source_fidelity_gate.passed:
        raise GauntletProposalError("promoted content edit failed source fidelity")
    if round_.comparison is None or not round_.comparison.stable:
        raise GauntletProposalError("promoted content edit lacks stable comparison evidence")
    context = record.history.evaluation_context
    if context is None:
        raise GauntletProposalError("legacy unbound history cannot become an authored proposal")
    directive = round_.critic.result.largest_gap if round_.critic is not None else None
    if directive is None:
        raise GauntletProposalError("promoted content edit lacks its critic directive")

    before_by_key = {item.item_key: item for item in before_artifact.items}
    after_by_key = {item.item_key: item for item in after_artifact.items}
    if list(before_by_key) != list(after_by_key):
        raise GauntletProposalError("content proposal added, removed, replaced, or reordered items")
    actual_touched = {
        key
        for key in before_by_key
        if canonical_json(before_by_key[key].payload)
        != canonical_json(after_by_key[key].payload)
    }
    if actual_touched != set(round_.touched_item_keys):
        raise GauntletProposalError("persisted touched_item_keys do not match the exact payload delta")
    if len(actual_touched) != 1:
        raise GauntletProposalError(
            "atomic authored repair promotion currently requires exactly one changed item"
        )

    emitted_changes: list[ProposalFieldChange] = []
    authored_changes: list[AuthoredItemChange] = []
    for item_key in sorted(actual_touched):
        before = before_by_key[item_key]
        after = after_by_key[item_key]
        fields = _changed_fields(before.payload, after.payload)
        if not fields or not set(fields).issubset(directive.allowed_payload_fields):
            raise GauntletProposalError(
                f"item {item_key!r} changes fields outside its approved critic directive"
            )
        for field in fields:
            emitted_changes.append(
                ProposalFieldChange(
                    item_key=item_key,
                    artifact_path=before.path,
                    field=field,
                    before=before.payload.get(field),
                    after=after.payload.get(field),
                )
            )
        bank_path, bank_index, bank_item, source_file = _unique_bank_target(ws, item_key)
        source_path = ws.sources_dir / source_file
        if not source_path.is_file() or source_path.is_symlink():
            raise GauntletProposalError(f"authoritative source is unsafe or missing: {source_file}")
        source_sha256 = sha256_text(source_path.read_text(encoding="utf-8"))
        if bank_item.source_hash != source_sha256:
            raise GauntletProposalError(
                f"item {item_key!r} is not bound to the exact authoritative source hash"
            )
        translated = _translate_to_authored_payload(
            bank_item, before.payload, after.payload, fields
        )
        authored_changes.append(
            AuthoredItemChange(
                item_key=item_key,
                bank_path=str(bank_path.relative_to(ws.root)),
                bank_item_index=bank_index,
                source_file=source_file,
                source_sha256=source_sha256,
                item_source_sha256=bank_item.source_hash,
                bank_payload_sha256=canonical_sha256(bank_item.payload),
                provenance_before=bank_item.provenance,
                pinned_before=bank_item.pinned,
                payload_before=bank_item.payload,
                payload_after=translated,
                changed_payload_fields=_changed_fields(bank_item.payload, translated),
            )
        )

    record_rel = str(record_path.relative_to(ws.root))
    data: dict[str, Any] = {
        "proposal_id": f"{record.history.run_id}-round-{round_number}",
        "proposal_sha256": "0" * 64,
        "unit_key": record.unit_key,
        "run_id": record.history.run_id,
        "round_number": round_number,
        "record_path": record_rel,
        "record_sha256": _model_sha256(record),
        "history_sha256": _model_sha256(record.history),
        "compiled_artifact_sha256": record.compiled_artifact_sha256,
        "champion_before_sha256": round_.champion_hash_before,
        "champion_after_sha256": round_.challenger_hash,
        "evaluation_context_sha256": context.context_sha256,
        "gauntlet_policy_sha256": context.gauntlet_policy_sha256,
        "repair_summary": round_.repair_summary or round_.decision_evidence,
        "repair_instruction": directive.repair_instruction,
        "emitted_changes": emitted_changes,
        "authored_changes": authored_changes,
        "evidence": ProposalEvidence(
            hard_gate=round_.hard_gate,
            source_fidelity_critic=round_.source_fidelity_critic,
            source_fidelity_gate=round_.source_fidelity_gate,
            comparison=round_.comparison,
        ),
    }
    unhashed = GauntletAuthoredProposal.model_construct(**data)
    data["proposal_sha256"] = unhashed.calculated_sha256()
    return GauntletAuthoredProposal.model_validate(data)


def list_authored_proposals(course_dir: str | Path) -> list[GauntletAuthoredProposal]:
    """Return only reconstructable promoted content edits for the exact workspace."""

    ws = Workspace(course_dir).require()
    compiled = compile_workspace(course_dir)
    if compiled.problems:
        raise GauntletProposalError(
            "current compiled learner artifact fails deterministic validation: "
            + "; ".join(compiled.problems[:10])
        )
    compiled_hash = hash_data(compiled.tl_course)
    artifacts = compiled_unit_artifacts(compiled)
    proposals: list[GauntletAuthoredProposal] = []
    latest: dict[str, tuple[Path, GauntletRecord]] = {}
    for path, record in iter_gauntlet_records(course_dir, strict=True):
        current_artifact = artifacts.get(record.unit_key)
        if (
            record.compiled_artifact_sha256 != compiled_hash
            or current_artifact is None
            or record.history.initial_champion_hash != current_artifact.content_hash()
        ):
            continue
        current = latest.get(record.unit_key)
        if current is None or record.history.started_at > current[1].history.started_at:
            latest[record.unit_key] = (path, record)
    for path, record in latest.values():
        # Histories are immutable evidence for the artifact they reviewed.  Once
        # an approved authored repair changes the bank, those histories remain
        # valid history but are no longer proposals for the current artifact.
        if record.compiled_artifact_sha256 != compiled_hash:
            continue
        current_artifact = artifacts.get(record.unit_key)
        for round_ in record.history.rounds:
            if (
                round_.decision is RoundDecision.promoted
                and round_.touched_item_keys
                and current_artifact is not None
                and round_.champion_hash_before == current_artifact.content_hash()
            ):
                # Pre-snapshot histories are exportable only when their final
                # champion is this exact challenger. A later promotion makes
                # the intermediate payload unknowable; preserve that history
                # for diagnostics, but never guess a proposal from it.
                if (
                    round_.promoted_champion_before is None
                    and record.champion.content_hash() != round_.challenger_hash
                ):
                    continue
                try:
                    proposals.append(
                        _proposal_for_round(
                            ws,
                            record_path=path,
                            record=record,
                            round_number=round_.round_number,
                            compiled_hash=compiled_hash,
                            current_artifacts=artifacts,
                        )
                    )
                except GauntletProposalError as exc:
                    # A promoted edit can be valid Gauntlet evidence while still
                    # being impossible to map mechanically to the authoritative
                    # bank shape (ordering, matching, and other non-choice
                    # mechanics are deliberately unsupported).  Such edits are
                    # authored-rebuild work, not a reason to hide every other
                    # reconstructable proposal in the workspace.
                    continue
    return sorted(proposals, key=lambda item: (item.run_id, item.round_number))


def list_authored_rebuild_queue(course_dir: str | Path) -> list[AuthoredRebuildQueueEntry]:
    """List blocking unsafe/unbounded critic findings separately from proposals."""

    ws = Workspace(course_dir).require()
    compiled = compile_workspace(course_dir)
    if compiled.problems:
        raise GauntletProposalError(
            "current compiled learner artifact fails deterministic validation: "
            + "; ".join(compiled.problems[:10])
        )
    compiled_hash = hash_data(compiled.tl_course)
    artifacts = compiled_unit_artifacts(compiled)
    entries: list[AuthoredRebuildQueueEntry] = []
    latest: dict[str, tuple[Path, GauntletRecord]] = {}
    for path, record in iter_gauntlet_records(course_dir, strict=True):
        current_artifact = artifacts.get(record.unit_key)
        if (
            record.compiled_artifact_sha256 != compiled_hash
            or current_artifact is None
            or record.history.initial_champion_hash != current_artifact.content_hash()
        ):
            continue
        current = latest.get(record.unit_key)
        if current is None or record.history.started_at > current[1].history.started_at:
            latest[record.unit_key] = (path, record)
    for path, record in latest.values():
        context = record.history.evaluation_context
        if context is None:
            continue
        for round_ in record.history.rounds:
            current_artifact = artifacts.get(record.unit_key)
            if (
                record.compiled_artifact_sha256 == compiled_hash
                and current_artifact is not None
                and round_.decision is RoundDecision.promoted
                and round_.touched_item_keys
                and round_.champion_hash_before == current_artifact.content_hash()
                and not (
                    round_.promoted_champion_before is None
                    and record.champion.content_hash() != round_.challenger_hash
                )
            ):
                try:
                    _proposal_for_round(
                        ws,
                        record_path=path,
                        record=record,
                        round_number=round_.round_number,
                        compiled_hash=compiled_hash,
                        current_artifacts=artifacts,
                    )
                except GauntletProposalError as exc:
                    entries.append(
                        AuthoredRebuildQueueEntry(
                            queue_id=f"{record.history.run_id}-round-{round_.round_number}",
                            unit_key=record.unit_key,
                            run_id=record.history.run_id,
                            round_number=round_.round_number,
                            reason=HumanReviewReason.unsafe_or_unbounded_repair,
                            summary=f"Promoted edit requires an authored rebuild: {exc}",
                            record_path=str(path.relative_to(ws.root)),
                            record_sha256=_model_sha256(record),
                            history_sha256=_model_sha256(record.history),
                            compiled_artifact_sha256=record.compiled_artifact_sha256,
                            evaluation_context_sha256=context.context_sha256,
                            gauntlet_policy_sha256=context.gauntlet_policy_sha256,
                        )
                    )
            if round_.critic is None:
                continue
            result = round_.critic.result
            if result.human_review_reason is not HumanReviewReason.unsafe_or_unbounded_repair:
                continue
            entries.append(
                AuthoredRebuildQueueEntry(
                    queue_id=f"{record.history.run_id}-round-{round_.round_number}",
                    unit_key=record.unit_key,
                    run_id=record.history.run_id,
                    round_number=round_.round_number,
                    reason=result.human_review_reason,
                    summary=result.concise_summary,
                    record_path=str(path.relative_to(ws.root)),
                    record_sha256=_model_sha256(record),
                    history_sha256=_model_sha256(record.history),
                    compiled_artifact_sha256=record.compiled_artifact_sha256,
                    evaluation_context_sha256=context.context_sha256,
                    gauntlet_policy_sha256=context.gauntlet_policy_sha256,
                )
            )
    unique = {entry.queue_id: entry for entry in entries}
    return sorted(unique.values(), key=lambda item: (item.run_id, item.round_number))


def find_authored_proposal(
    course_dir: str | Path, identifier: str
) -> GauntletAuthoredProposal:
    matches = [
        proposal
        for proposal in list_authored_proposals(course_dir)
        if identifier in {proposal.proposal_id, proposal.run_id}
    ]
    if not matches:
        raise GauntletProposalError(f"no current authored proposal matches {identifier!r}")
    if len(matches) != 1:
        raise GauntletProposalError(f"authored proposal identifier {identifier!r} is ambiguous")
    return matches[0]


def write_authored_proposal(
    proposal: GauntletAuthoredProposal, output: str | Path
) -> Path:
    path = Path(output)
    if path.exists():
        raise GauntletProposalError(f"refusing to overwrite proposal review artifact: {path}")
    _atomic_write_text(path, proposal.model_dump_json(indent=2) + "\n")
    return path


def render_authored_proposal_review(proposal: GauntletAuthoredProposal) -> str:
    """Render a self-contained human review page; hashes stay secondary."""

    if len(proposal.authored_changes) != 1:
        raise GauntletProposalError(
            "human review page currently requires exactly one authored item change"
        )
    change = proposal.authored_changes[0]

    def question_card(label: str, payload: Mapping[str, Any], css_class: str) -> str:
        prompt = escape(str(payload.get("prompt") or payload.get("statement") or ""))
        mechanic = str(payload.get("question_type") or "unknown")
        mechanic_label = {
            "single_choice": "Single choice — exactly one answer",
            "multi_choice": "Multi-select — two or more answers required",
        }.get(mechanic, mechanic.replace("_", " ").title())
        correct_count = sum(
            option.get("is_correct") is True for option in payload.get("options", [])
        )
        mechanic_warning = (
            '<div class="mechanic-warning">INVALID: this is still multi-select but has fewer than two correct answers.</div>'
            if mechanic == "multi_choice" and correct_count < 2
            else ""
        )
        option_rows: list[str] = []
        for index, option in enumerate(payload.get("options", []), start=1):
            correct = option.get("is_correct") is True
            badge = '<span class="correct">CORRECT</span>' if correct else ""
            rationale = escape(str(option.get("rationale") or ""))
            option_rows.append(
                f'<li class="{"is-correct" if correct else ""}">'
                f'<div class="option-head"><span>{index}. {escape(str(option.get("text") or ""))}</span>{badge}</div>'
                f'<div class="rationale">{rationale}</div></li>'
            )
        explanation = escape(str(payload.get("feedback_for_correct") or payload.get("explanation") or ""))
        return (
            f'<section class="question-card {css_class}"><h2>{escape(label)}</h2>'
            f'<div class="mechanic">{escape(mechanic_label)}</div>{mechanic_warning}'
            f'<div class="prompt">{prompt}</div><ol>{"".join(option_rows)}</ol>'
            f'<div class="explanation"><strong>Answer explanation</strong><br>{explanation}</div></section>'
        )

    evidence = proposal.evidence
    source_score = evidence.source_fidelity_critic.result.scores()[
        QualityDimension.factual_fidelity
    ]
    comparison = evidence.comparison
    raw_json = escape(proposal.model_dump_json(indent=2))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gauntlet question review — {escape(proposal.unit_key)}</title>
<style>
:root{{--ink:#172033;--muted:#596579;--paper:#f5f7fb;--line:#dbe1eb;--before:#fff8ed;--after:#effbf4;--green:#147a45;--amber:#9a5b00}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:1280px;margin:0 auto;padding:40px 28px 64px}} h1{{font-size:32px;margin:0 0 8px}} .lede{{font-size:18px;color:var(--muted);max-width:900px}}
.status{{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}} .pill{{background:white;border:1px solid var(--line);border-radius:999px;padding:7px 12px;font-weight:650}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}} .question-card{{border:1px solid var(--line);border-radius:16px;padding:24px;box-shadow:0 8px 24px #1c2d4a12}}
.before{{background:var(--before)}} .after{{background:var(--after)}} h2{{margin-top:0}} .prompt{{font-size:20px;font-weight:700;margin:16px 0 20px}}
.mechanic{{display:inline-block;background:#e8edf5;border-radius:999px;padding:5px 10px;font-size:13px;font-weight:750}} .mechanic-warning{{margin-top:12px;background:#fff0f0;border:2px solid #c93535;color:#8b1f1f;border-radius:10px;padding:10px 12px;font-weight:750}}
ol{{padding-left:24px}} li{{background:#ffffffb8;border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}} li.is-correct{{border:2px solid var(--green)}}
.option-head{{display:flex;justify-content:space-between;gap:12px;font-weight:650}} .correct{{color:white;background:var(--green);border-radius:999px;padding:2px 8px;font-size:12px}}
.rationale{{color:var(--muted);font-size:14px;margin-top:6px}} .explanation{{border-top:1px solid var(--line);padding-top:16px;margin-top:20px}}
.decision{{background:white;border:1px solid var(--line);border-radius:16px;padding:24px;margin-top:24px}} .decision strong{{font-size:19px}}
details{{margin-top:20px;color:var(--muted)}} pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#101828;color:#dbe7ff;padding:18px;border-radius:12px;font-size:12px}}
@media(max-width:850px){{.grid{{grid-template-columns:1fr}} main{{padding:24px 16px}}}}
</style></head><body><main>
<h1>Review one proposed question fix</h1>
<p class="lede">Nothing has been applied. Compare the current question with the proposed correction below. You are reviewing one question only—not a course rebuild.</p>
<div class="status">
  <span class="pill">Hard gate: PASS</span>
  <span class="pill">Source fidelity: PASS ({source_score:.2f})</span>
  <span class="pill">Blind comparisons: {len(comparison.records)}/2 prefer proposal</span>
</div>
<div class="grid">
{question_card("Current question", change.payload_before, "before")}
{question_card("Proposed question", change.payload_after, "after")}
</div>
<section class="decision"><strong>What you decide</strong>
<p>Approve if the proposed wording, correct answer, option rationales, and explanation are accurate. Reject it if anything should change.</p>
<p><strong>To approve in Codex:</strong> “I approve this question fix. Reviewed by YOUR NAME.”<br>
<strong>To request changes:</strong> describe the wording or answer you want changed.</p>
<p>After approval, Codex updates only this authoritative bank item, marks it human-edited and pinned, and runs deterministic validation. No Terra generation or course-wide rebuild is involved.</p></section>
<details><summary>Technical evidence and exact hashes</summary><pre>{raw_json}</pre></details>
</main></body></html>"""


def write_authored_proposal_review(
    proposal: GauntletAuthoredProposal, output: str | Path
) -> Path:
    path = Path(output)
    if path.exists():
        raise GauntletProposalError(f"refusing to overwrite proposal review page: {path}")
    _atomic_write_text(path, render_authored_proposal_review(proposal))
    return path


def load_authored_proposal(path: str | Path) -> GauntletAuthoredProposal:
    try:
        return GauntletAuthoredProposal.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise GauntletProposalError(f"invalid authored proposal {path}: {exc}") from exc


def approve_authored_proposal(
    proposal: GauntletAuthoredProposal,
    *,
    approved_by: str,
    exact_proposal_sha256: str,
    exact_history_sha256: str,
    exact_compiled_artifact_sha256: str,
    exact_champion_after_sha256: str,
    approved_at: datetime | None = None,
) -> GauntletProposalApproval:
    data: dict[str, Any] = {
        "approval_sha256": "0" * 64,
        "proposal": proposal,
        "approved_by": approved_by,
        "approved_at": approved_at or datetime.now(timezone.utc),
        "exact_proposal_sha256": exact_proposal_sha256,
        "exact_history_sha256": exact_history_sha256,
        "exact_compiled_artifact_sha256": exact_compiled_artifact_sha256,
        "exact_champion_after_sha256": exact_champion_after_sha256,
    }
    unhashed = GauntletProposalApproval.model_construct(**data)
    data["approval_sha256"] = unhashed.calculated_sha256()
    try:
        return GauntletProposalApproval.model_validate(data)
    except ValueError as exc:
        raise GauntletProposalError(f"proposal approval is not exact: {exc}") from exc


def write_proposal_approval(
    course_dir: str | Path,
    approval: GauntletProposalApproval,
    output: str | Path | None = None,
) -> Path:
    ws = Workspace(course_dir).require()
    path = Path(output) if output is not None else (
        ws.root / "gauntlet" / "proposals" / "approved" / f"{approval.proposal.proposal_id}.json"
    )
    if path.exists():
        raise GauntletProposalError(f"approval is immutable and already exists: {path}")
    _atomic_write_text(path, approval.model_dump_json(indent=2) + "\n")
    return path


def load_proposal_approval(path: str | Path) -> GauntletProposalApproval:
    try:
        return GauntletProposalApproval.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise GauntletProposalError(f"invalid proposal approval {path}: {exc}") from exc


def verify_approval_against_workspace(
    course_dir: str | Path,
    approval: GauntletProposalApproval,
) -> GauntletAuthoredProposal:
    """Recompute the exact proposal and reject any history/artifact/bank drift."""

    current = find_authored_proposal(course_dir, approval.proposal.proposal_id)
    if current != approval.proposal:
        raise GauntletProposalError(
            "current history/artifact/authored mapping differs from the approved proposal"
        )
    ws = Workspace(course_dir).require()
    record_path = ws.root / current.record_path
    record = load_gauntlet_record(record_path)
    if _model_sha256(record) != current.record_sha256:
        raise GauntletProposalError("immutable record hash differs from the approved proposal")
    return current


def verify_approval_record(
    course_dir: str | Path,
    approval: GauntletProposalApproval,
) -> GauntletAuthoredProposal:
    """Verify immutable evidence without requiring the baseline bundle to remain current."""

    ws = Workspace(course_dir).require()
    proposal = approval.proposal
    record_path = ws.root / proposal.record_path
    record = load_gauntlet_record(record_path)
    if _model_sha256(record) != proposal.record_sha256:
        raise GauntletProposalError("immutable record hash differs from the approved proposal")
    if _model_sha256(record.history) != proposal.history_sha256:
        raise GauntletProposalError("immutable history hash differs from the approved proposal")
    if record.compiled_artifact_sha256 != proposal.compiled_artifact_sha256:
        raise GauntletProposalError("immutable record artifact differs from the approved proposal")
    return proposal


def apply_approved_proposal_to_banks(
    course_dir: str | Path,
    approval: GauntletProposalApproval,
    *,
    allow_compiled_artifact_drift: bool = False,
) -> list[str]:
    """Apply exact approved payloads to authoritative banks, never compiled units.

    The caller must subsequently run deterministic audits and, when required,
    obtain fresh v6 Gauntlet evidence for the affected unit. It must not
    regenerate the source merely to preserve this reviewed bank edit.
    """

    proposal = (
        verify_approval_record(course_dir, approval)
        if allow_compiled_artifact_drift
        else verify_approval_against_workspace(course_dir, approval)
    )
    ws = Workspace(course_dir).require()
    source_files: list[str] = []
    grouped: dict[str, list[AuthoredItemChange]] = {}
    for change in proposal.authored_changes:
        grouped.setdefault(change.bank_path, []).append(change)
        if change.source_file not in source_files:
            source_files.append(change.source_file)

    with ws.publication_lock():
        for relative, changes in grouped.items():
            path = ws.root / relative
            if path.is_symlink() or path.parent != ws.bank_dir:
                raise GauntletProposalError(f"approved bank path is unsafe: {relative}")
            bank = ws.load_bank(path.stem)
            for change in changes:
                matches = [
                    (index, item)
                    for index, item in enumerate(bank.items)
                    if item.item_key == change.item_key
                ]
                if len(matches) != 1:
                    raise GauntletProposalError(
                        f"approved item {change.item_key!r} no longer maps uniquely in {relative}"
                    )
                index, item = matches[0]
                if index != change.bank_item_index:
                    raise GauntletProposalError(
                        f"approved bank index drifted for {change.item_key!r}"
                    )
                if canonical_sha256(item.payload) != change.bank_payload_sha256:
                    raise GauntletProposalError(
                        f"authoritative payload drifted for {change.item_key!r}"
                    )
                if item.source_hash != change.item_source_sha256:
                    raise GauntletProposalError(
                        f"item source provenance drifted for {change.item_key!r}"
                    )
                source_path = ws.sources_dir / change.source_file
                if (
                    not source_path.is_file()
                    or source_path.is_symlink()
                    or sha256_text(source_path.read_text(encoding="utf-8"))
                    != change.source_sha256
                ):
                    raise GauntletProposalError(
                        f"authoritative source drifted for {change.item_key!r}"
                    )
                updated = item.model_copy(deep=True)
                updated.payload = change.payload_after
                updated.payload_hash = payload_hash(updated.payload)
                updated.provenance = "human-edited"
                updated.pinned = True
                # source_hash is intentionally preserved: the approved edit remains
                # tied to the same authoritative source evidence.
                bank.items[index] = updated
            ws.save_bank(bank)
    return source_files


def restore_approved_proposal_banks(
    course_dir: str | Path,
    approval: GauntletProposalApproval,
) -> None:
    """Restore the exact pre-approval bank payload after a failed validation."""

    ws = Workspace(course_dir).require()
    proposal = approval.proposal
    grouped: dict[str, list[AuthoredItemChange]] = {}
    for change in proposal.authored_changes:
        grouped.setdefault(change.bank_path, []).append(change)
    with ws.publication_lock():
        for relative, changes in grouped.items():
            path = ws.root / relative
            if path.is_symlink() or path.parent != ws.bank_dir:
                raise GauntletProposalError(f"approved bank path is unsafe: {relative}")
            bank = ws.load_bank(path.stem)
            for change in changes:
                matches = [
                    (index, item)
                    for index, item in enumerate(bank.items)
                    if item.item_key == change.item_key
                ]
                if len(matches) != 1:
                    raise GauntletProposalError(
                        f"approved item {change.item_key!r} no longer maps uniquely in {relative}"
                    )
                index, item = matches[0]
                if canonical_json(item.payload) != canonical_json(change.payload_after):
                    raise GauntletProposalError(
                        f"cannot restore {change.item_key!r}: current payload is not the applied proposal"
                    )
                restored = item.model_copy(deep=True)
                restored.payload = change.payload_before
                restored.payload_hash = payload_hash(restored.payload)
                restored.provenance = change.provenance_before
                restored.pinned = change.pinned_before
                bank.items[index] = restored
            ws.save_bank(bank)


def promote_validated_authored_repair(
    course_dir: str | Path,
    approval: GauntletProposalApproval,
    *,
    amendment_path: str | Path,
    receipt_path: str | Path,
) -> AuthoredRepairPublication:
    """Promote one exact reviewed bank delta into publication state.

    This is intentionally narrower than a source rebuild.  It proves that the
    current workspace differs from the previous promoted bank state by exactly
    the approved item, verifies the human mechanic amendment and application
    receipt hashes, and re-runs the deterministic compiler before changing
    build state.  It never edits a bank, source, history, or compiled unit.
    """

    ws = Workspace(course_dir).require()
    workspace_root = ws.root.resolve()
    proposal = approval.proposal
    if approval.calculated_sha256() != approval.approval_sha256:
        raise GauntletProposalError("approval_sha256 does not match approval contents")
    if len(proposal.authored_changes) != 1:
        raise GauntletProposalError("authored repair promotion currently requires exactly one item")
    change = proposal.authored_changes[0]

    amendment_file = Path(amendment_path).resolve()
    receipt_file = Path(receipt_path).resolve()
    expected_approved = workspace_root / "gauntlet" / "proposals" / "approved"
    expected_applied = workspace_root / "gauntlet" / "proposals" / "applied"
    if amendment_file.parent != expected_approved or amendment_file.is_symlink():
        raise GauntletProposalError("mechanic amendment path is outside the approved evidence directory")
    if receipt_file.parent != expected_applied or receipt_file.is_symlink():
        raise GauntletProposalError("application receipt path is outside the applied evidence directory")
    try:
        amendment = json.loads(amendment_file.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GauntletProposalError(f"cannot load authored repair evidence: {exc}") from exc

    amendment_hash = amendment.get("amendment_sha256")
    amendment_body = dict(amendment)
    amendment_body.pop("amendment_sha256", None)
    if amendment.get("schema_version") != "techlingo-gauntlet-authored-amendment-v1":
        raise GauntletProposalError("unsupported authored amendment schema")
    if amendment_hash != hash_data(amendment_body):
        raise GauntletProposalError("amendment_sha256 does not match amendment contents")
    exact_refs = {
        "approval_sha256": approval.approval_sha256,
        "proposal_sha256": proposal.proposal_sha256,
        "history_sha256": proposal.history_sha256,
        "item_key": change.item_key,
        "bank_path": change.bank_path,
    }
    for field, expected in exact_refs.items():
        if amendment.get(field) != expected:
            raise GauntletProposalError(f"mechanic amendment {field} is not exact")
    if canonical_json(amendment.get("payload_before")) != canonical_json(change.payload_before):
        raise GauntletProposalError("mechanic amendment is not based on the approved original payload")
    expected_after = dict(change.payload_after)
    amended_after = amendment.get("payload_after")
    if canonical_json(amended_after) != canonical_json(expected_after):
        mechanic_after = dict(expected_after)
        mechanic_after["question_type"] = "single_choice"
        if (
            change.payload_before.get("question_type") != "multi_choice"
            or canonical_json(amended_after) != canonical_json(mechanic_after)
        ):
            raise GauntletProposalError(
                "mechanic amendment changes more than the approved payload or the "
                "supported multi-choice to single-choice correction"
            )
        expected_after = mechanic_after

    required_receipt = {
        "proposal_sha256": proposal.proposal_sha256,
        "approval_sha256": approval.approval_sha256,
        "history_sha256": proposal.history_sha256,
        "amendment_sha256": amendment_hash,
        "item_key": change.item_key,
        "bank_path": change.bank_path,
    }
    for field, expected in required_receipt.items():
        if receipt.get(field) != expected:
            raise GauntletProposalError(f"application receipt {field} is not exact")
    validated_artifact = receipt.get("validated_compiled_artifact_sha256")
    if not isinstance(validated_artifact, str) or len(validated_artifact) != 64:
        raise GauntletProposalError("application receipt lacks a validated artifact hash")

    with ws.publication_lock():
        state_before = ws.load_build_state()
        receipt_relative = str(receipt_file.relative_to(workspace_root))
        if any(record.receipt_path == receipt_relative for record in state_before.authored_repairs):
            raise GauntletProposalError("application receipt was already promoted")
        banks = {bank.lesson: bank for bank in ws.iter_banks()}
        current_bank_hash = banks_sha256(banks)
        target_path = (ws.root / change.bank_path).resolve()
        if target_path.parent != ws.bank_dir.resolve() or target_path.is_symlink():
            raise GauntletProposalError("approved bank path is unsafe")
        target = banks.get(target_path.stem)
        if target is None:
            raise GauntletProposalError("approved bank no longer exists")
        matches = [(index, item) for index, item in enumerate(target.items) if item.item_key == change.item_key]
        if len(matches) != 1 or matches[0][0] != change.bank_item_index:
            raise GauntletProposalError("approved item no longer maps uniquely at its reviewed index")
        index, item = matches[0]
        if canonical_json(item.payload) != canonical_json(expected_after):
            raise GauntletProposalError("current authoritative payload is not the exact amended repair")
        if item.provenance != "human-edited" or item.pinned is not True:
            raise GauntletProposalError("current repaired item is not human-edited and pinned")
        if item.source_hash != change.item_source_sha256:
            raise GauntletProposalError("current repaired item source provenance drifted")
        if receipt.get("bank_file_sha256") != sha256_text(target_path.read_text(encoding="utf-8")):
            raise GauntletProposalError("application receipt does not bind the exact bank file")

        previous_banks = {key: bank.model_copy(deep=True) for key, bank in banks.items()}
        previous_item = previous_banks[target_path.stem].items[index]
        previous_item.payload = change.payload_before
        previous_item.payload_hash = payload_hash(previous_item.payload)
        previous_item.provenance = change.provenance_before
        previous_item.pinned = change.pinned_before
        previous_bank_hash = banks_sha256(previous_banks)
        if previous_bank_hash != state_before.bank_sha256:
            raise GauntletProposalError(
                "workspace contains changes beyond the exact approved item relative to promoted state"
            )

        source_state = state_before.sources.get(change.source_file)
        if source_state is None or source_state.last_known_good is None:
            raise GauntletProposalError("authoritative source has no last-known-good publication")
        source_path = ws.sources_dir / change.source_file
        if ws.source_hash(source_path) != change.source_sha256:
            raise GauntletProposalError("authoritative source drifted after proposal approval")
        module_keys = set(source_state.last_known_good.module_keys)
        current_source_banks = {key: bank for key, bank in banks.items() if bank.module in module_keys}
        previous_source_banks = {
            key: bank for key, bank in previous_banks.items() if bank.module in module_keys
        }
        previous_source_hash = banks_sha256(previous_source_banks)
        if previous_source_hash != source_state.last_known_good.bank_sha256:
            raise GauntletProposalError("approved item is not the sole delta in its source-owned banks")

        compiled = compile_workspace(ws.root)
        if compiled.problems:
            raise GauntletProposalError(
                "deterministic validation failed: " + "; ".join(compiled.problems[:10])
            )
        artifact_hash = hash_data(compiled.tl_course)
        if artifact_hash != validated_artifact:
            raise GauntletProposalError("application receipt artifact hash does not match current course")

        record = AuthoredRepairPublication(
            receipt_path=receipt_relative,
            receipt_sha256=sha256_text(receipt_file.read_text(encoding="utf-8")),
            source_file=change.source_file,
            item_key=change.item_key,
            proposal_sha256=proposal.proposal_sha256,
            approval_sha256=approval.approval_sha256,
            amendment_sha256=amendment_hash,
            previous_bank_sha256=previous_bank_hash,
            bank_sha256=current_bank_hash,
            previous_source_bank_sha256=previous_source_hash,
            source_bank_sha256=banks_sha256(current_source_banks),
            artifact_sha256=artifact_hash,
            promoted_at=utc_now_iso(),
        )
        state_after = state_before.model_copy(deep=True)
        state_after.bank_sha256 = current_bank_hash
        state_after.sources[change.source_file].last_known_good.bank_sha256 = record.source_bank_sha256
        state_after.authored_repairs.append(record)
        ws.save_build_state(state_after)
        readiness = inspect_publication_readiness(ws.root)
        if not readiness.ok:
            ws.save_build_state(state_before)
            raise GauntletProposalError(
                "authored repair did not satisfy publication safety: " + "; ".join(readiness.blockers)
            )
        return record


__all__ = [
    "APPROVAL_SCHEMA",
    "PROPOSAL_SCHEMA",
    "QUEUE_SCHEMA",
    "AuthoredItemChange",
    "AuthoredRebuildQueueEntry",
    "GauntletAuthoredProposal",
    "GauntletProposalApproval",
    "GauntletProposalError",
    "ProposalFieldChange",
    "apply_approved_proposal_to_banks",
    "approve_authored_proposal",
    "find_authored_proposal",
    "list_authored_proposals",
    "list_authored_rebuild_queue",
    "load_authored_proposal",
    "load_proposal_approval",
    "promote_validated_authored_repair",
    "restore_approved_proposal_banks",
    "verify_approval_against_workspace",
    "verify_approval_record",
    "write_authored_proposal",
    "write_authored_proposal_review",
    "write_proposal_approval",
]
