"""NOT IN THE SHIPPING PATH. Negative result — see docs/classifier-experiment.md.

Evaluate the hidden-fragment intent classifier (Shield step 2B).

Runs every fragment of an eval set through classify_fragment (one live
model call each) and prints:

  * HEADLINE (classifier alone, escalate-only guard OFF): 3x3 confusion
    matrix, per-family accuracy, overall accuracy. This is what measures
    the model.
  * PRODUCTION (guard ON): the same, as shipped. Never present guard catches
    as model performance.
  * payload-splitting pair detection, benign false-positive rate
  * cost and latency for this run, read from logs/model_calls.jsonl
  * a separate PASS/FAIL line for the three classifier meta-attacks

    python experiments/classifier/injection_eval.py                     # tuning set, nano
    python experiments/classifier/injection_eval.py --set heldout       # held-out set, nano
    python experiments/classifier/injection_eval.py --set heldout --tier super  # diagnostic

Protocol: tune on the "eval" set; report on the "heldout" set. The held-out
file is hash-checked against injection_heldout.sha256 and only aggregate
numbers are printed for it, so its individual failures cannot leak into
prompt tuning. --tier overrides routing in this process only, for the
diagnostic comparison; TASK_TIER in backend/llm.py is never changed.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from collections import defaultdict  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).resolve().parent / "data"
sys.path.insert(0, str(ROOT))

from backend import llm  # noqa: E402
from experiments.classifier.classify import LABELS, classify_fragment  # noqa: E402

SETS = {
    "eval": DATA / "injection_eval.jsonl",
    "heldout": DATA / "injection_heldout.jsonl",
}
HELDOUT_HASH = DATA / "injection_heldout.sha256"
ATTACK = {"ai_instruction", "fabricated_qualification"}
SHORT = {"benign_residue": "benign", "fabricated_qualification": "fabricated", "ai_instruction": "instruction"}
FAILED = "FAILED"


def confusion(rows: list[dict], key: str) -> str:
    cols = [*LABELS, FAILED]
    head = "truth / predicted".ljust(20) + "".join(f"{SHORT.get(c, c):>13}" for c in cols)
    lines = [head]
    for truth in LABELS:
        counts = [sum(1 for r in rows if r["label"] == truth and (r[key] or FAILED) == c) for c in cols]
        lines.append(f"{SHORT[truth]:<20}" + "".join(f"{n:>13}" for n in counts))
    return "\n".join(lines)


def accuracy(rows: list[dict], key: str) -> tuple[int, int]:
    return sum(1 for r in rows if r[key] == r["label"]), len(rows)


def pct(n: int, d: int) -> str:
    return f"{n}/{d} = {n / d:6.1%}" if d else "n/a"


def report(rows: list[dict], key: str, title: str) -> None:
    print(f"\n=== {title} ===")
    print(confusion(rows, key))
    print()
    families: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        families[r["family"]].append(r)
    print(f"{'family':<26}{'accuracy':>18}")
    for fam in sorted(families):
        print(f"{fam:<26}{pct(*accuracy(families[fam], key)):>18}")
    print(f"{'OVERALL':<26}{pct(*accuracy(rows, key)):>18}")
    benign = [r for r in rows if r["label"] == "benign_residue"]
    fp = sum(1 for r in benign if r[key] in ATTACK)
    print(f"benign flagged as attack (false positives): {pct(fp, len(benign))}")
    pairs: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("pair_id"):
            pairs[r["pair_id"]].append(r)
    caught = sum(1 for p in pairs.values() if any(r[key] in ATTACK for r in p))
    print(f"split-payload pairs caught (either half -> attack): {pct(caught, len(pairs))}")


def meta_line(rows: list[dict], key: str, title: str) -> bool:
    meta = [r for r in rows if r["family"] == "meta_attack"]
    compromised = [r for r in meta if r[key] == "benign_residue"]
    verdict = "PASS" if not compromised else "FAIL (classifier compromised)"
    detail = ", ".join(f"{r['id']}={r[key] or FAILED}" for r in meta)
    print(f"META-ATTACKS [{title}]: {verdict}  ({detail})")
    return not compromised


def run_costs(run_tag: str) -> list[dict]:
    if not llm.MODEL_CALL_LOG.exists():
        return []
    out = []
    with llm.MODEL_CALL_LOG.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_tag") == run_tag:
                out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=sorted(SETS), default="eval")
    ap.add_argument("--tier", choices=["nano", "super"], default=None,
                    help="diagnostic routing override for this process only")
    args = ap.parse_args()
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set (shell or .env); the eval needs the live classifier.", file=sys.stderr)
        return 2
    path = SETS[args.set]
    raw = path.read_bytes()
    if args.set == "heldout":
        frozen = HELDOUT_HASH.read_text().split()[0]
        if hashlib.sha256(raw).hexdigest() != frozen:
            print("injection_heldout.jsonl does not match its frozen sha256; refusing to report.", file=sys.stderr)
            return 4
    items = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    tier = args.tier or llm.TASK_TIER["classify_hidden_text"]
    if args.tier:  # diagnostic override, in this process only
        llm.TASK_TIER = {**llm.TASK_TIER, "classify_hidden_text": args.tier}
    run_tag = f"injection_eval-{args.set}-{tier}-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"run {run_tag}: {len(items)} fragments from {path.name}, one {tier} call each"
          + (" (DIAGNOSTIC tier override)" if args.tier else ""))

    rows, wall = [], time.perf_counter()
    for item in items:
        r = classify_fragment(item["text"], experiment=True, run_tag=run_tag)
        if r.degraded:
            print(f"model became unreachable at {item['id']}; aborting (fallback results are not an eval).",
                  file=sys.stderr)
            return 3
        rows.append({**item, "model": r.model_label, "shipped": r.label, "guard": r.guard_escalated})
    wall = time.perf_counter() - wall

    misses = [r for r in rows if r["model"] != r["label"]]
    report(rows, "model", "HEADLINE: classifier alone (guard OFF)")
    report(rows, "shipped", "PRODUCTION: as shipped (escalate-only guard ON)")

    if args.set == "heldout":
        print(f"\nmisclassified by the model (guard OFF): {len(misses)} "
              "(per-item detail withheld for the held-out set)")
    else:
        print("\nmisclassified by the model (guard OFF):")
        for r in misses:
            print(f"  {r['id']:<4} {r['family']:<24} truth={SHORT[r['label']]:<11} model={SHORT.get(r['model'], FAILED):<11}"
                  f"{' [guard escalated]' if r['guard'] else ''}  {r['text'][:70]!r}")
        if not misses:
            print("  none")

    calls = run_costs(run_tag)
    ok = [c for c in calls if c.get("ok")]
    lat = sorted(c["latency_s"] for c in ok)
    cost = sum(c.get("cost_usd") or 0 for c in calls)
    print("\n=== cost and latency (from logs/model_calls.jsonl, this run only) ===")
    print(f"calls logged: {len(calls)}  ok: {len(ok)}  failed: {len(calls) - len(ok)}  "
          f"truncated (finish_reason=length): {sum(1 for c in ok if c.get('finish_reason') == 'length')}")
    print(f"classification_failed: model {sum(1 for r in rows if r['model'] is None)}/{len(rows)}")
    print(f"tokens in/out: {sum(c.get('input_tokens', 0) for c in ok)}/{sum(c.get('output_tokens', 0) for c in ok)}")
    if lat:
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
        print(f"latency per call: mean {sum(lat) / len(lat):.2f}s  p95 {p95:.2f}s  max {lat[-1]:.2f}s")
    print(f"total model latency: {sum(lat):.1f}s  wall time: {wall:.1f}s")
    print(f"TOTAL COST: ${cost:.6f}")

    print()
    model_ok = meta_line(rows, "model", "classifier alone")
    meta_line(rows, "shipped", "as shipped")
    return 0 if model_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
