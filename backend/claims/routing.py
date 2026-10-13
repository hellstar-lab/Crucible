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


# J4a: a job-title line ("Senior Engineer, Northwind Payments", "Software Engineer at Contoso") states a role. Measured:
# it routed to "other", so 4B said "no checkability rule matched" instead of the role reason.
_TITLE_NOUNS = (r"(?:engineer|developer|programmer|architect|scientist|analyst|consultant|intern|designer|administrator|"
                r"researcher|manager|director|lead|founder|co-founder|cto|ceo|specialist|technician|officer|associate|"
                r"fellow|instructor|sre|devops)")
_TITLE_LINE = re.compile(rf"(?i)^\s*(?:[\w./&+-]+\s+){{0,4}}{_TITLE_NOUNS}s?\b\s*(?:,|\bat\b|@|[-–—|])\s*\S")
_ACTION_VERB = re.compile(r"(?i)\b(built|build|implemented|wrote|written|authored|designed|developed|created|architected|refactored|"
                          r"rewrote|migrated|maintained|shipped|contributed|led|managed|mentored|reduced|improved|deployed|"
                          r"automated|added|used|worked)\b")


_PERSON_AGENT = re.compile(r"(?i)\b(i|me|we|us|he|she|they|them|someone|everyone|team|teams|engineers?|developers?|"
                           r"managers?|leads?|people|staff|students?|members?|colleagues?|contractors?|interns?|admins?|"
                           r"administrators?|users?|customers?|clients?|owners?|founders?|reviewers?|maintainers?)\b")
_BY_AGENT = re.compile(r"(?i)\s+by\s+((?:the|a|an|its|our|their|this|that)\s+[^,.;:]{1,60})")


def _person_role(claim_text: str) -> bool:
    """Stage 2 follow-up: a role word counts only when its agent is a person or a team. One condition: a role word
    followed by "by <determiner> <agent>" whose agent names no person or team ("a buffer managed by the OS layer") is
    an artifact, not a role. Every other use (no "by", or a person / team agent, or a bare name) is a role, as before."""
    rx = dict(_RX)["role_or_team"]
    for m in rx.finditer(claim_text or ""):
        agent = _BY_AGENT.match(claim_text[m.end():])
        if not agent or _PERSON_AGENT.search(agent.group(1)):
            return True
    return False


def is_title_line(text: str) -> bool:
    return bool(_TITLE_LINE.match(text or "")) and len((text or "").split()) <= 12 and not _ACTION_VERB.search(text or "")


CAREER_REASON = ("career-scope {kind} claim (education dates, employment duration, years of experience, or volume across a "
                 "career): a repository sample cannot establish or refute it, so it is not searched and no model sees it")


def apply_scope(rt: dict, scope: str) -> dict:
    """J3: a timeline or scale claim whose scope is career is not checkable from code. Decided at routing, so the
    claim never reaches retrieval or a model; 4C's history/count gate stays as defence in depth."""
    if rt["kind"] in ("timeline", "scale") and scope == "career":
        return {**rt, "checkable_from_code": "no", "evidence_kind": None,
                "why_not_checkable": CAREER_REASON.format(kind=rt["kind"]), "checkability_note": None}
    return rt


def route(claim_text: str) -> dict:
    """{kind, secondary_kinds, checkable_from_code, evidence_kind, why_not_checkable, checkability_note}."""
    if is_title_line(claim_text):
        checkable, evidence, why = ROUTES["role_or_team"]
        return {"kind": "role_or_team", "secondary_kinds": [], "checkable_from_code": checkable, "evidence_kind": evidence,
                "why_not_checkable": why, "checkability_note": None}
    kinds: list[str] = [k for k, rx in _RX if rx.search(claim_text)]
    if "role_or_team" in kinds and not _person_role(claim_text):
        kinds.remove("role_or_team")
        if not kinds:
            kinds = ["artifact_exists"]  # a non-person agent: the claim is about an artifact
    if any(_is_tech(e) for e in support.entities(claim_text)):
        kinds.append("technology_use")
    order = ["role_or_team", "outcome", "metric", "scale", "timeline", "artifact_exists", "technology_use", "ownership"]
    kinds = sorted(dict.fromkeys(kinds), key=order.index)
    if not kinds:
        concept = _concept_kind(claim_text)  # Stage 2: a named code concept is searchable in the code itself
        if concept:
            kinds = [concept]
    # ownership (a generic verb) is primary only when nothing more specific applies
    primary: ClaimKind = kinds[0] if kinds else "other"  # type: ignore[assignment]
    checkable, evidence, why = ROUTES[primary]
    return {"kind": primary, "secondary_kinds": [k for k in kinds[1:]], "checkable_from_code": checkable,
            "evidence_kind": evidence, "why_not_checkable": why, "checkability_note": NOTES.get(primary)}


