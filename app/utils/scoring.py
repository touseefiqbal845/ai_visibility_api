"""Opportunity scoring.

The score answers one question: how much is it worth to this domain to start
appearing in the AI answer for this query? Four factors feed it, each normalised to
0-1 and combined as a weighted sum. Reasoning for the weights is in the README.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.constants import INTENT_COMMERCIAL, INTENT_COMPARISON, INTENT_INFORMATIONAL

WEIGHT_VISIBILITY_GAP = 0.35
WEIGHT_VOLUME = 0.30
WEIGHT_DIFFICULTY = 0.20
WEIGHT_INTENT = 0.15

# Monthly searches at which the volume factor saturates. Keyword volume is heavily
# right-skewed, so this is a log scale: the gap between 50 and 500 searches matters
# more than the gap between 20,000 and 20,450.
VOLUME_CEILING = 50_000

INTENT_WEIGHTS = {
    INTENT_COMPARISON: 1.00,
    INTENT_COMMERCIAL: 0.70,
    INTENT_INFORMATIONAL: 0.35,
}

# Being absent is the whole point of the product, so it dominates. But a domain
# mentioned last in a ten-brand list still has real upside, hence the tiering.
GAP_ALREADY_FIRST = 0.05
GAP_TOP_THREE = 0.30
GAP_MENTIONED_LATER = 0.60
GAP_ABSENT = 1.00


@dataclass
class ScoreBreakdown:
    """Per-factor contributions, kept so a score can be explained rather than trusted."""

    score: float
    factors: dict[str, float | None] = field(default_factory=dict)
    weights_applied: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def normalise_volume(volume: int | None) -> float | None:
    if volume is None:
        return None
    if volume <= 0:
        return 0.0
    return min(1.0, math.log10(1 + volume) / math.log10(1 + VOLUME_CEILING))


def normalise_difficulty(difficulty: int | None) -> float | None:
    """Lower difficulty is a better opportunity, so this inverts the 0-100 scale."""
    if difficulty is None:
        return None
    clamped = max(0, min(100, difficulty))
    return 1.0 - (clamped / 100.0)


def visibility_gap(domain_visible: bool | None, position: int | None) -> float | None:
    if domain_visible is None:
        return None
    if not domain_visible:
        return GAP_ABSENT
    if position is None:
        return GAP_MENTIONED_LATER
    if position <= 1:
        return GAP_ALREADY_FIRST
    if position <= 3:
        return GAP_TOP_THREE
    return GAP_MENTIONED_LATER


def intent_weight(intent: str | None) -> float:
    return INTENT_WEIGHTS.get(intent or "", INTENT_WEIGHTS[INTENT_INFORMATIONAL])


def compute_opportunity_score(
    *,
    search_volume: int | None,
    competitive_difficulty: int | None,
    domain_visible: bool | None,
    visibility_position: int | None,
    commercial_intent: str | None,
) -> ScoreBreakdown:
    """Weighted sum of the four factors, renormalised when inputs are missing.

    Missing inputs are dropped rather than defaulted. Substituting a made-up volume
    would quietly pull every affected query toward the same score; redistributing the
    weight keeps the remaining signal honest and comparable.
    """
    factors: dict[str, float | None] = {
        "visibility_gap": visibility_gap(domain_visible, visibility_position),
        "volume": normalise_volume(search_volume),
        "difficulty": normalise_difficulty(competitive_difficulty),
        "intent": intent_weight(commercial_intent),
    }
    base_weights = {
        "visibility_gap": WEIGHT_VISIBILITY_GAP,
        "volume": WEIGHT_VOLUME,
        "difficulty": WEIGHT_DIFFICULTY,
        "intent": WEIGHT_INTENT,
    }

    available = {k: w for k, w in base_weights.items() if factors[k] is not None}
    notes: list[str] = []

    if not available:
        return ScoreBreakdown(score=0.0, factors=factors, notes=["no usable factors"])

    total_weight = sum(available.values())
    weights_applied = {k: w / total_weight for k, w in available.items()}

    missing = sorted(set(base_weights) - set(available))
    if missing:
        notes.append(f"weights redistributed; missing factors: {', '.join(missing)}")

    score = sum(weights_applied[k] * float(factors[k]) for k in available)

    return ScoreBreakdown(
        score=round(min(1.0, max(0.0, score)), 4),
        factors=factors,
        weights_applied={k: round(v, 4) for k, v in weights_applied.items()},
        notes=notes,
    )
