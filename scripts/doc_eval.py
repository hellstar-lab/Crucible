"""Score the document analyzer against the frozen fixture corpus and the real-world documents.

    python scripts/doc_eval.py              # fixtures + real-world documents
    python scripts/doc_eval.py --no-external

Fixtures are defined by tests/fixtures/docs/MANIFEST.json (hash-frozen: FROZEN.sha256). Each one is
checked for status, for the exact set of findings (info-severity facts excluded; hidden fragments
count as "hidden_text") and for the fixture-specific detail the manifest records.

False positives are counted on documents expected clean: control fixtures, tool-generated controls
and the real-world corpus. A false positive is hidden_text_found or any medium/high finding. A
document truncated by a cap is reported as truncated (needs_review by design), not as a false positive.
No thresholds are tuned here; this script only measures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.shield.analyze import analyze_document  # noqa: E402

DOCS = ROOT / "tests" / "fixtures" / "docs"
TODAY = date(2026, 10, 10)
logging.disable(logging.CRITICAL)


def counted(rep) -> dict:
    c = Counter(f.detector for f in rep.security_findings if f.severity != "info")
    if rep.hidden_fragments:
        c["hidden_text"] = len(rep.hidden_fragments)
    return dict(c)


def _norm_url(u: str) -> str:
    return u.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")


def specific(name: str, d: dict, rep) -> list[str]:
    """Fixture-specific checks; returns a list of failure descriptions."""
    bad: list[str] = []
    st, rs = rep.structure, rep.resume

    def eq(label, got, want):
        if got != want:
            bad.append(f"{label}: got {got!r}, want {want!r}")

    if "per_page_kind" in d:
        eq("per_page_kind", rep.document.per_page_kind, d["per_page_kind"])
    if "ocr_required_pages" in d:
        eq("ocr_required_pages", rep.extraction_quality.ocr_required_pages, d["ocr_required_pages"])
    if d.get("text_fields_none"):
        eq("structure is None", rep.structure, None)
        eq("chars_total is None", rep.extraction_quality.chars_total, None)
    if "has_form" in d:
        eq("has_form", rep.document.has_form, d["has_form"])
    if name.startswith("provenance_"):
        p = rep.provenance
        for k in ("author", "created", "creator", "date_order_invalid", "mod_minus_create_days", "modified", "producer"):
            if k in d:
                got = getattr(p, k)
                eq(f"provenance.{k}", round(got, 2) if k == "mod_minus_create_days" and got is not None else got, d[k])
    if name == "revision_incremental.pdf":
        f = [x for x in rep.security_findings if x.detector == "revision_history"]
        eq("revision_count", f[0].evidence.get("revision_count") if f else None, d["revision_count"])
        eq("severity", f[0].severity if f else None, d["severity"])
    if name == "embedded_file.pdf":
        f = [x for x in rep.security_findings if x.detector == "embedded_files"][0]
        eq("name", f.evidence["name"], d["name"]), eq("size", f.evidence["size"], d["size"]), eq("first8", f.evidence["first8_hex"], d["first8_hex"])
    if name == "active_javascript.pdf":
        kinds = sorted({k for x in rep.security_findings if x.detector == "active_content" for k in (x.evidence.get("kinds") or [x.evidence.get("kind")])})
        eq("kinds", kinds, sorted(d["kinds"]))
    if name in ("link_mismatch.pdf", "link_launch_gotor.pdf"):
        shown = sorted(x.evidence["display_text"] for x in rep.security_findings if x.detector == "link_target_mismatch")
        eq("mismatch_display", shown, sorted(d["mismatch_display"]))
    if name.startswith("offpage_") or name == "cropbox_shrunk.pdf":
        if d.get("text"):
            eq("hidden text", [f.text for f in rep.hidden_fragments], [d["text"]])
            f = [x for x in rep.security_findings if x.detector in ("offpage_text", "cropbox_shrunk")][0]
            eq("boxes_violated", sorted(f.evidence.get("boxes_violated", [])), sorted(d["boxes_violated"]))
    if name == "font_midline.pdf":
        f = [x for x in rep.security_findings if x.detector == "font_anomaly"][0]
        eq("severity", f.severity, "low")
    if name == "report_project.pdf":
        eq("pages>=", rep.document.pages >= d["min_pages"], True)
        eq("headings", [[h.text, h.level] for h in st.headings], d["headings"])
        eq("tables", [[t.n_rows, t.n_cols] for t in (st.tables or [])], d["table_shapes"])
        eq("numeric_claims", [c.match for c in st.numeric_claims], d["numeric_claims"])
        eq("arxiv", st.references.arxiv_ids, d["arxiv"]), eq("dois", st.references.dois, d["dois"])
        eq("references", len(st.references.entries), d["references"])
        eq("github_targets", sorted({l.github_target for l in st.links if l.github_target}), d["github_targets"])
        ann = [l.url for l in st.links if "annotation" in l.sources]
        eq("annotation link", d["links"]["annotation"] in ann, True)
        eq("text-only link", [l.url for l in st.links if l.sources == ["text"] and l.url == d["links"]["text_only"]], [d["links"]["text_only"]])
        eq("body_size", st.body_size, d["body_size"])
    if name == "report_single_font.pdf":
        eq("headings", [[h.text, h.level] for h in st.headings], d["headings"])
        eq("method", {h.method for h in st.headings}, {d["method"]})
    if name == "report_prose_only.pdf":
        eq("headings", len(st.headings), d["headings"]), eq("tables", len(st.tables or []), d["tables"])
    if name == "report_two_column.pdf":
        text = "\n".join(s.text for s in st.sections)
        pos = [text.find(f"s{i:02d}") for i in range(1, d["markers"] + 1)]
        eq("markers present and ascending", all(p >= 0 for p in pos) and pos == sorted(pos), True)
    if name in ("resume_standard.pdf", "docx_clean_resume.docx"):
        eq("resume block", rs is not None, True)
        if rs:
            eq("contradictions", len(rs.timeline_contradictions), 0)
    if name == "resume_standard.pdf" and rs:
        eq("emails", rs.contact_block.emails, d["emails"]), eq("phones", len(rs.contact_block.phones), d["phones"])
        eq("urls", sorted(_norm_url(u) for u in rs.contact_block.urls), sorted(d["urls"]))
        eq("skills", sorted(s.skill for s in rs.skills_mentioned), sorted(d["skills"]))
        eq("timeline", [[t.raw, t.is_current] for t in rs.timeline], d["timeline"])
        eq("resume_confidence", rep.document.resume_confidence, 1.0)
    if name == "resume_contradictions.pdf" and rs:
        eq("contradictions", dict(Counter(c.kind for c in rs.timeline_contradictions)), d["contradictions"])
        for kind, cites in d["citations"].items():
            got = [c.cites for c in rs.timeline_contradictions if c.kind == kind]
            eq(f"cites[{kind}]", sorted(got[0]) if got else None, sorted(cites))
    if name == "docx_hidden.docx":
        eq("reasons", sorted(r for f in rep.hidden_fragments for r in f.reasons), sorted(d["reasons"]))
        eq("texts", sorted(f.text for f in rep.hidden_fragments), sorted(d["texts"]))
    if name == "docx_structure.docx":
        eq("headings", [[h.text, h.level] for h in st.headings], d["headings"])
        eq("tables", [[t.n_rows, t.n_cols] for t in st.tables], d["tables"])
        eq("numeric_claims", [c.match for c in st.numeric_claims], d["numeric_claims"])
        p = rep.provenance
        eq("creator", p.creator, d["creator"]), eq("last_modified_by", p.last_modified_by, d["last_modified_by"])
        eq("creator_differs", p.creator_differs_from_last_modified_by, True)
        eq("revision", p.revision, d["revision"]), eq("total_editing_minutes", p.total_editing_minutes, d["total_editing_minutes"])
    return bad


def evaluate(name: str, spec: dict) -> tuple[object, list[str]]:
    """(report, failures) for one manifest fixture."""
    rep = analyze_document(DOCS / name, today=TODAY)
    failures = []
    if rep.status != spec["status"]:
        failures.append(f"status: got {rep.status}, want {spec['status']}")
    if counted(rep) != spec["findings"]:
        failures.append(f"findings: got {counted(rep)}, want {spec['findings']}")
    if rep.detector_errors:
        failures.append(f"detector errors: {rep.detector_errors}")
    return rep, failures + specific(name, spec.get("detail", {}), rep)


def run_fixtures(manifest: dict) -> list[dict]:
    rows = []
    for name, spec in sorted(manifest["files"].items()):
        t0 = time.monotonic()
        rep, failures = evaluate(name, spec)
        rows.append({"name": name, "kind": spec["kind"], "status": rep.status, "failures": failures, "secs": round(time.monotonic() - t0, 2),
                     "false_positive": spec["status"] == "clean" and rep.status != "clean"})
    return rows


def run_controls(manifest: dict) -> list[dict]:
    rows = []
    for name, spec in sorted(manifest["tool_controls"].items()):
        path = DOCS / "controls" / name
        if not path.exists():
            continue
        rep = analyze_document(path, today=TODAY)
        rows.append({"name": "controls/" + name, "status": rep.status, "findings": counted(rep), "false_positive": rep.status != "clean"})
    return rows


def run_external(manifest: dict) -> list[dict]:
    rows = []
    for name, spec in sorted(manifest["external_real_world"].items()):
        path = Path(spec["local"]) if spec.get("local") else Path(spec["source"])
        path = path if path.is_absolute() else ROOT / path
        if not path.exists():
            rows.append({"name": name, "skipped": "file not present on this machine"})
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != spec["sha256"]:
            rows.append({"name": name, "skipped": "sha256 differs from the frozen manifest"})
            continue
        t0 = time.monotonic()
        rep = analyze_document(path, today=TODAY)
        serious = [f for f in rep.security_findings if f.severity in ("medium", "high")]
        rows.append({"name": name, "kind": spec["kind"], "status": rep.status, "pages": rep.document.pages, "pages_total": rep.document.pages_total,
                     "truncated": rep.document.truncated, "hidden": len(rep.hidden_fragments), "findings": counted(rep), "secs": round(time.monotonic() - t0, 1),
                     "false_positive": bool(rep.hidden_fragments) or bool(serious), "needs_review": rep.needs_review,
                     "fp_sources": sorted({f.detection_method for f in rep.hidden_fragments} | {f.detector for f in serious}),
                     "known_pre2c": bool(spec.get("known_pre2c_finding"))})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-external", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    manifest = json.loads((DOCS / "MANIFEST.json").read_text())
    fx, ctl = run_fixtures(manifest), run_controls(manifest)
    ext = [] if args.no_external else run_external(manifest)
    if args.json:
        print(json.dumps({"fixtures": fx, "controls": ctl, "external": ext}, indent=1))
    else:
        print(f"{'fixture':<34}{'kind':<10}{'status':<19}{'secs':>6}  result")
        for r in fx:
            print(f"{r['name']:<34}{r['kind']:<10}{r['status']:<19}{r['secs']:>6}  {'PASS' if not r['failures'] else 'FAIL'}")
            for f in r["failures"]:
                print(f"    - {f}")
        print()
        for r in ctl:
            print(f"control {r['name']:<40}{r['status']:<12}{r['findings']}")
        print()
        for r in ext:
            if "skipped" in r:
                print(f"external {r['name']:<50}SKIPPED ({r['skipped']})")
            else:
                note = f"TRUNCATED {r['pages']}/{r['pages_total']} pages (cap)" if r["truncated"] else ""
                fp = f" FP via {r['fp_sources']}{' (known pre-2C)' if r['known_pre2c'] else ''}" if r["false_positive"] else ""
                print(f"external {r['name']:<50}{r['status']:<19}{r['kind']:<12}hidden={r['hidden']} {r['findings']} {r['secs']}s {note}{fp}")
    n_fail = sum(1 for r in fx if r["failures"])
    fps = [r["name"] for r in fx + ctl + ext if r.get("false_positive")]
    n_clean = sum(1 for r in fx if r["kind"] in ("control", "kind", "structure")) + len(ctl) + sum(1 for r in ext if "skipped" not in r)
    by_src = Counter(src for r in ext if r.get("false_positive") for src in r["fp_sources"])
    print(f"\nfalse positives by detector: {dict(by_src)} (render_diff / attribute = Phase 2A; offpage_text, cropbox_shrunk and the pypdf detectors = 2C)")
    print(f"fixtures: {len(fx) - n_fail}/{len(fx)} pass | clean-expected documents examined: {n_clean} | false positives: {len(fps)} {fps}")
    return 1 if n_fail or fps else 0


if __name__ == "__main__":
    raise SystemExit(main())
