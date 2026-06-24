# Arthur AI — Ideal Customer Profile (Source of Truth)

**This document is the single source of truth for what Arthur sells, who buys it,
and what signals indicate they're entering the buying window.** Every ingestor
keyword set, the LLM validator prompt, the scoring rubric weights, and the seed
target-account list in this repo derive from this doc.

> **Regulatory update (April 17, 2026 — OCC Bulletin 2026-13, interagency MRM
> rewrite):** Generative AI and agentic AI are now explicitly OUT of SR 11-7 /
> OCC 2011-12 scope, with an RFI to follow. Messaging and the validator's
> `summary_for_ae` must NOT claim SR 11-7 governs a prospect's GenAI/agentic
> systems. Current pegs for those systems: fair lending (Reg B, FCRA), NYDFS
> Part 500, FFIEC third-party risk, SEC AI-disclosure scrutiny, state laws
> (Colorado AI Act, CA DFPI), and the forthcoming RFI. SR 11-7 still applies to
> traditional quantitative models (credit-scoring regressions, VaR, PD) only.
> SR 11-7 stays in the ingestor keyword lists as a *detection* term (it still
> flags a model-risk-aware buyer); it is just no longer the governance frame we
> assert for GenAI.

When this doc changes, update:
- `signal_agent/ingestors/keywords.py` — job posting classifier keywords
- `signal_agent/ingestors/news.py` — news keyword groups
- `signal_agent/ingestors/sec_edgar.py` — SEC filing keywords
- `signal_agent/ingestors/competitive.py` — `COMPETITORS` list
- `signal_agent/scoring/validator.py` — LLM system prompt (positive/negative examples)
- `signal_agent/scoring/rubric.py` — per-signal-type weights
- `signal_agent/seeds/icp_companies.yaml` — target accounts + tier assignment
- `signal_agent/seeds/suppression.yaml` — disqualification patterns

---

## 1. What Arthur sells

**Agent Discovery & Governance (ADG) platform** — a unified control plane for
AI agents and models. Core capabilities:

- Automated agent discovery across cloud and on-prem
- Centralized agent registry with ownership and risk metadata
- Runtime guardrails: PII blocking, hallucination detection, toxicity,
  prompt injection defense
- Continuous evaluation across the agent development lifecycle
- Deep agent tracing for audit trails
- Access management policy enforcement

**Stack-agnostic.** Works across AWS Bedrock, Google Vertex AI, Agent Foundry,
custom frameworks. Deploys via federated Data Plane/Control Plane so sensitive
data stays in customer VPC. On AWS and Google Cloud Marketplaces.

Also offers: open-source **Arthur Evals Engine**, **Agent Development Toolkit**.

**Positioning:** the governance layer that lets enterprises scale agentic AI
without sacrificing safety, compliance, or visibility. Governance as an
accelerator, not a bottleneck.

---

## 2. ICP segments

### Segment A — Large Regulated Enterprises (Core)

- 5,000–50,000+ employees (Fortune 500 / Global 2000)
- Banking, capital markets, insurance, healthcare, government
- Multi-cloud (AWS + GCP, sometimes Azure or on-prem)
- Dozens to thousands of agents deployed or in development
- Head of AI / CAIO / AI CoE exists
- Existing model risk programs (SR 11-7, OCC, HIPAA, EU AI Act)

**Deal shape:** ACV $100K–$250K+; 3–9 mo cycle; marketplace procurement.
**Champions:** Head of AI, VP Data Science, CISO.
**Economic buyers:** CTO, CIO, CFO.
**Influencers:** CCO, Head of Model Risk, internal audit.

### Segment B — Mid-Market Scaling AI

- 1,000–5,000 employees
- Regional banks, fintechs, insurtechs, healthcare payers, enterprise SaaS
- Transitioning from pilot to production
- 5–20 person AI teams without dedicated governance function

**Deal shape:** ACV $25K–$100K; 1–4 mo cycle.
**Champions:** VP Engineering, Head of AI/ML, Lead Data Scientist.
**Economic buyers:** CTO, VP Engineering.

### Segment C — AI-Native Startups

- <500 employees, venture-backed
- AI agents are the core product (not internal use)
- Single-cloud, modern frameworks
- Need governance to sell to enterprise buyers

**Deal shape:** ACV $10K–$50K; 2–8 weeks.
**Entry:** Arthur Evals Engine (OSS) → platform.

---

## 3. Buying personas

| Persona | Titles | Cares about | Role |
|---|---|---|---|
| Head of AI / CAIO | VP AI, Head of AI/ML, CAIO | Scaling without risk; single pane of glass; board reporting | Primary champion / decision maker |
| CTO / CIO | CTO, CIO, SVP Tech | Architecture, consolidation, budget, marketplace alignment | Economic buyer |
| CISO | CISO, VP Infosec, Head of AppSec | Shadow agents, access control, audit, data sovereignty | Strong influencer |
| CCO / Head of Risk | CCO, CRO, Head of Model Risk, VP Compliance | Exam readiness, inventory, policy, SR 11-7 | Urgency creator |
| VP/Director DS/ML | VP DS, Dir ML Eng, Lead DS | Guardrails, evals, debug, stop "vibes-based" dev | Technical champion |

