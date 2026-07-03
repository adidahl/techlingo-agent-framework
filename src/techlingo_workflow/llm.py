from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from agent_framework import ChatAgent
from agent_framework.openai import OpenAIChatClient

from .prompts import SYSTEM_JSON_ONLY

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """Thin wrapper around Microsoft Agent Framework OpenAIChatClient with JSON enforcement."""

    def __init__(
        self,
        *,
        model_id: str,
        instructions: str = SYSTEM_JSON_ONLY,
        name: str = "TechlingoPipeline",
    ) -> None:
        self._agent = ChatAgent(
            chat_client=OpenAIChatClient(model_id=model_id),
            name=name,
            instructions=instructions,
        )

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
        last_err: Exception | None = None
        current = prompt
        for _ in range(max_retries + 1):
            result = await self._agent.run(current)
            # Agent Framework returns a rich response; str() typically yields text content.
            text = str(result).strip()
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
          1. **Structured Outputs** — pass ``response_format=model`` so OpenAI
             constrains generation to the schema; invalid JSON and invented
             enum/discriminator values become impossible at the API level.
          2. **Repair retry** — if anything still slips through (or the schema is
             rejected and we fall back to prompt-only mode), feed the JSON/schema
             error back and ask for a correction instead of crashing the run.

        Returns the raw dict (callers still read fields like ``thought_process``)
        alongside the validated model.
        """
        last_err: Exception | None = None
        current = prompt
        use_structured = True
        attempts = 0
        while attempts <= max_retries:
            try:
                if use_structured:
                    result = await self._agent.run(current, response_format=model)
                else:
                    result = await self._agent.run(current)
            except Exception as e:  # noqa: BLE001 - schema may be rejected by the framework/API
                # Structured Outputs unsupported for this schema/model: drop to
                # prompt-only mode once (layer 2 takes over) without burning a retry.
                if use_structured:
                    use_structured = False
                    last_err = e
                    continue
                raise
            attempts += 1
            text = str(result).strip()
            try:
                data = json.loads(self._extract_json(text))
            except json.JSONDecodeError as e:
                last_err = e
                self._last_bad_output = text
                current = (
                    "Your previous output was not valid JSON.\n"
                    "Return ONLY a single JSON object, with no markdown fences or commentary.\n\n"
                    f"Original task:\n{prompt}\n\n"
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
                    f"Original task:\n{prompt}\n\n"
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


