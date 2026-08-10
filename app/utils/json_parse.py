"""Recovering JSON from model output.

Models mostly honour "return only JSON", but not always: you get code fences, a
sentence of preamble, or a trailing note. Rather than let a stray character fail a
whole pipeline run, we try progressively more forgiving strategies and only give up
if none of them yield valid JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

_OPENERS = {"{": "}", "[": "]"}


class MalformedLLMResponse(ValueError):
    """Raised when no valid JSON can be recovered from a model response."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        excerpt = raw[:300].replace("\n", " ")
        super().__init__(f"Could not parse JSON from model response: {excerpt!r}")


def _scan_balanced(text: str) -> str | None:
    """Return the first balanced {...} or [...] block, ignoring braces inside strings."""
    start = None
    closer = None
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if start is None:
            if char in _OPENERS:
                start = index
                closer = _OPENERS[char]
                depth = 1
            continue

        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if char == text[start]:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None


def extract_json(raw: str) -> Any:
    """Parse JSON out of a model response, tolerating fences and surrounding prose."""
    if raw is None:
        raise MalformedLLMResponse("")

    text = raw.strip()
    if not text:
        raise MalformedLLMResponse(raw)

    candidates: list[str] = [text]

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    balanced = _scan_balanced(text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue

    # Last resort: models occasionally emit a trailing comma before a closing brace.
    for candidate in candidates:
        repaired = re.sub(r",(\s*[}\]])", r"\1", candidate)
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, TypeError):
            continue

    raise MalformedLLMResponse(raw)


def expect_object(payload: Any, key: str) -> list[dict[str, Any]]:
    """Pull a list of objects out of a payload that may or may not be wrapped.

    Prompts ask for {"key": [...]} but models sometimes return the bare array, so
    both shapes are accepted and anything else raises.
    """
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        value = payload.get(key)
        if value is None:
            # Single-key wrapper under an unexpected name.
            values = [v for v in payload.values() if isinstance(v, list)]
            value = values[0] if len(values) == 1 else None
        items = value if isinstance(value, list) else None
    else:
        items = None

    if items is None:
        raise MalformedLLMResponse(json.dumps(payload)[:500])

    return [item for item in items if isinstance(item, dict)]
