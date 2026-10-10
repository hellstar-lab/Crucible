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

ENUM DESIGN RULE (measured, not guessed: docs/nebius-capabilities.md, FEEDBACK.md #7)

Under a strict enum the GENERATED value is not the model's answer. Measured with logprobs (sequence probability of
each value, summed over its tokenisations):
  * Prefix lock-in: told the honest verdict is "unclear" but offered only supported | unsupported |
    insufficient_evidence, all three tiers start the value with the token "un" at p=1.0, and the only allowed value
    continuing "un" is "unsupported". The abstention was suppressed by a shared prefix, not by vocabulary.
  * The generated value can be the minority: offered "unclear", nano generated it at p=0.302 while "unsupported" held
    0.696 (shared first token "un" 0.62 beat "uns" 0.38; then "clear" 0.48 beat "supported" 0.33).
  * Abstention can be present and invisible: ultra put 0.454 on "insufficient_evidence" and generated "unsupported".
An earlier version of this rule ("name the abstain value in the model's own vocabulary") was drawn from generated
values alone and was wrong.

So: every model-filled enum gives its values DISTINCT FIRST CHARACTERS (the judgement verdict is
supported | contradicted | no_evidence | inconclusive); llm.call re-ranks enum fields over full sequence
probabilities and records the distribution (shares among grammar-allowed tokens, not unconstrained preference); and
every prompt defines every label without hinting which applies. A constrained enum still returns some allowed value
with no error ("Passionate about clean code" was typed "metric" by an enum with no fitting value), so an enum that
does not cover the space manufactures an answer.

Add an abstain value only when the enum does not already cover the space. Adding one to a complete partition
creates coercion in the opposite direction. (Measured, Phase 4A: with "unclear" offered next to
cannot_be_disambiguated, nano chose "unclear" for "Improved it by 40%" while writing cannot_be_disambiguated's
exact reasoning. The 4A stage enums are complete partitions and carry no abstain value.)

"inconclusive" exists ONLY in the 4C judgement enum. Every stage-outcome enum is a complete partition and gets no
abstain value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class CrucibleModel(BaseModel):
    """Base of every Crucible contract. ``extra="forbid"`` makes the GENERATED JSON schema carry
    ``"additionalProperties": false``. Measured (T4): Nebius strict mode enforces that keyword, but only when
    the schema contains it, and pydantic leaves it out by default, so without this base the protection T4
    verified would be absent from our own schemas. It also rejects unknown keys when validating."""

    model_config = ConfigDict(extra="forbid")

# --------------------------------------------------------------------------
# Claims (step 2: shield + claim extraction)
# --------------------------------------------------------------------------


Severity = Literal["info", "low", "medium", "high"]


class SecurityFinding(CrucibleModel):
    """One finding from any document detector, PDF or DOCX (a single shape so
    downstream code never branches on format). ``evidence`` holds structured
    facts only; ``reconstructed_text`` is the hidden/mismatched text, capped at
    2000 characters. A finding is a FACT with a severity, never a verdict.
    Severity "medium" and "high" roll up to status "suspicious"; "low" and
    "info" are reported but do not change the status."""

    detector: str
    severity: Severity
    page: Optional[int] = None  # 1-based; None for document-level findings
    evidence: dict = {}
    reconstructed_text: Optional[str] = None


class HiddenFragment(CrucibleModel):
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


class ShieldSection(CrucibleModel):
    """Deterministic, offline detection result. ``hidden_text_found`` means
    fragments (with their recovered text and detector reasons) are returned
    for human review; no severity judgement is made."""

    status: Literal["clean", "suspicious", "hidden_text_found"]
    hidden_fragments: list[HiddenFragment]
    page_stats: dict
    degraded: bool = False  # experiment only (see HiddenFragment); always False when shipping
    # Phase 2C (additive): box-aware geometry findings and fail-toward-suspicion bookkeeping
    findings: list[SecurityFinding] = []
    needs_review: bool = False  # a detector errored or the document was truncated: never "clean"
    truncated: bool = False


class Claim(CrucibleModel):
    id: str
    type: Literal["skill", "project", "experience", "education", "other"]  # "other": none of these (G8 audit)
    subject: str
    assertion: str
    strength: Optional[str]
    source_line: Optional[int]


