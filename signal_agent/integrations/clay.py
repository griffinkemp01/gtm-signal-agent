"""Clay integration — push qualified accounts to the outbound last-mile.

The signal agent is an *awareness* tool: it scores ICP accounts and surfaces
the hot ones to Slack/HubSpot. This module is the missing last mile — when an
account crosses the alert threshold, we push it (with the validated signal
summary as personalization fuel) to a Clay webhook. Clay then does what it's
best at: waterfall contact discovery + email verification for the Segment A
persona set, a signal-keyed opener, and enrollment into HubSpot Sequences.

Design mirrors `integrations/hubspot.py` / `integrations/slack.py`:
 - narrow surface, plain-dict payloads (serializable across Inngest steps)
 - resilient: a Clay outage never breaks the alert pipeline
 - no-op when `CLAY_WEBHOOK_URL` is unset (Phase 1 / local runs can skip it)

The Clay table is wired on the Clay side; see docs/clay-last-mile.md for the
webhook payload contract, the opener prompt, and the table setup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from signal_agent.config import settings
from signal_agent.integrations.hubspot import company_record_url

log = structlog.get_logger()

# Segment A buyer set (docs/icp.md §A). Sent in the payload so the Clay table
# knows which roles to source per account — keeps the targeting in code next to
# the ICP rather than buried in Clay's UI. Broadened beyond the three core
# champions to also cover the economic buyers + the model-risk influencer, so
# each qualified account yields more reachable contacts for outbound. Trim here
# if reply rates say a role isn't converting.
SEGMENT_A_PERSONAS: list[str] = [
    # Champions
    "Head of AI / Chief AI Officer",
    "VP Data Science / Head of AI/ML",
    "CISO",
    # Influencer — owns model risk, core to Arthur's pitch
    "Head of Model Risk Management",
    # Economic buyers / technical sponsors
    "CTO / CIO",
    "Chief Data Officer",
    "VP Engineering",
]


@dataclass
class ClayAccountPayload:
    """The contract Clay receives when an account crosses the threshold.

    `signal_summary` is the agent's validated `arthur_signal_summary` — the
    "why now" the LLM already produced. Clay merges it into the opener; it is
    NOT re-generated per contact (see docs/clay-last-mile.md, §AI Research Step).
    """

    company_name: str
    company_domain: str
    hubspot_id: str | None
    target_tier: int | None
    segment: str | None
    cumulative_score: float
    tier: str                                  # "tier_1" | "tier_2" | "tier_3"
    signal_summary: str                        # personalization fuel
    triggering_signal: dict[str, Any]          # {"type", "url", "text"}
    top_signals: list[dict[str, Any]]          # up to 3 contributing signals
    # Why this account qualified for outbound (the Clay-push decision reason:
    # "first_push" | "cooldown_expired"). Outbound runs on its own, wider bar —
    # see scorer.should_push_to_clay.
    qualification_reason: str
    # Did this account ALSO clear the (stricter) Slack alert bar this run? Lets
    # Clay prioritize the hottest accounts in the sequence.
    also_alerted: bool = False
    target_personas: list[str] = field(default_factory=lambda: list(SEGMENT_A_PERSONAS))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": "qualified_account",
            # Stable per-account key — set this as the Clay table's dedupe column
            # so repeated pushes UPDATE the existing row instead of appending.
            "dedup_key": self.company_domain,
            "qualification_reason": self.qualification_reason,
            "also_alerted": self.also_alerted,
            "pushed_at": datetime.now(UTC).isoformat(),
            "company": {
                "name": self.company_name,
                "domain": self.company_domain,
                "hubspot_id": self.hubspot_id,
                "hubspot_url": company_record_url(self.hubspot_id),
                "target_tier": self.target_tier,
                "segment": self.segment,
            },
            "score": {
                "cumulative_score": self.cumulative_score,
                "tier": self.tier,
            },
            "signal_summary": self.signal_summary,
            "triggering_signal": self.triggering_signal,
            "top_signals": self.top_signals,
            "target_personas": self.target_personas,
        }


class ClayPusher:
    def __init__(self, webhook_url: str | None = None, client: httpx.Client | None = None) -> None:
        # `webhook_url=None` falls back to config; pass "" explicitly to force-disable.
        self._webhook_url = webhook_url if webhook_url is not None else settings.clay_webhook_url
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    def push(self, payload: ClayAccountPayload) -> bool:
        """POST the qualified account to the Clay webhook. Returns True on 2xx.

        No-op (returns False) when no webhook is configured. Never raises — a
        Clay failure must not abort the alert that already posted to Slack.
        """
        if not self.enabled:
            log.debug("clay.push.skipped_no_webhook", company=payload.company_domain)
            return False
        body = payload.to_dict()
        try:
            if self._client is not None:
                resp = self._client.post(self._webhook_url, json=body)
            else:
                resp = httpx.post(self._webhook_url, json=body, timeout=10.0)
            if resp.status_code >= 400:
                log.warning(
                    "clay.push.failed",
                    status=resp.status_code,
                    company=payload.company_domain,
                    body=getattr(resp, "text", "")[:300],
                )
                return False
            log.info(
                "clay.push.ok",
                company=payload.company_domain,
                qualification_reason=payload.qualification_reason,
                also_alerted=payload.also_alerted,
                tier=payload.tier,
            )
            return True
        except Exception as e:  # noqa: BLE001 — last-mile must never break the pipeline
            log.warning("clay.push.exception", company=payload.company_domain, err=str(e))
            return False
