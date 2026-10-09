"""Run git with an isolated, hostile-input-safe environment.

Every git invocation in Phase 3 goes through ``run_git`` so results never
depend on the developer's own git config (mailmap.file, blame.ignoreRevsFile,
core.autocrlf, ...) and untrusted repositories cannot run hooks or prompt.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from pathlib import Path

from .errors import GitError, GitTimeout

SAFE_CONFIG = (
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "core.autocrlf=false",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.ext.allow=never",
)


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
        GIT_ASKPASS="echo",
        LC_ALL="C",
    )
    return env


def run_git(args: list[str], cwd: Path | None = None, timeout: float | None = None,
            extra_config: tuple[str, ...] = ()) -> bytes:
    """Run ``git <args>`` and return stdout bytes. Kills the whole process
    group on timeout so helper processes (git-remote-https) cannot linger."""
    cmd = ["git", *SAFE_CONFIG, *extra_config, *args]
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=git_env(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise GitTimeout(f"git {args[0]} exceeded {timeout}s") from exc
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} failed ({proc.returncode}): {err.decode(errors='replace').strip()[:300]}")
    return out


def has_commits(repo: Path) -> bool:
    try:
        run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=repo, timeout=30)
        return True
    except GitError:
        return False


def blob_heads(repo: Path, shas: list[str], n: int = 600, timeout: float = 120) -> dict[str, bytes]:
    """First ``n`` bytes of each blob, streamed through one ``git cat-file
    --batch`` process so memory stays bounded whatever the blob sizes."""
    out: dict[str, bytes] = {}
    if not shas:
        return out
    cmd = ["git", *SAFE_CONFIG, "cat-file", "--batch"]
    proc = subprocess.Popen(cmd, cwd=repo, env=git_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert proc.stdin and proc.stdout
        for sha in shas:
            proc.stdin.write(sha.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                continue
            remaining, head = int(header[2]) + 1, b""  # +1 for the trailing newline
            while remaining:
                chunk = proc.stdout.read(min(remaining, 65536))
                if not chunk:
                    break
                if len(head) < n:
                    head += chunk[: n - len(head)]
                remaining -= len(chunk)
            out[sha] = head
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()  # type: ignore[union-attr]
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    return out
