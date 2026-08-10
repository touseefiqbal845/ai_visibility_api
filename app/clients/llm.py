"""Thin wrapper around the Anthropic Messages API.

Kept deliberately small: agents own their prompts, this owns transport, retries and
token accounting. It also gives the tests one obvious seam to mock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Transport-level failure talking to the model provider."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AnthropicClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        timeout: int = 90,
        max_retries: int = 1,
    ) -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=float(timeout),
            max_retries=max_retries,
        )

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.4,
        prefill: str | None = None,
    ) -> LLMResponse:
        """Single-turn completion.

        `prefill` seeds the assistant turn. Starting it with "{" is the cheapest
        reliable way to stop a model prepending "Here is the JSON you asked for".
        """
        messages: list[dict[str, str]] = [{"role": "user", "content": user}]
        if prefill:
            messages.append({"role": "assistant", "content": prefill})

        try:
            response = self._client.messages.create(
                model=model,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

        text = "".join(block.text for block in response.content if block.type == "text")
        if prefill:
            # The prefill is not echoed back, so stitch it on to get valid JSON.
            text = prefill + text

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
