from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .backends import CompletionBackend, backend_from_label, default_timeout_s
from .prompts import SYSTEM_JSON_ONLY

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """JSON-enforcing LLM wrapper over a pluggable completion backend.

    The backend (Claude Code CLI, Codex CLI) only turns one prompt
    into one raw text completion; everything that makes the pipeline robust —
    JSON extraction, schema-error feedback retries — lives here and is
    backend-agnostic.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        instructions: str = SYSTEM_JSON_ONLY,
        name: str = "TechlingoPipeline",
        backend: CompletionBackend | None = None,
        timeout_s: float | None = None,
    ) -> None:
        if backend is None:
            if not model_id:
                raise ValueError("LLMClient requires either a model_id label or a backend.")
            # model_id is a backend-qualified label ("claude-code:sonnet", "codex:o3").
            backend = backend_from_label(model_id, agent_name=name)
        self._backend = backend
        self.model_id = model_id or backend.model_label
        self._instructions = instructions
        self._timeout_s = timeout_s if timeout_s is not None else default_timeout_s()
        self._last_backend_calls = 0

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    @property
    def last_backend_calls(self) -> int:
        """Physical completion attempts made by the most recent JSON operation."""

        return self._last_backend_calls

    async def _complete(self, prompt: str, response_model: type[BaseModel] | None = None) -> str:
        self._last_backend_calls += 1
        return (
            await self._backend.complete(
                prompt,
                system=self._instructions,
                response_model=response_model,
                timeout_s=self._timeout_s,
            )
        ).strip()

    @staticmethod
    def _extract_json(text: str) -> str:
        """Best-effort cleanup of LLM output into a parseable JSON string.

        Handles the two most common ways models violate "JSON only":
        markdown code fences (```json ... ```) and surrounding prose. Falls
        back to the outermost {...} span when extra text brackets the object.
        """
        s = text.strip()
        # Strip markdown code fences if present.
        if s.startswith("```"):
            s = s[3:]
            if s[:4].lower() == "json":
                s = s[4:]
            end = s.rfind("```")
            if end != -1:
                s = s[:end]
            s = s.strip()
        # If there's leading/trailing prose, keep the outermost object span.
        if not s.startswith("{"):
            start = s.find("{")
            if start != -1:
                s = s[start:]
        end = s.rfind("}")
        if end != -1:
            s = s[: end + 1]
        return s.strip()

    async def run_json(self, prompt: str, *, max_retries: int = 2) -> dict[str, Any]:
        self._last_backend_calls = 0
        last_err: Exception | None = None
        current = prompt
        for _ in range(max_retries + 1):
            text = await self._complete(current)
            try:
                return json.loads(self._extract_json(text))
            except json.JSONDecodeError as e:
                last_err = e
                self._last_bad_output = text
                current = (
                    "Your previous output was not valid JSON.\n"
                    "Return ONLY a single JSON object, with no markdown fences or commentary.\n\n"
                    f"Original task:\n{prompt}\n\n"
                    f"JSON parse error:\n{e}"
                )
        assert last_err is not None
        raise last_err

    async def run_json_model(
        self, prompt: str, model: type[T], *, max_retries: int = 2
    ) -> tuple[dict[str, Any], T]:
        """Run, parse, and validate against `model` with maximum resilience.

        Defense in depth:
          1. **Structured output at the backend** — codex `--output-schema`
             constrains generation to the schema when the backend supports it
             (claude-code degrades gracefully to prompt-schema mode).
          2. **Repair retry** — if anything still slips through, feed the
             JSON/schema error back and ask for a correction instead of
             crashing the run.

        Returns the raw dict (callers still read fields like ``thought_process``)
        alongside the validated model.
        """
        self._last_backend_calls = 0
        last_err: Exception | None = None
        schema_json = json.dumps(model.model_json_schema(), indent=2, sort_keys=True)
        schema_prompt = (
            f"{prompt}\n\nREQUIRED OUTPUT JSON SCHEMA:\n{schema_json}\n\n"
            "Return only one JSON object that matches this schema exactly."
        )
        current = schema_prompt
        for _ in range(max_retries + 1):
            text = await self._complete(current, response_model=model)
            try:
                data = json.loads(self._extract_json(text))
            except json.JSONDecodeError as e:
                last_err = e
                self._last_bad_output = text
                current = (
                    "Your previous output was not valid JSON.\n"
                    "Return ONLY a single JSON object, with no markdown fences or commentary.\n\n"
                    f"Original task and required schema:\n{schema_prompt}\n\n"
                    f"JSON parse error:\n{e}"
                )
                continue
            try:
                return data, model.model_validate(data)
            except ValidationError as e:
                last_err = e
                self._last_bad_output = text
                current = (
                    "Your previous JSON did not match the required schema.\n"
                    "Return ONLY corrected JSON that matches the schema exactly. "
                    "Do not invent new field values for enums/discriminators; use only the allowed ones.\n\n"
                    f"Original task and required schema:\n{schema_prompt}\n\n"
                    f"Schema validation errors:\n{e}"
                )
        assert last_err is not None
        raise last_err

    async def run_and_parse(self, prompt: str, model: type[T], *, max_retries: int = 2) -> T:
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                data = await self.run_json(prompt)
                return model.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                # Retry with a simple "repair the JSON" instruction
                prompt = (
                    "Your previous output was invalid JSON or did not match the required schema.\n"
                    "Return ONLY corrected JSON that matches the required schema.\n\n"
                    f"Original task:\n{prompt}\n\n"
                    f"Error:\n{type(e).__name__}: {e}"
                )
        assert last_err is not None
        raise last_err
