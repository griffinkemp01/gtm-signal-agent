"""LLM signal validator.

Runs a Claude call that answers three questions:
 1. Is this signal genuinely about AI agent scaling (vs. a keyword false positive)?
 2. Confidence 0.0–1.0.
 3. Extract structured details + write a one-sentence "why this matters for Arthur".

Output is strict JSON, parsed and validated. Results are cached by hash of the
(signal_type + signal_text) so re-ingesting the same posting costs $0. The
cache TTL is controlled by `LLM_CACHE_TTL_DAYS` (default 67 days, which is
7 days beyond the scoring window so signals inside the window never miss).

The system prompt is the other main tuning lever besides `rubric.py`. When the
weekly review flags false positives, edit the negative examples here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import structlog
from anthropic import Anthropic
from sqlalchemy import select

from signal_agent.config import settings
from signal_agent.db import session_scope
from signal_agent.models import LLMCache
from signal_agent.schemas import NormalizedSignal, ValidationResult

log = structlog.get_logger()

SYSTEM_PROMPT = """\
You are a signal classifier for Arthur (arthur.ai). The full ICP brief this
prompt is derived from lives at docs/icp.md in the signal-agent repo.

What Arthur sells: the Agent Discovery & Governance (ADG) platform — a unified
control plane for AI agents and models. Core capabilities: automated agent
discovery, centralized agent registry, runtime guardrails (PII blocking,
hallucination detection, toxicity, prompt injection defense), continuous
evaluation, deep agent tracing, access management. Federated Data Plane /
Control Plane architecture keeps data in customer VPC. Stack-agnostic
(AWS Bedrock, Google Vertex AI, Agent Foundry, custom). Available on AWS +
Google Cloud Marketplaces.

Who Arthur sells to:
 - Segment A (Core): Fortune 500 / Global 2000, 5K–50K+ employees, regulated
   (banking, capital markets, insurance, healthcare, government), multi-cloud,
   existing model risk programs (SR 11-7, OCC, HIPAA, EU AI Act), has a
   Head of AI / CAIO / AI CoE, dozens-to-thousands of agents in dev/prod.
   ACV $100K–$250K+.
 - Segment B: Mid-market (1K–5K emp), fintechs, insurtechs, healthcare payers,
   enterprise SaaS, moving agents from pilot to production. ACV $25K–$100K.
 - Segment C: AI-native startups (<500), agents as their product, need
   governance to sell to enterprise. ACV $10K–$50K.

Buying personas to watch for: Head of AI / CAIO, CTO/CIO, CISO, CCO /
Head of Model Risk, VP/Director Data Science or ML Engineering.

You will be given a public signal — job posting, news article, SEC filing
excerpt, HN/Reddit post, or conference speaker listing. Decide whether it
is a genuine buying-intent signal for Arthur, and explain why.

POSITIVE signals (strong):
 Job postings:
 - AI Governance, Responsible AI, AI Risk, Model Risk Management roles
 - Chief AI Officer, Head of AI, VP of AI, Head of ML Platform, Head of MLOps
 - Roles mentioning EU AI Act, NIST AI RMF, SR 11-7, OCC, HIPAA, AI audit
 - Roles at companies clearly scaling an AI platform (multiple platform roles,
   references to existing LLM/agent deployments, Bedrock/Vertex/ADK stack)
 - AI Security / prompt injection / shadow-AI governance roles (CISO-world)

 News:
 - AI agent / LLM product launches in production
 - New Chief AI Officer or Head of AI hires
 - AI incidents (hallucination lawsuit, bias claim, LLM data leak, regulator
   inquiry) — HIGHEST intent, creates immediate urgency
 - Regulator actions (FINRA 2026 AI report, SEC AI exam priorities, OCC)
 - Foundation model / cloud AI marketplace commits

 SEC filings:
 - Substantive (non-boilerplate) mentions of AI/ML as a strategic initiative
   or material risk factor in MD&A or Risk Factors
 - Specific regulatory framework citations (SR 11-7, EU AI Act, NIST AI RMF)
 - Disclosure of LLM / generative AI / agentic AI deployment in operations

 HN / Reddit co-occurrence:
 - An ICP company name mentioned alongside an Arthur competitor (Credo AI,
   ModelOp, WitnessAI, Pillar Security, Arize, Langfuse, Braintrust, BigID,
   etc.) indicates in-market evaluation.

 Conference speakers:
 - An employee from an ICP company speaking at an MLOps / AI governance /
   agentic AI conference indicates they have production-scale work to discuss.

