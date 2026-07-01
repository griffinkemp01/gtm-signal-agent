"""Daily job-board ingestion.

Flow:
 1. `ingest_jobs_daily` runs on cron, reads ICP companies, emits one
    `ingest.jobs.company.requested` event per company.
 2. `ingest_jobs_for_company` handles each event, runs every enabled ingestor for
    that company, upserts Signal rows, and emits `signal.detected` per new or
    re-seen signal for the alert pipeline to pick up.

Fan-out is the Inngest sweet spot: per-company steps retry and rate-limit
independently, so one flaky Greenhouse slug can't block the whole run.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import inngest
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from signal_agent.db import session_scope
from signal_agent.ingestors.base import CompanyTarget
from signal_agent.ingestors.registry import enabled_ingestors
from signal_agent.models import Company, Signal, SignalStatus
from signal_agent.workflows.inngest_app import inngest_client

log = structlog.get_logger()


@inngest_client.create_function(
    fn_id="ingest_jobs_daily",
    trigger=[
        inngest.TriggerCron(cron="0 13 * * *"),  # 13:00 UTC daily
        inngest.TriggerEvent(event="ingest.jobs.daily.requested"),  # manual kick
    ],
)
async def ingest_jobs_daily(ctx: inngest.Context) -> dict:
    with session_scope() as s:
        companies = s.execute(
            select(Company).where(Company.is_icp.is_(True))
        ).scalars().all()
        # Fan out to every ICP company — each ingestor self-selects based on
        # which config fields are present (no slug → ingestor yields nothing).
        targets = [
            {
                "company_id": c.id,
                "domain": c.domain,
                "name": c.name,
                "greenhouse_slug": c.greenhouse_slug,
                "lever_slug": c.lever_slug,
                "ashby_slug": c.ashby_slug,
                "ticker": c.ticker,
                "workday": c.workday_config,
            }
            for c in companies
        ]

    # Fan out. Inngest dedupes events within a short window automatically.
    await ctx.step.send_event(
        "fan-out",
        [
            inngest.Event(
                name="ingest.jobs.company.requested",
                data=t,
                # idempotency: one run per day per company
                id=f"jobs-{t['company_id']}-{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            )
            for t in targets
        ],
    )
    return {"dispatched": len(targets)}


@inngest_client.create_function(
    fn_id="ingest_jobs_for_company",
    trigger=inngest.TriggerEvent(event="ingest.jobs.company.requested"),
    # Throttle removed in Phase 1 — the dev server has quirks with throttle keys
    # across runs. Restore (per-source, not per-function) when we move to
    # production Inngest. Greenhouse/Lever APIs are fine with concurrent fetches.
    retries=3,
)
async def ingest_jobs_for_company(ctx: inngest.Context) -> dict:
    data = ctx.event.data
    target = CompanyTarget(
        company_id=data["company_id"],
        domain=data["domain"],
        name=data["name"],
        greenhouse_slug=data.get("greenhouse_slug"),
        lever_slug=data.get("lever_slug"),
        ashby_slug=data.get("ashby_slug"),
        ticker=data.get("ticker"),
        workday=data.get("workday"),
    )

    # Fetch from all ingestors concurrently — each is independent I/O.
    async def run_one(ingestor) -> list:
        collected = []
        async for sig in ingestor.fetch_for_company(target):
            collected.append(sig)
        return collected

    results_lists = await asyncio.gather(
        *(run_one(ing) for ing in enabled_ingestors()),
        return_exceptions=True,
    )

    new_signal_ids: list[int] = []
    with session_scope() as s:
        for res in results_lists:
            if isinstance(res, Exception):
                log.warning("ingest.source_failed", err=str(res))
                continue
            for norm in res:
                # Upsert by (company_id, signal_type, dedup_key). On conflict,
                # bump last_seen_at but don't re-run validation.
                stmt = pg_insert(Signal).values(
                    company_id=target.company_id,
                    signal_type=norm.signal_type,
                    source=norm.source,
                    source_url=norm.source_url,
                    dedup_key=norm.dedup_key or norm.source_url,
                    signal_text=norm.signal_text,
                    raw_payload=norm.raw_payload,
                    status=SignalStatus.PENDING.value,  # pg_insert bypasses ORM coercion
                ).on_conflict_do_update(
                    constraint="uq_signal_dedup",
                    set_={"last_seen_at": datetime.now(timezone.utc)},
                ).returning(Signal.id, Signal.status)

                row = s.execute(stmt).first()
                if row is None:
                    continue
                signal_id, status = row
                # Only kick the pipeline for genuinely new (PENDING) rows.
                if status == SignalStatus.PENDING:
                    new_signal_ids.append(signal_id)

    # Emit signal.detected events for each new signal — alert pipeline picks up.
    if new_signal_ids:
        await ctx.step.send_event(
            "signal-detected",
            [
                inngest.Event(name="signal.detected", data={"signal_id": sid})
                for sid in new_signal_ids
            ],
        )

    return {"company_id": target.company_id, "new_signals": len(new_signal_ids)}
