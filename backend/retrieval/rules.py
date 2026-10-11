"""Phase 4B retrieval rules: one function each, separately testable, deterministic, no model, no network.

Each rule takes (claim_text, technologies, view) and returns a RuleResult: candidate EvidenceItems plus what it searched
and outcome markers (e.g. "no_benchmark_present"). Items always carry authored_by_candidate as decided by
RepoView.authored(); a rule never sets it on its own. Rules report facts; none decides whether a claim is true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath

from rapidfuzz import fuzz

from backend.contracts import EvidenceItem

from . import config, lexicon
from .sources import RepoView

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_SPLIT = re.compile(r"[/_.\-]|(?<=[a-z])(?=[A-Z])")
_STOP = frozenset("""the a an and or of for to in on at by with from into via over per using built build implemented designed developed
created wrote written authored own owned maintained shipped refactored rewrote migrated architected led worked made added
module modules service services system systems layer feature features app application code project tool tools""".split())


@dataclass
class RuleResult:
    items: list[EvidenceItem] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    structure_keys: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)


def _item(view: RepoView, **kw) -> EvidenceItem:
    path, start, end = kw.get("path"), kw.get("start_line"), kw.get("end_line")
    authored, ratio, caveat = view.authored(path, start, end) if kw.pop("blame", True) else (None, None, None)
    return EvidenceItem(repo_full_name=view.full_name, authored_by_candidate=authored, authored_ratio=ratio,
                        authorship_confidence=view.authorship_confidence, likely_identity_gap=view.likely_identity_gap,
                        authorship_caveat=caveat, **kw)


def _find_line(text: str | None, needle: str) -> int | None:
    if not text:
        return None
    rx = re.compile(rf"(?<![\w-]){re.escape(needle)}(?![\w-])", re.IGNORECASE)
    for i, line in enumerate(text.splitlines(), 1):
        if rx.search(line):
            return i
    return None


# --------------------------------------------------------------------------
# technology_use
# --------------------------------------------------------------------------


def r_dependency(claim: str, techs: list[str], view: RepoView) -> RuleResult:
    """Every parsed manifest entry vs the lexicon's package names: exact first, then rapidfuzz >= DEP_FUZZY_CUTOFF."""
    res = RuleResult()
    rf = view.report.repo_facts
    deps = [d for d in (rf.dependencies if rf else []) if view.eligible(d.manifest_path) or not view.snap]
    res.manifests = sorted({d.manifest_path for d in deps} | {m.manifest_path for m in (rf.manifests if rf else [])
                                                              if view.eligible(m.manifest_path) or not view.snap})
    lex = lexicon.technologies()
    for tech in techs:
        names = lex[tech]["deps"]
        for d in deps:
            exact = next((n for n in names if n.casefold() == d.name.casefold()), None)
            if exact:
                match, detail = "exact", f"manifest entry {d.name!r} == lexicon package {exact!r}"
            else:
                best = max(((fuzz.ratio(d.name.casefold(), n.casefold()), n) for n in names), default=(0, None))
                if best[0] < config.DEP_FUZZY_CUTOFF:
                    continue
                match, detail = "fuzzy", f"fuzzy ratio {best[0]:.1f} >= cutoff {config.DEP_FUZZY_CUTOFF} ({d.name!r} ~ {best[1]!r})"
            line = _find_line(view.text(d.manifest_path), d.name)
            res.items.append(_item(view, path=d.manifest_path, start_line=line, end_line=line, kind="dependency",
                                   excerpt=view.excerpt(d.manifest_path, line) if line else None, rule="r_dependency",
                                   match=match, match_detail=detail + f"; ecosystem {d.ecosystem}", technology=tech,
                                   blame=line is not None))
    if not res.items:
        res.outcomes.append("r_dependency:none")
    return res