NEGATIVE signals (mark is_valid=false):
 - Recruiting / staffing agencies posting on behalf of unnamed clients
 - Generic data science / analyst roles that happen to mention "AI"
 - AI-ADJACENT roles (AI marketing, AI sales content, AI copywriting)
 - News where the company is only tangentially mentioned
 - SEC boilerplate ("we may be affected by AI regulation") without specifics
 - Postings from AI *vendors* selling to the same buyers Arthur sells to
   (unless the vendor itself is operating production AI at enterprise scale)
 - Academic postings, internships, contractor/freelance listings
 - Single-chatbot / single-use-case deployments with no evidence of sprawl
 - Companies <500 employees with no AI-native product (fails ICP)
 - Fully committed to a competing platform with no switching signal

REGULATORY CURRENCY (as of the April 17, 2026 interagency MRM rewrite,
OCC Bulletin 2026-13) — this is load-bearing for summary_for_ae:
 - Generative AI and agentic AI were explicitly carved OUT of SR 11-7 /
   OCC 2011-12 scope. Do NOT write a summary_for_ae claiming SR 11-7 governs a
   company's GenAI or agentic systems. That framing is now wrong and makes
   Arthur look behind the news to a model-risk audience.
 - For GenAI / agentic signals at US financial institutions, the live
   governance pegs are fair lending (Reg B, FCRA), NYDFS Part 500, FFIEC
   third-party risk, SEC AI-disclosure scrutiny, and state laws (Colorado AI
   Act, CA DFPI), plus the forthcoming RFI that will define the next framework.
 - SR 11-7 / OCC 2011-12 remain valid ONLY for traditional quantitative models
   (credit-scoring regressions, VaR, PD). Reference them in a summary only when
   the signal is about classic models, never about agents or LLMs.
 - You may still record "SR 11-7" in extracted.regulatory_mentioned when the
   source text itself names it; just don't assert it as the governing frame for
   GenAI in summary_for_ae.

