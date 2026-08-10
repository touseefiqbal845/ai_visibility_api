"""Agent 3 - Content Recommendation.

Takes the highest-opportunity queries where the domain is absent and proposes
content that would plausibly get it into the answer.

Queries are handed to the model as a numbered list and it maps recommendations back
by index, not by UUID. Models transcribe long identifiers wrong often enough that
UUID echoing quietly orphans recommendations; an integer index is verifiable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agents.base import AgentResult, BaseAgent, TokenUsage, clean_str, clean_str_list
from app.constants import CONTENT_TYPES, DEFAULT_CONTENT_TYPE, PRIORITIES, PRIORITY_MEDIUM
from app.utils.json_parse import expect_object

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a content strategist specialising in getting brands cited by AI assistants.

You are given questions where an AI assistant's answer names competitors but not the \
target business. Propose content the business should publish so that a future answer \
has reason to name it.

What makes a good recommendation here:
- It targets one specific question from the list, not the category in general.
- The title is one a real publication would use, specific enough that someone \
searching the topic would click it. No placeholder brackets.
- The rationale names the actual gap: which competitors currently own the answer and \
what the content gives an assistant a reason to cite. Two sentences, concrete.
- Keywords are terms that would appear in the content, drawn from how the question is \
phrased. 3-6 of them, no stuffing.
- Prefer formats assistants cite readily: direct comparisons, structured explainers \
with clear claims, and pages that answer the question outright rather than dancing \
around it.

Priority reflects effort against payoff: "high" for a high-opportunity query the \
business can credibly win, "low" where the gap is real but the content is expensive \
or the query is peripheral.

Content type must be one of: blog_post, landing_page, comparison_page, faq, \
case_study, documentation.

Return a single JSON object and nothing else. No prose, no markdown fences.

Schema:
{
  "recommendations": [
    {
      "query_index": integer,      // index from the numbered list you were given
      "content_type": string,
      "title": string,
      "rationale": string,
      "target_keywords": [string],
      "priority": "high" | "medium" | "low"
    }
  ]
}
"""

USER_TEMPLATE = """\
Business:
  name: {name}
  domain: {domain}
  industry: {industry}
  description: {description}

Queries where this domain does not currently appear in the AI answer:
{queries}

Produce {count} recommendations. Each must target a different query. Prioritise the \
queries with the highest opportunity scores, but skip any where you cannot propose \
content this specific business could credibly publish.
"""


@dataclass
class RecommendationDraft:
    query_index: int
    content_type: str
    title: str
    rationale: str
    target_keywords: list[str]
    priority: str


@dataclass
class QueryContext:
    """One row of the numbered list handed to the model."""

    query_uuid: str
    query_text: str
    opportunity_score: float | None
    competitors_visible: list[str]


def _format_queries(queries: list[QueryContext]) -> str:
    lines = []
    for index, item in enumerate(queries):
        score = f"{item.opportunity_score:.2f}" if item.opportunity_score is not None else "n/a"
        competitors = ", ".join(item.competitors_visible) or "none detected"
        lines.append(
            f"[{index}] {item.query_text}\n"
            f"      opportunity_score: {score} | currently cited: {competitors}"
        )
    return "\n".join(lines)


class ContentRecommendationAgent(BaseAgent):
    name = "content_recommendation"
    system_prompt = SYSTEM_PROMPT
    max_tokens = 3000
    temperature = 0.6

    def run(
        self,
        *,
        name: str,
        domain: str,
        industry: str,
        description: str | None,
        queries: list[QueryContext],
        count: int = 5,
    ) -> AgentResult:
        usage = TokenUsage()
        warnings: list[str] = []

        if not queries:
            return AgentResult(
                data=[],
                usage=usage,
                warnings=["no visibility gaps to recommend against"],
            )

        prompt = USER_TEMPLATE.format(
            name=name,
            domain=domain,
            industry=industry,
            description=description or "(none provided)",
            queries=_format_queries(queries),
            count=min(count, len(queries)),
        )

        payload = self._call_json(prompt, usage=usage)
        items = expect_object(payload, "recommendations")

        drafts: list[RecommendationDraft] = []
        used_indexes: set[int] = set()

        for item in items:
            draft = self._parse_item(item, query_count=len(queries), warnings=warnings)
            if draft is None:
                continue
            if draft.query_index in used_indexes:
                warnings.append(
                    f"dropped duplicate recommendation for query index {draft.query_index}"
                )
                continue
            used_indexes.add(draft.query_index)
            drafts.append(draft)

        if not drafts:
            warnings.append("recommendation agent returned no usable recommendations")

        logger.info(
            "recommendations complete",
            extra={
                "agent": self.name,
                "recommendations": len(drafts),
                "tokens": usage.total,
            },
        )
        return AgentResult(data=drafts, usage=usage, warnings=warnings)

    def _parse_item(
        self, item: dict[str, Any], *, query_count: int, warnings: list[str]
    ) -> RecommendationDraft | None:
        raw_index = item.get("query_index")
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            warnings.append(f"dropped recommendation with unusable query_index {raw_index!r}")
            return None

        if not 0 <= index < query_count:
            warnings.append(f"dropped recommendation referencing out-of-range index {index}")
            return None

        title = clean_str(item.get("title"), max_length=500)
        rationale = clean_str(item.get("rationale"), max_length=2000)
        if not title or not rationale:
            warnings.append(f"dropped recommendation for index {index}: missing title/rationale")
            return None

        content_type = (clean_str(item.get("content_type"), max_length=64) or "").lower()
        if content_type not in CONTENT_TYPES:
            warnings.append(
                f"unrecognised content_type {content_type!r} for index {index}, "
                "defaulting to blog_post"
            )
            content_type = DEFAULT_CONTENT_TYPE

        priority = (clean_str(item.get("priority"), max_length=16) or "").lower()
        if priority not in PRIORITIES:
            priority = PRIORITY_MEDIUM

        return RecommendationDraft(
            query_index=index,
            content_type=content_type,
            title=title,
            rationale=rationale,
            target_keywords=clean_str_list(item.get("target_keywords"), limit=8),
            priority=priority,
        )
