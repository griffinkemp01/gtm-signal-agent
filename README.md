# Arthur Signal Agent

Automated agent that monitors the public web for signals indicating an
enterprise is scaling AI / agentic AI deployments, scores them against
Arthur's ICP, and surfaces awareness-level alerts to a Slack channel —
with structured data written back to HubSpot and full-fidelity traces
exported to the Arthur GenAI Engine.

Today the agent is an **awareness tool**: it posts triggers to a shared
Slack channel so the team has a running pulse on which ICP accounts are
moving. 

The canonical ICP definition lives at [docs/icp.md](docs/icp.md). Every
tunable knob (keywords, competitors, scoring rubric, LLM prompt) derives
from it.

## What it does, in order

1. **Ingests** signals per ICP company across many public sources:
   - Job boards: Greenhouse, Lever, Ashby, Workday
   - News: Google News RSS
   - Regulatory filings: SEC EDGAR (10-K / 10-Q / 8-K)
   - Competitive intel: Hacker News + Reddit (co-occurrence with Arthur competitors)
   - Conference speakers (ML/AI events)
   - LinkedIn exec hires (scaffolded, needs a paid data provider)
2. **Suppresses** the obvious — recruiting-agency posts, accounts already
   on an Arthur competitor's customer page, and operator-flagged disqualifications.
3. **Validates** every surviving signal with Claude. The validator prompt
   is Arthur-ICP aware (see `signal_agent/scoring/validator.py`).
4. **Scores** against a weighted rubric that accounts for signal freshness,
   LLM confidence, and the company's ICP tier (Segment A = 1.25x multiplier).
5. **Decides whether to alert** using three layers of gating — tier-1
   signal bypass, first-time threshold crossing, or material change during
   a 24h cooldown — so the channel doesn't flood when a single company
   generates many signals.
6. **Groups bursty alerts** into a digest when alert-rate exceeds 5/hour.
7. **Posts to Slack** — an awareness ping with Claim / Snooze / Open-in-HubSpot
   buttons so anyone watching can mark an account as being worked.
8. **Writes to HubSpot**: `arthur_signal_score`, `arthur_signal_tier`,
   `arthur_signal_summary`, `arthur_last_signal_date` on the company record.
9. **Traces everything** via OpenTelemetry to the Arthur GenAI Engine
   (prompt, completion, tokens, latency per LLM call + pipeline spans per stage).
10. **Pushes qualified accounts to Clay** for the outbound last mile — contact
    discovery (Head of AI / VP Data Science / CISO), email verification, a
    signal-keyed opener, and HubSpot Sequence enrollment. No-op unless
    `CLAY_WEBHOOK_URL` is set. See [docs/clay-last-mile.md](docs/clay-last-mile.md).

## Repo layout

```
signal_agent/
  config.py               env-driven settings (+ pydantic-settings)
  db.py                   SQLAlchemy session factory
  models.py               ORM models: Company, Signal, Alert, CompetitorCustomer, ...
  schemas.py              pydantic DTOs shared across modules

  ingestors/
    base.py               Ingestor ABC + CompanyTarget
    registry.py           enabled-ingestor list (config-driven via DISABLED_INGESTORS)
    greenhouse.py / lever.py / ashby.py / workday.py    ATS job boards
    news.py                                             Google News RSS per company
    sec_edgar.py                                        SEC filings keyword scan
    competitive.py                                      HN + Reddit co-occurrence
    conferences.py                                      conference speaker lists
    linkedin.py                                         exec hires (needs vendor key)
    html_util.py                                        HTML → plain-text

  scoring/
    rubric.py             per-signal-type weights, tier multipliers, freshness decay
    validator.py          LLM validation call (source-of-truth ICP prompt)
    scorer.py             cumulative rollup + alert-decision (cooldown, material-change)

  accounts/resolver.py    HubSpot match / create with fuzzy name fallback

  integrations/
    hubspot.py            property writes + timeline events + record-URL builder
    slack.py              Block Kit rendering + posting + circuit breaker DM
    clay.py               push qualified accounts to Clay (outbound last mile)

  quality/
    suppression.py              operator-managed disqualification patterns
    circuit_breaker.py          pause alerting on bursts
    digest.py                   batch non-Tier-1 alerts into one grouped post
    competitor_customers.py     scrape competitor customer pages, gate by match

  observability/
    tracing.py            OTel setup + OpenInference Anthropic instrumentor + stage_span

  workflows/              Inngest scheduled + event-driven functions
  api/                    FastAPI app (Inngest webhooks + Slack interactivity)
  seeds/                  icp_companies.yaml, suppression.yaml, conferences.yaml,
                          competitor_customers_overrides.yaml, loader

migrations/               Alembic schema history (latest: 0009 signal dedup_key)
scripts/                  CLI tools — run_pipeline, flush_digest, import_icp_csv,
                          refresh_competitor_customers, setup_hubspot
tests/                    pytest — 85 tests, all green
docs/                     icp.md, clay-last-mile.md, arthur-tracing.md,
                          deployment-plan.md, phase1-decisions.md
```

