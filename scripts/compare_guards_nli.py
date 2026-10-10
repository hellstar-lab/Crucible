"""P2-5: which claims do the deterministic guards catch, which does the NLI backstop catch, and which does NLI catch
that the guards MISS? Offline (local NLI model), over recorded extraction outputs. No API calls.

    python scripts/compare_guards_nli.py out1.json [out2.json ...]

Each input is a ClaimExtraction JSON (scripts/extract_claims.py --json). Every claim is checked, kept or excluded:
kept claims come from ``claims``; excluded ones are recovered from the exclusion detail. Guard = the current
deterministic checks (pipeline._Run.check_output + the qualifier guard). NLI = premise "unit text + Stage 3 output",
hypothesis = the claim, every sentence must be entailed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.claims import nli  # noqa: E402
from backend.claims.pipeline import _Run, dropped_qualifiers  # noqa: E402

_CLAIM_IN_DETAIL = re.compile(r"(?:claim '(?P<a>[^']+)' lost|text='(?P<b>[^']+)')")


def rows(paths: list[str]) -> list[dict]:
    out, seen = [], set()
    for p in paths:
        d = json.loads(Path(p).read_text())
        units = {u["unit_id"]: u for u in d["units"]}
        items = [(c["unit_id"], c["text"]) for c in d["claims"]]
        for e in d["excluded"]:
            m = _CLAIM_IN_DETAIL.search(e["detail"])
            if m and e["unit_id"] in units:
                items.append((e["unit_id"], (m.group("a") or m.group("b")).strip()))
        for uid, claim in items:
            u = units[uid]
            key = (u["text"], claim)
            if key in seen:
                continue
            seen.add(key)
            out.append({"unit": u["text"], "clarified": u.get("clarified_text") or u["text"], "claim": claim, "source": Path(p).name})
    return out


def judge(r: dict) -> dict:
    guard = _Run.check_output(r["claim"], r["unit"], r["unit"], f"{r['unit']}\n{r['clarified']}")
    qual = dropped_qualifiers(r["claim"], [], r["unit"])
    ok, per = nli.entails(f"{r['unit']} {r['clarified']}" if r["clarified"] != r["unit"] else r["unit"], r["claim"])
    worst = min(per, key=lambda x: x["entailment"]) if per else {}
    return {**r, "guard": guard[0] if guard else ("qualifier_dropped" if qual else None),
            "nli_entailed": ok, "nli_label": max(("contradiction", "entailment", "neutral"), key=lambda k: worst.get(k, 0)) if worst else None,
            "nli_entailment": worst.get("entailment")}


def main() -> int:
    res = [judge(r) for r in rows(sys.argv[1:])]
    cols = {"guards catch": [r for r in res if r["guard"]], "NLI catches": [r for r in res if not r["nli_entailed"]],
            "NLI catches, guards MISS": [r for r in res if not r["nli_entailed"] and not r["guard"]],
            "guards catch, NLI misses": [r for r in res if r["guard"] and r["nli_entailed"]]}
    print(f"{len(res)} distinct claims checked")
    for name, rs in cols.items():
        print(f"\n== {name}: {len(rs)}")
        for r in rs:
            print(f"   [{r['guard'] or '-'} | NLI {r['nli_label']} {r['nli_entailment']}] {r['claim']!r}  <- unit {r['unit'][:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
