"""Detecting brand mentions in an AI-generated answer.

This is deliberately deterministic rather than another LLM call. Asking a model
"did you mention this domain?" is asking it to introspect, and it will confidently
say yes about a brand it never wrote. String matching over the actual answer text
is both cheaper and something we can unit test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TLD_RE = re.compile(
    r"\.(com|io|ai|co|net|org|app|dev|so|xyz|tech|cloud|software)(\.[a-z]{2})?$",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Mention:
    identifier: str
    position_in_text: int


def domain_root(domain: str) -> str:
    """surferseo.com -> surferseo, www.frase.io -> frase."""
    cleaned = domain.strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = re.sub(r"^www\.", "", cleaned)
    cleaned = cleaned.split("/")[0]
    return _TLD_RE.sub("", cleaned)


def brand_variants(domain: str, name: str | None = None) -> list[str]:
    """Surface forms a model might use for one brand.

    "surferseo.com" is written as Surfer SEO, SurferSEO or surferseo.com depending on
    the sentence, so matching only the bare domain misses most real mentions.
    """
    variants: set[str] = set()

    cleaned_domain = re.sub(r"^https?://(www\.)?", "", domain.strip().lower()).split("/")[0]
    if cleaned_domain:
        variants.add(cleaned_domain)

    root = domain_root(domain)
    if root:
        variants.add(root)
        # surferseo -> "surfer seo": split a trailing acronym off the stem so the
        # spaced form of the brand also matches.
        spaced = re.sub(r"([a-z]{4,}?)(seo|ai|crm|hq|labs|app)$", r"\1 \2", root)
        if spaced != root:
            variants.add(spaced)

    if name:
        lowered = name.strip().lower()
        if lowered:
            variants.add(lowered)
            collapsed = _NON_ALNUM.sub("", lowered)
            if collapsed:
                variants.add(collapsed)

    # One-character or two-character variants match everything; drop them.
    return sorted((v for v in variants if len(v) >= 3), key=len, reverse=True)


def find_mention(text: str, variants: list[str]) -> int | None:
    """Index of the earliest match of any variant, or None.

    Matching is bounded by non-alphanumerics so "frase" does not fire on "phrase"
    and "ai" does not fire on "said".
    """
    if not text or not variants:
        return None

    lowered = text.lower()
    earliest: int | None = None

    for variant in variants:
        pattern = re.compile(
            r"(?<![a-z0-9])" + re.escape(variant).replace(r"\ ", r"[\s\-]*") + r"(?![a-z0-9])",
            re.IGNORECASE,
        )
        match = pattern.search(lowered)
        if match and (earliest is None or match.start() < earliest):
            earliest = match.start()

    return earliest


def rank_mentions(text: str, brands: dict[str, list[str]]) -> list[Mention]:
    """Order the brands that appear in `text` by where they first appear.

    Order of mention is the closest usable proxy for prominence in a prose answer:
    assistants tend to lead with their strongest recommendation.
    """
    found = [
        Mention(identifier=identifier, position_in_text=index)
        for identifier, variants in brands.items()
        if (index := find_mention(text, variants)) is not None
    ]
    return sorted(found, key=lambda m: m.position_in_text)
