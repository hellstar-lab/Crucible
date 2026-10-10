"""Identity-free snapshot of one repository at one commit (Phase 3.6).

Everything here is computed WITHOUT knowing who the candidate is, so a
snapshot can be cached by (full_name, head_sha, scoped_path, ruleset) and
reused for any identity. Authorship is derived from it afterwards
(authorship.py), so a cache hit can never leak one identity's attribution
into another's report.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import config
from .blame import parse_line_porcelain
from .counting import SOURCE_MARKS
from .dependencies import manifest_parser, parse_manifests
from .errors import GitError, GitTimeout
from .exclusions import VENDORED_PATH, linguist_path_reason
from .functions import extract_functions
from .gitutil import has_commits, run_git
from .readme import README_NAMES, extract_claims
from .repo_signals import commit_facts
from .structure import detect_structure

log = logging.getLogger(__name__)
SNAPSHOT_VERSION = 1
MAX_MANIFEST_BYTES = 512_000
MAX_README_BYTES = 256_000

_IMPORT_RES = {
    "python": [re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)],
    "js": [re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]"""), re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")],
    "java": [re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M)],
    "go": [re.compile(r'^\s*(?:import\s+)?(?:\w+\s+)?"([\w./-]+)"', re.M)],
}
_IMPORT_LANG = {".py": "python", ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
                ".java": "java", ".kt": "java", ".go": "go"}


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def in_scope(path: str, scoped: str | None) -> bool:
    return not scoped or path == scoped or path.startswith(scoped.rstrip("/") + "/")


def extract_imports(path: str, text: str) -> list[str]:
    lang = _IMPORT_LANG.get(PurePosixPath(path).suffix.lower())
    if not lang:
        return []
    found: set[str] = set()
    for rx in _IMPORT_RES[lang]:
        found.update(m.group(1) for m in rx.finditer(text))
    return sorted(found)[:200]


def _show(repo: Path, path: str, limit: int) -> str | None:
    try:
        data = run_git(["show", f"HEAD:{path}"], cwd=repo, timeout=30)
    except GitError:
        return None
    return data[:limit].decode("utf-8", "replace")


