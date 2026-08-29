"""Subscription-CLI adapters for the structured qualitative Gauntlet.

Each method constructs a new :class:`LLMClient`, so critic/comparator calls are
fresh isolated invocations even when the configured model happens to match the
builder.  Tests inject a client factory; no live call is required by the suite.
"""

from __future__ import annotations

from typing import Callable

from .backends import split_backend_label
from .gauntlet_models import (
    BlindComparisonRequest,
    BlindVerdict,
    CriticBackendResponse,
    CriticRequest,
    CriticResult,
    TargetedEditRequest,
    TargetedEditResult,
    UsageRecord,
)
from .llm import LLMClient

_SYSTEM = (
    "Return only JSON matching the requested schema. Evaluate observable artifact evidence only. "
    "Do not reveal or store private chain-of-thought; provide concise decisions and evidence."
)

ClientFactory = Callable[[str, str], LLMClient]


def _default_client(model_label: str, name: str) -> LLMClient:
    return LLMClient(model_id=model_label, instructions=_SYSTEM, name=name)


class _FreshCLIBackend:
    fresh_context = True

    def __init__(
        self,
        model_label: str,
        *,
        name: str,
        client_factory: ClientFactory = _default_client,
    ) -> None:
        if not model_label.strip():
            raise ValueError("model_label must be non-empty")
        backend_name, _model = split_backend_label(model_label)
        self.model_label = model_label
        # Protocol identity must be the configured backend id ("codex" or
        # "claude-code").  The descriptive role name is only for the fresh
        # client invocation and must not leak into GauntletConfig matching.
        self.name = backend_name
        self._client_name = name
        self._client_factory = client_factory

    def _client(self) -> LLMClient:
        return self._client_factory(self.model_label, self._client_name)


class FreshCLICriticBackend(_FreshCLIBackend):
    def __init__(self, model_label: str, *, client_factory: ClientFactory = _default_client) -> None:
        super().__init__(model_label, name="TechLingoGauntletCritic", client_factory=client_factory)

    async def evaluate(self, request: CriticRequest) -> CriticBackendResponse:
        prompt = (
            "Act as an independent pedagogical critic. Inspect the exact ordered learner artifact, "
            "the authoritative source material, rubric, and approved reference exemplars in this "
            "request. The supplied source material is authoritative. Approved references are optional "
            "pedagogical exemplars, not factual authorities. Their absence may modestly reduce numeric "
            "confidence but MUST NOT by itself trigger human review. Score every rubric "
            "dimension separately with concrete evidence. If any threshold fails and repair is safe, "
            "identify exactly one largest actionable remaining gap with item keys/paths and the "
            "narrowest repair scope. "
            "A below-threshold or critical defect is repairable when a safe source-bounded directive "
            "can resolve it. For repairable results set human_review_recommended=false, set "
            "human_review_reason=null, and supply largest_gap. Set human_review_recommended=true and "
            "choose a human_review_reason only when automatic repair is genuinely unsafe because "
            "authoritative evidence is insufficient or conflicting, answer ambiguity cannot be "
            "resolved from the source, no bounded repair can preserve deterministic constraints, or "
            "external subject-matter expertise is required; in that case set largest_gap=null. Never "
            "waive deterministic gates and do not edit the artifact. CITATION CONTRACT: every value "
            "in item_keys, affected_item_keys, paths, or affected_paths must be copied literally from "
            "an actual_final_artifact.items entry's item_key or path field. Never put JSON navigation "
            "expressions (such as actual_final_artifact.items[0].payload), array indexes, payload field "
            "paths, or source filenames in those arrays. Put source citations in the evidence statement "
            "text; for artifact-wide or source-only evidence, leave item_keys and paths empty.\n\n"
            "REPAIR SCOPE CONTRACT: session scope may ONLY reorder the existing item objects and must "
            "set allowed_payload_fields=[]; never use session scope for wording or payload changes. "
            "Use item scope for a bounded payload change to one or more explicit affected_item_keys, "
            "with the exact allowed top-level payload fields. Use course scope only for a coordinated "
            "cross-item repair that cannot be represented as a narrow item repair. The automatic editor "
            "MUST preserve question_type exactly. If the remaining defect can only be fixed by adding, "
            "removing, replacing, or changing the mechanic/question_type of an item, set "
            "human_review_recommended=true, human_review_reason=unsafe_or_unbounded_repair, and "
            "largest_gap=null because that requires the authored rebuild workflow.\n\n"
            "REQUEST JSON:\n"
            + request.model_dump_json(indent=2)
        )
        client = self._client()
        _raw, result = await client.run_json_model(prompt, CriticResult)
        return CriticBackendResponse(
            result=result,
            usage=UsageRecord(backend_calls=max(getattr(client, "last_backend_calls", 1), 1)),
        )


class FreshCLIEditorBackend(_FreshCLIBackend):
    def __init__(self, model_label: str, *, client_factory: ClientFactory = _default_client) -> None:
        super().__init__(model_label, name="TechLingoGauntletEditor", client_factory=client_factory)

    async def repair(self, request: TargetedEditRequest) -> TargetedEditResult:
        prompt = (
            "Act as a targeted editor. Apply only the supplied directive to the champion. Preserve every "
            "stable item key/path and all unrelated content. Item repairs may touch only allowed payload "
            "fields and affected keys; session repairs may only reorder; course repairs may touch only "
            "explicit affected keys. Report the exact fields and keys changed. For a session repair, "
            "copy every existing item object byte-for-byte, change only their order, set "
            "touched_item_keys=[], touched_payload_fields={}, and order_changed=true. For item/course "
            "repairs, do not reorder; touched_item_keys must equal exactly the keys whose payload changed, "
            "and touched_payload_fields must map each such key to exactly the changed top-level payload "
            "field names. Validation feedback from a prior attempt is authoritative and must be corrected. "
            "Never change question_type or attempt to add, remove, or replace an item; those changes "
            "require the authored rebuild workflow. "
            "Do not regenerate broadly."
            "\n\nREQUEST JSON:\n"
            + request.model_dump_json(indent=2)
        )
        client = self._client()
        _raw, result = await client.run_json_model(prompt, TargetedEditResult)
        return result.model_copy(
            update={
                "usage": UsageRecord(
                    backend_calls=max(getattr(client, "last_backend_calls", 1), 1)
                )
            }
        )


class FreshCLIComparisonBackend(_FreshCLIBackend):
    def __init__(self, model_label: str, *, client_factory: ClientFactory = _default_client) -> None:
        super().__init__(model_label, name="TechLingoGauntletComparator", client_factory=client_factory)

    async def compare(self, request: BlindComparisonRequest) -> BlindVerdict:
        prompt = (
            "Compare candidates A and B blindly against the rubric, source, and approved reference "
            "standard. Score every dimension for both candidates, choose A/B/tie, give concise evidence, "
            "confidence, and margin. Do not infer which is incumbent and do not favor position."
            "\n\nREQUEST JSON:\n"
            + request.model_dump_json(indent=2)
        )
        client = self._client()
        _raw, result = await client.run_json_model(prompt, BlindVerdict)
        return result.model_copy(
            update={
                "usage": UsageRecord(
                    backend_calls=max(getattr(client, "last_backend_calls", 1), 1)
                )
            }
        )


__all__ = [
    "FreshCLIComparisonBackend",
    "FreshCLICriticBackend",
    "FreshCLIEditorBackend",
]
