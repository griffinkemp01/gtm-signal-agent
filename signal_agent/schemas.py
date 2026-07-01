"""Pydantic DTOs used across modules.

Kept separate from SQLAlchemy models so that ingestors, scoring, and workflow code
can pass plain-data objects across Inngest step boundaries (which serialize to JSON).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class NormalizedSignal(BaseModel):
    """A signal as produced by an ingestor, before DB insertion or scoring."""

    company_domain: str
    company_name: str
    signal_type: str              # e.g. "job_posting.ai_governance"
    source: str                   # "greenhouse" | "lever" | ...
    source_url: str
    signal_text: str              # human-readable synopsis (title + excerpt)
    raw_payload: dict
    detected_at: datetime = Field(default_factory=datetime.utcnow)

    # Optional hints from the ingestor to help the scorer without re-parsing.
    # None of these are trusted as-is; the LLM validator confirms.
    suggested_tier_hint: int | None = None
    matched_keywords: list[str] = Field(default_factory=list)

    # Stable dedup identity for the (company, signal_type, dedup_key) uniqueness
    # constraint. Defaults to source_url at insert time when None — fine for ATS
    # / SEC where the URL is stable. News sets this explicitly to a title+date
    # hash because its Google-redirect source_url changes on every fetch, which
    # otherwise re-inserts the same story as a new row each run.
    dedup_key: str | None = None


class ValidationResult(BaseModel):
    is_valid: bool
    confidence: float             # 0.0 – 1.0
    reasoning: str
    summary_for_ae: str           # "why this matters for Arthur", 1 sentence
    extracted: dict = Field(default_factory=dict)  # role title, product name, etc.


class ScoredSignal(BaseModel):
    signal_id: int
    company_id: int
    raw_score: float
    tier: str                     # "tier_1" | "tier_2" | "tier_3"


class CompanyScoreRollup(BaseModel):
    company_id: int
    cumulative_score: float
    top_tier: str
    window_days: int
    contributing_signal_ids: list[int]