## Local setup

```bash
# 1. Services
docker compose up -d postgres

# 2. Python env
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 3. Config — copy the template and fill in your keys
cp .env.example .env
# Edit .env with credentials:
#   Anthropic      ANTHROPIC_API_KEY
#   HubSpot        HUBSPOT_ACCESS_TOKEN, HUBSPOT_PORTAL_ID (for working record URLs)
#   Slack          SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_ALERT_CHANNEL
#   Clay           CLAY_WEBHOOK_URL (outbound last mile; leave blank to disable)
#   Arthur Engine  ARTHUR_ENGINE_API_KEY, ARTHUR_TASK_ID (tracing)
# Note: docker-compose maps Postgres to host port 5433 — DATABASE_URL uses 5433.
# Optional: DISABLED_INGESTORS=competitive skips the Reddit source, which
# rate-limits hard across a large account list.

# 4. DB
.venv/bin/alembic upgrade head
.venv/bin/python -m signal_agent.seeds.load_icp

# 5. HubSpot (one-time: creates the 4 custom properties)
.venv/bin/python -m scripts.setup_hubspot

# 6. Competitor-customer cache (recommended first run)
.venv/bin/python -m scripts.refresh_competitor_customers

# 7. Run the pipeline
.venv/bin/python -m scripts.run_pipeline
```

See [docs/arthur-tracing.md](docs/arthur-tracing.md) for tracing setup
and [docs/deployment-plan.md](docs/deployment-plan.md) for the Render +
Inngest Cloud deployment plan.

## Bulk-importing ICP accounts

```bash
# CSV with a "Company Name" column. A "Domain" column is used directly when
# present (no LLM call); rows without one are resolved via Claude and
# low-confidence rows flagged for manual review. Segment + tier are derived
# from a "Size" (employee-count) column: 5,000+ → A/1, 1,000–4,999 → B/2,
# <1,000 → C/3.
.venv/bin/python -m scripts.import_icp_csv /path/to/accounts.csv
```

## Running the pipeline

```bash
# Manual run — ingest all sources, score, post alerts, write HubSpot.
.venv/bin/python -m scripts.run_pipeline

# Flush any batched digest alerts (normally scheduled via Inngest every 15 min).
.venv/bin/python -m scripts.flush_digest

# Daily competitor-customer cache refresh.
.venv/bin/python -m scripts.refresh_competitor_customers
```

## Outbound last-mile (Clay)

When an account qualifies, the agent POSTs it (company, score/tier, validated
`signal_summary`, triggering signal, target personas, and a `dedup_key`) to a
Clay webhook. Clay handles contact discovery, email verification, a signal-keyed
opener, and HubSpot Sequence enrollment. Full contract, opener prompt, and table
setup: [docs/clay-last-mile.md](docs/clay-last-mile.md).

The push is **decoupled from Slack alerting** — it runs on its own wider, lower
bar so more qualified accounts feed outbound without flooding the channel, with
a separate per-account re-touch cooldown. Tunables:

```bash
CLAY_WEBHOOK_URL=            # blank disables the push (Phase 1 / local runs)
CLAY_PUSH_SCORE_THRESHOLD=6  # outbound bar; lower = wider net (alert bar is 12)
CLAY_PUSH_COOLDOWN_DAYS=30   # don't re-push the same account within N days
```

## Tests

```bash
.venv/bin/pytest -q
```

