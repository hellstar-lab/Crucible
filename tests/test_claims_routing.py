"""Stage 5 typing and checkability routing. Deterministic, no API calls."""

from __future__ import annotations

import pytest

from backend.claims.routing import ROUTES, route


@pytest.mark.parametrize("claim,kind,checkable", [
    ("Led a team of 5 engineers on the payments migration", "role_or_team", "no"),
    ("Increased checkout conversion by 12% for 50,000 customers", "outcome", "no"),
    ("Reduced p95 latency by 70% on the read path", "metric", "conditional"),
    ("Built a Redis-backed caching layer serving 12k req/s", "metric", "conditional"),
    ("Cut deploy time from 2 hours to 10 minutes", "metric", "conditional"),
    ("Maintained 40 microservices", "scale", "yes"),
    ("Worked at Northwind Payments from Mar 2021 to Present", "timeline", "weak"),
    ("Wrote integration tests for the settlement service", "artifact_exists", "yes"),
    ("Migrated a card-settlement service to Kubernetes", "technology_use", "yes"),
    ("Designed the ledger reconciliation module", "ownership", "yes"),
    ("Speaks at local meetups", "other", "no"),
])
def test_kind_and_route(claim, kind, checkable):
    r = route(claim)
    assert (r["kind"], r["checkable_from_code"]) == (kind, checkable), r


def test_every_not_checkable_route_says_why_and_points_at_no_field():
    for kind, (checkable, evidence, why) in ROUTES.items():
        if checkable == "no":
            assert evidence is None and why and why.endswith(".")
        else:
            assert evidence and why is None


def test_role_claim_is_never_routed_to_code_even_with_a_technology():
    r = route("Led a team of 5 that migrated billing to Kubernetes")
    assert r["kind"] == "role_or_team" and r["checkable_from_code"] == "no" and r["evidence_kind"] is None
    assert "technology_use" in r["secondary_kinds"]


def test_metric_and_timeline_carry_a_note():
    assert "benchmark" in route("Reduced latency by 40%")["checkability_note"]
    assert "rewritten" in route("Joined in 2021")["checkability_note"]
