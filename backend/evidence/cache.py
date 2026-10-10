"""On-disk cache of identity-free snapshots.

Key: (full_name, head_sha, scoped_path, exclusion_ruleset_version). The
ruleset version hashes everything that changes what is counted (the
vendored Linguist data, the pinned pygount / lizard / tree-sitter versions,
the snapshot schema and the exclusion caps), so any change invalidates
every entry. Snapshots hold no identity, so a hit is safe for any identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
from importlib.metadata import version
from pathlib import Path

from . import config
from .exclusions import LINGUIST_DIR
from .snapshot import SNAPSHOT_VERSION

log = logging.getLogger(__name__)
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".cache" / "evidence"


def ruleset_version() -> str:
    h = hashlib.sha256()
    for name in ("vendor.yml", "documentation.yml"):
        h.update((LINGUIST_DIR / name).read_bytes())
    for pkg in ("pygount", "lizard", "tree-sitter-language-pack"):
        h.update(f"{pkg}={version(pkg)}".encode())
    h.update(f"snapshot={SNAPSHOT_VERSION};max_bytes={config.MAX_BLAME_FILE_BYTES};"
             f"max_files={config.MAX_SOURCE_FILES_PER_REPO}".encode())
    return h.hexdigest()[:16]


def _path(full_name: str, head_sha: str, scoped_path: str | None, ruleset: str, root: Path) -> Path:
    key = json.dumps([full_name.lower(), head_sha, scoped_path or "", ruleset])
    return root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def load(full_name: str, head_sha: str, scoped_path: str | None, root: Path | None = None) -> dict | None:
    path = _path(full_name, head_sha, scoped_path, ruleset_version(), root or CACHE_DIR)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(snap: dict, full_name: str, head_sha: str, scoped_path: str | None, root: Path | None = None) -> None:
    path = _path(full_name, head_sha, scoped_path, ruleset_version(), root or CACHE_DIR)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap))
    except OSError as exc:
        log.warning("cache write failed: %s", exc)
