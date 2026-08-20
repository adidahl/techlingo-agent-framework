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
            "the source material, rubric, and approved references in this request. Score every rubric "
            "dimension separately with concrete evidence; identify critical defects and exactly one "
            "largest actionable remaining gap with item keys/paths and the narrowest repair scope. "
            "Never waive deterministic gates and do not edit the artifact. When no approved reference "
            "exists, lower confidence explicitly.\n\nREQUEST JSON:\n"
            + request.model_dump_json(indent=2)
        )
        _raw, result = await self._client().run_json_model(prompt, CriticResult)
        return CriticBackendResponse(result=result, usage=UsageRecord(backend_calls=1))


class FreshCLIEditorBackend(_FreshCLIBackend):
    def __init__(self, model_label: str, *, client_factory: ClientFactory = _default_client) -> None:
        super().__init__(model_label, name="TechLingoGauntletEditor", client_factory=client_factory)

    async def repair(self, request: TargetedEditRequest) -> TargetedEditResult:
        prompt = (
            "Act as a targeted editor. Apply only the supplied directive to the champion. Preserve every "
            "stable item key/path and all unrelated content. Item repairs may touch only allowed payload "
            "fields and affected keys; session repairs may only reorder; course repairs may touch only "
            "explicit affected keys. Report the exact fields and keys changed. Do not regenerate broadly."
            "\n\nREQUEST JSON:\n"
            + request.model_dump_json(indent=2)
        )
        _raw, result = await self._client().run_json_model(prompt, TargetedEditResult)
        return result.model_copy(update={"usage": UsageRecord(backend_calls=1)})


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
        _raw, result = await self._client().run_json_model(prompt, BlindVerdict)
        return result.model_copy(update={"usage": UsageRecord(backend_calls=1)})


__all__ = [
    "FreshCLIComparisonBackend",
    "FreshCLICriticBackend",
    "FreshCLIEditorBackend",
]
