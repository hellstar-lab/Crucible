"""THROWAWAY (claim track on the OSDBMS phase-II report). Score Crucible runs against the operator's hand-checked
ground truth. Each label is matched to extracted claims by a pattern on the claim text; a claim may match several
labels; a label no claim matches is "not extracted". Claims no label matches are listed: those are the ones the
operator could not label.

    python q7_phase2_score.py reports/creg_phase2_abhinav.json [reports/creg_phase2_kasak.json ...]
"""
import json
import re
import sys
from collections import Counter

VERDICTS = ["supported", "contradicted", "inconclusive", "no_evidence", "unverifiable"]
# (label, expected class, pattern on claim text)
TRUTH = [
    ("N1 registration stored procedures", "not_supported", r"registration stored procedure"),
    ("N2 stored procedure scripts", "not_supported", r"stored procedure script"),
    ("N3 'logs' in the MySQL schema", "not_supported", r"schema\b.*\blogs\b"),
    ("N4 adaptive feedback controller", "not_supported", r"feedback controller"),
    ("N5 monitors ... retry rates", "not_supported", r"retry rate"),
    ("S1 fixed-capacity buffer queue", "supported", r"fixed-capacity|buffer queue"),
    ("S2 worker thread pools", "supported", r"thread pool"),
    ("S3 synchronization semaphores", "supported", r"semaphore"),
    ("S4 transaction blocks, lock management", "supported", r"transaction block|lock management"),
    ("S5 seat limit and duplicate checks", "supported", r"seat limit|duplicate|overbook|seating cap"),
    ("S6 row-level locking, ACID", "supported", r"row-level locking|\bACID\b|row locks"),
    ("S7 Python and MySQL stack", "supported", r"implementation stack"),
    ("I1 automatic rollback triggers", "inconclusive", r"rollback trigger"),
    ("I2 High load test: Pending", "inconclusive", r"high load test"),
    ("I3 Load Benchmarking: Pending", "inconclusive", r"load benchmarking"),
    ("I4 concurrent request test: In progress", "inconclusive", r"concurrent request test|multi-client simulation"),
    ("U1 completed our literature review", "unverifiable", r"literature review"),
    ("U2 studying MD-ACC methodologies", "unverifiable", r"md-acc"),
]


def ok(expected, verdict):
    return {"not_supported": verdict != "supported", "supported": verdict == "supported",
            "inconclusive": verdict == "inconclusive", "unverifiable": verdict == "unverifiable"}[expected]


def score(path):
    data = json.load(open(path))
    rows = data["rows"]
    header = data.get("header") or {}
    # Stage 1 routes some text away from verdicts on purpose: list where each label went when no claim row matched
    elsewhere = [(f"non-assertion ({n['rule']})", n["text"]) for n in header.get("non_assertions") or []]
    elsewhere += [(f"excluded {e['reason_code']}", e.get("text") or "") for e in header.get("excluded_units") or []]
    elsewhere += [(f"tense guard rejected ({r['reason_code']})", r["source_text"]) for r in header.get("rejected_claims") or []]
    print(f"\n=== {path}: {len(rows)} claims; verdicts {dict(Counter(r['verdict'] for r in rows))}")
    labelled, conf = set(), Counter()
    print(f"\n{'label':<42} {'expect':<14} matched claims (verdict / content-matrix row)")
    for label, exp, pat in TRUTH:
        hits = [r for r in rows if re.search(pat, r["claim_text"], re.I)]
        labelled |= {r["claim_id"] for r in hits}
        for r in hits:
            conf[(exp, r["verdict"])] += 1
        routed = [w for w, t in elsewhere if re.search(pat, t, re.I)]
        flag = (("routed: " + "; ".join(sorted(set(routed)))) if routed else "NOT EXTRACTED") if not hits else ("pass" if all(ok(exp, r["verdict"]) for r in hits) else
                                                ("FAIL (false contradicted)" if any(r["verdict"] == "contradicted" for r in hits) else "fail"))
        print(f"{label:<42} {exp:<14} {flag}")
        for r in hits:
            print(f"{'':<44}{r['claim_id']:<9} {r['verdict']:<13} {r['matrix_row'] or '':<26} {r['claim_text'][:70]!r}")
    print("\nconfusion (claims): rows = expected, columns = verdict")
    print(f"{'':<15}" + "".join(f"{v:>14}" for v in VERDICTS))
    for exp in ("supported", "not_supported", "inconclusive", "unverifiable"):
        print(f"{exp:<15}" + "".join(f"{conf[(exp, v)]:>14}" for v in VERDICTS))
    other = [r for r in rows if r["claim_id"] not in labelled]
    print(f"\nnot in the ground truth: {len(other)} claims; verdicts {dict(Counter(r['verdict'] for r in other))}")
    for r in other:
        print(f"  {r['claim_id']:<9} {r['verdict']:<13} {r['claim_kind']:<15} {r['claim_text'][:95]!r}")
    return rows


if __name__ == "__main__":
    for p in sys.argv[1:]:
        score(p)
