"""Apply the rubric to DB-resident signals, compute company rollups.

The scorer is called after validation. It updates Signal rows with their score/tier,
computes the cumulative score for the company over the configured window, and
decides whether the incoming signal warrants an alert.

### Alert decision (docs/icp.md + build brief §7)

A naive "alert any time cumulative_score ≥ threshold" floods the channel: once
a company crosses the threshold, every subsequent validated signal re-crosses
it and fires again. We apply these layers of gating:

1. **Same-URL dedup** — if the triggering signal's `source_url` was already
   the trigger for a prior alert on this company, NEVER re-alert. Protects
   against the Slack channel seeing the same news article / job post twice
   on consecutive runs. Applies to ALL signal types including always-alert.

2. **Always-alert signal types** (Tier 1 urgency, bypass cooldown+threshold):
   - `news.ai_incident`
   - `job_posting.ai_governance`
   - `job_posting.ai_leadership`
   - `news.exec_hire_ai`
   - `linkedin.exec_hire_ai`
   Requires a DIFFERENT source_url than any previously-alerted signal.

3. **First-time threshold crossing** — company has never been alerted, score
   now crosses single-signal OR cumulative threshold. One alert.

4. **Same-signal-type cooldown** — within ALERT_SAME_TYPE_COOLDOWN_DAYS
   (default 7 days) of the PRIOR same-type alert for this company,
   re-alerting on the same signal_type is suppressed. A NEW signal type
   (e.g. exec_hire after a prior job_posting alert) passes through
   subject to the material-change check.

5. **Material-change during cooldown** — re-alert only if cumulative score
   has grown by ≥ ALERT_MATERIAL_CHANGE_RATIO over the last-alerted score.

After cooldown expires, alerts resume normally — still subject to URL dedup.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_agent.config import settings
from signal_agent.models import Alert, Company, Signal, SignalStatus, SignalTier
from signal_agent.schemas import CompanyScoreRollup
from signal_agent.scoring.rubric import score_signal, tier_for_score

# Signal types that always alert immediately, regardless of cooldown. Per the
# build brief: "Tier 1 signals always alert immediately."
ALWAYS_ALERT_SIGNAL_TYPES = {
    "news.ai_incident",
    "job_posting.ai_governance",
    "job_posting.ai_leadership",
    "news.exec_hire_ai",
    "linkedin.exec_hire_ai",
}


@dataclass
class AlertDecision:
    """Why we did or didn't fire an alert. Structured for observability + tests."""
    should_fire: bool
    reason: str   # "always_alert" | "first_crossing" | "material_change" | "cooldown" | ...
    delta_vs_last: float | None = None  # cumulative - last_alerted_score, when applicable


def update_signal_score(session: Session, signal: Signal) -> None:
    """Called after a Signal has been validated. Writes raw_score + tier in place."""
    score = score_signal(
        signal_type=signal.signal_type,
        detected_at=signal.detected_at,
        llm_confidence=signal.llm_confidence or 0.0,
        target_tier=signal.company.target_tier if signal.company else 2,
    )
    signal.raw_score = score
    signal.tier = SignalTier(tier_for_score(score))


def _cap_and_sum(rows, cap: int) -> tuple[float, list[int]]:
    """Sum raw_score keeping at most `cap` highest-scoring signals per signal_type.

    Pure helper (no DB) so the cap logic is unit-testable. Returns
    (total, contributing_signal_ids). One newsworthy event can spawn dozens of
    near-identical articles of the same signal_type; without the cap they sum
    linearly and swamp the score. `cap <= 0` disables the cap (sum everything).
    """
    from collections import defaultdict
    by_type: dict[str, list] = defaultdict(list)
    for r in rows:
        by_type[r.signal_type].append(r)
    total = 0.0
    contributing: list[int] = []
    for group in by_type.values():
        group = sorted(group, key=lambda r: r.raw_score, reverse=True)
        kept = group if cap <= 0 else group[:cap]
        total += sum(r.raw_score for r in kept)
        contributing.extend(r.id for r in kept)
    return round(total, 2), contributing