Respond with ONLY a JSON object:
{
  "is_valid": boolean,
  "confidence": number between 0 and 1,
  "reasoning": string, 1-2 sentences citing which Arthur ICP criterion applies,
  "summary_for_ae": string, ONE sentence describing why this matters for Arthur's
     pitch (reference the specific capability: discovery, guardrails, evals,
     tracing, access policy, or a CURRENT compliance framework per the
     REGULATORY CURRENCY note above — never SR 11-7 for GenAI/agentic systems),
  "extracted": {
    "role_title": string or null,
    "seniority": string or null,          // "ic" | "manager" | "director" | "vp" | "c_level"
    "regulatory_mentioned": string or null,   // SR 11-7, EU AI Act, NIST AI RMF, HIPAA, OCC, ...
    "product_mentioned": string or null,      // Bedrock, Vertex, LangChain, Agentforce, ...
    "competitor_mentioned": string or null,   // Arthur competitor named in the signal
    "incident_type": string or null,          // hallucination|bias|leak|injection|regulator
    "segment_guess": string or null           // "A" | "B" | "C"
  }
}
Do not include any other text. Do not wrap in code fences.
"""


def _cache_key(signal_type: str, signal_text: str) -> str:
    h = hashlib.sha256(f"{signal_type}\n{signal_text}".encode()).hexdigest()
    return h[:40]


def _get_cached(key: str) -> ValidationResult | None:
    with session_scope() as s:
        row = s.execute(
            select(LLMCache).where(
                LLMCache.cache_key == key,
                LLMCache.expires_at > datetime.now(timezone.utc),
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return ValidationResult.model_validate(row.result_json)


def _store_cached(key: str, result: ValidationResult) -> None:
    with session_scope() as s:
        s.add(
            LLMCache(
                cache_key=key,
                result_json=result.model_dump(),
                expires_at=datetime.now(timezone.utc) + timedelta(
                    days=settings.llm_cache_ttl_days
                ),
            )
        )


def _emit_validator_span_attrs(signal: NormalizedSignal, result: ValidationResult,
                               cache_hit: bool) -> None:
    """Set span attributes on the currently-active OTel span so Arthur (or any
    OTel-aware backend) can run continuous evals against the validator
    decisions without re-parsing the raw LLM output text.

    Keys are prefixed `signal_agent.validator.*` to namespace away from the
    auto-instrumented Anthropic + httpx attributes. Lazy import so that this
    module stays import-safe even if observability deps aren't installed.
    """
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        # Inputs the eval needs — these come from the signal we asked the
        # LLM to validate.
        span.set_attribute("signal_agent.validator.company_name", signal.company_name)
        span.set_attribute("signal_agent.validator.company_domain", signal.company_domain)
        span.set_attribute("signal_agent.validator.signal_type", signal.signal_type)
        span.set_attribute("signal_agent.validator.source", signal.source)
        span.set_attribute("signal_agent.validator.source_url", signal.source_url)
        # Truncate signal_text to keep span size reasonable. OTel attribute
        # values have implementation-defined size limits; 4 KB is safe.
        span.set_attribute(
            "signal_agent.validator.signal_text",
            (signal.signal_text or "")[:4000],
        )
        # Outputs — what the LLM decided.
        span.set_attribute("signal_agent.validator.is_valid", bool(result.is_valid))
        span.set_attribute("signal_agent.validator.confidence", float(result.confidence))
        span.set_attribute("signal_agent.validator.reasoning", (result.reasoning or "")[:2000])
        span.set_attribute("signal_agent.validator.summary_for_ae", (result.summary_for_ae or "")[:1000])
        span.set_attribute("signal_agent.validator.cache_hit", bool(cache_hit))
    except Exception:
        # Tracing must never break the pipeline.
        pass


def validate_signal(signal: NormalizedSignal, client: Anthropic | None = None) -> ValidationResult:
    key = _cache_key(signal.signal_type, signal.signal_text)
    cached = _get_cached(key)
    if cached is not None:
        log.debug("llm.cache_hit", key=key)
        # Still emit span attrs on cache hits so continuous evals see every
        # decision the validator returns, not just first-time decisions.
        _emit_validator_span_attrs(signal, cached, cache_hit=True)
        return cached

    client = client or Anthropic(api_key=settings.anthropic_api_key)

    user_msg = (
        f"Source: {signal.source}\n"
        f"Signal type (ingestor hint): {signal.signal_type}\n"
        f"Company: {signal.company_name} ({signal.company_domain})\n"
        f"URL: {signal.source_url}\n\n"
        f"Content:\n{signal.signal_text}"
    )

    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")

    # Claude often wraps JSON in ```json ... ``` fences despite prompt instructions.
    # Extract the JSON object robustly before parsing.
    def _extract_json(raw: str) -> str:
        s = raw.strip()
        if s.startswith("```"):
            # strip the opening fence + optional language tag
            s = s.split("\n", 1)[1] if "\n" in s else s[3:]
            # strip the trailing fence
            if s.rstrip().endswith("```"):
                s = s.rstrip()[:-3]
        # Find the outermost {...} block
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return s[start:end + 1]
        return s

    try:
        data = json.loads(_extract_json(text))
        result = ValidationResult.model_validate(data)
    except Exception as e:
        # On parse failure: treat as low confidence so it goes to the review queue
        # rather than firing a noisy alert or dropping silently.
        log.warning("llm.parse_failed", err=str(e), raw=text[:500])
        result = ValidationResult(
            is_valid=False,
            confidence=0.0,
            reasoning=f"LLM output could not be parsed: {e}",
            summary_for_ae="",
        )

    _store_cached(key, result)
    _emit_validator_span_attrs(signal, result, cache_hit=False)
    return result
