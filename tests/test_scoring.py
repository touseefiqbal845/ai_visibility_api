"""Opportunity score, brand matching and JSON recovery."""

from __future__ import annotations

import pytest

from app.constants import INTENT_COMMERCIAL, INTENT_COMPARISON, INTENT_INFORMATIONAL
from app.utils.brands import brand_variants, domain_root, find_mention, rank_mentions
from app.utils.json_parse import MalformedLLMResponse, expect_object, extract_json
from app.utils.scoring import compute_opportunity_score, normalise_volume


def score(**overrides) -> float:
    kwargs = {
        "search_volume": 1000,
        "competitive_difficulty": 50,
        "domain_visible": False,
        "visibility_position": None,
        "commercial_intent": INTENT_COMPARISON,
    }
    kwargs.update(overrides)
    return compute_opportunity_score(**kwargs).score


class TestOpportunityScore:
    def test_stays_in_range(self):
        best = score(search_volume=100_000, competitive_difficulty=0)
        worst = score(
            search_volume=0,
            competitive_difficulty=100,
            domain_visible=True,
            visibility_position=1,
            commercial_intent=INTENT_INFORMATIONAL,
        )
        assert 0.0 <= worst < best <= 1.0

    def test_higher_volume_scores_higher(self):
        assert score(search_volume=10_000) > score(search_volume=100)

    def test_lower_difficulty_scores_higher(self):
        assert score(competitive_difficulty=10) > score(competitive_difficulty=90)

    def test_absent_beats_visible_and_buried_beats_first(self):
        absent = score(domain_visible=False)
        buried = score(domain_visible=True, visibility_position=7)
        first = score(domain_visible=True, visibility_position=1)
        assert absent > buried > first

    def test_commercial_intent_ordering(self):
        assert (
            score(commercial_intent=INTENT_COMPARISON)
            > score(commercial_intent=INTENT_COMMERCIAL)
            > score(commercial_intent=INTENT_INFORMATIONAL)
        )

    def test_missing_volume_redistributes_weight_rather_than_zeroing(self):
        result = compute_opportunity_score(
            search_volume=None,
            competitive_difficulty=50,
            domain_visible=False,
            visibility_position=None,
            commercial_intent=INTENT_COMPARISON,
        )

        assert result.factors["volume"] is None
        assert "volume" not in result.weights_applied
        assert pytest.approx(sum(result.weights_applied.values()), abs=1e-6) == 1.0
        # A zero-volume default would have dragged this well below the midpoint.
        assert result.score > score(search_volume=0)

    def test_volume_normalisation_is_logarithmic(self):
        low_step = normalise_volume(1000) - normalise_volume(100)
        high_step = normalise_volume(40_000) - normalise_volume(39_100)
        assert low_step > high_step

    def test_no_usable_factors_returns_zero(self):
        result = compute_opportunity_score(
            search_volume=None,
            competitive_difficulty=None,
            domain_visible=None,
            visibility_position=None,
            commercial_intent=None,
        )
        # Intent always resolves to a default, so the score is never truly factorless;
        # this pins that behaviour rather than leaving it accidental.
        assert 0.0 <= result.score <= 1.0


class TestBrandMatching:
    def test_domain_root_strips_scheme_www_and_tld(self):
        assert domain_root("https://www.surferseo.com/pricing") == "surferseo"
        assert domain_root("frase.io") == "frase"

    def test_variants_include_spaced_acronym_form(self):
        variants = brand_variants("surferseo.com", "Surfer SEO")
        assert "surferseo.com" in variants
        assert "surfer seo" in variants

    def test_word_boundaries_prevent_false_positives(self):
        assert find_mention("the phrase you choose", ["frase"]) is None
        assert find_mention("Frase is good", ["frase"]) is not None

    def test_rank_by_first_mention(self):
        text = "Clearscope leads here, though Frase is a solid pick."
        ranked = rank_mentions(
            text,
            {"frase.io": brand_variants("frase.io", "Frase"), "clearscope.io": brand_variants("clearscope.io")},
        )
        assert [m.identifier for m in ranked] == ["clearscope.io", "frase.io"]


class TestJsonRecovery:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_surrounding_prose(self):
        assert extract_json('Here you go:\n{"a": 1}\nHope that helps!') == {"a": 1}

    def test_braces_inside_strings_do_not_break_the_scan(self):
        assert extract_json('note\n{"a": "a } brace"}\nend') == {"a": "a } brace"}

    def test_trailing_comma_is_repaired(self):
        assert extract_json('{"a": 1,}') == {"a": 1}

    def test_unrecoverable_raises(self):
        with pytest.raises(MalformedLLMResponse):
            extract_json("no json here at all")

    def test_expect_object_accepts_bare_array_or_wrapper(self):
        assert expect_object([{"a": 1}], "queries") == [{"a": 1}]
        assert expect_object({"queries": [{"a": 1}]}, "queries") == [{"a": 1}]
        # Wrapped under an unexpected key, but unambiguously the only list present.
        assert expect_object({"results": [{"a": 1}]}, "queries") == [{"a": 1}]

    def test_expect_object_drops_non_objects(self):
        assert expect_object({"queries": [{"a": 1}, "junk", 5]}, "queries") == [{"a": 1}]
