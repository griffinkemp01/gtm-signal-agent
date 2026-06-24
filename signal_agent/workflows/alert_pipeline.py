"""Per-signal alert pipeline.

Triggered by `signal.detected`. Steps:
 1. Suppression check → status=SUPPRESSED, done.
 2. LLM validation → low confidence goes to review (status=REVIEW).
 3. Score + tier assignment.
 4. Account resolution (HubSpot match/create).
 5. Compute company rollup, decide whether to alert.
 6. Circuit breaker check.
 7. Fire Slack alert + HubSpot property update + timeline event.
 8. Persist Alert row for audit/metrics.

Each step is an Inngest `step.run` so they retry individually and the orchestration
is visible in the Inngest dashboard (big operational win for a team that will
stare at this during the weekly review cycle).
"""
from __future__ import annotations

from datetime import UTC, datetime

import inngest
import structlog

from signal_agent.accounts.resolver import AccountResolver
from signal_agent.config import settings
from signal_agent.db import session_scope
from signal_agent.integrations.clay import ClayAccountPayload, ClayPusher
from signal_agent.integrations.hubspot import HubSpotClient, company_record_url
from signal_agent.integrations.slack import AlertContext, SlackAlerter
from signal_agent.models import Alert, Company, Signal, SignalStatus
from signal_agent.quality import circuit_breaker, competitor_customers, digest, suppression
from signal_agent.schemas import NormalizedSignal
from signal_agent.scoring import scorer
from signal_agent.scoring.validator import validate_signal
from signal_agent.workflows.inngest_app import inngest_client

log = structlog.get_logger()


