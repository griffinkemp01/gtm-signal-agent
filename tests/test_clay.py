from unittest.mock import MagicMock

from signal_agent.integrations.clay import (
    SEGMENT_A_PERSONAS,
    ClayAccountPayload,
    ClayPusher,
)


def _payload(**overrides) -> ClayAccountPayload:
    base = {
        "company_name": "Acme Bank",
        "company_domain": "acme.com",
        "hubspot_id": "123",
        "target_tier": 1,
        "segment": "A",
        "cumulative_score": 14.2,
        "tier": "tier_1",
        "signal_summary": "Acme hired its first Head of AI Governance and put an "
        "LLM fraud agent into production.",
        "triggering_signal": {
            "type": "job_posting.ai_governance",
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "text": "Head of AI Governance",
        },
        "top_signals": [
            {
                "type": "job_posting.ai_governance",
                "url": "https://x/1",
                "text": "Head of AI Governance",
            },
        ],
        "qualification_reason": "first_push",
        "also_alerted": True,
    }
    base.update(overrides)
    return ClayAccountPayload(**base)


def test_payload_to_dict_shape():
    body = _payload().to_dict()
    assert body["event"] == "qualified_account"
    assert body["qualification_reason"] == "first_push"
    assert body["also_alerted"] is True
    assert body["company"]["domain"] == "acme.com"
    # Exact URL format is covered in test_hubspot_url.py; here just confirm the
    # id is carried into a HubSpot URL (independent of whether a portal id is set).
    assert body["company"]["hubspot_url"].startswith("https://app.hubspot.com/contacts/")
    assert "123" in body["company"]["hubspot_url"]
    assert body["score"]["cumulative_score"] == 14.2
    assert body["score"]["tier"] == "tier_1"
    assert "Head of AI Governance" in body["signal_summary"]
    assert body["triggering_signal"]["type"] == "job_posting.ai_governance"
    # The Segment A persona set rides along so Clay knows who to source.
    assert body["target_personas"] == SEGMENT_A_PERSONAS
    assert "pushed_at" in body


def test_hubspot_url_is_none_without_id():
    body = _payload(hubspot_id=None).to_dict()
    assert body["company"]["hubspot_id"] is None
    assert body["company"]["hubspot_url"] is None


def test_segment_a_personas_expanded_for_scale():
    # The 3 core champions plus economic buyers + the model-risk influencer,
    # so each qualified account yields more reachable contacts.
    assert "Head of AI / Chief AI Officer" in SEGMENT_A_PERSONAS
    assert "Head of Model Risk Management" in SEGMENT_A_PERSONAS
    assert "CTO / CIO" in SEGMENT_A_PERSONAS
    assert len(SEGMENT_A_PERSONAS) >= 5


def test_push_noop_when_no_webhook():
    client = MagicMock()
    pusher = ClayPusher(webhook_url="", client=client)
    assert pusher.enabled is False
    assert pusher.push(_payload()) is False
    client.post.assert_not_called()


def test_push_posts_to_webhook():
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=200)
    pusher = ClayPusher(webhook_url="https://api.clay.com/webhook/abc", client=client)

    assert pusher.push(_payload()) is True
    client.post.assert_called_once()
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.clay.com/webhook/abc"
    assert kwargs["json"]["company"]["domain"] == "acme.com"
    assert "signal_summary" in kwargs["json"]


def test_push_returns_false_on_http_error():
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=500, text="boom")
    pusher = ClayPusher(webhook_url="https://api.clay.com/webhook/abc", client=client)
    assert pusher.push(_payload()) is False


def test_push_swallows_exceptions():
    # A Clay outage must never abort the alert pipeline.
    client = MagicMock()
    client.post.side_effect = RuntimeError("connection reset")
    pusher = ClayPusher(webhook_url="https://api.clay.com/webhook/abc", client=client)
    assert pusher.push(_payload()) is False
