# Clay Last-Mile — Contacts + Triggered Outbound

The signal agent is an **awareness** tool: it scores ICP accounts and surfaces
the hot ones to Slack/HubSpot. It tells you *which company is moving* — it does
not find *who to email there* or fire the outbound. This is the last mile.

**The workflow in one line:** an account qualifies for outbound → agent POSTs it
(with the validated signal summary) to a Clay webhook → Clay finds the Segment A
personas + verified emails → personalizes the opener off the agent's signal
summary → enrolls in a **HubSpot Sequence**.

> **Outbound is decoupled from Slack alerting.** The Slack alert bar is strict
> (keeps the awareness channel high-signal). Outbound runs on its own *wider,
> lower* bar (`CLAY_PUSH_SCORE_THRESHOLD`, default 6 vs. the alert threshold of
> 12) so more qualified accounts feed contact discovery — without flooding
> Slack. An account can be pushed to Clay without ever firing a Slack alert. See
> `scorer.should_push_to_clay`.

```
signal_agent (alert fires)
   │  POST qualified_account  →  Clay webhook
   ▼
Clay table
   ├─ waterfall: find Head of AI/CAIO, VP Data Science, CISO  (verified emails)
   ├─ AI column: signal-keyed, persona-aware opening line
   └─ enroll contact → HubSpot Sequence   ← the revenue action
```

> You are NOT rebuilding the agent. It already scores, validates, gates, and
> routes. Clay does the one thing it's best at: contact discovery + outbound.

---

## 1. Agent side (already built)

When an account crosses the threshold, `signal_agent` fires the push from the
alert pipeline (`scripts/run_pipeline.py` and `workflows/alert_pipeline.py`,
right where the Slack alert + HubSpot write happen). It's a no-op unless
`CLAY_WEBHOOK_URL` is set.

```bash
# .env
CLAY_WEBHOOK_URL=https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-...
CLAY_PUSH_SCORE_THRESHOLD=6    # outbound bar — lower = wider net (alert bar is 12)
CLAY_PUSH_COOLDOWN_DAYS=30     # don't re-enroll the same account within N days
```

The push fires for any account clearing the (lower) outbound bar, on its own
30-day re-touch cooldown — independent of whether a Slack alert fired. Decision
logic: `scorer.should_push_to_clay`. Push code: `signal_agent/integrations/clay.py`.

**Scaling the outbound net:** lower `CLAY_PUSH_SCORE_THRESHOLD` to capture more
accounts; the persona list in `clay.py` (`SEGMENT_A_PERSONAS`) controls how many
*people* are sourced per account. Keep Clay's email verification strict — expand
volume at account selection, not by emailing weak-fit contacts (deliverability).

### Webhook payload contract

This is exactly what each Clay row receives (verified against a local receiver):

```json
{
  "event": "qualified_account",
  "dedup_key": "acme.com",
  "qualification_reason": "first_push",
  "also_alerted": true,
  "pushed_at": "2026-06-10T00:10:29Z",
  "company": {
    "name": "Acme Bank",
    "domain": "acme.com",
    "hubspot_id": "6674329667",
    "hubspot_url": "https://app.hubspot.com/contacts/_/company/6674329667",
    "target_tier": 1,
    "segment": "A"
  },
  "score": { "cumulative_score": 14.2, "tier": "tier_1" },
  "signal_summary": "Acme hired its first Head of AI Governance and put an LLM fraud agent into production.",
  "triggering_signal": {
    "type": "job_posting.ai_governance",
    "url": "https://boards.greenhouse.io/acme/jobs/1",
    "text": "Head of AI Governance"
  },
  "top_signals": [
    { "type": "news.exec_hire_ai", "url": "https://x/2", "text": "Acme names Chief AI Officer" }
  ],
  "target_personas": [
    "Head of AI / Chief AI Officer", "VP Data Science / Head of AI/ML", "CISO",
    "Head of Model Risk Management", "CTO / CIO", "Chief Data Officer", "VP Engineering"
  ]
}
```

