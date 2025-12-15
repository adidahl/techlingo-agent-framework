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

    async def run_json(self, prompt: str) -> dict[str, Any]:
        result = await self._agent.run(prompt)
        # Agent Framework returns a rich response; str() typically yields text content.
        text = str(result).strip()
        return json.loads(text)

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