class Claims(CrucibleModel):
    candidate_id: str
    shield: ShieldSection
    claims: list[Claim]


# --------------------------------------------------------------------------
# EvidenceReport (step 3: claims vs authored code)
# --------------------------------------------------------------------------


class FileAuthorship(CrucibleModel):
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


class ExcludedFile(CrucibleModel):
    path: str
    lines: int  # raw lines (0 for binaries)
    reason: str  # one of the excluded_*_by_reason keys


class SkippedFile(CrucibleModel):
    """A file pygount did not analyse (state != analyzed), with its reason."""

    path: str
    state: str  # pygount SourceState: duplicate, generated, empty, binary, unknown, error
    state_info: Optional[str] = None


class UnmatchedAuthor(CrucibleModel):
    """An author of surviving source lines who did not match the candidate's
    identity set. The email is redacted (first 3 characters + domain): enough
    for the candidate to recognise their own address, never a full email of a
    third party."""

    email_redacted: str  # "aur...@gmail.com", or "abh...@<local-machine>" for hostname-derived domains
    name: str
    authored_lines: int  # non-blank source lines at HEAD
    commit_count: int
    reason: Optional[str] = None  # e.g. "user.email not configured on the committing machine"


class RepoInfo(CrucibleModel):
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


class Finding(CrucibleModel):
    claim_id: str
    # no_evidence = searched and found nothing; unclear = cannot determine (split per the G8 audit)
    status: Literal["verified", "partial", "no_evidence", "unclear"]
    confidence: float
    justification: str
    citations: list[str]


class EvidenceReport(CrucibleModel):
    candidate_id: str
    repositories: list[RepoInfo]
    findings: list[Finding]


# --------------------------------------------------------------------------
# Challenge (step 4: planted bug)
# --------------------------------------------------------------------------


class Challenge(CrucibleModel):
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


class SessionLog(CrucibleModel):
    session_id: str
    challenge_id: str
    actor: Literal["candidate", "ai_stranger"]
    events: list[dict]
    derived: dict
    rationale_answer: Optional[str]


# --------------------------------------------------------------------------
# FinalReport (step 6: evidence for a human; never a hire/reject verdict)
# --------------------------------------------------------------------------


class FinalReport(CrucibleModel):
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


class HistoryFacts(CrucibleModel):
    commit_count: int
    distinct_author_count: int  # canonical (mailmapped) author emails
    first_commit: Optional[str] = None  # ISO 8601, author date
    last_commit: Optional[str] = None
    span_days: float
    single_commit_import: Optional[bool] = None
    committed_in_one_session: bool  # every commit's author date within one hour
    committer_dates_clustered: Optional[bool] = None


class Structure(CrucibleModel):
    top_level_dirs: list[str] = []
    entry_points: list[str] = []
    test_dirs: list[str] = []
    test_files: list[str] = []
    ci_files: list[str] = []
    container_files: list[str] = []
    migration_dirs: list[str] = []
    config_files: list[str] = []


class Dependency(CrucibleModel):
    manifest_path: str
    ecosystem: str
    name: str
    version_spec: Optional[str] = None
    is_direct: bool = True


class Manifest(CrucibleModel):
    manifest_path: str
    ecosystem: str
    dependency_count: int = 0
    parse_error: Optional[str] = None  # an unparseable manifest is recorded, never dropped


class ReadmeClaim(CrucibleModel):
    text: str
    kind: Literal["sentence", "bullet"]
    line: int


class RepoFacts(CrucibleModel):
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


class HotFile(CrucibleModel):
    path: str
    authored_lines: int
    total_lines: int  # source lines in the file
    ratio: float


class CommitInfo(CrucibleModel):
    sha: str
    authored_date: str
    message_head: str  # first line, at most 100 characters
    files_changed: Optional[int] = None
    insertions: Optional[int] = None
    deletions: Optional[int] = None


class Authorship(CrucibleModel):
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


class Anchor(CrucibleModel):
    kind: Literal["authored_function", "hot_file", "entry_point", "unverified_authorship"]
    path: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    why: str


class AuthoredFunction(CrucibleModel):
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