def cumulative_company_score(session: Session, company_id: int) -> CompanyScoreRollup:
    """Cumulative score for the company over the window, capping per signal_type
    so a single high-volume news event can't dominate (see _cap_and_sum)."""
    window_start = datetime.now(timezone.utc) - timedelta(days=settings.alert_cumulative_window_days)
    rows = session.execute(
        select(Signal).where(
            Signal.company_id == company_id,
            Signal.status == SignalStatus.VALIDATED,
            Signal.detected_at >= window_start,
        )
    ).scalars().all()

    if not rows:
        return CompanyScoreRollup(
            company_id=company_id,
            cumulative_score=0.0,
            top_tier="tier_3",
            window_days=settings.alert_cumulative_window_days,
            contributing_signal_ids=[],
        )

    total, contributing = _cap_and_sum(rows, settings.score_max_signals_per_type)
    # top_tier reflects the strongest signal the company has, independent of the
    # score cap (tier_1 < tier_2 < tier_3 lexically).
    top_tier = min(r.tier.value for r in rows if r.tier is not None)
    return CompanyScoreRollup(
        company_id=company_id,
        cumulative_score=total,
        top_tier=top_tier,
        window_days=settings.alert_cumulative_window_days,
        contributing_signal_ids=contributing,
    )


def _prior_alert_summary(
    session: Session, company_id: int,
) -> tuple[set[str], dict[str, datetime]]:
    """Collect, for this company, (a) every source_url we've posted about
    forever, and (b) the most recent Slack-post timestamp for each
    signal_type we've alerted on.

    Two return values because their TTLs differ:
      - URL dedup is intentionally forever (the same article is the same
        article whether we saw it yesterday or last quarter)
      - Signal-type cooldown is a rolling window — an old product-launch
        alert from 3 months ago shouldn't block a fresh one today
    """
    rows = session.execute(
        select(Signal.source_url, Signal.signal_type, Alert.fired_at)
        .join(Alert, Alert.triggering_signal_id == Signal.id)
        .where(Alert.company_id == company_id)
    ).all()
    urls: set[str] = set()
    # For each signal_type, keep only the MOST RECENT fired_at. Older
    # entries lose because the cooldown runs from the latest post.
    latest_by_type: dict[str, datetime] = {}
    for url, stype, fired_at in rows:
        if url:
            urls.add(url)
        if stype and fired_at is not None:
            prev = latest_by_type.get(stype)
            if prev is None or fired_at > prev:
                latest_by_type[stype] = fired_at
    return urls, latest_by_type


def should_alert(
    rollup: CompanyScoreRollup,
    triggering: Signal,
    company: Company,
    session: Session | None = None,
    now: datetime | None = None,
) -> AlertDecision:
    """Decide whether to fire an alert for this signal.

    See module docstring for the full decision tree. Inputs:
      - rollup:      the company's cumulative score across the window
      - triggering:  the newly-ingested signal that landed us here
      - company:     has last_alerted_at / last_alerted_score for cooldown
      - session:     SQLAlchemy session; used to query prior-alert history
                     for cross-run URL + signal-type dedup. Optional for
                     backward compatibility — callers without session skip
                     the dedup checks (legacy behavior).
    """
    now = now or datetime.now(timezone.utc)

    # Gather the prior-alert history for this company (URLs forever +
    # per-signal-type most-recent timestamps). Empty if session is None.
    prior_urls: set[str] = set()
    prior_type_latest: dict[str, datetime] = {}
    if session is not None:
        prior_urls, prior_type_latest = _prior_alert_summary(session, company.id)

    # Helper: is this signal_type still in its rolling cooldown window?
    same_type_cooldown = timedelta(days=settings.alert_same_type_cooldown_days)

    def _in_same_type_window() -> bool:
        last = prior_type_latest.get(triggering.signal_type)
        return last is not None and (now - last) < same_type_cooldown

    # Rule 1: same-URL dedup. ALWAYS applies, including to always-alert
    # signal types. If we've Slack-posted about this specific article or
    # job posting before for this company, we never post it again.
    if triggering.source_url and triggering.source_url in prior_urls:
        return AlertDecision(should_fire=False, reason="already_alerted_on_this_url")

    # Rule 2: always-alert signal types. Bypass threshold + material-change,
    # BUT only if this signal type hasn't already alerted in the rolling
    # same-type cooldown window (default 7 days — see config). Two
    # news.exec_hire_ai articles about the same FICO CAIO appointment
    # shouldn't both fire within a week; a different always-alert type
    # (news.ai_incident) would still break through.
    if triggering.signal_type in ALWAYS_ALERT_SIGNAL_TYPES:
        if _in_same_type_window():
            return AlertDecision(
                should_fire=False,
                reason="always_alert_suppressed_same_type_in_cooldown",
            )
        return AlertDecision(should_fire=True, reason="always_alert")

    # Below threshold? Never alert.
    above_single = triggering.raw_score >= settings.alert_score_threshold
    above_cumulative = rollup.cumulative_score >= settings.alert_cumulative_threshold
    if not (above_single or above_cumulative):
        return AlertDecision(should_fire=False, reason="below_threshold")

    # Rule 3: first-time crossing — company has never been alerted.
    if company.last_alerted_at is None:
        return AlertDecision(should_fire=True, reason="first_crossing")

    # Rule 4: same signal type still in rolling cooldown → suppress.
    # This is the cross-run dedup gate that catches "the same product-launch
    # news keeps reappearing at the same company" regardless of general
    # material-change cooldown state.
    if _in_same_type_window():
        return AlertDecision(
            should_fire=False, reason="cooldown_same_signal_type",
        )

    # Rule 5: we're inside the GENERAL (24h) cooldown window.
    cooldown_end = company.last_alerted_at + timedelta(hours=settings.alert_cooldown_hours)
    in_cooldown = now < cooldown_end

    last_score = company.last_alerted_score or 0.0
    delta = rollup.cumulative_score - last_score
    # Material change: cumulative score has grown by >= ratio over the score
    # we last alerted on. Using last_score as the denominator ensures that
    # tiny-ratio alerts at very high scores still trigger (e.g., going from
    # 40 → 61 is material even though 40 > 0).
    if last_score <= 0:
        materially_changed = rollup.cumulative_score > 0
    else:
        materially_changed = delta / last_score >= settings.alert_material_change_ratio

    if in_cooldown and not materially_changed:
        return AlertDecision(
            should_fire=False, reason="cooldown", delta_vs_last=delta,
        )

    if in_cooldown and materially_changed:
        return AlertDecision(
            should_fire=True, reason="material_change", delta_vs_last=delta,
        )

    # Cooldown expired — a fresh threshold crossing reopens alerts.
    return AlertDecision(
        should_fire=True, reason="cooldown_expired", delta_vs_last=delta,
    )


