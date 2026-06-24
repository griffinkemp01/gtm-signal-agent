"""Direct pipeline runner — bypass Inngest for local verification.

Runs the same ingest → validate → score → alert pipeline as the Inngest
workflow, but synchronously, so we can see exactly what happens to each signal.

Usage:
    .venv/bin/python -m scripts.run_pipeline
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

# Initialize Arthur tracing BEFORE importing any module that uses Anthropic
# or httpx — the instrumentors wrap those libraries at import time.
from signal_agent.observability import tracing as _tracing

_tracing.initialize()

import structlog  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from signal_agent.accounts.resolver import AccountResolver  # noqa: E402
from signal_agent.config import settings  # noqa: E402
from signal_agent.db import session_scope  # noqa: E402
from signal_agent.ingestors.base import CompanyTarget  # noqa: E402
from signal_agent.ingestors.registry import enabled_ingestors  # noqa: E402
from signal_agent.integrations.clay import ClayAccountPayload, ClayPusher  # noqa: E402
from signal_agent.integrations.hubspot import HubSpotClient, company_record_url  # noqa: E402
from signal_agent.integrations.slack import AlertContext, SlackAlerter  # noqa: E402
from signal_agent.models import Alert, Company, Signal, SignalStatus  # noqa: E402
from signal_agent.observability.tracing import stage_span  # noqa: E402
from signal_agent.quality import (  # noqa: E402
    circuit_breaker,
    competitor_customers,
    digest,
    suppression,
)
from signal_agent.schemas import NormalizedSignal  # noqa: E402
from signal_agent.scoring import scorer  # noqa: E402
from signal_agent.scoring.validator import validate_signal  # noqa: E402

log = structlog.get_logger()


async def ingest_company(target: CompanyTarget) -> list[int]:
    """Fetch + upsert signals for one company. Returns IDs of newly-ingested signals.

    Wrapped in a span so each company's ingestion (across all ingestors) shows
    up as a separate trace in Arthur, with HTTP calls nested under it.
    """
    async def run_one(ing):
        collected = []
        async for s in ing.fetch_for_company(target):
            collected.append(s)
        return collected

    with stage_span(
        "ingest_company",
        company_id=target.company_id,
        company_name=target.name,
        domain=target.domain,
    ):
        results = await asyncio.gather(
            *(run_one(ing) for ing in enabled_ingestors()),
            return_exceptions=True,
        )

    new_ids: list[int] = []
    with session_scope() as s:
        for res in results:
            if isinstance(res, Exception):
                log.warning("ingest.source_failed", err=str(res))
                continue
            for norm in res:
                stmt = pg_insert(Signal).values(
                    company_id=target.company_id,
                    signal_type=norm.signal_type,
                    source=norm.source,
                    source_url=norm.source_url,
                    signal_text=norm.signal_text,
                    raw_payload=norm.raw_payload,
                    status=SignalStatus.PENDING.value,
                ).on_conflict_do_update(
                    constraint="uq_signal_dedup",
                    set_={"last_seen_at": datetime.now(UTC)},
                ).returning(Signal.id, Signal.status)
                row = s.execute(stmt).first()
                if row is None:
                    continue
                sid, status = row
                if status == SignalStatus.PENDING:
                    new_ids.append(sid)
    return new_ids


def process_signal(signal_id: int, per_run_alerted_companies: set[int] | None = None) -> dict:
    """Full alert pipeline for one signal.

    `per_run_alerted_companies` is a caller-owned set of company_ids that have
    already been alerted within THIS pipeline invocation. When a company is
    in the set, subsequent signals for it are suppressed with outcome
    `suppressed_already_alerted_this_run`. This is a hard cap on top of the
    persistent cooldown — it prevents the Slack channel from getting three
    alerts for the same company in 40 seconds when many queued signals all
    push the cumulative score above thresholds during one processing loop.

    Wrapped in a parent OTel span so the whole pipeline shows up as a single
    trace in Arthur. LLM and HTTP calls auto-instrument as child spans.
    """
    with stage_span("process_signal", signal_id=signal_id) as parent_span:
        result = _process_signal_inner(signal_id, per_run_alerted_companies)
        # Record outcome on the parent span so Arthur's Trace Viewer can
        # filter/group by it.
        parent_span.set_attribute("signal_agent.outcome", result.get("outcome", "error"))
        for k in ("tier", "cumulative", "alert_reason", "reason"):
            if k in result and result[k] is not None:
                parent_span.set_attribute(f"signal_agent.{k}", str(result[k]))
        return result


def _process_signal_inner(
    signal_id: int, per_run_alerted_companies: set[int] | None = None,
) -> dict:
    with session_scope() as s:
        sig: Signal = s.get(Signal, signal_id)
        if sig is None:
            return {"signal_id": signal_id, "error": "not found"}

        # Attach company context to the current span so traces are filterable.
        parent = _tracing.get_tracer()  # noqa: F841 (side-effect init)
        from opentelemetry import trace
        span = trace.get_current_span()
        span.set_attribute("signal_agent.company_id", sig.company_id)
        span.set_attribute("signal_agent.company_name", sig.company.name)
        span.set_attribute("signal_agent.signal_type", sig.signal_type)
        span.set_attribute("signal_agent.target_tier", sig.company.target_tier or 2)

        # 1. Suppression
        norm = NormalizedSignal(
            company_domain=sig.company.domain,
            company_name=sig.company.name,
            signal_type=sig.signal_type,
            source=sig.source,
            source_url=sig.source_url,
            signal_text=sig.signal_text,
            raw_payload=sig.raw_payload,
            detected_at=sig.detected_at,
        )
        with stage_span("suppression_check"):
            suppressed, reason = suppression.is_suppressed(s, norm)
        if suppressed:
            sig.status = SignalStatus.SUPPRESSED
            sig.llm_reasoning = f"suppressed: {reason}"
            return {"signal_id": signal_id, "outcome": "suppressed", "reason": reason}

        # 1b. Competitor-customer disqualification.
        with stage_span("competitor_customer_check") as cc_span:
            cc_status = competitor_customers.is_competitor_customer(s, sig.company_id)
            cc_span.set_attribute("signal_agent.is_competitor_customer", cc_status.is_customer)
            if cc_status.is_customer:
                cc_span.set_attribute("signal_agent.competitors",
                                      ", ".join(cc_status.competitors))
        if cc_status.is_customer:
            sig.status = SignalStatus.SUPPRESSED
            sig.llm_reasoning = (
                f"suppressed: known customer of "
                f"{', '.join(cc_status.competitors)}"
            )
            return {
                "signal_id": signal_id,
                "outcome": "suppressed_competitor_customer",
                "competitors": cc_status.competitors,
                "confidence": cc_status.confidence,
                "evidence_url": cc_status.evidence_url,
            }

        # 2. LLM validation (Anthropic auto-instrumented — captures prompt,
        # completion, tokens, model, latency automatically).
        with stage_span("llm_validation"):
            result = validate_signal(norm)
        sig.llm_confidence = result.confidence
        sig.llm_reasoning = result.reasoning
        sig.llm_summary = result.summary_for_ae

        if not result.is_valid:
            sig.status = SignalStatus.REJECTED
            return {"signal_id": signal_id, "outcome": "rejected", "reasoning": result.reasoning[:120]}
        if result.confidence < settings.llm_confidence_floor:
            sig.status = SignalStatus.REVIEW
            return {"signal_id": signal_id, "outcome": "review", "confidence": result.confidence}
        sig.status = SignalStatus.VALIDATED

        # 3. Score + alert decision
        with stage_span("score_and_decide") as dec_span:
            scorer.update_signal_score(s, sig)
            rollup = scorer.cumulative_company_score(s, sig.company_id)
            decision = scorer.should_alert(rollup, sig, sig.company, session=s)
            dec_span.set_attribute("signal_agent.raw_score", sig.raw_score)
            dec_span.set_attribute("signal_agent.cumulative", rollup.cumulative_score)
            dec_span.set_attribute("signal_agent.decision_reason", decision.reason)
            dec_span.set_attribute("signal_agent.should_fire", decision.should_fire)

        score_info = {
            "raw_score": sig.raw_score,
            "tier": sig.tier.value if sig.tier else None,
            "cumulative": rollup.cumulative_score,
            "alert_reason": decision.reason,
            "delta_vs_last": decision.delta_vs_last,
        }

        # Contributing-signal context — built once here, reused by the Clay
        # push (below) and the Slack alert (further down).
        recent = (
            s.query(Signal)
            .filter(Signal.id.in_(rollup.contributing_signal_ids))
            .order_by(Signal.detected_at.desc())
            .limit(3)
            .all()
        )
        top_signals = [
            {"type": r.signal_type, "url": r.source_url,
             "text": r.signal_text.split("\n", 1)[0][:120]}
            for r in recent
        ]

        # 3a. Outbound last-mile — DECOUPLED from Slack alerting. Pushes the
        # account to Clay on a wider, lower bar (CLAY_PUSH_SCORE_THRESHOLD) with
        # its own cooldown, so qualified-but-not-alert-worthy accounts still
        # feed contact discovery + sequencing. Runs regardless of the Slack
        # alert decision below. No-op when CLAY_WEBHOOK_URL is unset.
        clay_decision = scorer.should_push_to_clay(rollup, sig, sig.company)
        clay_outcome = clay_decision.reason
        if clay_decision.should_push:
            with stage_span("clay_push") as clay_span:
                pushed = ClayPusher().push(ClayAccountPayload(
                    company_name=sig.company.name,
                    company_domain=sig.company.domain,
                    hubspot_id=sig.company.hubspot_id,
                    target_tier=sig.company.target_tier,
                    segment=sig.company.segment,
                    cumulative_score=rollup.cumulative_score,
                    tier=rollup.top_tier,
                    signal_summary=sig.llm_summary or "",
                    triggering_signal={"type": sig.signal_type, "url": sig.source_url,
                                       "text": sig.signal_text.split("\n", 1)[0][:120]},
                    top_signals=top_signals,
                    qualification_reason=clay_decision.reason,
                    also_alerted=decision.should_fire,
                ))
                clay_span.set_attribute("signal_agent.clay_pushed", pushed)
            if pushed:
                scorer.mark_clay_pushed(sig.company, rollup.cumulative_score)
            clay_outcome = "pushed" if pushed else f"not_pushed_{clay_decision.reason}"

        if not decision.should_fire:
            return {
                "signal_id": signal_id,
                "outcome": f"suppressed_{decision.reason}",
                "clay": clay_outcome,
                **score_info,
            }

        # 3b. Per-run hard cap — one alert per company per pipeline run.
        # The persistent cooldown still governs cross-run re-alerts; this
        # is a stricter intra-run guard that prevents the channel flooding
        # when many queued signals push the cumulative score up in bursts.
        if per_run_alerted_companies is not None and sig.company_id in per_run_alerted_companies:
            return {
                "signal_id": signal_id,
                "outcome": "suppressed_already_alerted_this_run",
                **score_info,
            }

        # 4. Account resolution (HubSpot calls are httpx → auto-instrumented)
        with stage_span("account_resolution"):
            resolver = AccountResolver()
            hubspot_id = resolver.resolve(s, sig.company)

        # 5. Circuit breaker
        if circuit_breaker.is_tripped(s):
            circuit_breaker.record_trip(s, settings.circuit_breaker_alerts_per_hour)
            SlackAlerter().notify_circuit_breaker(settings.circuit_breaker_alerts_per_hour)
            return {"signal_id": signal_id, "outcome": "alert_skipped_circuit_breaker"}

        # 6. Snooze check
        if sig.company.snoozed_until and sig.company.snoozed_until > datetime.now(UTC):
            return {"signal_id": signal_id, "outcome": "alert_skipped_snoozed"}

        # 7. Fire alert (always persist Alert row for audit/metrics)
        alert = Alert(
            company_id=sig.company_id,
            triggering_signal_id=sig.id,
            cumulative_score=rollup.cumulative_score,
            tier=sig.tier,
            slack_channel=settings.slack_alert_channel,
        )
        s.add(alert)
        s.flush()

        # Register with the per-run hard cap + persistent cooldown immediately
        # on Alert creation — applies whether we post live OR queue for digest.
        # (Previously this only ran after live Slack post, which let two
        # near-simultaneous signals each create a digest-queued Alert for the
        # same company in a single run.)
        scorer.mark_alerted(sig.company, rollup.cumulative_score)
        if per_run_alerted_companies is not None:
            per_run_alerted_companies.add(sig.company_id)

        # (top_signals + the Clay push were handled in step 3a above — the Clay
        # push is decoupled from alerting and already fired for this account.)

        # 7b. Digest-mode gating — bursty non-Tier-1 alerts get batched.
        if digest.should_batch(s, sig.tier):
            digest.enqueue(s, alert)
            return {
                "signal_id": signal_id,
                "outcome": "queued_for_digest",
                "alert_id": alert.id,
                **score_info,
            }

        hubspot_url = company_record_url(sig.company.hubspot_id)
        ctx = AlertContext(
            company_name=sig.company.name,
            company_domain=sig.company.domain,
            cumulative_score=rollup.cumulative_score,
            tier=rollup.top_tier,
            summary_for_ae=sig.llm_summary or "",
            top_signals=top_signals,
            hubspot_url=hubspot_url,
            owner_name=None,
            deal_stage=None,
            alert_id=alert.id,
        )
        with stage_span("slack_post"):
            ts = SlackAlerter().post_alert(ctx)
        alert.slack_ts = ts
        # (cooldown + per-run cap were registered earlier at Alert creation
        # so they also apply to digest-queued alerts.)

        if sig.company.hubspot_id:
            with stage_span("hubspot_write"):
                hs = HubSpotClient()
                hs.update_signal_properties(
                    hubspot_company_id=sig.company.hubspot_id,
                    score=rollup.cumulative_score,
                    tier=rollup.top_tier,
                    summary=sig.llm_summary or "",
                    last_signal_date_iso=sig.detected_at.astimezone(UTC).isoformat(),
                )

        return {
            "signal_id": signal_id,
            "outcome": "alerted",
            "alert_id": alert.id,
            "slack_ts": ts,
            **score_info,
        }


async def main() -> int:
    print(f"=== SignalAgent pipeline run — {datetime.utcnow().isoformat()}Z ===\n")

    # Phase 1: ingest all ICP companies
    with session_scope() as s:
        companies = s.execute(
            select(Company).where(Company.is_icp.is_(True))
        ).scalars().all()
        # Every ICP company is a target for every ingestor — each ingestor
        # self-selects via which config fields it needs (slug/ticker/workday).
        targets = [
            CompanyTarget(
                company_id=c.id, domain=c.domain, name=c.name,
                greenhouse_slug=c.greenhouse_slug, lever_slug=c.lever_slug,
                ashby_slug=c.ashby_slug, ticker=c.ticker,
                workday=c.workday_config,
            )
            for c in companies
        ]

    print(f"[ingest] polling {len(targets)} ICP companies...")
    all_new: list[int] = []
    for target in targets:
        try:
            ids = await ingest_company(target)
            if ids:
                print(f"  + {target.name}: {len(ids)} new signal(s)")
            all_new.extend(ids)
        except Exception as e:
            print(f"  ! {target.name}: {type(e).__name__}: {e}")

    # Also re-process existing pending signals (useful on first local run)
    with session_scope() as s:
        pending = s.execute(
            select(Signal.id).where(Signal.status == SignalStatus.PENDING)
        ).scalars().all()
    all_pending = sorted(set(all_new) | set(pending))
    print(f"\n[process] running {len(all_pending)} signals through pipeline...\n")

    # Hard cap: each company alerts at most once per pipeline run. This set
    # is owned by main() and passed into process_signal so the guard is
    # testable (not hidden module state).
    alerted_this_run: set[int] = set()

    outcomes: dict[str, int] = {}
    for sid in all_pending:
        try:
            result = process_signal(sid, per_run_alerted_companies=alerted_this_run)
            oc = result.get("outcome", "error")
            outcomes[oc] = outcomes.get(oc, 0) + 1
            tag = "✓" if oc == "alerted" else " "
            print(f"  {tag} signal {sid}: {oc}"
                  + (f"  score={result.get('cumulative')}"
                     f"  conf={result.get('confidence', 'n/a')}" if result.get("cumulative") else ""))
        except Exception as e:
            outcomes["error"] = outcomes.get("error", 0) + 1
            print(f"  ! signal {sid}: {type(e).__name__}: {e}")

    print("\n=== Summary ===")
    for k, v in sorted(outcomes.items()):
        print(f"  {k}: {v}")

    # Flush any pending trace spans to Arthur before the process exits.
    # BatchSpanProcessor uses a background thread — without this, the final
    # few spans can be lost if Python exits before the batch flushes.
    _tracing.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
