"""HubSpot integration.

Responsibilities:
 - search for a company by domain, return hubspot id if found
 - create a company record when ICP-qualified and not present
 - write `arthur_signal_*` properties when a company's score changes
 - post a custom timeline event per fired alert so AEs see history on the record

We wrap the official `hubspot-api-client` but keep calls narrow so we can swap
transports later without touching callers. All methods return plain dicts so they
can be serialized across Inngest step boundaries.

Custom property creation is a one-time setup task — see `scripts/setup_hubspot.py`
(to be added in Phase 2). For Phase 1 the properties must exist in the portal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from hubspot import HubSpot
from hubspot.crm.companies import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.companies.exceptions import ApiException

from signal_agent.config import settings

log = structlog.get_logger()


def company_record_url(hubspot_id: str | None) -> str | None:
    """Build a clickable HubSpot company-record URL.

    Uses the portal id + the modern object-record path (`/record/0-2/<id>`,
    where `0-2` is the company object type). Falls back to the legacy
    `_`-placeholder form only when HUBSPOT_PORTAL_ID is unset — that form relies
    on HubSpot redirecting to the logged-in portal and 404s in practice, so set
    the portal id in .env.
    """
    if not hubspot_id:
        return None
    if settings.hubspot_portal_id:
        return (
            f"https://app.hubspot.com/contacts/"
            f"{settings.hubspot_portal_id}/record/0-2/{hubspot_id}"
        )
    return f"https://app.hubspot.com/contacts/_/company/{hubspot_id}"


@dataclass
class HubSpotCompany:
    id: str
    domain: str | None
    name: str | None


class HubSpotClient:
    def __init__(self, token: str | None = None) -> None:
        self._client = HubSpot(access_token=token or settings.hubspot_access_token)

    def find_company_by_domain(self, domain: str) -> HubSpotCompany | None:
        req = PublicObjectSearchRequest(
            filter_groups=[FilterGroup(filters=[
                Filter(property_name="domain", operator="EQ", value=domain)
            ])],
            properties=["domain", "name"],
            limit=1,
        )
        try:
            resp = self._client.crm.companies.search_api.do_search(public_object_search_request=req)
        except ApiException as e:
            log.warning("hubspot.search_failed", domain=domain, err=str(e))
            return None
        if not resp.results:
            return None
        c = resp.results[0]
        return HubSpotCompany(id=c.id, domain=c.properties.get("domain"), name=c.properties.get("name"))

    def create_company(self, domain: str, name: str) -> HubSpotCompany:
        payload = SimplePublicObjectInputForCreate(properties={"domain": domain, "name": name})
        c = self._client.crm.companies.basic_api.create(simple_public_object_input_for_create=payload)
        return HubSpotCompany(id=c.id, domain=domain, name=name)

    def update_signal_properties(
        self,
        hubspot_company_id: str,
        score: float,
        tier: str,
        summary: str,
        last_signal_date_iso: str,
    ) -> None:
        """Write the four custom properties defined in section 8 of the brief.

        HubSpot date properties require epoch-ms at UTC midnight — anything else
        gets rejected with INVALID_DATE. Parse the ISO string and floor to 00:00Z.
        """
        from datetime import datetime, time, timezone
        dt = datetime.fromisoformat(last_signal_date_iso.replace("Z", "+00:00"))
        midnight_utc = datetime.combine(
            dt.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc,
        )
        epoch_ms = int(midnight_utc.timestamp() * 1000)

        payload = SimplePublicObjectInput(properties={
            "arthur_signal_score": str(score),
            "arthur_signal_tier": tier,
            "arthur_signal_summary": summary,
            "arthur_last_signal_date": str(epoch_ms),
        })
        try:
            self._client.crm.companies.basic_api.update(
                company_id=hubspot_company_id, simple_public_object_input=payload
            )
        except ApiException as e:
            log.warning("hubspot.update_failed", company_id=hubspot_company_id, err=str(e))

    def emit_timeline_event(self, hubspot_company_id: str, signal_summary: dict[str, Any]) -> None:
        """Write a custom timeline event so the signal appears on the company record.

        Requires HUBSPOT_TIMELINE_APP_ID and HUBSPOT_TIMELINE_EVENT_TEMPLATE_ID to
        be configured. No-op if either is missing (Phase 1 can run without).
        """
        if not settings.hubspot_timeline_app_id or not settings.hubspot_timeline_event_template_id:
            log.debug("hubspot.timeline.skipped_no_template")
            return
        # The hubspot-api-client doesn't cover Timeline Events in all versions;
        # call the REST endpoint directly to keep things simple.
        import httpx

        url = (
            f"https://api.hubapi.com/crm/v3/timeline/{settings.hubspot_timeline_app_id}/events"
        )
        body = {
            "eventTemplateId": settings.hubspot_timeline_event_template_id,
            "objectId": hubspot_company_id,
            "tokens": signal_summary,
        }
        headers = {"Authorization": f"Bearer {settings.hubspot_access_token}"}
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=10.0)
            if r.status_code >= 400:
                log.warning("hubspot.timeline.failed", status=r.status_code, body=r.text[:300])
        except Exception as e:
            log.warning("hubspot.timeline.exception", err=str(e))