def _commit_table(repo: Path, scoped: str | None) -> list[dict]:
    """Every commit reachable from HEAD (touching the scope, if scoped)."""
    scope = ["--", scoped] if scoped else []
    out = run_git(["log", "--format=%x01%H%x00%at%x00%ct%x00%aE%x00%aN%x00%s", "--shortstat", *scope],
                  cwd=repo, timeout=120).decode("utf-8", "replace")
    commits = []
    for chunk in out.split("\x01")[1:]:
        head, _, rest = chunk.partition("\n")
        parts = head.split("\x00")
        if len(parts) < 6:
            continue
        stat = re.search(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?", rest)
        commits.append({
            "sha": parts[0], "author_time": int(parts[1]), "committer_time": int(parts[2]),
            "author_email": parts[3].strip().lower(), "author_name": parts[4], "message_head": parts[5][:100],
            "files_changed": int(stat.group(1)) if stat else 0,
            "insertions": int(stat.group(2) or 0) if stat else 0,
            "deletions": int(stat.group(3) or 0) if stat else 0,
        })
    return commits


def _blame_counted(repo: Path, counted, deadline: float):
    """Per counted file: commit sha per line (identity-free)."""
    def work(fc):
        if time.monotonic() > deadline:
            return fc.path, None, "analysis time budget exhausted"
        try:
            out = run_git(["blame", "--line-porcelain", "HEAD", "--", fc.path], cwd=repo,
                          timeout=config.BLAME_TIMEOUT_S)
        except GitTimeout:
            return fc.path, None, f"blame exceeded {config.BLAME_TIMEOUT_S}s"
        except GitError as exc:
            return fc.path, None, f"blame failed ({exc})"
        lines = parse_line_porcelain(out)
        marks = list(fc.marks)
        extra = lines[len(marks):]
        if len(lines) > len(marks) and all(ln.blank for ln in extra):
            marks += ["e"] * len(extra)
        if len(lines) != len(marks):
            return fc.path, None, f"blame has {len(lines)} lines, pygount {len(fc.marks)}"
        return fc.path, (lines, "".join(marks)), None

    with ThreadPoolExecutor(max_workers=config.BLAME_WORKERS) as pool:
        return list(pool.map(work, counted))


def build_snapshot(repo: Path, full_name: str, scoped_path: str | None = None,
                   budget_s: float = config.ANALYSIS_TIMEOUT_S) -> dict:
    """Identity-free facts for the repository at HEAD (restricted to ``scoped_path``)."""
    from . import _gitattributes, _list_files, _measure, _raw_line_counts, _text_paths  # avoid import cycle

    deadline = time.monotonic() + budget_s
    snap: dict = {"version": SNAPSHOT_VERSION, "full_name": full_name, "scoped_path": scoped_path, "notes": []}
    if not has_commits(repo):
        snap["empty"] = True
        return snap
    snap["empty"] = False
    snap["head_sha"] = run_git(["rev-parse", "HEAD"], cwd=repo, timeout=30).decode().strip()
    all_files = _list_files(repo)
    files = [f for f in all_files if in_scope(f[0], scoped_path)]
    if scoped_path and not files:
        snap["notes"].append(f"scoped_path {scoped_path!r} matches no files at HEAD")
    paths = [p for p, _s, _h in files]

    with tempfile.TemporaryDirectory(prefix="crucible-snap-") as tmp:
        counted, excluded, skipped = _measure(repo, files, _gitattributes(repo), _text_paths(repo),
                                              _raw_line_counts(repo), Path(tmp))
        functions, imports = [], {}
        for fc in counted:
            src = (Path(tmp) / fc.path).read_bytes()
            for fn in extract_functions(fc.path, src):
                functions.append(fn.__dict__ | {"boundaries_agree": fn.boundaries_agree})
            found = extract_imports(fc.path, src.decode("utf-8", "replace"))
            if found:
                imports[fc.path] = found

    commits = _commit_table(repo, scoped_path)
    index = {c["sha"]: i for i, c in enumerate(commits)}
    files_out: dict[str, dict] = {}
    for fc, (path, result, problem) in zip(counted, _blame_counted(repo, counted, deadline)):
        if result is None:
            excluded.append((path, fc.line_count, "unparseable" if "lines" in (problem or "") else "too_large"))
            snap["notes"].append(f"{path}: not counted, {problem}")
            continue
        lines, marks = result
        line_commits = []
        for ln in lines:
            if ln.sha not in index:  # boundary or out-of-scope commit seen only by blame
                index[ln.sha] = len(commits)
                commits.append({"sha": ln.sha, "author_time": None, "committer_time": None,
                                "author_email": ln.author_email, "author_name": "", "message_head": "",
                                "files_changed": None, "insertions": None, "deletions": None})
            line_commits.append(index[ln.sha])
        files_out[path] = {"language": fc.language, "code": fc.code, "documentation": fc.documentation,
                           "string": fc.string, "empty": fc.empty, "marks": marks, "line_commits": line_commits}

    counted_paths = set(files_out)
    facts = commit_facts(repo, lambda p: p in counted_paths, pathspec=scoped_path)
    total_raw = sum(f["code"] + f["documentation"] + f["string"] + f["empty"] for f in files_out.values())
    share = facts.largest_commit_added / total_raw if total_raw else None
    a_times = [c["author_time"] for c in commits if c["author_time"]]

    manifest_texts = {}
    for p in paths:
        if manifest_parser(p) and linguist_path_reason(p) != VENDORED_PATH:
            text = _show(repo, p, MAX_MANIFEST_BYTES)
            if text is not None:
                manifest_texts[p] = text
    manifests, deps = parse_manifests(manifest_texts)

    readme_path = None
    root = (scoped_path.rstrip("/") + "/") if scoped_path else ""
    for cand in [root + n for n in README_NAMES] + ([n for n in README_NAMES] if root else []):
        if cand in {p for p, _s, _h in all_files}:
            readme_path = cand
            break
    readme_text = _show(repo, readme_path, MAX_README_BYTES) if readme_path else None

    snap.update({
        "files_in_scope": paths,
        "counted": files_out,
        "excluded": excluded,
        "skipped_files": [s.model_dump() for s in skipped],
        "commits": commits,
        "functions": [f for f in functions if f["path"] in counted_paths],
        "imports": imports,
        "manifests": [m.model_dump() for m in manifests],
        "dependencies": [d.model_dump() for d in deps],
        "readme_path": readme_path,
        "readme_claims": [c.model_dump() for c in extract_claims(readme_text)] if readme_text else [],
        "structure": detect_structure(paths).model_dump(),
        "mailmap": _show(repo, ".mailmap", 64_000) or "",
        "history": {
            "commit_count": facts.commit_count,
            "distinct_author_count": facts.contributor_count,
            "first_commit": _iso(min(a_times)) if a_times else None,
            "last_commit": _iso(max(a_times)) if a_times else None,
            "span_days": facts.author_date_span_days,
            "author_span_s": (max(a_times) - min(a_times)) if a_times else 0,
            "single_commit_import": (share > config.SINGLE_COMMIT_IMPORT_RATIO) if share is not None else None,
            "committer_dates_clustered": facts.committer_dates_clustered,
        },
    })
    return snap


def source_line_flags(entry: dict) -> list[bool]:
    return [m in SOURCE_MARKS for m in entry["marks"]]