def mark_alerted(company: Company, cumulative_score: float,
                 now: datetime | None = None) -> None:
    """Update cooldown state after an alert fires. Call from within a session."""
    company.last_alerted_at = now or datetime.now(timezone.utc)
    company.last_alerted_score = cumulative_score


# ---- Clay outbound push decision -------------------------------------------
#
# DECOUPLED from Slack alerting. The Slack alert bar is deliberately strict so
# the awareness channel stays high-signal. Outbound wants a WIDER net — more
# accounts into contact discovery + sequencing — so it runs on its own, lower
# threshold (CLAY_PUSH_SCORE_THRESHOLD) and its own (longer) cooldown
# (CLAY_PUSH_COOLDOWN_DAYS). An account can be pushed to Clay without ever
# firing a Slack alert, and vice-versa.


@dataclass
class ClayPushDecision:
    """Why we did or didn't push an account to the Clay outbound last-mile."""
    should_push: bool
    reason: str   # "first_push" | "cooldown_expired" | "in_cooldown" | "below_clay_threshold"


def should_push_to_clay(
    rollup: CompanyScoreRollup,
    triggering: Signal,
    company: Company,
    now: datetime | None = None,
) -> ClayPushDecision:
    """Decide whether to push this (already-validated) account to Clay.

    Wider + simpler than `should_alert`: there's no URL/same-type dedup (Clay
    enrolls at the *account* level, not per-article), just a score floor and an
    account-level re-touch cooldown. Always-alert signal types push regardless
    of score — if it's worth alerting Slack, it's worth an outbound touch.
    """
    now = now or datetime.now(timezone.utc)

    above_bar = (
        triggering.signal_type in ALWAYS_ALERT_SIGNAL_TYPES
        or rollup.cumulative_score >= settings.clay_push_score_threshold
        or triggering.raw_score >= settings.clay_push_score_threshold
    )
    if not above_bar:
        return ClayPushDecision(should_push=False, reason="below_clay_threshold")

    if company.last_clay_pushed_at is not None:
        cooldown = timedelta(days=settings.clay_push_cooldown_days)
        if (now - company.last_clay_pushed_at) < cooldown:
            return ClayPushDecision(should_push=False, reason="in_cooldown")
        return ClayPushDecision(should_push=True, reason="cooldown_expired")

    return ClayPushDecision(should_push=True, reason="first_push")


def mark_clay_pushed(company: Company, cumulative_score: float,
                     now: datetime | None = None) -> None:
    """Record the Clay-push cooldown state. Call from within a session after a
    successful push."""
    company.last_clay_pushed_at = now or datetime.now(timezone.utc)
    company.last_clay_pushed_score = cumulative_score
