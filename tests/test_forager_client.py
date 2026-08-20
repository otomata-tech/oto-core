"""Contrat du client Forager (REST, `X-API-KEY`, `account_id` résolu au runtime)."""
from __future__ import annotations

import pytest

from oto.tools.forager import client as fc
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        self.content = body is not None

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    return seen


def _client(account_id=42):
    return fc.ForagerClient(api_key="fg-test-key", account_id=account_id)


def test_auth_header_is_x_api_key(capture):
    _client().get_current_user()
    assert capture["kwargs"]["headers"] == {"X-API-KEY": "fg-test-key"}


def test_get_current_user_hits_users_current(capture):
    _client().get_current_user()
    assert capture["method"] == "GET"
    assert capture["url"] == "https://api-v2.forager.ai/api/users/current/"


def test_account_id_passed_at_construction_is_used_without_extra_call(capture):
    _client(account_id=42).search_job_posts(title="engineer")
    assert capture["url"] == "https://api-v2.forager.ai/api/42/datastorage/job_search/"
    assert capture["kwargs"]["json"] == {"title": "engineer"}


def test_resolve_account_id_single_account_succeeds(monkeypatch):
    def fake_request(method, url, **kwargs):
        assert url.endswith("/api/users/current/")
        return _Resp(200, {"accounts": [{"id": 7, "name": "Acme"}]})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    c = fc.ForagerClient(api_key="k")
    assert c.resolve_account_id() == 7
    assert c._account_id == 7  # cached on the instance


def test_resolve_account_id_multiple_accounts_refuses(monkeypatch):
    def fake_request(method, url, **kwargs):
        return _Resp(200, {"accounts": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    c = fc.ForagerClient(api_key="k")
    with pytest.raises(ValueError, match="multiple accounts"):
        c.resolve_account_id()


def test_resolve_account_id_no_accounts_refuses(monkeypatch):
    def fake_request(method, url, **kwargs):
        return _Resp(200, {"accounts": []})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    c = fc.ForagerClient(api_key="k")
    with pytest.raises(ValueError, match="no account"):
        c.resolve_account_id()


def test_search_job_posts_totals(capture):
    _client().search_job_posts_totals(is_remote=True)
    assert capture["url"].endswith("/datastorage/job_search/totals/")
    assert capture["kwargs"]["json"] == {"is_remote": True}


def test_search_organizations(capture):
    _client().search_organizations(locations=[1, 2])
    assert capture["url"].endswith("/datastorage/organization_search/")
    assert capture["kwargs"]["json"] == {"locations": [1, 2]}


def test_search_person_roles_totals(capture):
    _client().search_person_roles_totals(role_is_current=True)
    assert capture["url"].endswith("/datastorage/person_role_search/totals/")


def test_lookup_website_by_domain(capture):
    _client().lookup_website(domain="acme.com")
    assert capture["url"].endswith("/datastorage/website_detail_lookup/")
    assert capture["kwargs"]["json"] == {"domain": "acme.com"}


def test_lookup_person_by_email(capture):
    _client().lookup_person_by_email("jane@acme.com")
    assert capture["url"].endswith("/datastorage/person_detail_reverse_lookup/by_email/")
    assert capture["kwargs"]["json"] == {"email": "jane@acme.com"}


def test_lookup_person_by_phone_number(capture):
    _client().lookup_person_by_phone_number("+15551234567")
    assert capture["url"].endswith("/datastorage/person_detail_reverse_lookup/by_phone_number/")


def test_lookup_person_personal_emails_one_of(capture):
    _client().lookup_person_personal_emails(person_id=99)
    assert capture["kwargs"]["json"] == {"person_id": 99}


def test_lookup_person_work_emails_with_enrichment_flag(capture):
    _client().lookup_person_work_emails(linkedin_public_identifier="janedoe", do_contacts_enrichment=True)
    assert capture["kwargs"]["json"] == {
        "linkedin_public_identifier": "janedoe",
        "do_contacts_enrichment": True,
    }


def test_submit_personal_email_feedback_required_and_optional_fields(capture):
    _client().submit_personal_email_feedback("jane@gmail.com", "valid", True, name="Jane Doe")
    assert capture["url"].endswith("/datastorage/feedback/personal_emails/")
    assert capture["kwargs"]["json"] == {
        "email": "jane@gmail.com",
        "contact_status": "valid",
        "is_correct_person": True,
        "name": "Jane Doe",
    }


def test_submit_phone_number_feedback(capture):
    _client().submit_phone_number_feedback("+15551234567", "connected", False)
    assert capture["url"].endswith("/datastorage/feedback/phone_numbers/")
    assert capture["kwargs"]["json"] == {
        "phone_number": "+15551234567",
        "contact_status": "connected",
        "is_correct_person": False,
    }


def test_submit_work_email_feedback(capture):
    _client().submit_work_email_feedback("jane@acme.com", "invalid", False, person_id=5)
    assert capture["url"].endswith("/datastorage/feedback/work_emails/")


def test_list_balance_change_logs_query_params(capture):
    _client().list_balance_change_logs(date_created_start="2026-01-01", page=2)
    assert capture["method"] == "GET"
    assert capture["url"].endswith("/subscriptions/balance_change_logs/")
    assert capture["kwargs"]["params"] == {"date_created_start": "2026-01-01", "page": 2}


def test_get_balance_change_totals(capture):
    _client().get_balance_change_totals()
    assert capture["url"].endswith("/subscriptions/balance_change_logs/totals/")
    assert capture["kwargs"]["params"] == {}


def test_autocomplete_locations(capture):
    _client().autocomplete_locations("Paris", page=1)
    assert capture["url"] == "https://api-v2.forager.ai/api/42/datastorage/autocomplete/locations/"
    assert capture["kwargs"]["params"] == {"q": "Paris", "page": 1}


def test_autocomplete_industries_requires_q_only(capture):
    _client().autocomplete_industries("SaaS")
    assert capture["kwargs"]["params"] == {"q": "SaaS"}


def test_autocomplete_organizations(capture):
    _client().autocomplete_organizations("Acme")
    assert capture["url"].endswith("/datastorage/autocomplete/organizations/")


def test_autocomplete_organization_keywords(capture):
    _client().autocomplete_organization_keywords("fintech")
    assert capture["url"].endswith("/datastorage/autocomplete/organization_keywords/")


def test_autocomplete_person_skills(capture):
    _client().autocomplete_person_skills("python")
    assert capture["url"].endswith("/datastorage/autocomplete/person_skills/")


def test_autocomplete_web_technologies(capture):
    _client().autocomplete_web_technologies("react")
    assert capture["url"].endswith("/datastorage/autocomplete/web_technologies/")


def test_upstream_error_raises_typed_exception(monkeypatch):
    def fake_request(method, url, **kwargs):
        return _Resp(401, {"detail": "Invalid API key"})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as exc_info:
        _client().get_current_user()
    assert exc_info.value.status_code == 401
