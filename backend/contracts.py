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


# --------------------------------------------------------------------------
# Phase 3.6: target-repository mode (two layers + question surface)
# --------------------------------------------------------------------------

TargetStatus = Literal[
    "resolved", "not_found", "access_denied", "empty_repo", "archived_ok", "too_large", "clone_failed",
    "timeout", "invalid_input", "rate_limited", "duplicate",
]


class HistoryFacts(BaseModel):
    commit_count: int
    distinct_author_count: int  # canonical (mailmapped) author emails
    first_commit: Optional[str] = None  # ISO 8601, author date
    last_commit: Optional[str] = None
    span_days: float
    single_commit_import: Optional[bool] = None
    committed_in_one_session: bool  # every commit's author date within one hour
    committer_dates_clustered: Optional[bool] = None


class Structure(BaseModel):
    top_level_dirs: list[str] = []
    entry_points: list[str] = []
    test_dirs: list[str] = []
    test_files: list[str] = []
    ci_files: list[str] = []
    container_files: list[str] = []
    migration_dirs: list[str] = []
    config_files: list[str] = []


class Dependency(BaseModel):
    manifest_path: str
    ecosystem: str
    name: str
    version_spec: Optional[str] = None
    is_direct: bool = True


class Manifest(BaseModel):
    manifest_path: str
    ecosystem: str
    dependency_count: int = 0
    parse_error: Optional[str] = None  # an unparseable manifest is recorded, never dropped


class ReadmeClaim(BaseModel):
    text: str
    kind: Literal["sentence", "bullet"]
    line: int


class RepoFacts(BaseModel):
    """Facts computed WITHOUT any identity. Present whenever status is
    resolved / archived_ok (clone-based), and partially (API-only fields)
    for too_large repos analysed in degraded mode."""

    full_name: str
    redirected_from: Optional[str] = None
    default_branch: Optional[str] = None
    ref_analyzed: Optional[str] = None
    head_sha: Optional[str] = None
    ref_requested: Optional[str] = None  # None = the default branch was requested
    scoped_path: Optional[str] = None
    visibility: Optional[str] = None
    archived: Optional[bool] = None
    size_kb: Optional[int] = None  # KiB of the packed git repository (GitHub API "size")
    created_at: Optional[str] = None
    pushed_at: Optional[str] = None
    is_fork: Optional[bool] = None
    parent_full_name: Optional[str] = None
    source_full_name: Optional[str] = None
    fork_ahead_by: Optional[int] = None
    fork_behind_by: Optional[int] = None
    fork_total_commits: Optional[int] = None
    files_truncated: Optional[bool] = None
    history: Optional[HistoryFacts] = None
    languages: Optional[dict[str, int]] = None  # language -> source lines, post-exclusion (pygount)
    source_files: Optional[int] = None
    total_source_lines: Optional[int] = None
    excluded_files_by_reason: dict[str, int] = {}
    excluded_lines_by_reason: dict[str, int] = {}
    top_excluded_files: list[ExcludedFile] = []
    structure: Optional[Structure] = None
    dependencies: list[Dependency] = []
    manifests: list[Manifest] = []
    readme_path: Optional[str] = None
    readme_claims: list[ReadmeClaim] = []


class HotFile(BaseModel):
    path: str
    authored_lines: int
    total_lines: int  # source lines in the file
    ratio: float


class CommitInfo(BaseModel):
    sha: str
    authored_date: str
    message_head: str  # first line, at most 100 characters
    files_changed: Optional[int] = None
    insertions: Optional[int] = None
    deletions: Optional[int] = None