class ClaimCheck(CrucibleModel):
    claim: str
    technologies: list[str] = []
    evidence_for: list[str] = []
    evidence_against: list[str] = []
    verdict: Literal["supported", "unsupported", "unclear"]  # "unclear" (was "indeterminate"): the abstain word models use (G5)


class QuestionSurface(CrucibleModel):
    """Structured input for Phase 4. Anchors reference ONLY candidate-authored
    code; on an unverifiable repo they are kind="unverified_authorship"."""

    anchors: list[Anchor] = []
    authored_functions: list[AuthoredFunction] = []
    boundary_disagreements: list[AuthoredFunction] = []
    bug_targets: list[BugTarget] = []
    claim_vs_code: list[ClaimCheck] = []
    coverage_gaps: list[str] = []


class TargetReport(CrucibleModel):
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


class InputSpec(CrucibleModel):
    user: Optional[str] = None
    extra_emails_count: int = 0  # emails themselves are not echoed
    # identity precedence: --email (explicit) > positional username > none
    identity_source: Optional[Literal["email", "user"]] = None
    inputs: list[dict] = []  # {raw, kind, resolved_to, warnings}
    run_mode: str  # human-readable explanation of what ran
    duplicates: list[dict] = []  # {raw, duplicate_of}


class AnalysisReport(CrucibleModel):
    input_spec: InputSpec
    targets: list[TargetReport]
    api_calls: int = 0
    rate_limit_remaining: Optional[int] = None
    stopped_reason: Optional[str] = None
    elapsed_s: float = 0.0
    exclusion_ruleset_version: str



# --------------------------------------------------------------------------
# Phase 2C: document analyzer (Shield -> structured evidence, zero model calls)
#
# Every unmeasured field is None. Not 0.0, not False, not "": None means
# "not measured", and a page that needs OCR has every text-derived field None.
# --------------------------------------------------------------------------


class DocumentInfo(CrucibleModel):
    path: str  # file name only
    format: Literal["pdf", "docx"]
    pages: Optional[int] = None  # pages analysed
    pages_total: Optional[int] = None  # pages in the file (differs from ``pages`` when truncated)
    encrypted: bool = False
    per_page_kind: Optional[list[str]] = None  # text_pdf | image_only | slide_deck | sparse | blank, per page
    per_page_stats: Optional[list[dict]] = None  # {page, chars, images, width, height, body_size}
    has_form: Optional[bool] = None  # AcroForm with fields
    resume_confidence: Optional[float] = None
    truncated: bool = False  # a cap (pages, bytes, seconds) was hit: the result is partial
    truncation_reasons: list[str] = []


class Provenance(CrucibleModel):
    producer: Optional[str] = None
    creator: Optional[str] = None
    author: Optional[str] = None
    created: Optional[str] = None  # ISO 8601; None when the date string cannot be parsed
    modified: Optional[str] = None
    mod_minus_create_days: Optional[float] = None
    date_order_invalid: Optional[bool] = None  # ModDate earlier than CreationDate (impossible)
    xmp_created: Optional[str] = None
    xmp_modified: Optional[str] = None
    # DOCX only
    last_modified_by: Optional[str] = None
    revision: Optional[int] = None
    total_editing_minutes: Optional[int] = None
    creator_differs_from_last_modified_by: Optional[bool] = None


class Heading(CrucibleModel):
    text: str
    level: int  # 1 = largest
    page: int
    size: Optional[float] = None
    method: Literal["size", "bold", "numbering", "style"]


class Section(CrucibleModel):
    heading: Optional[str] = None  # None for text before the first heading
    level: Optional[int] = None
    page_start: int
    page_end: int
    char_start: int  # offsets into the cleaned full text
    char_end: int
    text: str


class Table(CrucibleModel):
    page: int
    caption: Optional[str] = None
    strategy: Literal["lines", "text_region", "docx"]
    n_rows: int
    n_cols: int
    rows: list[list[str]]


class LinkRef(CrucibleModel):
    url: str
    host: Optional[str] = None
    kind: str  # github | gitlab | linkedin | kaggle | huggingface | devpost | email | other
    sources: list[str]  # "annotation" and/or "text"
    page: Optional[int] = None
    display_text: Optional[str] = None
    mismatch: bool = False
    github_account: Optional[str] = None
    github_target: Optional[str] = None  # "owner/repo": consumable by the Phase 3.6 resolver


