"""Run Shield over every fixture and print a precision/recall table.

Fragment-level scoring against each fixture's .json ground truth:
  TP = a planted hidden string recovered exactly as a fragment
  FN = a planted string not recovered
  FP = a reported fragment that matches no planted string
Status accuracy compares the reported status with expected_status.

Shield is deterministic and offline: no model calls, no cost, fully
reproducible. (The removed LLM classifier experiment has its own numbers in
docs/classifier-experiment.md; never merge the two.)

    python scripts/shield_report.py
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # entry point only: library code reads os.environ

import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.shield import ShieldError, scan_pdf  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
log = logging.getLogger("shield_report")


def _ratio(n: int, d: int) -> str:
    return f"{n / d:.3f}" if d else "  n/a"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stdout)
    rows, tp_all, fp_all, fn_all, status_ok, status_n = [], 0, 0, 0, 0, 0
    for truth_path in sorted(FIXTURES.glob("*.json")):
        truth = json.loads(truth_path.read_text())
        pdf = FIXTURES / truth["file"]
        start = time.perf_counter()
        if truth.get("expect_error"):
            try:
                scan_pdf(pdf)
                got = "no error"
            except ShieldError as exc:
                got = type(exc).__name__
            ok = got == truth["expect_error"]
            rows.append((pdf.name, "-", "-", "-", "-", "-", got, "ok" if ok else "FAIL", time.perf_counter() - start))
            status_ok += ok
            status_n += 1
            continue
        result = scan_pdf(pdf)
        planted = [h["text"] for h in truth["hidden"]]
        found = [f.text for f in result.hidden_fragments]
        tp = sum(1 for p in planted if p in found)
        fn = len(planted) - tp
        fp = sum(1 for f in found if f not in planted)
        ok = result.status == truth["expected_status"]
        tp_all, fp_all, fn_all = tp_all + tp, fp_all + fp, fn_all + fn
        status_ok += ok
        status_n += 1
        rows.append((pdf.name, len(planted), tp, fp, fn, _ratio(tp, tp + fp) if (tp + fp) else "  n/a",
                     result.status, "ok" if ok else "FAIL", time.perf_counter() - start))

    header = f"{'fixture':<16}{'planted':>8}{'TP':>4}{'FP':>4}{'FN':>4}{'prec':>7}  {'status':<22}{'':<5}{'secs':>6}"
    lines = [header, "-" * len(header)]
    for name, planted, tp, fp, fn, prec, status, ok, secs in rows:
        lines.append(f"{name:<16}{planted!s:>8}{tp!s:>4}{fp!s:>4}{fn!s:>4}{prec:>7}  {status:<22}{ok:<5}{secs:>6.2f}")
    lines.append("-" * len(header))
    lines.append(f"precision = TP/(TP+FP) = {tp_all}/{tp_all + fp_all} = {_ratio(tp_all, tp_all + fp_all)}")
    lines.append(f"recall    = TP/(TP+FN) = {tp_all}/{tp_all + fn_all} = {_ratio(tp_all, tp_all + fn_all)}")
    lines.append(f"status / error-type accuracy = {status_ok}/{status_n}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if (fp_all == 0 and fn_all == 0 and status_ok == status_n) else 1


if __name__ == "__main__":
    raise SystemExit(main())
