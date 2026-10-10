"""A deliberately degraded ClaimEvidenceReport, for checking that the report page stays honest when the data is weak.

    python scripts/degraded_report_fixture.py [--out reports/degraded.json]

REAL: the two fixture repositories (tests/fixtures/make_git_fixtures.py), their 3.6 analysis (mocked GitHub API),
4B retrieval, the 4C verdict matrix and gates, 4D grounding and the report builder.
SCRIPTED (no model is called): the content judgements and the Shield result. The page's provenance banner says so.

Degradations present:
  * identity gap: identity_gap repo, 0% authored, one unmatched author wrote most lines;
  * authorship_confidence none: single_commit_import repo (one commit, blame credits everything to the committer);
  * a grounding failure: the identity_gap snapshot is withheld from 4D (as if evicted between retrieval and grounding),
    so the citation cannot be confirmed (c_path unavailable) and the verdict is downgraded;
  * a Shield finding: hidden text found, extraction ran on cleaned text, units removed;
  * two claims not checkable from code (role_or_team);
  * one claim whose Super AND Ultra samples disagree (inconclusive, both sample sets kept).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "tests" / "fixtures"):
    sys.path.insert(0, str(p))

import make_git_fixtures as M  # noqa: E402
import test_target as T  # noqa: E402
from backend import grounding, llm, retrieval, verdicts  # noqa: E402
from backend.claims.routing import scope_of  # noqa: E402
from backend.contracts import ClaimExtraction, ExcludedUnit, ShieldGate  # noqa: E402
from backend.evidence import cache  # noqa: E402
from backend.evidence.identity import IdentitySet  # noqa: E402
from backend.grounding.report import build_report  # noqa: E402
from backend.retrieval.sources import RepoView  # noqa: E402
from backend.verdicts import judge  # noqa: E402

CLAIMS = [("c0", "Built a caching layer with Redis", "technology_use", "yes"),
          ("c1", "Wrote the cache module", "ownership", "yes"),
          ("c2", "Built the key-value store service", "ownership", "yes"),
          ("c3", "Led a team of 6 engineers", "role_or_team", "no"),
          ("c4", "Managed hiring for the platform group", "role_or_team", "no")]
RUN_LABEL = ("Degraded fixture, not a live run: real fixture repositories, retrieval, verdict rules and grounding, but the "
             "model judgements and the Shield result are scripted and no model was called. The identity_gap snapshot was "
             "withheld from grounding on purpose.")


def _sample(label, idx, prob):
    data = {"evidence_summary": "scripted", "content_reason": f"Scripted judgement citing evidence item {idx}." if idx is not None
            else "Scripted judgement with no item cited.", "content_supported": label, "cited_item_index": idx}
    dist = {"values": {v: {"prob": prob if v == label else round((1 - prob) / 3, 4)} for v in
                       ("supported", "contradicted", "no_evidence", "inconclusive")}, "argmax": label, "robust": True}
    return {"text": json.dumps(data), "data": data, "value_distributions": {"content_supported": dist}, "greedy_disagreement": False,
            "error": None}


def build(tmp: Path) -> dict:
    cache.CACHE_DIR = tmp / "cache"
    reports, repos = [], {}
    for name, builder in (("identity_gap", M.identity_gap), ("single_commit_import", M.single_commit_import)):
        repo = tmp / name
        builder(repo)
        routes = {f"/repos/{M.LOGIN}/{name}": T.meta(M.LOGIN, name)}
        if name == "identity_gap":
            routes[f"/users/{M.LOGIN}"] = {**T.PROFILE, "email": None}
        reports.append(T.run([f"{M.LOGIN}/{name}"], routes, {f"{M.LOGIN}/{name}": repo}, use_cache=True).targets[0])
        repos[name] = repo
    ident = IdentitySet(login=M.LOGIN, user_id=M.USER_ID)
    ident.add(M.CAND[1], "profile_email")
    views = [retrieval.load_view(r, snapshot_root=cache.CACHE_DIR, checkout=repos[name], identity=ident)
             for r, name in zip(reports, ("identity_gap", "single_commit_import"))]
    claims = []
    for cid, text, kind, checkable in CLAIMS:
        scope, target = scope_of(text, kind)
        claims.append({"claim_id": cid, "text": text, "kind": kind, "checkable_from_code": checkable, "scope": scope,
                       "scope_target": target})
    sets = retrieval.retrieve_all(claims, views)
    by_id = {s.claim_id: s for s in sets}

    def idx(cid, repo, pred):
        return next(i for i, it in enumerate(by_id[cid].items) if it.repo_full_name.endswith("/" + repo) and pred(it))

    i0 = idx("c0", "identity_gap", lambda it: it.rule == "r_import")
    i1 = idx("c1", "single_commit_import", lambda it: it.path == "src/cache.py")
    i2 = next((i for i, it in enumerate(by_id["c2"].items)), None)
    script = {  # claim text -> one answer per request (a list = one answer per sample)
        "Built a caching layer with Redis": [[("supported", i0, 0.97)] * 3],
        "Wrote the cache module": [[("supported", i1, 0.95)] * 3],
        "Built the key-value store service": [[("supported", i2, 0.9), ("no_evidence", None, 0.9), ("inconclusive", None, 0.8)],
                                              [("supported", i2, 0.9), ("no_evidence", None, 0.85), ("no_evidence", None, 0.85)]],
    }

    def fake_call(task, prompt, schema=None, max_tokens=2000, temperature=0, *, system=None, thinking=None, run_tag=None,
                  json_schema=None, tier=None, n=1):
        assert task == "verify_claim" and json_schema == judge.SCHEMA
        text = next(t for t in script if t in prompt)
        per = script[text].pop(0)
        choices = [_sample(*a) for a in per]
        return {**choices[0], "cost_usd": None, "prompt_hash": None, "choices": choices}

    real, llm.call = llm.call, fake_call
    try:
        run = verdicts.verdicts_for(claims, sets, run_tag="degraded_fixture")
    finally:
        llm.call = real
    # 4D with the identity_gap snapshot withheld: the c0 citation cannot be confirmed
    gap = next(v for v in views if v.full_name.endswith("/identity_gap"))
    broken = RepoView(report=gap.report, snap=None, checkout=None, identity=ident)
    gviews = [broken if v is gap else v for v in views]
    grounded, failures = grounding.ground_all(run.verdicts, sets, gviews)

    extraction = ClaimExtraction(
        document="degraded_fixture_resume.pdf", cost_cap_usd=0.0, calls=0, cost_usd=None,
        shield=ShieldGate(applied=True, document_status="hidden_text_found", needs_review=True,
                          detectors=["hidden_text", "instruction_injection"], hidden_fragments=2, units_removed=1),
        excluded=[ExcludedUnit(unit_id="u7", source_span=(812, 940), source_section="Experience", stage="shield",
                               reason_code="shield_payload", detail="unit contained a hidden-text fragment flagged by the Shield"),
                  ExcludedUnit(unit_id="u0", source_span=(0, 58), source_section=None, stage="split", reason_code="contact_info",
                               detail="contact line (name, email, phone): not a claim")])
    rep = build_report(verdicts=grounded, evidence_sets=sets, target_reports=reports, extraction=extraction,
                       grounding_failures=failures, run=run, run_label=RUN_LABEL)
    return json.loads(rep.model_dump_json())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "degraded.json")
    a = ap.parse_args()
    logging.disable(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="crucible-degraded-") as tmp:
        data = build(Path(tmp))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(data, indent=1))
    print(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
