"""Run the frozen claim-extraction eval set (tests/eval/extraction_set.jsonl).

    python scripts/eval_extraction.py --tiers nano                 # Stage 4 on nano
    python scripts/eval_extraction.py --tiers nano,super,ultra     # Stage 4 tier comparison (A3)
    python scripts/eval_extraction.py --tiers nano --json out.json
    python scripts/eval_extraction.py --compare-stage4 nano,super,ultra --runs 3   # S7 protocol; needs a FROZEN set

Tier comparison protocol (S7): Stage 2 (nano) and Stage 3 (config.STAGE3_TIER, ultra) run ONCE and are cached to disk
(tests/eval/stage23_cache.json); every Stage 4 arm replays that cache byte-identically, so only Stage 4's tier
varies. Three runs per arm. It refuses to run unless tests/eval/FROZEN.sha256 holds the sha256 of the current
extraction_set.jsonl: the reviewer writes that file when the labels are final.

Only LABELLED rows run (every expected_* field set). Unlabelled rows are listed and skipped; there is no flag to
run them. Labels belong to the reviewer; no prompt is tuned against this set until the reviewer declares it frozen.

The platform is NOT deterministic at temperature 0 (measured: byte-identical prompts, 5 runs, 3 distinct selection
replies and 5 distinct decomposition replies), so every row runs --runs times (default 5) and is reported as
"k of n passed", never as a single verdict and never only as a mean; when a row is not unanimous its distinct
outputs are printed. Per run: units, exclusions (unit + stage + reason_code) and claims; aggregates are pooled
precision and recall on claims and on exclusions, plus counts of unanimous-pass, unanimous-fail and split rows. A produced claim MATCHES an expected claim when, in both directions, it carries no quantity or
entity the other lacks (backend.claims.support), it drops no condition attached to a quantity
(pipeline.dropped_qualifiers), and the stemmed token-set overlap is at least MATCH_RATIO. Kinds are compared on
matched claims. Live model calls: costs are reported.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.claims import config, pipeline, support  # noqa: E402

SET = ROOT / "tests" / "eval" / "extraction_set.jsonl"
FROZEN = ROOT / "tests" / "eval" / "FROZEN.sha256"
CACHE = ROOT / "tests" / "eval" / "stage23_cache.json"
MATCH_RATIO = 0.75
EXPECTED = ("expected_units", "expected_excluded", "expected_claims", "expected_kinds")


def labelled(row: dict) -> bool:
    return all(row.get(k) is not None for k in EXPECTED)


def claims_match(produced: str, expected: str) -> bool:
    a, b = support.check(produced, expected), support.check(expected, produced)
    if a.hard_fail or b.hard_fail:
        return False
    if pipeline.dropped_qualifiers(produced, [], expected):
        return False
    return min(support.claim_support_ratio(produced, expected), support.claim_support_ratio(expected, produced)) >= MATCH_RATIO


def score_row(row: dict, out) -> dict:
    norm = lambda s: " ".join(s.split())  # noqa: E731
    unit_text = {u.unit_id: u.text for u in out.units or []}
    got_units = [u.text for u in out.units or []]
    got_excl = sorted((norm(unit_text.get(e.unit_id, "")), e.stage, e.reason_code) for e in out.excluded or [])
    want_excl = sorted((norm(e["unit"]), e["stage"], e["reason_code"]) for e in row["expected_excluded"])
    produced = [(c.text, c.kind) for c in out.claims or []]
    used, matched = set(), []
    for exp, kind in zip(row["expected_claims"], row["expected_kinds"]):
        hit = next((i for i, (p, _k) in enumerate(produced) if i not in used and claims_match(p, exp)), None)
        if hit is not None:
            used.add(hit)
            matched.append((exp, produced[hit][0], kind, produced[hit][1]))
    failures = []
    if [norm(u) for u in got_units] != [norm(u) for u in row["expected_units"]]:
        failures.append(f"units: got {got_units}, want {row['expected_units']}")
    if got_excl != want_excl:
        failures.append(f"excluded: got {got_excl}, want {want_excl}")
    if len(matched) != len(row["expected_claims"]) or len(used) != len(produced):
        failures.append(f"claims: got {[p for p, _k in produced]}, want {row['expected_claims']}")
    for exp, got, kind, got_kind in matched:
        if kind != got_kind:
            failures.append(f"kind of {got!r}: got {got_kind}, want {kind}")
    excl_tp = len(set(got_excl) & set(want_excl))
    return {"id": row["id"], "pass": not failures, "failures": failures, "claims_produced": len(produced), "claims_expected": len(row["expected_claims"]),
            "claims_matched": len(matched), "excl_produced": len(got_excl), "excl_expected": len(want_excl), "excl_tp": excl_tp,
            "produced": [p for p, _k in produced], "excluded": got_excl,
            "decisions": [[d.model_dump() for d in u.decisions] for u in out.units or []], "calls": out.calls, "cost_usd": out.cost_usd,
            "units_detail": [{"text": u.text, "selected_text": u.selected_text, "dropped_text": u.dropped_text, "clarified_text": u.clarified_text}
                             for u in out.units or []],
            "excluded_detail": [e.model_dump() for e in out.excluded or []],
            "counters": out.counters, "key_order_follows_schema_rate": out.key_order_follows_schema_rate}


def frozen_ok() -> tuple[bool, str]:
    if not FROZEN.exists():
        return False, f"{FROZEN.relative_to(ROOT)} does not exist: the eval set is not frozen"
    want = FROZEN.read_text().split()[0] if FROZEN.read_text().split() else ""
    got = hashlib.sha256(SET.read_bytes()).hexdigest()
    return (got == want), ("frozen" if got == want else f"extraction_set.jsonl changed since it was frozen ({got[:12]} != {want[:12]})")


def compare_stage4(rows: list[dict], tiers: list[str], runs: int) -> dict:
    """S7: record Stages 2-3 once (or reuse the cache), then replay into each Stage 4 arm, `runs` times."""
    if CACHE.exists():
        cache_entries = pipeline.StageCache.load(CACHE).entries
        print(f"replaying {CACHE.relative_to(ROOT)} ({len(cache_entries)} entries)")
    else:
        rec = pipeline.StageCache("record")
        for row in rows:
            ctx = row.get("context") or {}
            pipeline.extract_from_text(row["input_text"], headings=ctx.get("headings"), before=ctx.get("before"), decomposition_tier="nano",
                                       disambiguation_tier=config.STAGE3_TIER, cache=rec, run_tag=f"eval4a-cache:{row['id']}")
        rec.save(CACHE)
        cache_entries = rec.entries
        print(f"recorded {len(cache_entries)} Stage 2/3 results to {CACHE.relative_to(ROOT)}")
    raw = CACHE.read_bytes()
    report = {}
    for tier in tiers:
        report[tier] = []
        for run in range(1, runs + 1):
            results = []
            for row in rows:
                ctx = row.get("context") or {}
                cache = pipeline.StageCache("replay", cache_entries)
                out = pipeline.extract_from_text(row["input_text"], headings=ctx.get("headings"), before=ctx.get("before"),
                                                 decomposition_tier=tier, cache=cache, run_tag=f"eval4a-s4:{row['id']}:{tier}:{run}")
                results.append(score_row(row, out))
            assert pipeline.StageCache.dumps(pipeline.StageCache("replay", cache_entries).entries).encode() == raw
            report[tier].append(results)
            print(f"[{tier} run {run}] passed {sum(r['pass'] for r in results)}/{len(results)}")
    return report


def _ratio(n, d):
    return round(n / d, 3) if d else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="nano", help="Stage 4 tier(s), comma-separated")
    ap.add_argument("--rows", help="comma-separated row ids (labelled rows only)")
    ap.add_argument("--selection-tier", default="nano")
    ap.add_argument("--disambiguation-tier", default=config.STAGE3_TIER)
    ap.add_argument("--compare-stage4", help="S7 protocol: Stage 4 tiers, comma-separated (needs a FROZEN set)")
    ap.add_argument("--runs", type=int, default=5, help="runs per row (the platform is not deterministic at temperature 0)")
    ap.add_argument("--stage3-n", type=int, default=None, help="Stage 3 samples (1 = single call; default config: ensemble of 3)")
    ap.add_argument("--reason-first", dest="reason_first", action="store_true", default=config.REASON_FIRST,
                    help="reason before verdict in the stage schemas (the default)")
    ap.add_argument("--no-reason-first", dest="reason_first", action="store_false", help="A/B: verdict before reason")
    ap.add_argument("--stage3-escalation", default=None, choices=[None, "ultra"], help="escalate Stage 3 to ultra when a guard fires")
    ap.add_argument("--json")
    args = ap.parse_args()
    rows = [json.loads(line) for line in SET.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.rows:
        wanted = set(args.rows.split(","))
        rows = [r for r in rows if r["id"] in wanted]
    skipped = [r["id"] for r in rows if not labelled(r)]
    rows = [r for r in rows if labelled(r)]
    print(f"labelled rows: {len(rows)}; unlabelled rows skipped (not run): {len(skipped)}")
    if args.compare_stage4:
        ok, why = frozen_ok()
        if not ok:
            print(f"refusing the tier comparison: {why}")
            return 2
        report = compare_stage4(rows, args.compare_stage4.split(","), args.runs)
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=1, default=str))
        return 0
    report = {}
    for tier in args.tiers.split(","):
        rows_out = []
        for row in rows:
            ctx = row.get("context") or {}
            runs = []
            for run in range(1, args.runs + 1):
                out = pipeline.extract_from_text(row["input_text"], headings=ctx.get("headings"), before=ctx.get("before"),
                                                 decomposition_tier=tier, selection_tier=args.selection_tier,
                                                 disambiguation_tier=args.disambiguation_tier, stage3_escalation=args.stage3_escalation,
                                                 stage3_n=args.stage3_n, reason_first=args.reason_first,
                                                 run_tag=f"eval4a:{row['id']}:{tier}:{run}")
                runs.append(score_row(row, out))
            k = sum(r["pass"] for r in runs)
            outcomes = {}
            for r in runs:  # distinct outputs: produced claims + exclusions, in order of first appearance
                key = json.dumps({"claims": r["produced"], "excluded": r["excluded"]}, ensure_ascii=False)
                outcomes.setdefault(key, {"count": 0, "pass": r["pass"], "failures": r["failures"]})["count"] += 1
            rows_out.append({"id": row["id"], "passed": k, "runs": args.runs, "distinct_outcomes": len(outcomes), "runs_detail": runs,
                             "outcomes": [{"output": json.loads(o), **v} for o, v in outcomes.items()]})
            print(f"[{tier}] {row['id']:<28} {k} of {args.runs} passed  ({len(outcomes)} distinct outcome{'s' if len(outcomes) != 1 else ''})")
            if len(outcomes) > 1 or k < args.runs:
                for o, v in outcomes.items():
                    oo = json.loads(o)
                    print(f"      x{v['count']} {'PASS' if v['pass'] else 'FAIL'} claims={oo['claims']} excluded={[e[1] + '/' + e[2] for e in oo['excluded']]}")
                    for f in v["failures"]:
                        print(f"            - {f}")
        allruns = [r for ro in rows_out for r in ro["runs_detail"]]
        cp, ce, cm = (sum(r[k] for r in allruns) for k in ("claims_produced", "claims_expected", "claims_matched"))
        ep, ee, et = (sum(r[k] for r in allruns) for k in ("excl_produced", "excl_expected", "excl_tp"))
        agg = {"rows": len(rows_out), "runs_per_row": args.runs, "rows_unanimous_pass": sum(ro["passed"] == ro["runs"] for ro in rows_out),
               "rows_unanimous_fail": sum(ro["passed"] == 0 for ro in rows_out), "rows_split": sum(0 < ro["passed"] < ro["runs"] for ro in rows_out),
               "claim_precision_pooled": _ratio(cm, cp), "claim_recall_pooled": _ratio(cm, ce),
               "exclusion_precision_pooled": _ratio(et, ep), "exclusion_recall_pooled": _ratio(et, ee),
               "calls": sum(r["calls"] for r in allruns), "cost_usd": round(sum(r["cost_usd"] or 0 for r in allruns), 6)}
        print(f"[{tier}] {json.dumps(agg)}")
        report[tier] = {"aggregate": agg, "rows": rows_out}
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
