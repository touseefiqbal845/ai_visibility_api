"""Shared plumbing for the three agents.

Each agent owns its prompts and its output validation. What they share is the
call-parse-retry loop: ask for JSON, try to recover JSON from whatever comes back,
and on failure give the model one corrective turn before giving up.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.clients.llm import AnthropicClient, LLMError, LLMResponse
from app.constants import LLM_STATUS_ERROR, LLM_STATUS_SUCCESS, LLM_STATUS_UNPARSEABLE
from app.utils.json_parse import MalformedLLMResponse, extract_json
from app.utils.run_audit import record_llm_call

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, response: LLMResponse) -> None:
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.calls += 1

    def merge(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class AgentResult:
    """What every agent returns: the payload, the tokens it cost, what went wrong."""

    data: Any = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    warnings: list[str] = field(default_factory=list)


class AgentError(RuntimeError):
    """An agent could not produce usable output."""


class BaseAgent:
    name: str = "agent"
    model: str = ""
    max_tokens: int = 2000
    temperature: float = 0.4
    system_prompt: str = ""

    def __init__(self, llm: AnthropicClient, model: str) -> None:
        self.llm = llm
        self.model = model

    def _call_json(
        self,
        user_prompt: str,
        *,
        usage: TokenUsage,
        prefill: str = "{",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Call the model and return parsed JSON, retrying once on unparseable output.

        The retry sends the offending text back with a narrow instruction rather than
        simply re-rolling the same prompt: a model that drifted into prose usually
        stays drifted, but corrects reliably when shown the failure.
        """
        attempts: list[dict[str, str]] = [{"role": "user", "content": user_prompt}]

        raw = ""
        for attempt in range(2):
            prompt = attempts[-1]["content"]
            started = time.perf_counter()
            try:
                response = self.llm.complete(
                    model=self.model,
                    system=self.system_prompt,
                    user=prompt,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=self.temperature if temperature is None else temperature,
                    prefill=prefill,
                )
            except LLMError as exc:
                record_llm_call(
                    agent=self.name,
                    model=self.model,
                    attempt=attempt + 1,
                    status=LLM_STATUS_ERROR,
                    started=started,
                    error=str(exc),
                )
                raise AgentError(f"{self.name}: {exc}") from exc

            usage.add(response)
            raw = response.text

            try:
                parsed = extract_json(raw)
            except MalformedLLMResponse:
                # The first bad body is a retry, not a failure. The second one is.
                gave_up = attempt == 1
                record_llm_call(
                    agent=self.name,
                    model=self.model,
                    attempt=attempt + 1,
                    status=LLM_STATUS_ERROR if gave_up else LLM_STATUS_UNPARSEABLE,
                    started=started,
                    response=response,
                    error="response was not valid JSON",
                )
                if gave_up:
                    break
                logger.warning(
                    "agent returned unparseable json, retrying",
                    extra={"agent": self.name, "excerpt": raw[:200]},
                )
                attempts.append(
                    {
                        "role": "user",
                        "content": (
                            f"{user_prompt}\n\n"
                            "Your previous response could not be parsed as JSON. "
                            "It began:\n"
                            f"{raw[:400]}\n\n"
                            "Return the corrected response as a single valid JSON value "
                            "and nothing else. No prose, no markdown fences, no trailing "
                            "commentary."
                        ),
                    }
                )
                continue

            record_llm_call(
                agent=self.name,
                model=self.model,
                attempt=attempt + 1,
                status=LLM_STATUS_SUCCESS,
                started=started,
                response=response,
            )
            return parsed

        raise AgentError(f"{self.name}: model did not return parseable JSON")


def clean_str(value: Any, *, max_length: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:max_length]


def clean_str_list(value: Any, *, limit: int = 10, max_length: int = 120) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = clean_str(item, max_length=max_length)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out
