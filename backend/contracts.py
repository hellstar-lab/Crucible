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


class RepoInfo(BaseModel):
    name: str
    authored_ratio: float
    builds: bool
    tests_present: bool
    commit_span_days: int


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
