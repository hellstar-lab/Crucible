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


# --- Phase 4J J4a: job-title lines are role_or_team -----------------------------------------------------------------

@pytest.mark.parametrize("text", ["Senior Engineer, Northwind Payments", "Software Engineer at Contoso",
                                  "Backend Developer @ Fabrikam", "Staff Software Engineer - Tailspin Toys",
                                  "Engineer, Contoso Freight Jul 2018 - Feb 2021", "Data Scientist, Example Labs"])
def test_title_lines_route_to_role_or_team(text):
    r = route(text)
    assert r["kind"] == "role_or_team" and r["checkable_from_code"] == "no" and "Roles, titles" in r["why_not_checkable"]


@pytest.mark.parametrize("text", ["Built the cache client", "Built a caching layer with Redis",
                                  "Worked with data engineers at Contoso on the ingestion pipeline",
                                  "Designed the ledger reconciliation module", "Mentored two junior developers"])
def test_sentences_with_a_title_noun_are_not_title_lines(text):
    from backend.claims.routing import is_title_line
    assert not is_title_line(text)


# --- Phase 4J J3: career-scope timeline / scale are not checkable --------------------------------------------------

@pytest.mark.parametrize("text", ["Completed the degree between 2014 and 2018", "6 years of backend experience",
                                  "Maintained 300k lines of code"])
def test_career_scope_timeline_and_scale_are_not_checkable(text):
    from backend.claims.routing import apply_scope, scope_of
    rt = route(text)
    assert rt["kind"] in ("timeline", "scale")
    out = apply_scope(rt, scope_of(text, rt["kind"])[0])
    assert out["checkable_from_code"] == "no" and out["why_not_checkable"].startswith("career-scope")


def test_project_scoped_scale_claim_stays_searchable():
    from backend.claims.routing import apply_scope, scope_of
    text = "Grew the crucible repository to 40k lines"
    rt = route(text)
    scope = scope_of(text, rt["kind"])
    assert rt["kind"] == "scale" and scope == ("project", "crucible") and apply_scope(rt, scope[0])["checkable_from_code"] == "yes"
