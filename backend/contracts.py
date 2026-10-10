"""Data contracts shared across every Crucible pipeline stage.

FROZEN: changing any contract requires updating every module that uses it,
so treat these field names as frozen. Add new optional fields rather than
renaming or retyping existing ones.

Pipeline order:
    Claims          — shield (step 2) + extracted resume claims
    EvidenceReport  — claims checked against code the candidate authored
    Challenge       — a validated bug planted in one of their functions
    SessionLog      — what the candidate / AI stranger did while solving it
    FinalReport     — evidence for a human interviewer; never hire/reject
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

# --------------------------------------------------------------------------
# Claims (step 2: shield + claim extraction)
# --------------------------------------------------------------------------


class HiddenFragment(BaseModel):
    """A run of text a machine can read but a human cannot see.

    ``page`` is 1-based; 0 means the fragment came from document metadata
    rather than a page. ``bbox`` is (left, bottom, right, top) in PDF points.
    """

    text: str
    page: int
    bbox: tuple[float, float, float, float]
    reasons: list[str]
    detection_method: str
    # The three fields below are set only by the removed classifier experiment
    # (experiments/classifier, docs/classifier-experiment.md). The shipping
    # path never sets them; severity is judged by the human reviewer.
    classification: Optional[
        Literal["benign_residue", "fabricated_qualification", "ai_instruction"]
    ] = None
    classification_confidence: Optional[float] = None
    classification_failed: bool = False


class ShieldSection(BaseModel):
    """Deterministic, offline detection result. ``hidden_text_found`` means
    fragments (with their recovered text and detector reasons) are returned
    for human review; no severity judgement is made."""

    status: Literal["clean", "hidden_text_found"]
    hidden_fragments: list[HiddenFragment]
    page_stats: dict
    degraded: bool = False  # experiment only (see HiddenFragment); always False when shipping


class Claim(BaseModel):
    id: str
    type: Literal["skill", "project", "experience", "education"]
    subject: str
    assertion: str
    strength: Optional[str]
    source_line: Optional[int]


class Claims(BaseModel):
    candidate_id: str
    shield: ShieldSection
    claims: list[Claim]


# --------------------------------------------------------------------------
# EvidenceReport (step 3: claims vs authored code)
# --------------------------------------------------------------------------


class FileAuthorship(BaseModel):
    """Candidate authorship of one source file (only files with
    authored_lines > 0 are recorded). Line numbers are 1-based, inclusive.
    Line classes are pygount's: code, documentation (comments, docstrings),
    string, empty; source = code + string."""

    path: str
    total_lines: int  # pygount line_count (every line)
    code_lines: int  # pygount code_count
    documentation_lines: int = 0  # pygount documentation_count
    string_lines: int = 0  # pygount string_count
    empty_lines: int = 0  # pygount empty_count
    source_lines: int = 0  # code + string: the basis for authorship
    authored_lines: int  # candidate-authored SOURCE lines
    authored_ranges: list[tuple[int, int]]  # merged across gaps of non-source lines
    committer_emails: dict[str, int] = {}  # REDACTED committer email -> authored lines (attribution stays on author)


class ExcludedFile(BaseModel):
    path: str
    lines: int  # raw lines (0 for binaries)
    reason: str  # one of the excluded_*_by_reason keys


class SkippedFile(BaseModel):
    """A file pygount did not analyse (state != analyzed), with its reason."""

    path: str
    state: str  # pygount SourceState: duplicate, generated, empty, binary, unknown, error
    state_info: Optional[str] = None


class UnmatchedAuthor(BaseModel):
    """An author of surviving source lines who did not match the candidate's
    identity set. The email is redacted (first 3 characters + domain): enough
    for the candidate to recognise their own address, never a full email of a
    third party."""

    email_redacted: str  # "aur...@gmail.com", or "abh...@<local-machine>" for hostname-derived domains
    name: str
    authored_lines: int  # non-blank source lines at HEAD
    commit_count: int
    reason: Optional[str] = None  # e.g. "user.email not configured on the committing machine"


class RepoInfo(BaseModel):
    """Phase 3 facts about one repository. No field is a judgement.

    ``authored_ratio`` = candidate-authored SOURCE lines / SOURCE lines, where
    source = pygount code + string lines in countable files (comments and
    blank lines are not evidence of engineering). ``None`` always means "not
    measured", never zero: every numeric field is None on a skipped repo
    (``skip_reason`` set), and every line count and the ratio are None when
    no countable file survives exclusion.
    """

    name: str
    authored_ratio: Optional[float] = None
    builds: Optional[bool] = None  # None = not evaluated (Phase 3 cannot know)
    tests_present: Optional[bool] = None  # None = not checked (skipped repo), never "no tests"
    commit_span_days: Optional[int] = None  # first to last commit, by author date
    language: Optional[str] = None
    is_fork: Optional[bool] = None
    commit_count: Optional[int] = None
    contributor_count: Optional[int] = None
    single_commit_import: Optional[bool] = None
    largest_commit_share: Optional[float] = None  # largest commit's added source lines / current source lines
    author_date_span_days: Optional[float] = None
    committer_date_span_days: Optional[float] = None
    committer_dates_clustered: Optional[bool] = None  # history-rewrite signal; a fact, not proof. None = not evaluated
    source_files: Optional[int] = None  # countable files (0 when everything was excluded)
    total_lines: Optional[int] = None  # pygount line_count over countable files
    code_lines: Optional[int] = None  # pygount code_count
    documentation_lines: Optional[int] = None
    string_lines: Optional[int] = None
    empty_lines: Optional[int] = None
    source_lines: Optional[int] = None  # code + string: the ratio's denominator
    authored_lines: Optional[int] = None  # candidate-authored source lines
    skip_reason: Optional[str] = None
    notes: list[str] = []  # partial-analysis facts, e.g. files skipped over a cap
    files: list[FileAuthorship] = []
    excluded_files_by_reason: dict[str, int] = {}  # every reason key, 0 included
    excluded_lines_by_reason: dict[str, int] = {}
    top_excluded_files: list[ExcludedFile] = []  # up to 10, most lines first
    skipped_files: list[SkippedFile] = []  # pygount non-analysed files (capped)
    unmatched_authors: list[UnmatchedAuthor] = []  # analysed repos only; top 10 by authored_lines
    # Data shape of "same person, unlinked git identity": owned, not a fork,
    # 0% authored, one unmatched author > 80% of source lines. A fact, not a
    # conclusion. None = not evaluated (skipped, or ownership unknown).
    likely_identity_gap: Optional[bool] = None


class Finding(BaseModel):
    claim_id: str
    status: Literal["verified", "partial", "no_evidence"]
    confidence: float
    justification: str
    citations: list[str]


class EvidenceReport(BaseModel):
    candidate_id: str
    repositories: list[RepoInfo]
    findings: list[Finding]


# --------------------------------------------------------------------------
# Challenge (step 4: planted bug)
# --------------------------------------------------------------------------


class Challenge(BaseModel):
    challenge_id: str
    repo: str
    snapshot_uri: str
    buggy_file: str
    buggy_function: str
    bug_strategy: str
    bug_class: str
    hidden_tests: list[str]
    time_limit_minutes: int
    validation: dict


# --------------------------------------------------------------------------
# SessionLog (step 5: candidate and AI-stranger attempts)
# --------------------------------------------------------------------------


class SessionLog(BaseModel):
    session_id: str
    challenge_id: str
    actor: Literal["candidate", "ai_stranger"]
    events: list[dict]
    derived: dict
    rationale_answer: Optional[str]


# --------------------------------------------------------------------------
# FinalReport (step 6: evidence for a human; never a hire/reject verdict)
# --------------------------------------------------------------------------


class FinalReport(BaseModel):
    candidate_id: str
    generated_at: datetime
    analyzer_version: str
    skills: list[dict]
    challenge_result: dict
    familiarity: dict
    questions: list[str]
    red_flags: list[str]
    bias_self_check: dict
    cost: dict
