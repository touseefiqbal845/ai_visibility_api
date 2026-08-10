"""Agent 1 - Query Discovery.

Generates the questions real buyers put to an AI assistant when they are shopping in
this category. Also classifies commercial intent, which Agent 2 would otherwise have
to re-derive with a second call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agents.base import AgentResult, BaseAgent, TokenUsage, clean_str, clean_str_list
from app.constants import INTENT_COMMERCIAL, INTENT_COMPARISON, INTENT_INFORMATIONAL
from app.utils.json_parse import expect_object

logger = logging.getLogger(__name__)

VALID_INTENTS = {INTENT_COMPARISON, INTENT_COMMERCIAL, INTENT_INFORMATIONAL}

SYSTEM_PROMPT = """\
You are a search demand analyst. You model how real buyers phrase questions to AI \
assistants (ChatGPT, Claude, Perplexity) when they are researching or choosing a \
product in a given market.

Rules:
- Write questions a buyer would actually type, not marketing copy and not keyword \
strings. Full natural-language questions, 6-16 words.
- Cover the buying journey: category discovery, head-to-head comparisons, \
alternatives to a named competitor, pricing and value, and specific use cases.
- Name real competitor brands where the question naturally calls for one. Never \
invent a brand that was not supplied to you.
- Never mention the target business by name. These are questions asked by people who \
may not know it exists; a question containing the brand name tests recall, not \
discovery.
- No duplicates and no near-duplicates that differ only by word order.

Classify each question's commercial intent:
- "comparison": weighs two or more named or implied options against each other, \
including "best X" and "alternatives to X" questions.
- "commercial": buying-adjacent but not comparative, e.g. pricing, whether a tool \
supports a workflow, is it worth it.
- "informational": explains a concept or how to do something, with no purchase \
decision attached.

Return a single JSON object and nothing else. No prose, no markdown fences.

Schema:
{
  "queries": [
    {
      "query_text": string,
      "commercial_intent": "comparison" | "commercial" | "informational",
      "reasoning": string   // one short clause on why a buyer asks this
    }
  ]
}
"""

USER_TEMPLATE = """\
Target business (do not name it in any question):
  name: {name}
  domain: {domain}
  industry: {industry}
  description: {description}

Known competitors: {competitors}

Generate exactly {count} questions.

Intent mix: roughly half "comparison", a third "commercial", the rest \
"informational". Weight the set toward questions where an AI assistant's answer \
would plausibly name specific vendors, because those are the answers this business \
can compete to appear in.
"""


@dataclass
class DiscoveredQueryDraft:
    query_text: str
    commercial_intent: str
    reasoning: str | None = None


class QueryDiscoveryAgent(BaseAgent):
    name = "query_discovery"
    system_prompt = SYSTEM_PROMPT
    max_tokens = 3000
    # Higher than the other agents: a low temperature here produces fifteen
    # rephrasings of the same two questions.
    temperature = 0.8

    def run(
        self,
        *,
        name: str,
        domain: str,
        industry: str,
        description: str | None,
        competitors: list[str],
        count: int = 15,
    ) -> AgentResult:
        usage = TokenUsage()
        warnings: list[str] = []

        prompt = USER_TEMPLATE.format(
            name=name,
            domain=domain,
            industry=industry,
            description=description or "(none provided)",
            competitors=", ".join(competitors) if competitors else "(none provided)",
            count=count,
        )

        payload = self._call_json(prompt, usage=usage)
        items = expect_object(payload, "queries")

        drafts: list[DiscoveredQueryDraft] = []
        seen: set[str] = set()

        for item in items:
            text = clean_str(item.get("query_text"), max_length=400)
            if not text:
                warnings.append("dropped a query with no usable query_text")
                continue

            # Case- and whitespace-insensitive dedupe; the model occasionally emits
            # the same question twice with different capitalisation.
            fingerprint = " ".join(text.lower().split())
            if fingerprint in seen:
                continue
            seen.add(fingerprint)

            intent = clean_str(item.get("commercial_intent"), max_length=32)
            intent = (intent or "").lower()
            if intent not in VALID_INTENTS:
                warnings.append(f"unrecognised intent {intent!r}, defaulting to informational")
                intent = INTENT_INFORMATIONAL

            drafts.append(
                DiscoveredQueryDraft(
                    query_text=text,
                    commercial_intent=intent,
                    reasoning=clean_str(item.get("reasoning"), max_length=300),
                )
            )

        if not drafts:
            warnings.append("discovery returned no usable queries")

        logger.info(
            "discovery complete",
            extra={"agent": self.name, "queries": len(drafts), "tokens": usage.total},
        )
        return AgentResult(data=drafts, usage=usage, warnings=warnings)


__all__ = ["QueryDiscoveryAgent", "DiscoveredQueryDraft", "clean_str_list"]