- `qualification_reason` — why it entered outbound (`first_push` | `cooldown_expired`).
- `also_alerted` — whether it *also* cleared the stricter Slack bar this run.
  Use it in Clay to prioritize the hottest accounts in the sequence.

`signal_summary` is the personalization fuel — the agent's validated
`arthur_signal_summary`, the real "why now." **Do not re-research it per
contact.** That's the scope creep that sinks the build.

---

## 2. Clay table setup

1. **New table → import source → Webhook.** Copy the generated webhook URL into
   `CLAY_WEBHOOK_URL` in `.env`. Send one test event (see §4) so Clay learns the
   schema and auto-creates columns.
1a. **Set the dedupe key to `dedup_key`** (the company domain). In the webhook
   source's settings, choose `dedup_key` as the unique/dedupe column so repeated
   pushes for the same company UPDATE the existing row instead of appending a
   duplicate. The agent also enforces a 30-day per-account push cooldown, so in
   steady state a company pushes at most once a month — but the table-level
   dedupe key is what cleans up existing duplicates and protects against
   cooldown resets.
2. **Map the columns.** Clay nests JSON — pull out `company.domain`,
   `signal_summary`, `score.tier`, `company.hubspot_id`, `qualification_reason`,
   `also_alerted`.
3. **Find contacts (the waterfall).** Add an enrichment that finds people at
   `company.domain` for the `target_personas` (sent in the payload). Use Clay's
   "Find People / Find Contacts" with a job-title filter:
   - Head of AI · Chief AI Officer · CAIO · Head of AI Governance
   - VP Data Science · Head of AI/ML · Head of Machine Learning
   - CISO · Chief Information Security Officer
   - Head of Model Risk Management · Model Risk
   - CTO · CIO · Chief Data Officer
   - VP Engineering · Head of Engineering
   This expands the row into one row per contact. Trim roles in
   `SEGMENT_A_PERSONAS` (`clay.py`) if reply rates say one isn't converting.
4. **Verify emails.** Add Clay's "Waterfall: Work Email" → keep only
   `Valid`/`Verified`. Drop catch-all/risky to protect deliverability.
5. **Opener (the ONE allowed AI step).** Add a "Claude / AI" column with the
   prompt in §3. It reads `signal_summary` + the contact's title → one opening
   line. This is the ceiling; no second research column in v1.
6. **Enroll → HubSpot Sequence (the revenue action).** Add the HubSpot
   "Enroll in Sequence" action (or "Create/Update Contact" + sequence). Map:
   - email → contact email
   - `opener` (AI column) → a custom property or the sequence's first-step token
   - associate to company via `company.hubspot_id`
   - set enrollment owner / sequence id

---

## 3. The opener prompt (paste into the Clay AI column)

Persona-aware, signal-keyed, ≤2 sentences. Uses Clay column refs `/field`.

