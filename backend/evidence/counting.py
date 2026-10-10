"""Line classification with pygount (BSD-3-Clause), used as a library.

pygount reports per-file counts: code, documentation (comments,
docstrings), string, empty. Authorship needs a per-LINE answer (blame
credits individual lines), so each line is also given the mark pygount
itself would give it, by running pygount's own line classifier on the same
decoded text with the same lexer. Those per-line marks must reconcile
exactly with pygount's ``source_count`` (code + string) and ``line_count``;
if they do not, the file is not counted.

``_line_parts`` is a private pygount function, so pygount is pinned in
requirements.txt and the reconciliation above guards against changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pygount import SourceAnalysis, SourceState
from pygount.analysis import (DEFAULT_GENERATED_NAME_PATTERNS_TEXT, DuplicatePool, _line_parts, encoding_for,
                              guess_lexer, is_markup_file)
from pygount.common import regexes_from

log = logging.getLogger(__name__)
# pygount logs a warning per unreadable file, with temp-directory paths. The
# same facts are recorded (repo-relative) in skipped_files, so keep it quiet.
logging.getLogger("pygount").setLevel(logging.ERROR)

GROUP = "crucible"
SOURCE_MARKS = frozenset("cs")  # code or string lines count as source
# pygount's own default generated-name list (lock files etc.). The library
# applies it only when passed explicitly; the CLI passes it by default.
GENERATED_NAME_REGEXES = regexes_from(DEFAULT_GENERATED_NAME_PATTERNS_TEXT)


@dataclass
class FileCount:
    path: str  # repo-relative
    state: str  # pygount SourceState name
    state_info: str | None = None
    language: str | None = None
    code: int = 0
    documentation: int = 0
    string: int = 0
    empty: int = 0
    marks: list[str] = field(default_factory=list)  # per line: c / s / d / e

    @property
    def counted(self) -> bool:
        return self.state == SourceState.analyzed.name

    @property
    def line_count(self) -> int:
        return self.code + self.documentation + self.string + self.empty

    @property
    def source_count(self) -> int:
        return self.code + self.string


def choose_encoding(disk_path: Path) -> str:
    """UTF-8 whenever the bytes are valid UTF-8 (utf-8-sig with a BOM);
    otherwise pygount's own automatic detection.

    pygount's "automatic" mode falls back to cp1252 for UTF-8 files that
    contain multi-byte characters (emoji, box-drawing, accented names), so
    real source files failed to decode and were dropped as unparseable."""
    data = disk_path.read_bytes()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return encoding_for(str(disk_path), "automatic", "cp1252")
    return "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"


def line_marks(disk_path: Path, encoding: str) -> list[str]:
    """Per-line mark exactly as pygount's from_file assigns it (the last of
    d, s, c present in the line wins; otherwise e)."""
    with open(disk_path, encoding=encoding) as fh:
        text = fh.read()
    lexer = guess_lexer(str(disk_path), text)
    marks = []
    for parts in _line_parts(lexer, text, is_markup=is_markup_file(str(disk_path))):
        mark = "e"
        for candidate in ("d", "s", "c"):
            if candidate in parts:
                mark = candidate
        marks.append(mark)
    return marks


def _relative_info(info: str | None, root: Path | None) -> str | None:
    """pygount reports temp-directory paths (e.g. a duplicate's original);
    make them repo-relative so no local filesystem path reaches the report."""
    if info is None or root is None:
        return info
    prefix = str(root).rstrip("/") + "/"
    return info.replace(prefix, "")


def count_file(disk_path: Path, rel_path: str, pool: DuplicatePool | None, root: Path | None = None) -> FileCount:
    """Analyse one file on disk. Never raises: failures become state=error."""
    try:
        encoding = choose_encoding(disk_path)
        a = SourceAnalysis.from_file(str(disk_path), GROUP, encoding=encoding, duplicate_pool=pool,
                                     generated_name_regexes=GENERATED_NAME_REGEXES)
    except Exception as exc:  # noqa: BLE001 - third-party parser on untrusted input
        return FileCount(rel_path, SourceState.error.name,
                         _relative_info(f"pygount failed: {type(exc).__name__}: {exc}", root))
    fc = FileCount(rel_path, a.state.name, _relative_info(a.state_info, root), a.language, a.code_count,
                   a.documentation_count, a.string_count, a.empty_count)
    if not fc.counted:
        return fc
    try:
        marks = line_marks(disk_path, encoding)
    except Exception as exc:  # noqa: BLE001
        return FileCount(rel_path, SourceState.error.name, _relative_info(f"per-line classification failed: {exc}", root))
    n_source = sum(1 for m in marks if m in SOURCE_MARKS)
    if len(marks) != a.line_count or n_source != a.source_count:
        return FileCount(rel_path, SourceState.error.name,
                         f"per-line marks do not reconcile with pygount (lines {len(marks)} vs {a.line_count}, "
                         f"source {n_source} vs {a.source_count})")
    fc.marks = marks
    return fc
