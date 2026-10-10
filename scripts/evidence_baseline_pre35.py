"""Step 0 of Phase 3.5: record the PRE-3.5 measurement for a user, so later
changes can be diffed against it. Run once, at commit 688c280 (before any
Phase 3.5 change). Deterministic; no model calls.

    python scripts/evidence_baseline_pre35.py hellstar-lab backend/evidence/baseline_pre35.json
"""

from dotenv import load_dotenv

load_dotenv()  # entry point only (GITHUB_TOKEN)

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.evidence import _gitattributes, _list_files, _text_paths, analyze_github_detailed, config  # noqa: E402
from backend.evidence.clone import cloned_repo  # noqa: E402
from backend.evidence.exclusions import classify  # noqa: E402
from backend.evidence.github_client import GitHubClient  # noqa: E402
from backend.evidence.gitutil import blob_heads, run_git  # noqa: E402


def top_source_files(clone_url: str, n: int = 15) -> list[dict]:
    """Top-n files counted as source by the pre-3.5 rules, by raw line count."""
    with cloned_repo(clone_url) as repo:
        files = _list_files(repo)
        attrs, text = _gitattributes(repo), _text_paths(repo)
        heads = blob_heads(repo, [sha for _p, size, sha in files if 0 < size <= config.MAX_BLAME_FILE_BYTES])
        cats = {p: classify(p, attrs, head=heads.get(sha), is_binary=(size > 0 and p not in text))
                for p, size, sha in files}
        counts: dict[str, int] = {}
        out = run_git(["grep", "-I", "-c", "-e", "", "HEAD"], cwd=repo).decode("utf-8", "replace")
        for line in out.splitlines():
            ref, _, num = line.rpartition(":")
            counts[ref.split(":", 1)[1]] = int(num)
        ranked = sorted(((p, c) for p, c in counts.items() if cats.get(p) == "source"), key=lambda x: (-x[1], x[0]))
        return [{"path": p, "lines": c} for p, c in ranked[:n]]


def main(username: str, out_path: str) -> int:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    result = analyze_github_detailed(username)
    with GitHubClient() as gh:
        metas = {m["name"]: m for m in gh.list_repos(username)}
    repos = []
    for r in result.repos:
        entry = {"name": r.name, "skip_reason": r.skip_reason, "total_lines": r.total_lines,
                 "code_lines": r.code_lines, "authored_lines": r.authored_lines,
                 "authored_ratio": r.authored_ratio, "source_files": r.source_files}
        if r.skip_reason is None:
            entry["top_files_by_lines"] = top_source_files(metas[r.name]["clone_url"])
        repos.append(entry)
    doc = {"measurement": "pre-phase-3.5 baseline", "username": username, "code_commit": commit,
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "counting": "raw lines (total_lines) and non-blank lines (code_lines); authored = non-blank",
           "repos": repos}
    Path(out_path).write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {out_path}: {len(repos)} repos, code at {commit[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
