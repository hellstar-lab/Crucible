"""Full-history bare clones of untrusted repositories, always cleaned up.

Never shallow: a --depth clone makes blame attribute everything to the
boundary commit. Cost is controlled with size and time caps instead. Bare
clones mean no checkout, so no smudge filters or LFS downloads run.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Iterator

from . import config
from .errors import GitError
from .gitutil import run_git

log = logging.getLogger(__name__)


def _force_remove(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)  # git pack files are read-only
    func(path)


def dir_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total / 1_048_576


@contextlib.contextmanager
def cloned_repo(url: str, *, allow_local: bool = False) -> Iterator[Path]:
    """Yield the path of a full-history bare clone of ``url``; the temp
    directory is removed afterwards, including on failure or timeout.
    ``allow_local`` permits file paths / file:// (tests only)."""
    tmp = Path(tempfile.mkdtemp(prefix="crucible-clone-"))
    dest = tmp / "repo.git"
    try:
        extra = ("-c", "protocol.file.allow=" + ("always" if allow_local else "never"))
        run_git(["clone", "--bare", "--quiet", "--no-tags", "--single-branch", url, str(dest)],
                timeout=config.CLONE_TIMEOUT_S, extra_config=extra)
        if run_git(["rev-parse", "--is-shallow-repository"], cwd=dest, timeout=30).strip() != b"false":
            raise GitError("clone is shallow; blame would be meaningless")
        yield dest
    finally:
        shutil.rmtree(tmp, onerror=_force_remove)
