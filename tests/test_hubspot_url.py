"""Tests for the HubSpot company-record URL builder (the 'Open in HubSpot' link)."""
from unittest.mock import patch

from signal_agent.integrations.hubspot import company_record_url


def test_none_id_returns_none():
    assert company_record_url(None) is None
    assert company_record_url("") is None


def test_uses_portal_id_and_record_path():
    with patch("signal_agent.integrations.hubspot.settings") as s:
        s.hubspot_portal_id = "5729023"
        url = company_record_url("35086622328")
    # Modern, working format: /contacts/<portal>/record/0-2/<id> (0-2 = company)
    assert url == "https://app.hubspot.com/contacts/5729023/record/0-2/35086622328"


def test_falls_back_to_legacy_when_no_portal_id():
    with patch("signal_agent.integrations.hubspot.settings") as s:
        s.hubspot_portal_id = ""
        url = company_record_url("35086622328")
    assert url == "https://app.hubspot.com/contacts/_/company/35086622328"