@inngest_client.create_function(
    fn_id="process_signal",
    trigger=inngest.TriggerEvent(event="signal.detected"),
    retries=3,
)
async def process_signal(ctx: inngest.Context) -> dict:
    signal_id = ctx.event.data["signal_id"]

    # ---- step 1: suppression check ------------------------------------------
    def _suppress_step() -> dict:
        with session_scope() as s:
            sig = s.get(Signal, signal_id)
            if sig is None:
                return {"skip": True}
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
            suppressed, reason = suppression.is_suppressed(s, norm)
            if suppressed:
                sig.status = SignalStatus.SUPPRESSED
                sig.llm_reasoning = f"suppressed: {reason}"
                return {"suppressed": True, "reason": reason}
            # Competitor-customer disqualification. Don't spend LLM budget
            # or surface to AEs if the account is already on a competitor's
            # customer page (or operator-marked as such in overrides).
            cc = competitor_customers.is_competitor_customer(s, sig.company_id)
            if cc.is_customer:
                sig.status = SignalStatus.SUPPRESSED
                sig.llm_reasoning = (
                    f"suppressed: known customer of {', '.join(cc.competitors)}"
                )
                return {"suppressed": True, "reason": "competitor_customer",
                        "competitors": cc.competitors}
            return {"suppressed": False}

    supp = await ctx.step.run("suppression_check", _suppress_step)
    if supp.get("skip") or supp.get("suppressed"):
        return supp

    # ---- step 2: LLM validation ---------------------------------------------
    def _validate_step() -> dict:
        with session_scope() as s:
            sig = s.get(Signal, signal_id)
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
            result = validate_signal(norm)
            sig.llm_confidence = result.confidence
            sig.llm_reasoning = result.reasoning
            sig.llm_summary = result.summary_for_ae

            if not result.is_valid:
                sig.status = SignalStatus.REJECTED
                return {"proceed": False, "reason": "llm_rejected"}
            if result.confidence < settings.llm_confidence_floor:
                sig.status = SignalStatus.REVIEW
                return {"proceed": False, "reason": "low_confidence",
                        "confidence": result.confidence}
            sig.status = SignalStatus.VALIDATED
            return {"proceed": True, "confidence": result.confidence,
                    "summary": result.summary_for_ae}

    val = await ctx.step.run("llm_validation", _validate_step)
    if not val.get("proceed"):
        return val

    # ---- step 3: score + alert decision --------------------------------------
    def _score_step() -> dict:
        with session_scope() as s:
            sig = s.get(Signal, signal_id)
            scorer.update_signal_score(s, sig)
            rollup = scorer.cumulative_company_score(s, sig.company_id)
            decision = scorer.should_alert(rollup, sig, sig.company, session=s)
            return {
                "raw_score": sig.raw_score,
                "tier": sig.tier.value if sig.tier else None,
                "cumulative": rollup.cumulative_score,
                "top_tier": rollup.top_tier,
                "alert_needed": decision.should_fire,
                "alert_reason": decision.reason,
                "delta_vs_last": decision.delta_vs_last,
                "contributing": rollup.contributing_signal_ids,
            }

    scored = await ctx.step.run("score", _score_step)

    # ---- step 3b: outbound last-mile (DECOUPLED from Slack alerting) ---------
    # Pushes the account to Clay on a wider, lower bar (CLAY_PUSH_SCORE_THRESHOLD)
    # with its own cooldown, so qualified-but-not-alert-worthy accounts still
    # feed contact discovery + sequencing. Runs for every scored account,
    # regardless of the Slack alert decision. No-op when CLAY_WEBHOOK_URL unset.
    def _clay_push_step() -> dict:
        with session_scope() as s:
            sig = s.get(Signal, signal_id)
            rollup = scorer.cumulative_company_score(s, sig.company_id)
            decision = scorer.should_push_to_clay(rollup, sig, sig.company)
            if not decision.should_push:
                return {"pushed": False, "reason": decision.reason}
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
                qualification_reason=decision.reason,
                also_alerted=scored["alert_needed"],
            ))
            if pushed:
                scorer.mark_clay_pushed(sig.company, rollup.cumulative_score)
            return {"pushed": pushed, "reason": decision.reason}

    clay_res = await ctx.step.run("clay_push", _clay_push_step)

    if not scored["alert_needed"]:
        return {**scored, "clay": clay_res}

    # ---- step 4: account resolution -----------------------------------------
    def _resolve_step() -> dict:
        with session_scope() as s:
            sig = s.get(Signal, signal_id)
            resolver = AccountResolver()
            hubspot_id = resolver.resolve(s, sig.company)
            return {"hubspot_id": hubspot_id}

    resolved = await ctx.step.run("resolve_account", _resolve_step)

    # ---- step 5: circuit breaker --------------------------------------------
    def _cb_step() -> dict:
        with session_scope() as s:
            if circuit_breaker.is_tripped(s):
                circuit_breaker.record_trip(s, settings.circuit_breaker_alerts_per_hour)
                SlackAlerter().notify_circuit_breaker(settings.circuit_breaker_alerts_per_hour)
                return {"tripped": True}
            return {"tripped": False}

    cb = await ctx.step.run("circuit_breaker", _cb_step)
    if cb["tripped"]:
        return {"alert_skipped": "circuit_breaker"}

    # ---- step 6: fire alert --------------------------------------------------
    def _fire_step() -> dict:
        with session_scope() as s:
            sig: Signal = s.get(Signal, signal_id)
            company: Company = sig.company

            # Respect manual snoozes.
            if company.snoozed_until and company.snoozed_until > datetime.now(UTC):
                return {"alert_skipped": "snoozed", "until": company.snoozed_until.isoformat()}

            # Persist alert row first so the Slack button can reference alert_id.
            alert = Alert(
                company_id=company.id,
                triggering_signal_id=sig.id,
                cumulative_score=scored["cumulative"],
                tier=sig.tier,
                slack_channel=settings.slack_alert_channel,
            )
            s.add(alert)
            s.flush()

            # Gather the top 3 most recent contributing signals for the Slack
            # alert. (The Clay push is decoupled — handled in step 3b above.)
            recent = (
                s.query(Signal)
                .filter(Signal.id.in_(scored["contributing"]))
                .order_by(Signal.detected_at.desc())
                .limit(3)
                .all()
            )
            top_signals = [
                {"type": r.signal_type, "url": r.source_url,
                 "text": r.signal_text.split("\n", 1)[0][:120]}
                for r in recent
            ]

            # Digest-mode gating — non-Tier-1 alerts during a burst go to the
            # pending queue; flush_digest posts them as a grouped message.
            if digest.should_batch(s, sig.tier):
                digest.enqueue(s, alert)
                return {"alert_id": alert.id, "outcome": "queued_for_digest"}

            hubspot_url = company_record_url(company.hubspot_id)

            ctx_obj = AlertContext(
                company_name=company.name,
                company_domain=company.domain,
                cumulative_score=scored["cumulative"],
                tier=scored["top_tier"],
                summary_for_ae=sig.llm_summary or "",
                top_signals=top_signals,
                hubspot_url=hubspot_url,
                owner_name=None,       # Phase 2: populate from HubSpot
                deal_stage=None,       # Phase 2: populate from HubSpot
                alert_id=alert.id,
            )
            ts = SlackAlerter().post_alert(ctx_obj)
            alert.slack_ts = ts
            # Record cooldown state so subsequent signals respect the window.
            scorer.mark_alerted(company, scored["cumulative"])

            # HubSpot writes (no-op if hubspot_id unresolved / not ICP)
            if company.hubspot_id:
                hs = HubSpotClient()
                hs.update_signal_properties(
                    hubspot_company_id=company.hubspot_id,
                    score=scored["cumulative"],
                    tier=scored["top_tier"],
                    summary=sig.llm_summary or "",
                    last_signal_date_iso=sig.detected_at.astimezone(UTC).isoformat(),
                )
                hs.emit_timeline_event(
                    hubspot_company_id=company.hubspot_id,
                    signal_summary={
                        "signal_type": sig.signal_type,
                        "signal_text": sig.signal_text[:200],
                        "source_url": sig.source_url,
                        "score": str(scored["cumulative"]),
                        "tier": scored["top_tier"],
                    },
                )

            return {"alert_id": alert.id, "slack_ts": ts,
                    "hubspot_written": bool(company.hubspot_id)}

    fired = await ctx.step.run("fire_alert", _fire_step)
    log.info("alert.fired", **fired, signal_id=signal_id)
    return fired
