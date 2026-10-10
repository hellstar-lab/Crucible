"""One repository as retrieval sees it: the 3.6 TargetReport, its cached identity-free snapshot, an optional local checkout.

Read-only use of Phase 3.5/3.6 output:
  * the TargetReport (repo_facts, authorship, question_surface);
  * the snapshot cached by backend.evidence.cache, keyed by (full_name, head_sha, scoped_path, ruleset): countable files
    with per-line marks and commits, the 3.5 exclusion list, imports, functions, manifests;
  * a local checkout, ONLY to read verbatim excerpts with ``git show <head_sha>:<path>``. Never fetched, never mutated.

ELIGIBILITY (asserted, see ``assert_eligible``): evidence may only come from files that survived the 3.5 exclusion rules
and are in scope. Vendored, generated, minified and documentation files, binaries and duplicates are never evidence.

AUTHORSHIP: per-file authorship is recomputed from the snapshot's per-line commits with the candidate identity passed in
(the report stores emails redacted, so it cannot be rebuilt from the report), and then CHECKED against the report's
own authored_source_lines. If the totals disagree the identity was not reproduced, and every item says so with
authored_by_candidate = None rather than a guess.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from backend.contracts import TargetReport
from backend.evidence import cache
from backend.evidence.authorship import line_authorship
from backend.evidence.identity import IdentitySet
from backend.evidence.snapshot import in_scope

from . import config


class ExclusionViolation(AssertionError):
    """An excluded or out-of-scope file reached the evidence. Excluded code is never evidence."""


@dataclass
class RepoView:
    report: TargetReport
    snap: dict | None
    checkout: Path | None = None
    identity: IdentitySet | None = None
    notes: list[str] = field(default_factory=list)
    _auth: dict | None = None
    _identity_ok: bool | None = None
    _texts: dict = field(default_factory=dict)

    # ---- identity-free facts -------------------------------------------------------------------------------
    @property
    def full_name(self) -> str:
        return self.report.repo_facts.full_name if self.report.repo_facts else self.report.target.get("full_name", "?")

    @property
    def head_sha(self) -> str | None:
        return self.report.repo_facts.head_sha if self.report.repo_facts else None

    @property
    def scoped_path(self) -> str | None:
        return self.report.repo_facts.scoped_path if self.report.repo_facts else None

    @property
    def excluded_paths(self) -> set[str]:
        out = {e[0] if isinstance(e, (list, tuple)) else e.get("path") for e in (self.snap or {}).get("excluded", [])}
        out |= {s["path"] for s in (self.snap or {}).get("skipped_files", []) if s.get("state") in config.UNUSABLE_SKIP_STATES}
        return out

    @property
    def eligible_paths(self) -> list[str]:
        """In scope and not excluded by the 3.5 rules (nor generated / binary / duplicate)."""
        if not self.snap:
            return []
        bad = self.excluded_paths
        return [p for p in self.snap.get("files_in_scope", []) if p not in bad and in_scope(p, self.scoped_path)]

    @property
    def counted(self) -> dict:
        return (self.snap or {}).get("counted", {})

    def eligible(self, path: str | None) -> bool:
        return path is None or (path in set(self.eligible_paths))

    # ---- authorship ------------------------------------------------------------------------------------------
    @property
    def authorship(self):
        return self.report.authorship

    @property
    def authorship_confidence(self) -> str | None:
        return self.authorship.authorship_confidence if self.authorship else None

    @property
    def likely_identity_gap(self) -> bool | None:
        return self.authorship.likely_identity_gap if self.authorship else None

    def _file_auth(self) -> dict | None:
        if self._auth is None and self.snap and self.identity is not None and self.counted:
            self._auth = line_authorship(self.snap, self.identity)
            mine = sum(1 for flags in self._auth.values() for src, a in flags if src and a)
            reported = self.authorship.authored_source_lines if self.authorship else None
            self._identity_ok = reported is not None and mine == reported
            if not self._identity_ok:
                self.notes.append(f"{self.full_name}: identity not reproduced offline (recomputed {mine} authored source lines, "
                                  f"the report says {reported}); authored_by_candidate is None on every item")
        return self._auth

    def file_ratio(self, path: str) -> float | None:
        flags = (self._file_auth() or {}).get(path)
        if not flags:
            return None
        src = [a for s, a in flags if s]
        return round(sum(src) / len(src), 4) if src else None

    def authored(self, path: str | None, start: int | None = None, end: int | None = None) -> tuple[bool | None, float | None, str | None]:
        """(authored_by_candidate, authored_ratio for the file, caveat). Decided here, never inferred later."""
        if self.authorship is None:
            return None, None, "no candidate identity was given, so authorship was not computed"
        if self.authorship.mode == "api_commits_only":
            return None, None, "repository analysed in api_commits_only mode: no line-level authorship"
        flags = (self._file_auth() or {}).get(path) if path else None
        ratio = self.file_ratio(path) if path else None
        gap = ("likely_identity_gap: 0% authored but one unmatched author wrote most lines; possibly the candidate under an "
               "email not linked to their account (verify before reading a False here)") if self.likely_identity_gap else None
        if self.authorship_confidence == "none":
            return None, ratio, f"authorship_confidence none: {self.authorship.authorship_confidence_reason}"
        if self.identity is None:
            return None, ratio, "no candidate identity was passed to retrieval, so per-file authorship was not recomputed"
        if self._identity_ok is False:
            return None, ratio, "identity not reproduced offline; see EvidenceSet.notes"
        if not flags:
            return None, None, (path and "file is not countable source, so it was never blamed") or None
        if start is not None:
            span = flags[start - 1:(end or start)]
            if not span:
                return None, ratio, "line range outside the blamed file"
            return any(a for _s, a in span), ratio, gap
        return (ratio or 0) > 0, ratio, gap

    def line_flags(self, path: str) -> list[tuple[bool, bool]] | None:
        """Per line (is_source, authored_by_candidate) from blame, or None (not countable, no identity, or the identity
        was not reproduced). Used by 4D's c_authored check."""
        flags = (self._file_auth() or {}).get(path)
        return flags if (flags and self._identity_ok) else None

    def line_count(self, path: str) -> int | None:
        """Lines in the file at head_sha: from the snapshot when the file was counted, else from the checkout."""
        entry = self.counted.get(path)
        if entry is not None:
            return len(entry["marks"])
        t = self.text(path)
        return None if t is None else len(t.splitlines())

    def in_snapshot(self, path: str) -> bool | None:
        """Does the path exist in the analysed tree (scope applied)? None when there is no snapshot to ask."""
        return None if not self.snap else path in set(self.snap.get("files_in_scope", []))

    # ---- verbatim text ---------------------------------------------------------------------------------------
    def text(self, path: str) -> str | None:
        if path in self._texts:
            return self._texts[path]
        out = None
        if self.checkout and self.head_sha:
            try:
                r = subprocess.run(["git", "show", f"{self.head_sha}:{path}"], cwd=self.checkout, capture_output=True, timeout=30)
                if r.returncode == 0 and len(r.stdout) <= config.MAX_TEXT_BYTES:
                    out = r.stdout.decode("utf-8", "replace")
            except (OSError, subprocess.TimeoutExpired):
                out = None
        self._texts[path] = out
        return out

    def excerpt(self, path: str, start: int, end: int | None = None) -> str | None:
        t = self.text(path)
        if t is None:
            return None
        lines = t.splitlines()
        return "\n".join(lines[start - 1:(end or start)])[: config.EXCERPT_MAX_CHARS]


