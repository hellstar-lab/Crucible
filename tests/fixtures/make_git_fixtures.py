"""Build synthetic git repositories with scripted commits from known
authors, so every authorship number has exact ground truth (EXPECTED).

Reproducible on any machine: the developer's git config is ignored
(GIT_CONFIG_GLOBAL/SYSTEM point at the null device), the branch is pinned
with ``git init -b main``, no template hooks are copied, core.autocrlf is
false, files are written as raw bytes with LF endings, and every commit sets
BOTH author and committer dates. The same script therefore yields the same
commit SHAs everywhere.

All people, emails and code are synthetic.

    python tests/fixtures/make_git_fixtures.py [out_dir]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent / "git_repos"

LOGIN, USER_ID = "alex-sample-demo", 12345678
CAND_NOREPLY = ("Alex Sample", f"{USER_ID}+{LOGIN}@users.noreply.github.com")
CAND = ("Alex Sample", "alex.sample@example.com")  # the candidate's public profile email
CAND_WORK = ("Alex Sample", "alex@work.example")  # aliases unified only by .mailmap
CAND_HOME = ("A. Sample", "alex.home@example.net")
IMPOSTOR = ("Alex Sample", "alex.sample@impostor.example")  # same name, different email: NOT the candidate
# git's hostname-derived address when user.email is unset: the domain leaks a name and device model
UNLINKED = ("Alex Sample", "alex@alexs-macbook-pro-7.local")
BOB = ("Bob Builder", "bob@example.org")
CAROL = ("Carol Coder", "carol@example.org")


def _env(author, adate, committer=None, cdate=None) -> dict[str, str]:
    committer = committer or author
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1", LC_ALL="C",
        GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_AUTHOR_DATE=adate,
        GIT_COMMITTER_NAME=committer[0], GIT_COMMITTER_EMAIL=committer[1], GIT_COMMITTER_DATE=cdate or adate,
    )
    return env


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    env = env or _env(BOB, "2024-01-01T00:00:00+00:00")
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def init(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main", "--template=")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "commit.gpgsign", "false")


def commit(repo: Path, files: dict[str, bytes], author, adate: str, committer=None, cdate: str | None = None,
           msg: str = "change") -> None:
    for rel, data in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A", env=_env(author, adate, committer, cdate))
    _git(repo, "commit", "-q", "-m", msg, env=_env(author, adate, committer, cdate))


def code(prefix: str, n: int, blank_every: int = 0) -> bytes:
    """n non-blank lines; a blank line after every ``blank_every`` lines (not at the end)."""
    out = []
    for i in range(1, n + 1):
        out.append(f"{prefix}_{i} = {i}")
        if blank_every and i % blank_every == 0 and i != n:
            out.append("")
    return ("\n".join(out) + "\n").encode()


def replace_lines(data: bytes, start: int, end: int, prefix: str) -> bytes:
    lines = data.decode().split("\n")
    for i in range(start, end + 1):
        lines[i - 1] = f"{prefix}_{i} = {i}"
    return "\n".join(lines).encode()


# --------------------------------------------------------------------------
# fixtures (each returns nothing; ground truth lives in EXPECTED)
# --------------------------------------------------------------------------


def solo_repo(repo: Path) -> None:
    init(repo)
    commit(repo, {"src/app.py": code("app", 40, blank_every=10)}, CAND_NOREPLY, "2024-01-01T10:00:00+00:00")
    commit(repo, {"src/util.py": code("util", 20)}, CAND_NOREPLY, "2024-03-01T10:00:00+00:00")


def shared_repo(repo: Path) -> None:
    init(repo)
    core = code("core", 50)
    commit(repo, {"src/core.py": core}, CAND, "2024-01-01T10:00:00+00:00")
    helpers = code("helper", 25, blank_every=10) + b"\n"  # 25 code + 3 blank
    commit(repo, {"src/core.py": replace_lines(core, 21, 30, "bob"), "src/helpers.py": helpers},
           BOB, "2024-02-01T10:00:00+00:00")
    commit(repo, {"tests/test_core.py": code("check", 25)}, CAROL, "2024-03-01T10:00:00+00:00")


def fork_like(repo: Path) -> None:
    init(repo)
    commit(repo, {"lib/big.py": code("imported", 900)}, CAND, "2024-01-01T10:00:00+00:00", msg="initial import")
    commit(repo, {"src/x.py": code("x", 50)}, CAND, "2024-01-10T10:00:00+00:00")
    commit(repo, {"src/y.py": code("y", 50)}, CAND, "2024-01-20T10:00:00+00:00")


def vendored_repo(repo: Path) -> None:
    init(repo)
    commit(repo, {
        "src/main.js": code("main", 50),
        "vendor/ours.py": code("ours", 10),  # unmarked by .gitattributes below: counts as source
        ".gitattributes": b"vendor/ours.py -linguist-vendored\ngen/** linguist-generated\n",
        "README.md": code("readme", 10),
    }, CAND, "2024-01-01T10:00:00+00:00")
    commit(repo, {
        "node_modules/leftpad/index.js": code("leftpad", 200),
        "package-lock.json": code("lock", 300),
        "static/app.min.js": b"var a=1;" * 400 + b"\n",
        "dist/bundle.js": code("bundle", 100),
        "docs/guide.md": code("guide", 30),
        "gen/api.py": code("api", 40),
        "assets/logo.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4,
        "api/user_pb2.py": code("pb", 60),
    }, BOB, "2024-02-01T10:00:00+00:00")


def mailmap_repo(repo: Path) -> None:
    init(repo)
    commit(repo, {"a.py": code("a", 10)}, CAND_WORK, "2024-01-01T10:00:00+00:00")
    commit(repo, {"b.py": code("b", 20)}, CAND_HOME, "2024-02-01T10:00:00+00:00")
    commit(repo, {"c.py": code("c", 20)}, BOB, "2024-03-01T10:00:00+00:00")
    commit(repo, {"d.py": code("d", 10)}, IMPOSTOR, "2024-04-01T10:00:00+00:00")
    mailmap = (
        f"Alex Sample <{CAND_NOREPLY[1]}> <{CAND_WORK[1]}>\n"
        f"Alex Sample <{CAND_NOREPLY[1]}> <{CAND_HOME[1]}>\n"
    ).encode()
    commit(repo, {".mailmap": mailmap}, CAND_HOME, "2024-05-01T10:00:00+00:00")


def identity_gap_repo(repo: Path) -> None:
    """The owner committed from a machine whose git email was never linked to
    their GitHub account: 0% authored, one unmatched author with 90%."""
    init(repo)
    commit(repo, {"src/app.py": code("app", 90)}, UNLINKED, "2024-01-01T10:00:00+00:00")
    commit(repo, {"src/fix.py": code("fix", 10)}, BOB, "2024-02-01T10:00:00+00:00")


def empty_repo(repo: Path) -> None:
    init(repo)


def rewritten_repo(repo: Path) -> None:
    """Author dates spread over 400 days; committer dates all within minutes,
    as left by git filter-branch / filter-repo."""
    init(repo)
    for i, adate in enumerate(["2023-01-01", "2023-05-01", "2023-09-01", "2024-02-05"]):
        commit(repo, {f"src/m{i}.py": code(f"m{i}", 25)}, CAND, f"{adate}T10:00:00+00:00",
               cdate=f"2026-01-01T10:0{i}:00+00:00")


BUILDERS = {f.__name__: f for f in (solo_repo, shared_repo, fork_like, vendored_repo, mailmap_repo,
                                    identity_gap_repo, empty_repo, rewritten_repo)}

EXPECTED: dict[str, dict] = {
    "solo_repo": dict(authored_ratio=1.0, code_lines=60, authored_lines=60, total_lines=63, source_files=2,
                      commit_count=2, contributor_count=1, tests_present=False, committer_dates_clustered=False,
                      commit_span_days=60, ranges={"src/app.py": [(1, 43)], "src/util.py": [(1, 20)]}),
    "shared_repo": dict(authored_ratio=0.4, code_lines=100, authored_lines=40, total_lines=103, source_files=3,
                        commit_count=3, contributor_count=3, tests_present=True, committer_dates_clustered=False,
                        commit_span_days=60, ranges={"src/core.py": [(1, 20), (31, 50)]}),
    "fork_like": dict(authored_ratio=1.0, code_lines=1000, authored_lines=1000, total_lines=1000, source_files=3,
                      commit_count=3, contributor_count=1, single_commit_import=True, largest_commit_share=0.9),
    "vendored_repo": dict(authored_ratio=1.0, code_lines=60, authored_lines=60, total_lines=60, source_files=2,
                          source_paths={"src/main.js", "vendor/ours.py"}),
    "mailmap_repo": dict(authored_ratio=0.5, code_lines=60, authored_lines=30, total_lines=60, source_files=4,
                         commit_count=5, contributor_count=3,
                         authored_paths={"a.py", "b.py"}),
    "identity_gap_repo": dict(authored_ratio=0.0, code_lines=100, authored_lines=0, source_files=2),
    "empty_repo": dict(skip_reason="no commits"),
    "rewritten_repo": dict(authored_ratio=1.0, committer_dates_clustered=True, author_date_span_days=400.0,
                           committer_date_span_days=round(180 / 86400, 3)),
}
CATEGORIES = {  # vendored_repo: expected classification of every file
    "src/main.js": "source", "vendor/ours.py": "source", ".gitattributes": "documentation",
    "README.md": "documentation", "node_modules/leftpad/index.js": "vendored",
    "package-lock.json": "generated", "static/app.min.js": "generated", "dist/bundle.js": "generated",
    "docs/guide.md": "documentation", "gen/api.py": "generated", "assets/logo.png": "binary",
    "api/user_pb2.py": "generated",
}


def build(name: str, out_dir: Path = DEFAULT_OUT) -> Path:
    repo = out_dir / name
    if repo.exists():
        shutil.rmtree(repo)
    BUILDERS[name](repo)
    return repo


def build_all(out_dir: Path = DEFAULT_OUT) -> dict[str, Path]:
    return {name: build(name, out_dir) for name in BUILDERS}


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    repos = build_all(out)
    for name, path in repos.items():
        try:
            head = _git(path, "rev-parse", "--short", "HEAD").strip()
        except subprocess.CalledProcessError:
            head = "(no commits)"
        print(f"{name:<16} {head}  {path}")