```
You write the first line of a cold email for Arthur. Arthur is the AI
performance platform: evaluation, monitoring, and guardrails that let
enterprises put LLMs, agents, and ML models into production with confidence,
and, where it matters, meet governance / model-risk obligations.

Account: /company.name
Industry: /Industry          (may be blank)
Recipient title: /Find People → Job Title
Why this account is in motion right now (validated signal): /signal_summary

Write ONE opening sentence, 25 words MAXIMUM, and shorter is better. Not two
sentences. If your draft has a comma splice or an "and" joining two thoughts,
cut the weaker thought. It must:
- Lead with the specific signal above, naming the concrete thing (the hire, the
  product launch, the filing, the team they're building). No generic "I saw
  you're scaling AI."
- Connect it to the problem this person owns. Pick the angle by title:
    • Head of AI / CAIO → getting agents from pilot to production reliably;
      proving they work before (and after) they ship
    • VP Data Science / Head of AI-ML → eval infrastructure, model + LLM
      monitoring, regression-catching before users notice
    • CISO → visibility into what agentic systems are doing; AI risk inside
      the security program
    • Head of Model Risk → their GenAI and agentic systems just fell outside
      SR 11-7 scope, so governing them now means defining the model boundary
      themselves and defending it under consumer-protection and third-party
      risk rules, not MRM guidance
    • CTO / CIO / CDO / VP Engineering → shipping AI features without
      babysitting them; observability for systems built on non-deterministic
      models
- Get the regulatory frame right (this changed on April 17, 2026):
  - The interagency MRM rewrite (OCC Bulletin 2026-13) explicitly carved
    generative AI and agentic AI OUT of SR 11-7 / OCC 2011-12 scope. NEVER tell
    a prospect SR 11-7 governs their GenAI or agentic systems. It does not
    anymore. Citing it as the reason they need governance is now wrong.
  - For US banks / financial institutions, the live pegs for GenAI systems are
    fair lending (Reg B, FCRA adverse action), NYDFS Part 500, FFIEC third-party
    risk, SEC scrutiny of AI disclosures, and state laws (Colorado AI Act, CA
    DFPI), plus the open RFI that will define the next framework. Strong, timely
    angle: the rules for their agents are being written right now.
  - SR 11-7 still applies ONLY to traditional quantitative models (credit-scoring
    regressions, VaR, PD), so only mention it if the signal is about classic
    models, never about agents or LLMs.
  - UK firms: SS1/23 still applies and is stronger.
  - Airlines, retailers, software companies: skip regulation entirely; the angle
    is reliability, customer trust, and not shipping a model that embarrasses
    them. Never assume regulated-industry pain that isn't there.
- Sound like a person, not marketing. No "I hope this finds you well." No
  exclamation points. No em dashes or hyphens used as connectors; join thoughts
  with a comma, "so", or "and" instead. Do not pitch or ask for a meeting,
  that's the next line's job.

Calibration:
- GOOD (17 words): "Saw Wells Fargo just named a Head of AI for Wealth, so
  production agents can't be far behind."
- TOO LONG (do not do this): "I noticed that Wells Fargo recently announced
  the appointment of a new Head of AI for its Wealth & Investment Management
  division, which suggests the organization is preparing to scale agentic AI
  into production, and that typically creates governance challenges."

Return only the opening sentence.
```

`/Industry` isn't in the webhook payload — map it from a Clay enrichment column
(Clay's company-enrich or the LinkedIn page both return industry). If you skip
it, the prompt still works; the industry-stakes rule just leans on the signal.

**Before you build the message step, spot-check 5 real `signal_summary` values.**
If they're terse, "hyper-personalized" collapses into mail-merge — that's exactly
what this one Claude step is for: turning a thin summary into a usable opener.

---

## 4. Local testing (no Clay account needed)

Point `CLAY_WEBHOOK_URL` at any receiver (e.g. a webhook.site URL, or your real
Clay webhook) and run the pipeline — qualified accounts POST automatically:

```bash
set -a; . ./.env; set +a
.venv/bin/python -m scripts.run_pipeline
```

To exercise just the push with a sample payload:

```python
from signal_agent.integrations.clay import ClayAccountPayload, ClayPusher
ClayPusher().push(ClayAccountPayload(
    company_name="Acme Bank", company_domain="acme.com", hubspot_id="123",
    target_tier=1, segment="A", cumulative_score=14.2, tier="tier_1",
    signal_summary="Acme hired its first Head of AI Governance...",
    triggering_signal={"type": "job_posting.ai_governance", "url": "https://x/1", "text": "Head of AI Governance"},
    top_signals=[], alert_id=1, alert_reason="tier_1_bypass",
))
```

---

## Shippability check (do this in the first 15 minutes)

- **Input ready?** Yes — the agent writes `arthur_signal_*` to HubSpot and now
  POSTs qualified accounts to Clay.
- **Destination access ready?** ⚠️ Confirm Clay can enroll into **HubSpot
  Sequences** (Sales Hub Pro+; Clay's native HubSpot integration must be
  connected with sequence scope). **If enrollment is blocked → scope down:**
  write contacts + opener back to the HubSpot contact record (or CSV) for the
  rep to enroll manually. Still ships, still a revenue action.
- **Riskiest assumption:** that `signal_summary` is rich enough to personalize
  off. Spot-check 5 before building the message step.

*Build this one thing. If you finish: add a second persona-specific opener
variant — not a second research step, and not a second workflow.*
```
