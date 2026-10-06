"""Agent 2 - Visibility Scoring.

The visibility check is done by asking the model the query the way a user would,
then looking for the target domain in the answer it actually produced. The
alternative - asking a model to predict whether a domain would appear - collapses
into a vibes check; this way the answer text is evidence we can store, diff on a
re-check, and test against.

Volume and difficulty are not estimated at all. They come from DataForSEO.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.agents.base import AgentResult, BaseAgent, TokenUsage
from app.clients.dataforseo import KeywordMetrics
from app.clients.llm import LLMError
from app.constants import LLM_STATUS_ERROR, LLM_STATUS_SUCCESS, VISIBILITY_NOT_VISIBLE, VISIBILITY_VISIBLE
from app.utils.brands import brand_variants, rank_mentions
from app.utils.run_audit import record_llm_call
from app.utils.scoring import ScoreBreakdown, compute_opportunity_score

logger = logging.getLogger(__name__)

ANSWER_EXCERPT_LIMIT = 4000

# No JSON here on purpose: we want the model behaving like an assistant answering a
# user, not like a component of our pipeline. Anything that primes it about the
# target domain would contaminate the measurement.
SYSTEM_PROMPT = """\
You are a helpful AI assistant answering a user's question about software products.

Answer as you normally would: recommend specific real products or services by name \
where the question calls for them, and briefly say what each is good for. Name the \
tools you would genuinely recommend, in the order you would recommend them. If the \
question does not call for product names, answer it without forcing any in.

Keep the answer under 250 words. Plain prose or a short list. Do not ask the user \
follow-up questions.
"""


@dataclass
class VisibilityResult:
    query_text: str
    domain_visible: bool
    visibility_position: int | None
    competitors_visible: list[str]
    answer_excerpt: str
    search_volume: int | None
    competitive_difficulty: int | None
    opportunity_score: float
    breakdown: ScoreBreakdown

    @property
    def visibility_status(self) -> str:
        return VISIBILITY_VISIBLE if self.domain_visible else VISIBILITY_NOT_VISIBLE


class VisibilityScoringAgent(BaseAgent):
    name = "visibility_scoring"
    system_prompt = SYSTEM_PROMPT
    max_tokens = 700
    # Near-greedy: we want the answer this query typically gets, not a creative one.
    temperature = 0.2

    def score(
        self,
        *,
        query_text: str,
        commercial_intent: str,
        target_domain: str,
        target_name: str,
        competitors: list[str],
        metrics: KeywordMetrics | None,
    ) -> AgentResult:
        usage = TokenUsage()
        warnings: list[str] = []

        # LLMError propagates: the orchestrator records it against this one query and
        # carries on with the rest of the batch. The call itself is audited either way.
        started = time.perf_counter()
        try:
            response = self.llm.complete(
                model=self.model,
                system=self.system_prompt,
                user=query_text,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        except LLMError as exc:
            record_llm_call(
                agent=self.name,
                model=self.model,
                attempt=1,
                status=LLM_STATUS_ERROR,
                started=started,
                error=str(exc),
            )
            raise
        record_llm_call(
            agent=self.name,
            model=self.model,
            attempt=1,
            status=LLM_STATUS_SUCCESS,
            started=started,
            response=response,
        )
        usage.add(response)

        answer = response.text or ""

        brands = {target_domain: brand_variants(target_domain, target_name)}
        for competitor in competitors:
            brands[competitor] = brand_variants(competitor)

        ranked = rank_mentions(answer, brands)
        ordered_ids = [mention.identifier for mention in ranked]

        domain_visible = target_domain in ordered_ids
        position = ordered_ids.index(target_domain) + 1 if domain_visible else None
        competitors_visible = [i for i in ordered_ids if i != target_domain]

        if not answer.strip():
            warnings.append("model returned an empty answer")

        search_volume = metrics.search_volume if metrics else None
        difficulty = metrics.difficulty if metrics else None
        if metrics is None:
            warnings.append("no keyword metrics available for this query")
        elif search_volume is None:
            warnings.append("provider returned no search volume for this query")

        breakdown = compute_opportunity_score(
            search_volume=search_volume,
            competitive_difficulty=difficulty,
            domain_visible=domain_visible,
            visibility_position=position,
            commercial_intent=commercial_intent,
        )

        result = VisibilityResult(
            query_text=query_text,
            domain_visible=domain_visible,
            visibility_position=position,
            competitors_visible=competitors_visible,
            answer_excerpt=answer[:ANSWER_EXCERPT_LIMIT],
            search_volume=search_volume,
            competitive_difficulty=difficulty,
            opportunity_score=breakdown.score,
            breakdown=breakdown,
        )

        logger.info(
            "query scored",
            extra={
                "agent": self.name,
                "query": query_text[:80],
                "domain_visible": domain_visible,
                "position": position,
                "score": breakdown.score,
            },
        )
        return AgentResult(data=result, usage=usage, warnings=warnings)
