"""News ingestor — Google News RSS per ICP company.

Why RSS / Google News: no auth, no API key, no cost. For each company we issue
a site-agnostic search against Google News with a query built from the company
name + AI-signal keywords, then parse the RSS feed and pass matches through the
same keyword→LLM pipeline used for jobs.

Limitations:
 - Google News rate-limits heavily if you poll aggressively. Daily cadence is
   fine; sub-hourly will get you throttled.
 - Title text is `<original title> - <publication>`. We split on the last `-`.
 - Descriptions are truncated and often HTML-encoded. We strip and truncate.
 - If a Phase-3 budget opens up, swap to a managed feed (GDELT or NewsAPI).

Keyword groups for news are defined in this file (not shared with jobs) because
the matcher operates on a different surface: headlines + short descriptions,
not job titles + long descriptions.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import structlog
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from signal_agent.ingestors.base import CompanyTarget, Ingestor
from signal_agent.ingestors.html_util import strip_html
from signal_agent.schemas import NormalizedSignal

log = structlog.get_logger()

RSS_BASE = "https://news.google.com/rss/search"

# News-specific keyword groups (source: docs/icp.md §4).
# Each group produces a distinct signal_type so the rubric can weight it.
NEWS_KEYWORD_GROUPS: list[tuple[str, list[str]]] = [
    (
        "news.ai_incident",
        [
            "ai incident", "ai hallucination", "llm hallucination",
            "ai lawsuit", "ai complaint", "chatbot lawsuit",
            "data leak", "ai data leak", "ai bias lawsuit",
            "regulator investigation ai", "ai enforcement",
            "prompt injection attack", "model poisoning",
            # Regulator-driven triggers (high urgency for Arthur's pitch)
            "finra ai", "occ ai", "sec ai exam", "finra 2026",
            "ai audit finding",
        ],
    ),
    (
        "news.exec_hire_ai",
        [
            "chief ai officer", "head of ai", "vp of ai",
            "head of agentic ai", "head of responsible ai",
            "appoints ai", "hires ai", "names chief ai",
            "ai lead hire", "chief artificial intelligence officer",
            "head of ai governance", "head of model risk",
            "new ai leader",
        ],
    ),
    (
        "news.ai_product_launch",
        [
            "launches ai", "unveils ai", "announces ai",
            "ai agent", "ai assistant", "agentic product",
            "genai product", "rolls out ai", "debuts ai",
            "ai strategy", "ai center of excellence",
            # Partnerships / cloud AI commits — direct ICP signal
            "bedrock partnership", "vertex ai partnership",
            "anthropic partnership", "openai partnership",
            "aws marketplace ai", "google cloud ai",
        ],
    ),
]

# Per-company query template. `name` is quoted so Google treats it as an exact phrase.
# Terms reflect Arthur's trigger events (docs/icp.md §4).
QUERY_TEMPLATE = (
    '"{name}" (AI OR "machine learning" OR "artificial intelligence" OR governance '
    'OR "agentic AI" OR "AI agent" OR incident OR "chief AI officer" OR "head of AI" '
    'OR hallucination OR bias OR "model risk" OR "EU AI Act" OR "NIST AI RMF" '
    'OR "SR 11-7" OR FINRA OR OCC)'
)


def _classify_news(title: str, description: str) -> tuple[str, list[str]] | None:
    haystack = f"{title}\n{description}".lower()
    for signal_type, keywords in NEWS_KEYWORD_GROUPS:
        matched = [k for k in keywords if k in haystack]
        if matched:
            return signal_type, matched
    return None


def _split_title(title: str) -> tuple[str, str]:
    """Google News RSS titles are 'Headline - Publication'. Split on the last ' - '."""
    if " - " in title:
        head, _, pub = title.rpartition(" - ")
        return head.strip(), pub.strip()
    return title.strip(), ""


def news_dedup_key(title: str, pub_date: datetime) -> str:
    """Stable dedup identity for a news item.

    Keys on headline + publish date, NOT the Google News link — the link is a
    redirect token that changes every fetch, so keying on it re-inserts the same
    story as a new row on each run and inflates cumulative scores. Namespaced
    with a `news:` prefix so it can't collide with a URL-based dedup_key.
    """
    h = hashlib.sha1(f"{title}|{pub_date.isoformat()}".encode()).hexdigest()[:16]
    return f"news:{h}"


class NewsIngestor(Ingestor):
    source = "news"

    # Only return items newer than this many days to keep the cache + scoring
    # tight. Rubric's freshness decay handles older items gracefully anyway.
    MAX_AGE_DAYS = 30

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # UA matters: Google blocks generic httpx UA sometimes.
            self._client = httpx.AsyncClient(
                timeout=20.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SignalAgent/0.1)"},
            )
        return self._client

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        query = QUERY_TEMPLATE.format(name=target.name)
        url = f"{RSS_BASE}?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        client = await self._get_client()

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                reraise=True,
            ):
                with attempt:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    body = resp.text
        except Exception as e:
            log.warning("news.fetch_failed", company=target.name, err=str(e))
            return

        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            log.warning("news.xml_parse_failed", company=target.name, err=str(e))
            return

        now = datetime.now(timezone.utc)
        for item in root.iterfind(".//item"):
            title_raw = (item.findtext("title") or "").strip()
            if not title_raw:
                continue
            title, publication = _split_title(title_raw)
            description = strip_html(item.findtext("description") or "")
            link = (item.findtext("link") or "").strip()
            pub_date_raw = item.findtext("pubDate") or ""

            # Parse RFC-822 pubDate; skip items we can't date.
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
            except (TypeError, ValueError):
                continue
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            age_days = (now - pub_date).total_seconds() / 86400
            if age_days > self.MAX_AGE_DAYS or age_days < 0:
                continue

            # Weak pre-filter: the article must mention the company name
            # (Google sometimes returns tangential results).
            if target.name.lower() not in f"{title} {description}".lower():
                continue

            classification = _classify_news(title, description)
            if classification is None:
                continue
            signal_type, matched = classification

            # Stable dedup key from title+pub_date — the link changes each fetch.
            dedup = news_dedup_key(title, pub_date)

            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type=signal_type,
                source=self.source,
                source_url=link or dedup,
                dedup_key=dedup,
                signal_text=f"{title}\n\n{description[:400]}",
                raw_payload={
                    "title": title,
                    "publication": publication,
                    "pub_date": pub_date.isoformat(),
                    "description_excerpt": description[:400],
                    "rss_item_hash": dedup,
                },
                detected_at=pub_date,
                matched_keywords=matched,
                suggested_tier_hint=1 if signal_type == "news.ai_incident" else 2,
            )
