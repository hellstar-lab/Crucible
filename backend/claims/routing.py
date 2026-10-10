"""Stage 5: claim kind and checkability routing. Deterministic. Ours, not Claimify's.

Claimify assumed a web-search verifier; Crucible verifies against a repository, so every claim states whether a
repository COULD settle it, which field would, and, when nothing in a repository can, why not. That last case is
the honesty layer and a correct, valuable output: "role_or_team, not checkable from code: team size is not
knowable from a repository". Nothing here manufactures a path to checking a claim that code cannot settle.

  kind             checkable_from_code  what would settle it
  technology_use   yes                  dependency manifests, imports
  ownership        yes                  authorship.hot_files, authored_functions
  artifact_exists  yes                  repo_facts.structure (tests, CI, docker)
  scale            yes                  repo_facts.languages, line counts
  timeline         weak                 commit dates (the candidate controls them; a weak signal)
  metric           conditional          only if a benchmark or performance test exists in the repo
  role_or_team     no                   never knowable from a repository
  outcome          no                   business impact, never in code
  other            no                   no rule matched; not routed to code rather than guessed

A claim can match several kinds; ``kind`` is the first in the priority order (role_or_team, outcome, metric,
scale, timeline, artifact_exists, technology_use, ownership) and the rest are
``secondary_kinds``. Not-checkable kinds come first on purpose: "Led a team of 5 that cut latency 40%" is about
a team before it is about latency.
"""

from __future__ import annotations

import re

from backend.contracts import Checkability, ClaimKind

from . import support

ROUTES: dict[str, tuple[Checkability, str | None, str | None]] = {
    "technology_use": ("yes", "repo_facts.dependencies / imports", None),
    "ownership": ("yes", "authorship.hot_files / authored_functions", None),
    "artifact_exists": ("yes", "repo_facts.structure (tests, CI, container files)", None),
    "scale": ("yes", "repo_facts.languages / line counts", None),
    "timeline": ("weak", "commit dates (authorship history)", None),
    "metric": ("conditional", "a benchmark or performance test in repo_facts.structure", None),
    "role_or_team": ("no", None, "Roles, titles, team size and leadership are not knowable from a repository."),
    "outcome": ("no", None, "Business impact (revenue, users, cost, adoption) is never recorded in code."),
    "other": ("no", None, "No checkability rule matched this claim, so it is not routed to code rather than guessed."),
}
NOTES = {
    "timeline": "Commit dates are set by the committer and can be rewritten; at best they are consistent with the claim.",
    "metric": "Checkable only if the repository contains a benchmark or performance test that measures it; otherwise not checkable.",
}

_RX: list[tuple[str, re.Pattern]] = [
    ("role_or_team", re.compile(r"(?i)\b(led|lead|leading|managed|manager|mentored|mentoring|hired|recruited|supervised|"
                                r"team of|teams?|engineers?\b.*\breport|direct reports?|stakeholders?|cross[- ]functional|"
                                r"head of|director|cto|vp\b|tech lead|scrum master|product owner|people)\b")),
    ("outcome", re.compile(r"(?i)(\$\s?\d|\b(revenue|sales|profit|arr|mrr|customers?|clients?|users?|retention|churn|conversion|"
                           r"adoption|nps|market share|savings?|saved|cost(s)? (by|reduction)|business|growth|engagement|"
                           r"funding|valuation|award|won|ranked)\b)")),
    ("metric", re.compile(r"(?i)(\d\s?(?:k|m)?\s?(%|ms|s\b|x\b|×|rps|qps|req/s|requests?/s|tps|gb|mb|tb|hours?\b|minutes?\b|mins?\b|seconds?\b)|\b(latency|throughput|p\d{2}|uptime|"
                          r"faster|slower|speed[- ]?up|performance|response time|availability|error rate|memory usage|cpu)\b)")),
    ("scale", re.compile(r"(?i)\b\d[\d,.]*\s?(k|m)?\s*(loc|lines of code|lines|services|microservices|repositories|repos|"
                         r"endpoints|modules|tables|files|packages|libraries|servers|nodes|clusters|pipelines)\b")),
    ("timeline", re.compile(r"(?i)\b((19|20)\d{2}|present|current(ly)?|since|months?|weeks?|years?|days?|"
                            r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b")),
    ("artifact_exists", re.compile(r"(?i)\b(tests?|test suite|unit tests?|integration tests?|ci|cd|ci/cd|pipeline|github actions|"
                                   r"docker(file)?|containers?|helm|terraform modules?|migrations?|readme|documentation|docs|"
                                   r"benchmarks?|linting|pre-commit)\b")),
    ("ownership", re.compile(r"(?i)\b(built|build|implemented|wrote|written|authored|designed|developed|created|architected|"
                             r"refactored|rewrote|migrated|maintained|own(ed)?|shipped|contributed)\b")),
]


def route(claim_text: str) -> dict:
    """{kind, secondary_kinds, checkable_from_code, evidence_kind, why_not_checkable, checkability_note}."""
    kinds: list[str] = [k for k, rx in _RX if rx.search(claim_text)]
    if any(_is_tech(e) for e in support.entities(claim_text)):
        kinds.append("technology_use")
    order = ["role_or_team", "outcome", "metric", "scale", "timeline", "artifact_exists", "technology_use", "ownership"]
    kinds = sorted(dict.fromkeys(kinds), key=order.index)
    # ownership (a generic verb) is primary only when nothing more specific applies
    primary: ClaimKind = kinds[0] if kinds else "other"  # type: ignore[assignment]
    checkable, evidence, why = ROUTES[primary]
    return {"kind": primary, "secondary_kinds": [k for k in kinds[1:]], "checkable_from_code": checkable,
            "evidence_kind": evidence, "why_not_checkable": why, "checkability_note": NOTES.get(primary)}


def _is_tech(entity: str) -> bool:
    table, norm = support._lexicon()
    return norm(entity) in table or any(c == entity for c, _a in table.values())