85 tests cover scoring rubric, alert-decision, the Clay-push decision,
digest batching, HTML stripping, keyword classifiers, suppression rules,
competitor-customer matching, Slack block rendering, the Clay payload, and
the HubSpot record-URL builder. Integration tests (HubSpot / Slack /
Anthropic / Clay) are gated by env vars and skipped by default.

## Observability

Every pipeline run produces a trace visible in the Arthur Engine Trace
Viewer (configured via `ARTHUR_ENGINE_BASE_URL` + `ARTHUR_TASK_ID`).

One trace = one signal's journey. Span hierarchy:

```
signal_agent.process_signal              (per-signal parent span)
  ├─ signal_agent.suppression_check
  ├─ signal_agent.competitor_customer_check
  ├─ signal_agent.llm_validation
  │    └─ anthropic.messages.create      (auto-instrumented)
  ├─ signal_agent.score_and_decide
  ├─ signal_agent.clay_push                (outbound last mile → httpx → Clay)
  ├─ signal_agent.account_resolution
  │    └─ httpx request → HubSpot        (auto-instrumented)
  ├─ signal_agent.slack_post
  │    └─ httpx request → Slack
  └─ signal_agent.hubspot_write
```

Filter in the Arthur UI by `signal_agent.company_name`,
`signal_agent.outcome`, or `signal_agent.alert_reason` to slice by account
or decision type.

## Key design choices (and why)

- **Keywords pre-filter, LLM decides.** Keywords are cheap and noisy;
  they decide whether to spend an LLM call. The LLM is authoritative.
- **Cumulative score with cooldown, not per-signal alerting.** Once a
  company is alerted, re-alerting is gated until either 24h passes,
  a Tier-1 signal fires, or the score jumps by ≥50%. Prevents flooding.
- **Tier multiplier on scoring.** Same signal at a Segment A account
  (tier 1) scores 1.25× what it does at a Segment C account (tier 3).
- **Per-signal-type score cap.** One newsworthy event can spawn 40+
  near-identical articles; cumulative scoring counts at most
  `SCORE_MAX_SIGNALS_PER_TYPE` (default 3) of each signal_type per company so a
  single event can't dominate the score.
- **Dedup on a stable key, not source_url.** Signals dedupe on
  `(company, signal_type, dedup_key)`. News keys on a title+date hash because
  its Google-redirect URL changes every fetch; stable-URL sources key on the
  URL. Prevents the same story re-inserting as a new row each run.
- **Competitor-customer check before LLM spend.** Accounts already on
  an Arthur competitor's public customer page are suppressed at ingest —
  we don't waste LLM dollars or channel attention on them.
- **Operator overrides in YAML.** Inside information (case studies,
  private knowledge) flows into `seeds/competitor_customers_overrides.yaml`
  and wins over the scraper.
- **Source of truth is docs/icp.md.** All tunables (keywords, weights,
  competitor list, LLM prompt) reference it. When the ICP changes, the
  doc changes first, then the code follows.
- **Outbound is decoupled from awareness.** The Clay push runs on a lower,
  wider bar than Slack alerting (and its own cooldown), so the awareness
  channel stays high-signal while outbound still gets a broad net. An account
  can be pushed to Clay without ever firing a Slack alert.
- **Regulatory framing is dated, not assumed.** Per the April 17, 2026
  interagency MRM rewrite (OCC 2026-13), GenAI/agentic systems are out of
  SR 11-7 scope. The validator + messaging frame them under fair lending,
  NYDFS, FFIEC, SEC, and state laws instead; SR 11-7 stays a detection keyword.

## Status

- Phase 1 (jobs) — done
- Phase 2 (news + SEC) — done
- Phase 3 (HN, Reddit, conferences, Workday, LinkedIn scaffold, digest) — done
- Arthur tracing — done, exporting to https://engine.development.arthur.ai
- Outbound last-mile (Clay push, decoupled bar, contact enrichment) — done
- ICP list — seed ships 8 companies; bulk-load the full target list via
  `scripts.import_icp_csv` (see Bulk-importing ICP accounts)

## Deploy

See [docs/deployment-plan.md](docs/deployment-plan.md) for the Render +
Inngest Cloud playbook.

## License

Internal. Do not redistribute.