**Outbound lead:** Head of AI, CTO, or CISO. Loop in compliance later.

---

## 4. Trigger events (signal catalog)

These map 1:1 onto what our ingestors should detect and score:

| Trigger | Ingestor that catches it | Signal type |
|---|---|---|
| Hired Head of AI / CAIO / AI Governance lead | `linkedin`, `news` | `linkedin.exec_hire_ai`, `news.exec_hire_ai` |
| Posted MLOps / AI platform / AI governance jobs | `greenhouse`, `lever`, `ashby`, `workday` | `job_posting.*` |
| Announced AI strategy / CoE / agentic initiative | `news`, `sec_edgar` | `news.ai_product_launch`, `filing.sec_ai_mention` |
| AI-related incident, fine, or audit finding | `news`, `sec_edgar` | `news.ai_incident`, `filing.sec_ai_mention` |
| Multi-cloud footprint (AWS + GCP + Azure) | (no ingestor yet — Phase 4) | — |
| Using agent frameworks (LangChain, CrewAI, ADK, Bedrock Agents) | `competitive`, `news` | `news.ai_product_launch` |
| Thought leadership on AI governance | `conference`, `news` | `conference.speaker`, `news.*` |
| Executives stated AI governance is a priority | `news`, `sec_edgar` | `news.*`, `filing.sec_ai_mention` |
| Regulatory frameworks mentioned (SR 11-7, OCC, HIPAA, EU AI Act, NIST) | `sec_edgar`, `news` | `filing.sec_ai_mention` |

---

## 5. Disqualification (must-reject patterns)

These become `suppression.yaml` entries and LLM validator negative examples:

- No AI deployment / pure experimentation
- Single-agent single-use-case deployment (one chatbot)
- <500 employees with no AI-native product
- No regulated data / no compliance pressure
- Fully committed to a competing platform on long-term contract
- No technical AI leadership

---

## 6. Competitors (trigger co-occurrence)

Co-occurrence of an ICP company name with any of these in public posts is a
strong in-market signal. Used by `ingestors/competitive.py`.

| Category | Vendors | Arthur differentiation |
|---|---|---|
| Governance system of record | Credo AI, ModelOp | Arthur = governance + evals + guardrails + tracing + discovery. Others are inventory/policy only. |
| Security / runtime | WitnessAI, Pillar Security, Bifrost | Arthur adds governance + evals alongside runtime. Security-only tools miss discovery/evals/lifecycle. |
| Cloud-native AI | Salesforce Agentforce, Dataiku, DataRobot | These are build platforms with governance bolted on. Arthur = governance-first, platform-agnostic. |
| Observability | Arize, Langfuse, Braintrust | Arthur = governance (policy, access, discovery) beyond observability. |
| Data discovery / DLP | BigID, OneTrust | They find data exposure; Arthur finds agents, enforces guardrails, evaluates continuously. |

---

## 7. Target accounts (tiered)

From the ICP brief, tiered by signal-agent priority. Tier 1 = highest weighting
in scoring, most aggressive polling cadence.

| Company | Segment | Arthur tier | Notes |
|---|---|---|---|
| JPMorgan Chase | A | 1 | Existing customer. Massive AI, multi-cloud, OCC/SEC/FINRA. |
| Goldman Sachs | A | 1 | Engineering-forward, heavy AI adoption. |
| Fidelity Investments | A | 1 | Wealth mgmt + service AI, SEC/FINRA regulated. |
| UnitedHealth / Optum | A | 1 | Claims, clinical, member AI. HIPAA. |
| State Street | A | 1 | Asset mgmt/custody. Multi-cloud. |
| Kaiser Permanente | A | 1 | Clinical AI, HIPAA + state regs. |
| Allstate | B | 2 | Insurance, underwriting/claims/CX AI. |
| Broadridge Financial | B | 2 | Fintech selling AI to banks. Needs to prove governance. |
| Jackson Lewis P.C. | B | 2 | Mid-size law firm with active AI CoE. Already follows Arthur. |
| Upsolve | C | 3 | Existing customer. AI-native. Startup Partner reference. |

---

## 8. Regulatory anchors (keyword seeds)

These phrases in public signals are high-value because they map to buyer urgency:

- **Financial:** SR 11-7, OCC, FINRA, SEC exam priorities, FinCEN
- **Healthcare:** HIPAA, FDA AI/ML SaMD, ONC HTI-1
- **Horizontal:** EU AI Act, NIST AI RMF, ISO 42001, SOC 2 for AI
- **Risk mgmt:** model risk management, MRM, agentic AI risk

---

## 9. Arthur-specific phrase list

Phrases that uniquely map to Arthur's pitch surface (useful for LLM prompt +
news keyword tuning):

- "agent discovery", "agent inventory", "shadow agents"
- "agentic AI", "agent sprawl"
- "AI governance", "responsible AI", "AI risk"
- "runtime guardrails", "PII blocking", "hallucination detection",
  "prompt injection defense"
- "continuous evaluation", "agent tracing", "model risk management"
- "federated data plane", "data sovereignty for AI"
- AWS Bedrock, Google Vertex AI, Agent Foundry
- LangChain, CrewAI, ADK, Google ADK, Bedrock Agents
