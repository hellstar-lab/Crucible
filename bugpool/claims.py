"""Claim-targeted selection: plant the bug where the résumé made a claim (joins Phase 4 to Phase 5).

Input: the Phase 4 claim-verdict record, the same JSON the evidence table renders from (ClaimEvidenceReport: a header and
rows, each row with a verdict and a citation). A pool candidate is CLAIM-CITED when its module (path) is a row's cited
path. Ranking, strongest claim first:
  0 supported   1 inconclusive   2 any other verdict that cites the module (contradicted, ...)   3 uncited
and within a rank: a bug inside the cited line span before one elsewhere in the module, then claims about the work
itself (ownership, metric, other) before technology_use citations (a "Python" row cites one file among many).

If no candidate lands in a claimed module the session says so (NO_CLAIMED_MODULE) instead of silently planting
elsewhere: a result, not a failure. Planting in an uncited module then needs an explicit ``allow_uncited``.

The Shield rule holds: claim TEXT never reaches anything the participant or the stranger is shown. Candidates carry only
claim ids, kinds and verdicts; the text is looked up from the record when the session record is written, after the hunt.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

NO_CLAIMED_MODULE = "no validated bug available in a module this résumé claims."
VERDICT_RANK = {"supported": 0, "inconclusive": 1}
OTHER_CITED, UNCITED = 2, 3
_KIND_RANK = {"ownership": 0, "metric": 1, "other": 1, "role_or_team": 1, "technology_use": 2}


def load(record) -> dict:
    return json.loads(Path(record).read_text()) if isinstance(record, (str, Path)) else record


def citations(record) -> list[dict]:
    """Every row that cites a file: {claim_id, claim_kind, verdict, path, start_line, end_line, repo}. No text."""
    out = []
    for r in load(record).get("rows", []):
        c = r.get("citation") or {}
        if c.get("path"):
            out.append({"claim_id": r["claim_id"], "claim_kind": r.get("claim_kind"), "verdict": r.get("verdict"),
                        "path": c["path"], "start_line": c.get("start_line"), "end_line": c.get("end_line"), "repo": c.get("repo")})
    return out


def claim_text(record, claim_id: str) -> str | None:
    return next((r.get("claim_text") for r in load(record).get("rows", []) if r.get("claim_id") == claim_id), None)


def _in_span(cite: dict, line: int) -> bool:
    a, b = cite.get("start_line"), cite.get("end_line")
    return isinstance(a, int) and isinstance(b, int) and a <= line <= b


def target_for(path: str, line: int, cites: list[dict]) -> dict:
    """The claim targeting of one candidate: whether its module is cited, by which claims, and its rank."""
    hits = [c for c in cites if c["path"] == path]
    if not hits:
        return {"cited": False, "rank": UNCITED, "verdict": None, "claims": []}

    def key(c):
        return (VERDICT_RANK.get(c["verdict"], OTHER_CITED), not _in_span(c, line), _KIND_RANK.get(c["claim_kind"], 1))

    hits.sort(key=key)
    best = hits[0]
    return {"cited": True, "rank": key(best)[0], "verdict": best["verdict"], "span_cited": _in_span(best, line),
            "claim_id": best["claim_id"], "claim_kind": best["claim_kind"], "sort_key": list(key(best)),
            "claims": [{"claim_id": c["claim_id"], "claim_kind": c["claim_kind"], "verdict": c["verdict"],
                        "cited_lines": [c["start_line"], c["end_line"]], "span_cited": _in_span(c, line)} for c in hits]}


def annotate(pool: dict, record, label: str | None = None) -> dict:
    """A copy of ``pool`` with ``claim_target`` on every candidate and a ``claim_targeting`` summary."""
    rec = load(record)
    cites = citations(rec)
    out = dict(pool, candidates=[dict(c, claim_target=target_for(c["path"], c["line"], cites)) for c in pool.get("candidates", [])])
    analysed = (rec.get("header") or {}).get("analysed") or []
    by_rank = {r: sum(1 for c in out["candidates"] if c["claim_target"]["rank"] == r) for r in (0, 1, OTHER_CITED, UNCITED)}
    out["claim_targeting"] = {
        "record": label or (str(record) if isinstance(record, (str, Path)) else None),
        "record_head_shas": [a.get("head_sha") for a in analysed], "pool_head_sha": pool.get("head_sha"),
        "matched_by": "module path (the record's citation path == the candidate's path)",
        "cited_modules": sorted({c["path"] for c in cites}),
        "candidates_in_claimed_modules": sum(by_rank[r] for r in (0, 1, OTHER_CITED)),
        "by_rank": {"supported": by_rank[0], "inconclusive": by_rank[1], "other_cited_verdict": by_rank[OTHER_CITED],
                    "uncited": by_rank[UNCITED]},
        "of": len(out["candidates"]),
    }
    return out


def rank_key(c: dict) -> tuple:
    t = c.get("claim_target") or {}
    return tuple(t.get("sort_key") or [t.get("rank", UNCITED), True, 9])


def choose(candidates: list[dict], seed: int | None, allow_uncited: bool = False) -> tuple[dict | None, str]:
    """(candidate, how it was chosen). Seeded random WITHIN the best-ranked tier, so the strongest claim is targeted
    first and the choice inside it stays unpredictable. None when nothing is cited and uncited planting is not allowed."""
    cited = [c for c in candidates if (c.get("claim_target") or {}).get("cited")]
    pool = cited or (candidates if allow_uncited else [])
    if not pool:
        return None, NO_CLAIMED_MODULE
    best = min(rank_key(c) for c in pool)
    tier = [c for c in pool if rank_key(c) == best]
    how = "claim-targeted" if cited else "uncited (allowed explicitly): " + NO_CLAIMED_MODULE
    return random.Random(seed).choice(tier), how