class Authorship(BaseModel):
    """Identity-dependent attribution.

    RENDERING RULE (binding for every consumer): when
    ``authored_ratio_meaningful`` is False, a percentage must never be shown
    on its own. Render ``authorship_confidence_reason`` alongside it, because
    blame attributes every line to the committer by construction when there
    is no history to compare against.
    """

    mode: Literal["blame", "api_commits_only"]
    identity_used: dict[str, list[str]]  # {"emails": [...], "logins": [...]}
    authored_source_lines: Optional[int] = None
    total_source_lines: Optional[int] = None
    authored_ratio: Optional[float] = None
    authored_ratio_whole_repo: Optional[float] = None  # forks only; MISLEADING on a fork: includes upstream code
    authored_ratio_fork_delta: Optional[float] = None  # forks only; lines from commits ahead of the parent
    fork_delta_source_lines: Optional[int] = None
    fork_delta_reason: Optional[str] = None
    authored_ratio_meaningful: bool
    authorship_confidence: Literal["none", "low", "medium", "high"]
    authorship_confidence_reason: str
    unmatched_authors: list[UnmatchedAuthor] = []
    likely_identity_gap: Optional[bool] = None
    hot_files: list[HotFile] = []
    commits: list[CommitInfo] = []
    # api_commits_only (degraded) mode
    commit_count: Optional[int] = None
    first_commit: Optional[str] = None
    last_commit: Optional[str] = None
    files_touched: list[str] = []
    files_touched_truncated: Optional[bool] = None
    commit_message_heads: list[str] = []


class Anchor(BaseModel):
    kind: Literal["authored_function", "hot_file", "entry_point", "unverified_authorship"]
    path: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    why: str


class AuthoredFunction(BaseModel):
    path: str
    name: str
    start_line: int  # tree-sitter boundary
    end_line: int
    ccn: Optional[int] = None  # lizard cyclomatic complexity
    nloc: Optional[int] = None
    params: Optional[int] = None
    authored_line_ratio: float  # candidate-authored source lines / source lines in the function
    lizard_start_line: Optional[int] = None
    lizard_end_line: Optional[int] = None
    boundaries_agree: Optional[bool] = None  # None when lizard found no matching function


class BugTarget(AuthoredFunction):
    eligible: bool
    reason: str


class ClaimCheck(BaseModel):
    claim: str
    technologies: list[str] = []
    evidence_for: list[str] = []
    evidence_against: list[str] = []
    verdict: Literal["supported", "unsupported", "indeterminate"]


class QuestionSurface(BaseModel):
    """Structured input for Phase 4. Anchors reference ONLY candidate-authored
    code; on an unverifiable repo they are kind="unverified_authorship"."""

    anchors: list[Anchor] = []
    authored_functions: list[AuthoredFunction] = []
    boundary_disagreements: list[AuthoredFunction] = []
    bug_targets: list[BugTarget] = []
    claim_vs_code: list[ClaimCheck] = []
    coverage_gaps: list[str] = []


class TargetReport(BaseModel):
    raw_input: str
    target: dict  # backend.evidence.resolve.Target as a dict
    status: TargetStatus
    http_status: Optional[int] = None
    message: Optional[str] = None
    mode: Optional[Literal["blame", "api_commits_only"]] = None  # which measurement this report is
    ref_requested: Optional[str] = None  # what the input asked for (None = default branch)
    ref_analyzed: Optional[str] = None  # what was actually analysed (None = nothing was analysed)
    repo_facts: Optional[RepoFacts] = None
    authorship: Optional[Authorship] = None
    identity_unresolved_reason: Optional[str] = None
    question_surface: Optional[QuestionSurface] = None
    notes: list[str] = []
    cache_hit: bool = False


class InputSpec(BaseModel):
    user: Optional[str] = None
    extra_emails_count: int = 0  # emails themselves are not echoed
    # identity precedence: --email (explicit) > positional username > none
    identity_source: Optional[Literal["email", "user"]] = None
    inputs: list[dict] = []  # {raw, kind, resolved_to, warnings}
    run_mode: str  # human-readable explanation of what ran
    duplicates: list[dict] = []  # {raw, duplicate_of}


class AnalysisReport(BaseModel):
    input_spec: InputSpec
    targets: list[TargetReport]
    api_calls: int = 0
    rate_limit_remaining: Optional[int] = None
    stopped_reason: Optional[str] = None
    elapsed_s: float = 0.0
    exclusion_ruleset_version: str
