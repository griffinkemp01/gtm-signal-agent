"""ORM models.

Schema follows the brief's section 4–5: a common `Signal` row per detected event,
a `Company` row keyed on domain (ICP seed or discovered), `Alert` rows for audit,
and a `Suppression` table for hard-coded false-positive patterns.

Design notes:
- `Signal.dedup_key` is `(company_id, signal_type, source_url)`. Re-ingesting the
  same posting updates `last_seen_at` and leaves `detected_at` alone.
- `Signal.raw_payload` stores the normalized source row (JSON). We keep the
  raw artifact in S3 for 90 days per the brief — not implemented in Phase 1.
- `Company.hubspot_id` is nullable: we may detect a signal before the company
  exists in HubSpot, and we only create it on account resolution.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SignalTier(str, enum.Enum):
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3 = "tier_3"


class SignalStatus(str, enum.Enum):
    PENDING = "pending"          # ingested, not yet validated
    VALIDATED = "validated"      # passed LLM validation
    REJECTED = "rejected"        # LLM rejected as false positive
    REVIEW = "review"            # below confidence floor, human-held
    SUPPRESSED = "suppressed"    # matched suppression rule


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    hubspot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    greenhouse_slug: Mapped[str | None] = mapped_column(String(128))
    lever_slug: Mapped[str | None] = mapped_column(String(128))
    ashby_slug: Mapped[str | None] = mapped_column(String(128))
    ticker: Mapped[str | None] = mapped_column(String(16))  # SEC EDGAR (Phase 2)
    workday_config: Mapped[dict | None] = mapped_column(JSON)  # {tenant, pod, portal}
    # Arthur-specific tiering (docs/icp.md §7). Used as a score multiplier so
    # a given signal at a Tier 1 account outweighs the same signal at Tier 3.
    target_tier: Mapped[int] = mapped_column(Integer, default=2)
    segment: Mapped[str | None] = mapped_column(String(4))  # "A" | "B" | "C"
    is_icp: Mapped[bool] = mapped_column(Boolean, default=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-company cooldown tracking. After each alert:
    #   last_alerted_at    = when we most recently posted a Slack alert
    #   last_alerted_score = cumulative score AT THAT MOMENT
    # Used by should_alert() to suppress re-alerts unless a material change fires.
    last_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_alerted_score: Mapped[float | None] = mapped_column(Float)
    # Clay outbound push tracking. Decoupled from Slack alerting: an account can
    # be pushed to the outbound last-mile (lower bar) without ever firing a
    # Slack alert. Own cooldown so we don't re-enroll an account's contacts on
    # every pipeline run. Orthogonal to last_alerted_at / last_alerted_score.
    last_clay_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_clay_pushed_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    signals: Mapped[list["Signal"]] = relationship(back_populates="company")


class Signal(Base):
    __tablename__ = "signals"
    # Dedup on a STABLE key, not source_url — news source_urls (Google redirect
    # links) change every fetch, which re-inserted the same story as a new row
    # each run and inflated cumulative scores. See dedup_key below.
    __table_args__ = (
        UniqueConstraint("company_id", "signal_type", "dedup_key", name="uq_signal_dedup"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    signal_type: Mapped[str] = mapped_column(String(64), index=True)   # e.g. "job_posting.ai_governance"
    source: Mapped[str] = mapped_column(String(32))                    # "greenhouse" | "lever" | ...
    source_url: Mapped[str] = mapped_column(String(1024))              # human-clickable link (may change)
    # Stable dedup identity: source_url for stable-URL sources (ATS/SEC), a
    # title+date hash for news. Populated at ingest (defaults to source_url).
    dedup_key: Mapped[str] = mapped_column(String(1024))
    signal_text: Mapped[str] = mapped_column(Text)                     # job title + key excerpt
    raw_payload: Mapped[dict] = mapped_column(JSON)

    # values_callable makes SQLAlchemy send `.value` (lowercase) to PG, matching
    # the enum type created in the migration. Without this, the default behavior
    # sends member names (uppercase) and PG rejects them.
    status: Mapped[SignalStatus] = mapped_column(
        Enum(
            SignalStatus,
            name="signal_status",
            values_callable=lambda obj: [e.value for e in obj],
        ),
        default=SignalStatus.PENDING,
        index=True,
    )
    llm_confidence: Mapped[float | None] = mapped_column(Float)
    llm_reasoning: Mapped[str | None] = mapped_column(Text)
    llm_summary: Mapped[str | None] = mapped_column(Text)              # "why this matters for Arthur"
    raw_score: Mapped[float] = mapped_column(Float, default=0.0)
    tier: Mapped[SignalTier | None] = mapped_column(
        Enum(SignalTier, name="signal_tier",
             values_callable=lambda obj: [e.value for e in obj]),
    )

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    company: Mapped[Company] = relationship(back_populates="signals")


class Alert(Base):
    """One row per fired alert. Audit trail + input to conversion metrics."""
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    triggering_signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    cumulative_score: Mapped[float] = mapped_column(Float)
    tier: Mapped[SignalTier] = mapped_column(
        Enum(SignalTier, name="signal_tier",
             values_callable=lambda obj: [e.value for e in obj]),
    )
    slack_channel: Mapped[str] = mapped_column(String(128))
    slack_ts: Mapped[str | None] = mapped_column(String(64))
    claimed_by: Mapped[str | None] = mapped_column(String(64))          # Slack user id
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Suppression(Base):
    """Hard suppression rules — pattern matched against signal_text or company name."""
    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(255))
    field: Mapped[str] = mapped_column(String(32))   # "signal_text" | "company_name"
    reason: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMCache(Base):
    """Hash(signal_text) → validation result. 30-day TTL via `expires_at`."""
    __tablename__ = "llm_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    result_json: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompetitorCustomer(Base):
    """Cache of ICP companies confirmed as customers of Arthur competitors.

    Populated by `quality.competitor_customers.refresh_cache()`. The alert
    pipeline consults this before firing — a company flagged at or above the
    confidence floor gets `suppressed_competitor_customer` instead of alerting.
    """
    __tablename__ = "competitor_customers"
    __table_args__ = (
        UniqueConstraint("company_id", "competitor",
                         name="uq_competitor_customer_company_comp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    competitor: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    evidence_url: Mapped[str] = mapped_column(String(1024))
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_override: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DigestItem(Base):
    """An alert queued for batched digest delivery.

    Populated when alert-rate exceeds DIGEST_RATE_THRESHOLD/hour and the alert
    isn't Tier 1. `flush_digest` drains these into a grouped Slack post.
    """
    __tablename__ = "digest_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), unique=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    flushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CircuitBreakerEvent(Base):
    """Records tripped-circuit-breaker events for visibility."""
    __tablename__ = "circuit_breaker_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    tripped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    alert_count: Mapped[int] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
