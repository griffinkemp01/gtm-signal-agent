"""Environment-driven configuration.

All runtime knobs live here so secrets never end up in code and scoring thresholds
can be tuned without redeploying. Loaded once, imported everywhere via `settings`.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env BEFORE pydantic-settings reads its defaults. override=True makes the
# file authoritative for local dev — this saves us from silent mis-configurations
# when a shell has a stale ANTHROPIC_API_KEY etc. exported. Disable by setting
# SIGNAL_AGENT_DOTENV_OVERRIDE=0 when deploying to an environment where the real
# env is authoritative (prod containers, CI, etc.).
_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if os.environ.get("SIGNAL_AGENT_DOTENV_OVERRIDE", "1") == "1":
    load_dotenv(_DOTENV_PATH, override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    database_url: str = Field(default="postgresql+psycopg://signal:signal@localhost:5432/signal_agent")
    log_level: str = "INFO"
    env: str = "dev"

    # Ingestion — comma-separated ingestor `source` names to SKIP (e.g.
    # "competitive,conference"). Useful to drop sources that rate-limit or add
    # little value at scale. `competitive` hits Reddit, which 429s aggressively
    # on unauthenticated search across a large account list.
    disabled_ingestors: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250929"

    # HubSpot
    hubspot_access_token: str = ""
    hubspot_timeline_app_id: str = ""
    hubspot_timeline_event_template_id: str = ""
    # Portal (hub) id — needed to build working company-record URLs. Without it
    # we fall back to the legacy `_`-placeholder URL, which HubSpot no longer
    # reliably resolves (the "Open in HubSpot" link 404s). Find it via
    # GET https://api.hubapi.com/account-info/v3/details (field: portalId).
    hubspot_portal_id: str = ""

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_alert_channel: str = "#gtm-signals-test"
    slack_owner_user_id: str = ""

    # Clay — outbound last-mile. When an account qualifies we POST it (with the
    # validated signal summary) to this Clay webhook, which handles contact
    # discovery + email verification + HubSpot Sequence enrollment. Leave blank
    # to disable the push (Phase 1 / local runs).
    clay_webhook_url: str = ""
    # Outbound is DECOUPLED from Slack alerting and runs on a wider, lower bar:
    # any account whose cumulative (or single-signal) score clears this pushes
    # to Clay even if it never fires a Slack alert. Set well below
    # alert_cumulative_threshold to cast a wider outbound net.
    clay_push_score_threshold: float = 6.0
    # Outbound re-touch cadence: once pushed, an account isn't re-pushed for
    # this many days (don't re-enroll the same contacts every run). Much longer
    # than the Slack alert cooldown — outbound sequences run for weeks.
    clay_push_cooldown_days: int = 30

    # Inngest
    inngest_app_id: str = "signal-agent"
    inngest_event_key: str = ""
    inngest_signing_key: str = ""
    inngest_dev: bool = True

    # Scoring
    # Cap how many signals of the SAME signal_type contribute to a company's
    # cumulative score within the window. Stops one newsworthy event (which can
    # spawn 40+ near-identical articles) from dominating the score — we count
    # the top-N highest-scoring items of each type and ignore the long tail.
    score_max_signals_per_type: int = 3
    alert_score_threshold: int = 8
    alert_cumulative_threshold: int = 12
    alert_cumulative_window_days: int = 60
    llm_confidence_floor: float = 0.7
    circuit_breaker_alerts_per_hour: int = 20
    # Digest mode: when alert rate exceeds this in the last hour, non-Tier-1
    # alerts are stashed for the next `flush_digest` run instead of posted live.
    digest_rate_threshold: int = 5
    digest_flush_interval_minutes: int = 15

    # Per-company cooldown: once a company is alerted, suppress further alerts
    # for this many hours UNLESS a material change fires (see ratio below).
    alert_cooldown_hours: int = 24
    # A new signal breaks cooldown early when it pushes the cumulative score
    # above the last-alerted score by this fraction (0.5 = +50%). Tier-1
    # signal types (news.ai_incident, job_posting.ai_*) bypass cooldown
    # regardless — those are always-alert per the build brief.
    alert_material_change_ratio: float = 0.5
    # Same-signal-type cooldown: separate (longer) window than the
    # general material-change cooldown. Prevents the Slack channel from
    # seeing "another product launch at Stripe" daily — even once the
    # 24h general cooldown expires, the same signal_type can't re-alert
    # for this many days after the prior alert. Covers both always-alert
    # types and the non-urgent path.
    alert_same_type_cooldown_days: int = 7

    # LLM cache TTL for validation results. Must be >= alert_cumulative_window_days
    # so signals inside the scoring window never re-prompt the LLM. Default
    # gives 7 days of headroom.
    llm_cache_ttl_days: int = 67

    # Arthur tracing (OpenTelemetry → Arthur Engine OTLP /v1/traces endpoint).
    # When enabled, Anthropic LLM calls, HTTP fetches, and pipeline-stage spans
    # are exported to your Arthur Engine. See docs/arthur-tracing.md.
    arthur_tracing_enabled: bool = False
    arthur_engine_base_url: str = "http://localhost:3030"
    arthur_engine_api_key: str = ""
    arthur_task_id: str = ""
    arthur_service_name: str = "signal-agent"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
