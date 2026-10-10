"""Typed fabrication checks (Phase 4A, replacing the failed G6 ratio threshold). Offline."""

from __future__ import annotations

import pytest

from backend.claims import support

SPAN = "Built a Redis-backed caching layer serving 12k req/s for the checkout API"
CASES = {
    "verbatim": "Built a Redis-backed caching layer serving 12k req/s",
    "paraphrase": "Implemented a cache layer backed by Redis that serves 12k req/s",
    "invented_number": "Built a Redis-backed caching layer serving 50k req/s",
    "absent_technology": "Built a Memcached-backed caching layer serving 12k req/s",
}
EXPECTED = {  # (fabricated_quantities, fabricated_entities)
    "verbatim": ([], []),
    "paraphrase": ([], []),
    "invented_number": (["50k"], []),
    "absent_technology": ([], ["Memcached"]),
}


def test_there_is_no_threshold_any_more():
    assert not hasattr(support, "SUPPORT_THRESHOLD")


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_four_fixture_cases(name):
    r = support.check(CASES[name], SPAN)
    assert (r.fabricated_quantities, r.fabricated_entities) == EXPECTED[name], (name, r)
    assert r.hard_fail == (name in ("invented_number", "absent_technology"))


@pytest.mark.parametrize("a,b", [("12k", "12,000"), ("12k", "12000"), ("1.2M", "1,200,000"), ("$3.5m", "3.5 million")])
def test_quantities_normalise(a, b):
    assert support.quantities(a)[0][1] == support.quantities(b)[0][1]


def test_paraphrased_quantity_is_not_a_fabrication():
    assert support.check("Served 12,000 req/s", SPAN).fabricated_quantities == []


def test_stemming_matches_cache_and_caching():
    assert support.stem("cache") == support.stem("caching") == support.stem("cached")
    assert "cache" not in [w.lower() for w in support.check(CASES["paraphrase"], SPAN).ignored]


def test_entity_from_context_is_allowed():
    # Stage 3 may move a referent from the heading context into the claim
    r = support.check("Built the payments ledger at Northwind Payments", "Built the payments ledger\nSenior Engineer, Northwind Payments")
    assert not r.hard_fail


def test_proper_noun_absent_from_source_fails():
    assert support.check("Built the payments ledger at Google", "Built the payments ledger").fabricated_entities == ["Google"]


def test_ambiguous_lexicon_word_in_lowercase_is_not_an_entity():
    assert support.check("Helped the team go faster", "Helped the team move faster").fabricated_entities == []


def test_ratio_is_reported_but_gates_nothing():
    r = support.check(CASES["invented_number"], SPAN)
    assert r.ratio > support.check(CASES["paraphrase"], SPAN).ratio and r.hard_fail  # high ratio, still a hard fail


def test_month_names_are_dates_not_entities():
    assert support.check("Worked from July 2018 to February 2021", "Engineer, Contoso Freight Jul 2018 - Feb 2021").fabricated_entities == []
