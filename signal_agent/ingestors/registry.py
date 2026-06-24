"""Registry of enabled ingestors.

Adding a source in a future phase: implement `Ingestor`, register here.
Workflow code iterates this registry — no conditional branching by source name.

Sources can be turned off without code changes via the `DISABLED_INGESTORS`
env var (comma-separated `source` names) — see `settings.disabled_ingestors`.
"""
from __future__ import annotations

import structlog

from signal_agent.config import settings
from signal_agent.ingestors.ashby import AshbyIngestor
from signal_agent.ingestors.base import Ingestor
from signal_agent.ingestors.competitive import CompetitiveIngestor
from signal_agent.ingestors.conferences import ConferenceIngestor
from signal_agent.ingestors.greenhouse import GreenhouseIngestor
from signal_agent.ingestors.lever import LeverIngestor
from signal_agent.ingestors.linkedin import LinkedInHiresIngestor
from signal_agent.ingestors.news import NewsIngestor
from signal_agent.ingestors.sec_edgar import SecEdgarIngestor
from signal_agent.ingestors.workday import WorkdayIngestor

log = structlog.get_logger()

ALL_INGESTORS: list[type[Ingestor]] = [
    GreenhouseIngestor,
    LeverIngestor,
    AshbyIngestor,
    WorkdayIngestor,
    NewsIngestor,
    SecEdgarIngestor,
    CompetitiveIngestor,
    ConferenceIngestor,
    LinkedInHiresIngestor,  # no-op without API key
]


def _disabled_sources() -> set[str]:
    return {s.strip() for s in settings.disabled_ingestors.split(",") if s.strip()}


def enabled_ingestors() -> list[Ingestor]:
    disabled = _disabled_sources()
    active = [cls() for cls in ALL_INGESTORS if cls.source not in disabled]
    if disabled:
        log.info("ingestors.disabled", sources=sorted(disabled),
                 active=[i.source for i in active])
    return active
