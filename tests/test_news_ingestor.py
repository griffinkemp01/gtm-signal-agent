from datetime import datetime, timezone

from signal_agent.ingestors.news import _classify_news, _split_title, news_dedup_key


def test_news_dedup_key_is_stable_and_link_independent():
    d = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # Same headline + date => same key regardless of the (volatile) Google link.
    assert news_dedup_key("Acme names Chief AI Officer", d) == news_dedup_key(
        "Acme names Chief AI Officer", d
    )
    assert news_dedup_key("Acme names Chief AI Officer", d).startswith("news:")


def test_news_dedup_key_differs_by_headline():
    d = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert news_dedup_key("Story A", d) != news_dedup_key("Story B", d)


def test_split_title_with_publication():
    headline, pub = _split_title("Acme hires Chief AI Officer - TechCrunch")
    assert headline == "Acme hires Chief AI Officer"
    assert pub == "TechCrunch"


def test_split_title_without_dash():
    headline, pub = _split_title("Standalone headline")
    assert headline == "Standalone headline"
    assert pub == ""


def test_exec_hire_classification():
    result = _classify_news(
        "Acme appoints new Chief AI Officer - Reuters",
        "The company said Jane Doe will lead AI strategy.",
    )
    assert result is not None
    assert result[0] == "news.exec_hire_ai"
    assert "chief ai officer" in result[1]


def test_incident_classification():
    result = _classify_news(
        "Bank faces AI bias lawsuit over lending algorithm",
        "Class action filed over alleged algorithmic discrimination.",
    )
    assert result is not None
    assert result[0] == "news.ai_incident"


def test_product_launch_classification():
    result = _classify_news(
        "Acme launches AI assistant for customer service",
        "New product uses large language models to respond.",
    )
    assert result is not None
    assert result[0] == "news.ai_product_launch"


def test_unrelated_news_returns_none():
    result = _classify_news(
        "Acme reports Q4 earnings beat",
        "Revenue came in above analyst expectations.",
    )
    assert result is None