def _ts_import_lines(path: str, text: str, module: str) -> list[tuple[int, int]] | None:
    """Import statements naming ``module`` via tree-sitter, as (start, end) 1-based lines; None if no grammar applies."""
    lang = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
            ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".java": "java"}.get(PurePosixPath(path).suffix.lower())
    if not lang:
        return None
    try:
        from tree_sitter_language_pack import get_parser
        tree = get_parser(lang).parse(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 - a parser failure falls back to the line regex, and says so
        return None
    kinds = {"import_statement", "import_from_statement", "import_declaration", "import_spec", "call_expression"}
    out = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type in kinds:
            frag = text.encode("utf-8")[n.start_byte:n.end_byte].decode("utf-8", "replace")
            if n.type == "call_expression" and not frag.startswith("require"):
                stack.extend(n.children)
                continue
            if re.search(rf"(?<![\w-]){re.escape(module)}(?![\w-])", frag):
                out.append((n.start_point[0] + 1, n.end_point[0] + 1))
                continue
        stack.extend(n.children)
    return sorted(set(out))


def r_import(claim: str, techs: list[str], view: RepoView) -> RuleResult:
    """Import / require / use statements in countable (post-exclusion) source files that name a lexicon import prefix."""
    res = RuleResult()
    imports = (view.snap or {}).get("imports", {})
    if not view.snap:
        res.outcomes.append("r_import:no_snapshot")
        return res
    lex = lexicon.technologies()
    for tech in techs:
        prefixes = lex[tech]["imports"]
        if not prefixes:
            continue
        for path, modules in sorted(imports.items()):
            if not view.eligible(path):
                continue
            for module in modules:
                pre = lexicon.import_matches(module, prefixes)
                if not pre:
                    continue
                text = view.text(path)
                spans, parser = None, "snapshot (no checkout: line unknown)"
                if text is not None:
                    spans = _ts_import_lines(path, text, module)
                    parser = "tree-sitter"
                    if not spans:
                        line = _find_line(text, module)
                        spans, parser = ([(line, line)] if line else []), "line-regex"
                for start, end in (spans or [(None, None)]):
                    res.items.append(_item(view, path=path, start_line=start, end_line=end, kind="import",
                                           excerpt=view.excerpt(path, start, end) if start else None, rule="r_import", match="exact",
                                           match_detail=f"import {module!r} matches lexicon prefix {pre!r}; parser={parser}",
                                           technology=tech, blame=True))
    if not res.items:
        res.outcomes.append("r_import:none")
    return res


def r_config(claim: str, techs: list[str], view: RepoView) -> RuleResult:
    """Lexicon file patterns (Dockerfile, *.tf, k8s/ ...) and config keys in eligible files. How Kubernetes, Terraform and
    Docker get found: none of them is a dependency entry."""
    res = RuleResult()
    paths = view.eligible_paths
    lex = lexicon.technologies()
    for tech in techs:
        entry = lex[tech]
        if not entry.get("code_artifacts", True):
            res.outcomes.append(f"r_config:{tech}:no_code_artifact")
            continue
        for pat in entry["files"]:
            for path in paths:
                if not lexicon.file_pattern_match(path, pat):
                    continue
                if isinstance(pat, dict):
                    text = view.text(path)
                    if text is None:
                        res.outcomes.append(f"r_config:{tech}:content_pattern_needs_checkout")
                        continue
                    line = _find_line(text, pat["contains"].rstrip(":")) if pat["contains"] in text else None
                    if line is None:
                        continue
                    res.items.append(_item(view, path=path, start_line=line, end_line=line, kind="config", rule="r_config",
                                           excerpt=view.excerpt(path, line), match="pattern", technology=tech,
                                           match_detail=f"{pat['glob']} containing {pat['contains']!r}"))
                else:
                    res.items.append(_item(view, path=path, kind="file_presence", rule="r_config", match="pattern",
                                           match_detail=f"path matches {pat!r}", technology=tech))
        keys = entry["config_keys"]
        if keys:
            for path in paths:
                p = PurePosixPath(path)
                if p.suffix.lower() not in config.CONFIG_SUFFIXES and p.name not in config.CONFIG_NAMES:
                    continue
                text = view.text(path)
                if text is None:
                    continue
                for key in keys:
                    line = next((i for i, ln in enumerate(text.splitlines(), 1) if key in ln), None)
                    if line:
                        res.items.append(_item(view, path=path, start_line=line, end_line=line, kind="config", rule="r_config",
                                               excerpt=view.excerpt(path, line), match="config_key", technology=tech,
                                               match_detail=f"config key {key!r}"))
                        break
            if view.checkout is None:
                res.outcomes.append("r_config:config_keys_need_checkout")
    seen, uniq = set(), []
    for it in res.items:  # one item per (path, line, technology)
        k = (it.path, it.start_line, it.technology)
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    res.items = uniq
    if not res.items:
        res.outcomes.append("r_config:none")
    return res


# --------------------------------------------------------------------------
# ownership, artifact_exists, scale, timeline, metric
# --------------------------------------------------------------------------


def artifact_words(claim: str) -> list[str]:
    """Words of the claim that can name an artifact (module, file, function): content words minus verbs and fillers."""
    return [w.lower() for w in _WORD.findall(claim or "") if len(w) >= config.OWNERSHIP_MIN_WORD and w.lower() not in _STOP]


def _tokens(s: str) -> list[str]:
    return [t.lower() for t in _SPLIT.split(s or "") if t]


def r_ownership(claim: str, techs: list[str], view: RepoView) -> RuleResult:
    """Functions and countable files whose path or name matches the artifact the claim names (rapidfuzz on tokens)."""
    res = RuleResult()
    words = artifact_words(claim)
    if not words:
        res.outcomes.append("r_ownership:no_artifact_named")
        return res
    if not view.snap:
        res.outcomes.append("r_ownership:no_snapshot")
        return res

    def best(tokens: list[str]) -> tuple[float, str, str]:
        return max(((fuzz.ratio(w, t), w, t) for w in words for t in tokens), default=(0.0, "", ""))

    for fn in view.snap.get("functions", []):
        if not view.eligible(fn["path"]):
            continue
        score, w, t = best(_tokens(fn["name"]) + _tokens(PurePosixPath(fn["path"]).stem))
        if score >= config.OWNERSHIP_FUZZY_CUTOFF:
            res.items.append(_item(view, path=fn["path"], start_line=fn["start_line"], end_line=fn["end_line"], kind="function",
                                   excerpt=view.excerpt(fn["path"], fn["start_line"], fn["end_line"]), rule="r_ownership",
                                   match="exact" if score == 100 else "fuzzy",
                                   match_detail=f"claim word {w!r} ~ {t!r} (ratio {score:.1f}, cutoff {config.OWNERSHIP_FUZZY_CUTOFF}); "
                                                f"function {fn['name']}"))
    for path in sorted(view.blamed):  # K3: authored fixture code can be the artifact an ownership claim names
        if not view.eligible(path):
            continue
        score, w, t = best([x for part in PurePosixPath(path).with_suffix("").parts for x in _tokens(part)])
        if score >= config.OWNERSHIP_FUZZY_CUTOFF:
            res.items.append(_item(view, path=path, kind="file_presence", rule="r_ownership", match="exact" if score == 100 else "fuzzy",
                                   match_detail=f"claim word {w!r} ~ path token {t!r} (ratio {score:.1f})"))
    if not res.items:
        res.outcomes.append("r_ownership:none")
    return res


def r_structure(claim: str, techs: list[str], view: RepoView) -> RuleResult:
    """Presence in repo_facts.structure for the keys the claim's words name. Entries under excluded paths are dropped."""
    res = RuleResult()
    low = (claim or "").lower()
    keys = [k for k, words in config.STRUCTURE_WORDS.items()
            if any(re.search(rf"(?<![\w/]){re.escape(w)}(?![\w/])", low) for w in words)]
    res.structure_keys = keys
    st = view.report.repo_facts.structure if view.report.repo_facts else None
    if not keys:
        res.outcomes.append("r_structure:no_structure_key_named")
        return res
    if st is None:
        res.outcomes.append("r_structure:no_structure")
        return res
    eligible = view.eligible_paths
    dropped = 0
    for key in keys:
        for entry in getattr(st, key):
            if key.endswith("_dirs"):
                inside = [p for p in eligible if p.startswith(entry.rstrip("/") + "/")]
                if not inside:
                    dropped += 1  # the directory exists only through excluded files: not evidence
                    continue
                res.items.append(_item(view, path=inside[0], kind="file_presence", rule="r_structure", match="structure",
                                       match_detail=f"structure.{key} contains {entry!r} ({len(inside)} eligible files)", blame=True))
            elif view.eligible(entry):
                res.items.append(_item(view, path=entry, kind="file_presence", rule="r_structure", match="structure",
                                       match_detail=f"structure.{key} contains {entry!r}"))
            else:
                dropped += 1
    if dropped:
        # The entry exists but 3.5 excluded it (e.g. Linguist treats .github/ as vendored). Not evidence; said out loud so
        # 4C does not read "nothing found" as "nothing there".
        res.outcomes.append(f"r_structure:excluded_by_3.5={dropped}")
    if not res.items:
        res.outcomes.append("r_structure:none")
    return res


_UNITS = {
    "total_source_lines": re.compile(r"(?i)\b(loc|lines?(?: of code)?|sloc)\b"),
    "source_files": re.compile(r"(?i)\bfiles?\b"),
    "repos": re.compile(r"(?i)\b(repositories|repos?|projects|services|microservices)\b"),
    "languages": re.compile(r"(?i)\blanguages?\b"),
}


def r_scale(claim: str, techs: list[str], view: RepoView, all_views: list[RepoView] | None = None) -> RuleResult:
    """The repository's own numbers next to the claimed ones. Reports both; decides nothing (that is 4C's job)."""
    from backend.claims.support import quantities

    res = RuleResult()
    claimed = ", ".join(w for w, _v in quantities(claim)) or None
    rf = view.report.repo_facts
    hits = [k for k, rx in _UNITS.items() if rx.search(claim or "")] or ["total_source_lines", "source_files"]
    for key in hits:
        if key == "repos":
            n = len(all_views or [view])
            obs = f"repositories analysed for this candidate: {n}"
        elif key == "languages":
            obs = f"languages (source lines, post-exclusion): {dict(sorted((rf.languages or {}).items())) if rf else None}"
        else:
            obs = f"{key}: {getattr(rf, key, None) if rf else None} (post-exclusion, scope {view.scoped_path or 'whole repo'})"
        res.items.append(_item(view, kind="count", rule="r_scale", match="count", observed_value=obs, claimed_value=claimed,
                               match_detail=f"unit {key}", blame=False))
    return res


def r_history(claim: str, techs: list[str], view: RepoView, today: date | None = None) -> RuleResult:
    """Overlap between the claim's dates and the repository's first/last commit. A fact, not a verdict."""
    from backend.shield import resume

    res = RuleResult()
    today = today or date.today()
    spans = []
    for m in resume._RANGE.finditer(claim or ""):
        a, b = resume.parse_token(m.group("s"), today), resume.parse_token(m.group("e"), today)
        if a and b:
            spans.append((m.group(0), a[0], b[1]))
    if not spans:
        for m in resume._SINGLE.finditer(claim or ""):
            t = resume.parse_token(m.group("s"), today)
            if t:
                spans.append((m.group(0), t[0], t[1]))
    h = view.report.repo_facts.history if view.report.repo_facts else None
    if not spans:
        res.outcomes.append("r_history:no_date_in_claim")
        return res
    if h is None or not h.first_commit:
        res.outcomes.append("r_history:no_history")
        return res
    first, last = date.fromisoformat(h.first_commit[:10]), date.fromisoformat(h.last_commit[:10])
    for raw, cs, ce in spans:
        overlap = (min(ce, last) - max(cs, first)).days + 1
        res.items.append(_item(view, kind="history", rule="r_history", match="history", blame=False,
                               observed_value=f"first_commit {first.isoformat()}, last_commit {last.isoformat()} "
                                              f"({h.commit_count} commits)",
                               claimed_value=f"{raw!r} -> {cs.isoformat()} .. {ce.isoformat()}",
                               match_detail=f"overlap {max(overlap, 0)} days"))
    return res


def r_benchmark(claim: str, techs: list[str], view: RepoView) -> RuleResult:
    """Benchmark, perf and load-test files FIRST. None present -> zero items, outcome no_benchmark_present: the honest
    evidence for a metric claim no repository artifact measures."""
    import fnmatch

    res = RuleResult()
    for path in view.eligible_paths:
        p = PurePosixPath(path)
        why = None
        if any(part.lower() in config.BENCHMARK_DIRS for part in p.parts[:-1]):
            why = "benchmark directory"
        elif any(fnmatch.fnmatchcase(p.name, g) for g in config.BENCHMARK_GLOBS):
            why = "benchmark file name"
        else:
            for g, needle in config.BENCHMARK_CONTENT:
                if fnmatch.fnmatchcase(p.name, g):
                    text = view.text(path)
                    if text and needle in text:
                        line = _find_line(text, needle.rstrip("("))
                        res.items.append(_item(view, path=path, start_line=line, end_line=line, kind="file_presence",
                                               excerpt=view.excerpt(path, line) if line else None, rule="r_benchmark",
                                               match="pattern", match_detail=f"{g} containing {needle!r}"))
                        break
        if why:
            res.items.append(_item(view, path=path, kind="file_presence", rule="r_benchmark", match="pattern", match_detail=why))
    if not res.items:
        res.outcomes.append("no_benchmark_present")
    return res


RULES = {"r_dependency": r_dependency, "r_import": r_import, "r_config": r_config, "r_ownership": r_ownership,
         "r_structure": r_structure, "r_scale": r_scale, "r_history": r_history, "r_benchmark": r_benchmark}