_BUILT = re.compile(r"(?i)^\s*(deployed|built|implemented|wrote|designed|developed|created|added|introduced|engineered|"
                    r"set up|configured|integrated|coded|programmed|constructed)\b")


def _concept_kind(claim_text: str) -> str | None:
    """Stage 2: a claim that would route to "other" but names a code concept (thread pools, semaphores, row locks ...;
    backend/retrieval/data/code_concepts.json) is routed to ownership when it states building it ("Deployed worker
    thread pools"), else to artifact_exists ("Row locks prevent conflicts"). Both kinds run r_artifact. Measured on the
    OSDBMS phase-II report: 22 such claims were 'other', so never searched."""
    from backend.retrieval.lexicon import recognise_concepts  # lazy: retrieval's data, not a 4A dependency at import
    if not recognise_concepts(claim_text):
        return None
    return "ownership" if _BUILT.search(claim_text) else "artifact_exists"


def _is_tech(entity: str) -> bool:
    table, norm = support._lexicon()
    return norm(entity) in table or any(c == entity for c, _a in table.values())


# --------------------------------------------------------------------------
# Scope (Phase 4C F1): what the claim is ABOUT. Deterministic. Used by the verdict matrix: a contradiction requires the
# evidence and the claim to be about the same object, and Crucible sees only a partial, candidate-selected sample of
# public repositories, so a career, timeline or aggregate claim can never be contradicted by it.
# --------------------------------------------------------------------------

_FILE_TOKEN = re.compile(r"(?<![\w/])([\w.-]*/)?[\w-]+\.(py|js|jsx|ts|tsx|go|java|kt|rb|rs|c|cc|cpp|h|hpp|cs|php|swift|scala|sql|"
                         r"ya?ml|json|toml|sh|tf|proto)\b")
_NAMED_PROJECT = re.compile(r"(?i)\b(?:the\s+)?([A-Za-z0-9][\w.-]*[A-Za-z0-9])\s+(?:project|repository|repo|codebase|library|package)\b")
_LABEL_PREFIX = re.compile(r"^\s*([A-Za-z0-9][\w.-]*[A-Za-z0-9])\s*[:–—]\s")
_CAREER = re.compile(r"(?i)\b(\d+\+?\s*(?:years?|yrs?)|years? of experience|career|throughout|over the (?:last|past)|"
                     r"(?:19|20)\d{2}\s*(?:-|–|to)\s*(?:(?:19|20)\d{2}|present))\b")
_COMPONENT_NOUNS = r"(?:module|service|library|package|component|engine|layer|client|pipeline|api|system|tool|job|worker|scheduler|parser|cli)"
_COMPONENT = re.compile(rf"(?i)\b(?:the|a|an|our|its)\s+((?:[a-z][\w-]*\s+){{0,3}}){_COMPONENT_NOUNS}\b")
_PROJECT_SECTIONS = re.compile(r"(?i)project|open source|portfolio")
_GENERIC = frozenset("new main core internal custom simple small large first own".split())


def scope_of(claim_text: str, kind: str, unit_text: str | None = None, section: str | None = None) -> tuple[str, str | None]:
    """(scope, scope_target). specific_artifact > project > career > none. A timeline or scale claim is career unless it
    names a file or a project ("Grew the crucible repository to 40k lines" is project-scoped and searchable)."""
    text = claim_text or ""
    m = _FILE_TOKEN.search(text)
    if m:
        return "specific_artifact", m.group(0)
    label = _LABEL_PREFIX.match(unit_text or "")
    if label and section and _PROJECT_SECTIONS.search(section) and label.group(1).lower() in text.lower():
        return "project", label.group(1)
    m = _NAMED_PROJECT.search(text)
    if m and m.group(1).lower() not in ("the", "a", "this", "that", "open-source", "open"):
        return "project", m.group(1)
    if _CAREER.search(text) or kind in ("timeline", "scale"):
        # a timeline / scale claim that names no file or project is about a career (J3: it is then not searched)
        return "career", None
    if kind == "ownership":
        m = _COMPONENT.search(text)
        if m:
            words = [w for w in m.group(1).split() if w.lower() not in _GENERIC]
            if words:
                # The whole component phrase ("ledger reconciliation"). The contradiction gate (backend/verdicts/matrix.py)
                # checks provenance and accepts a whole-word / path-component match on ANY of its content words; it does
                # not guess which word names the object from word order.
                return "specific_artifact", " ".join(w.strip("-").lower() for w in words)
    return "none", None