class NumericClaim(CrucibleModel):
    value: str
    unit: str
    match: str
    context_before: str  # verbatim, up to 60 characters
    context_after: str
    section: Optional[str] = None
    page: Optional[int] = None


class ReferencesBlock(CrucibleModel):
    section_found: bool
    entries: list[str]
    dois: list[str]
    arxiv_ids: list[str]


class StructureBlock(CrucibleModel):
    headings: list[Heading]
    sections: list[Section]
    tables: Optional[list[Table]] = None  # None when no page could be measured for tables
    links: list[LinkRef]
    numeric_claims: list[NumericClaim]
    references: ReferencesBlock
    body_size: Optional[float] = None
    notes: list[str] = []


class ContactBlock(CrucibleModel):
    emails: list[str]
    phones: list[str]
    urls: list[str]


class TimelineEntry(CrucibleModel):
    raw: str
    start: Optional[str] = None  # ISO date, earliest it could mean; None when unparseable (never invented)
    end: Optional[str] = None  # ISO date, latest it could mean
    start_precision: Optional[str] = None  # day | month | year | season
    end_precision: Optional[str] = None
    is_current: bool = False
    section: Optional[str] = None
    context: str = ""  # the line the range came from
    confidence: float


class Contradiction(CrucibleModel):
    kind: Literal["overlapping_full_time_roles", "end_before_start", "credential_before_education",
                  "multiple_present_roles", "experience_exceeds_span"]
    detail: str
    cites: list[str]  # the raw strings the contradiction comes from


class SkillMention(CrucibleModel):
    skill: str  # canonical name from the lexicon
    matched: str  # text as written in the document
    exact: bool
    score: float
    section: Optional[str] = None


class ResumeBlock(CrucibleModel):
    contact_block: ContactBlock
    timeline: list[TimelineEntry]
    timeline_contradictions: list[Contradiction]
    skills_mentioned: list[SkillMention]


class ExtractionQuality(CrucibleModel):
    chars_total: Optional[int] = None  # None when every page needs OCR
    chars_unmapped_ratio: Optional[float] = None  # > 0.3: downstream text is unreliable
    mojibake_fixed: Optional[bool] = None
    ocr_required_pages: list[int] = []
    pages_with_no_text: list[int] = []


class DocumentReport(CrucibleModel):
    """Everything the analyzer found in one document. ``status`` is a roll-up of
    the findings, never a verdict: clean | suspicious | hidden_text_found."""

    document: DocumentInfo
    status: Literal["clean", "suspicious", "hidden_text_found"]
    needs_review: bool = False  # a detector errored or the work was truncated: never reported as clean
    detector_errors: list[dict] = []
    security_findings: list[SecurityFinding] = []
    hidden_fragments: list[HiddenFragment] = []
    provenance: Optional[Provenance] = None
    font_inventory: Optional[list[dict]] = None  # PDF only: per page, fonts used / subset prefix / embedded
    structure: Optional[StructureBlock] = None
    resume: Optional[ResumeBlock] = None
    extraction_quality: ExtractionQuality
    warnings: list[str] = []


# --------------------------------------------------------------------------
# Phase 4A: claim extraction (Claimify stages 1-4 + checkability routing). Extraction only: no verdicts.
# --------------------------------------------------------------------------

ClaimKind = Literal["technology_use", "ownership", "artifact_exists", "scale", "timeline", "metric", "role_or_team",
                    "outcome", "other"]
Checkability = Literal["yes", "no", "conditional", "weak"]
ExclusionStage = Literal["shield", "split", "selection", "disambiguation", "decomposition", "support", "budget"]