def assert_eligible(view: RepoView, items) -> None:
    """Hard check: no item may point at an excluded or out-of-scope file."""
    allowed = set(view.eligible_paths)
    for it in items:
        if it.path is not None and it.path not in allowed:
            raise ExclusionViolation(f"{view.full_name}:{it.path} is excluded or out of scope but reached the evidence ({it.rule})")


def load_view(report: TargetReport, *, snapshot_root: Path | None = None, checkout: Path | None = None,
              identity: IdentitySet | None = None) -> RepoView:
    """Build a RepoView; a missing snapshot is recorded, never silently ignored."""
    snap, notes = None, []
    rf = report.repo_facts
    if rf and rf.head_sha:
        snap = cache.load(rf.full_name, rf.head_sha, rf.scoped_path, root=snapshot_root)
        if snap is None:
            notes.append(f"{rf.full_name}@{rf.head_sha[:8]}: no cached snapshot (analysis not cached, or the exclusion ruleset "
                         "changed since): only the TargetReport facts were searched")
    if checkout is not None:
        ok = rf and rf.head_sha and subprocess.run(["git", "cat-file", "-e", f"{rf.head_sha}^{{commit}}"], cwd=checkout,
                                                    capture_output=True).returncode == 0
        if not ok:
            notes.append(f"{report.raw_input}: checkout {checkout} does not contain head_sha; excerpts unavailable")
            checkout = None
    return RepoView(report=report, snap=snap, checkout=checkout, identity=identity, notes=notes)