class StageDecision(CrucibleModel):
    """One stage decision. In 4A there is no evidence_sufficient override (that is the 4C judgement contract), so
    evidence_sufficient / sufficiency_reason / disagreement stay at their unmeasured defaults; tier "pre-gate" means
    Stage 3 resolved the unit deterministically without a call."""
    stage: Literal["selection", "disambiguation", "decomposition"]
    tier: str
    evidence_sufficient: Optional[bool] = None
    sufficiency_reason: Optional[str] = None
    model_verdict: Optional[str] = None  # what the model wrote
    verdict: str  # the stage outcome (4A has no override; the 4C judgement override yields "inconclusive")
    reason: Optional[str] = None
    disagreement: bool = False  # model wrote a non-unclear verdict alongside evidence_sufficient=False
    key_order: Optional[list[str]] = None  # top-level keys in the order the raw reply listed them
    prompt_hash: Optional[str] = None
    cost_usd: Optional[float] = None
    reply_sha256: Optional[str] = None  # hash of the raw reply text: byte-identity across runs
    # P2-1: sequence probability of every enum value (shares among grammar-allowed tokens; None = not measured)
    value_distribution: Optional[dict] = None
    greedy_disagreement: bool = False  # re-ranking over values changed the generated answer
    samples: Optional[list[dict]] = None  # P2-3 Stage 3 ensemble: every sampled resolution (auditable disagreement)


class ClaimUnit(CrucibleModel):
    unit_id: str
    text: str
    source_span: tuple[int, int]  # [start, end) into ClaimExtraction.document_text
    source_section: Optional[str] = None
    heading_path: list[str] = []
    kind: Literal["bullet", "sentence", "entry_heading", "line"]
    bullet_id: Optional[str] = None  # sentences of one bullet share it
    parent_bullet_id: Optional[str] = None  # set on a sub-bullet
    decisions: list[StageDecision] = []
    selected_text: Optional[str] = None  # Stage 2 output (the verifiable part when "mixed")
    dropped_text: Optional[str] = None  # what Stage 2 removed
    clarified_text: Optional[str] = None  # Stage 3 output


class ExcludedUnit(CrucibleModel):
    unit_id: str
    source_span: tuple[int, int]
    source_section: Optional[str] = None
    stage: ExclusionStage
    reason_code: Literal["no_verifiable_claims", "cannot_be_disambiguated", "model_refused", "unparseable_response",
                         "fabricated_quantity", "fabricated_entity", "injection_suspected", "qualifier_dropped", "shield_payload",
                         "rubric_leak", "ungrounded_qualifier",
                         "contact_info", "cost_cap", "model_unavailable"]
    detail: str


class ExtractedClaim(CrucibleModel):
    claim_id: str
    unit_id: str
    text: str
    qualifiers: list[str] = []
    source_span: tuple[int, int]  # the unit's span: every claim traces to document text
    source_section: Optional[str] = None
    kind: ClaimKind
    secondary_kinds: list[ClaimKind] = []
    checkable_from_code: Checkability
    evidence_kind: Optional[str] = None  # which repo field would settle it; None when nothing in a repo can
    why_not_checkable: Optional[str] = None  # set whenever checkable_from_code is "no"
    checkability_note: Optional[str] = None  # for "weak" / "conditional"
    support_ratio: Optional[float] = None  # diagnostic only, never a gate
    decomposition_tier: str


class ShieldGate(CrucibleModel):
    applied: bool  # the document carried findings or hidden fragments, so extraction ran on cleaned text only
    document_status: Literal["clean", "suspicious", "hidden_text_found"]
    needs_review: bool = False
    detectors: list[str] = []  # security finding detectors present
    hidden_fragments: int = 0
    units_removed: int = 0  # units that still contained a hidden-fragment string (defence in depth)


class ClaimExtraction(CrucibleModel):
    """Phase 4A output. Facts about what the resume asserts and how each assertion could be checked. No verdicts."""
    document: str
    document_text: Optional[str] = None  # the cleaned text every source_span indexes into; None when nothing was extracted
    shield: ShieldGate
    units: Optional[list[ClaimUnit]] = None  # None = not extracted (e.g. no text layer), never [] for "did not look"
    claims: Optional[list[ExtractedClaim]] = None
    excluded: Optional[list[ExcludedUnit]] = None
    counters: dict[str, int] = {}
    tiers: dict[str, str] = {}
    calls: int = 0
    cost_usd: Optional[float] = None
    cost_cap_usd: float
    truncated: bool = False
    truncation_reason: Optional[str] = None
    not_extracted_reason: Optional[str] = None
    # judgements where the model wrote a non-"inconclusive" verdict next to evidence_sufficient=false / all judgements
    sufficiency_disagreement_rate: Optional[float] = None
    key_order_follows_schema_rate: Optional[float] = None  # replies whose top-level keys came in schema order
