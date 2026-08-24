"""Contrat du client Snitcher (REST v1, Bearer PAT).

Mocke `requests.request` : vérifie méthode/URL/query/body pour chacun des 27
endpoints du spec OpenAPI officiel, le header d'auth, le nettoyage des params
None, les gardes ValueError (list_contacts sans cible, list_sessions sans
date), et le typage des erreurs amont.
"""
from __future__ import annotations

import pytest

from oto.tools.snitcher import client as sc
from oto.tools.common.errors import UpstreamHTTPError

BASE = "https://api.snitcher.com/v1"


class _Resp:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        self.content = str(body).encode() if body is not None else b""
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"data": []})

    monkeypatch.setattr(sc.requests, "request", fake_request)
    return seen


def _client():
    return sc.SnitcherClient(api_key="snitch-test-token")


def test_auth_header_is_bearer(capture):
    _client().get_me()
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer snitch-test-token"
    assert capture["kwargs"]["headers"]["Accept"] == "application/json"


# --- user / workspaces --------------------------------------------------------

def test_get_me(capture):
    _client().get_me()
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/me")


def test_list_workspaces_drops_none_params(capture):
    _client().list_workspaces(page=2)
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/workspaces")
    assert capture["kwargs"]["params"] == {"page": 2}


def test_create_workspace(capture):
    _client().create_workspace("https://example.com")
    assert (capture["method"], capture["url"]) == ("POST", f"{BASE}/workspaces")
    assert capture["kwargs"]["json"] == {"url": "https://example.com"}


def test_get_update_delete_workspace(capture):
    _client().get_workspace("ws_1")
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/workspaces/ws_1")
    _client().update_workspace("ws_1", usage_limit=5000)
    assert (capture["method"], capture["url"]) == ("PATCH", f"{BASE}/workspaces/ws_1")
    assert capture["kwargs"]["json"] == {"usage_limit": 5000}
    _client().delete_workspace("ws_1")
    assert (capture["method"], capture["url"]) == ("DELETE", f"{BASE}/workspaces/ws_1")


def test_invite_and_workspace_tag(capture):
    _client().invite_user("ws_1", "j@example.com")
    assert (capture["method"], capture["url"]) == ("POST", f"{BASE}/workspaces/ws_1/users/invite")
    assert capture["kwargs"]["json"] == {"email": "j@example.com"}
    _client().create_workspace_tag("ws_1", "hot lead")
    assert (capture["method"], capture["url"]) == ("POST", f"{BASE}/workspaces/ws_1/tags")
    assert capture["kwargs"]["json"] == {"tag_name": "hot lead"}


# --- organisations ------------------------------------------------------------

def test_list_organisations_query_params(capture):
    _client().list_organisations("ws_1", segment_uuid="seg_1", date_from="2026-08-01",
                                 date_to="2026-08-23", name="acme", page=1, size=100)
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/workspaces/ws_1/organisations")
    assert capture["kwargs"]["params"] == {
        "segmentUuid": "seg_1", "date_from": "2026-08-01", "date_to": "2026-08-23",
        "name": "acme", "page": 1, "size": 100}


def test_filter_organisations_posts_filtergroup(capture):
    filters = {"operator": "AND", "conditions": [
        {"field": "employees", "comparison": "greater_than", "value": 200}]}
    _client().filter_organisations("ws_1", filters, size=50)
    assert (capture["method"], capture["url"]) == ("POST", f"{BASE}/workspaces/ws_1/organisations")
    assert capture["kwargs"]["json"] == {"filters": filters}
    assert capture["kwargs"]["params"] == {"size": 50}


def test_get_organisation_and_tags(capture):
    _client().get_organisation("ws_1", "org_1")
    assert (capture["method"], capture["url"]) == (
        "GET", f"{BASE}/workspaces/ws_1/organisations/org_1")
    _client().add_organisation_tag("ws_1", "org_1", "hot")
    assert (capture["method"], capture["url"]) == (
        "POST", f"{BASE}/workspaces/ws_1/organisations/org_1/tags")
    assert capture["kwargs"]["json"] == {"tag_name": "hot"}
    _client().remove_organisation_tag("ws_1", "org_1", "hot")
    assert (capture["method"], capture["url"]) == (
        "DELETE", f"{BASE}/workspaces/ws_1/organisations/org_1/tags")
    assert capture["kwargs"]["json"] == {"tag_name": "hot"}


# --- contacts -----------------------------------------------------------------

def test_list_contacts_requires_a_target():
    with pytest.raises(ValueError, match="organisation_uuid or domain"):
        _client().list_contacts("ws_1")


def test_list_contacts_by_domain(capture):
    _client().list_contacts("ws_1", domain="acme.com", size=10)
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/workspaces/ws_1/contacts")
    assert capture["kwargs"]["params"] == {"domain": "acme.com", "size": 10}


def test_reveal_contact_email_is_a_put(capture):
    _client().reveal_contact_email("ws_1", "c_1")
    assert (capture["method"], capture["url"]) == (
        "PUT", f"{BASE}/workspaces/ws_1/contacts/c_1/reveal-email")


# --- sessions -----------------------------------------------------------------

def test_list_sessions_requires_a_date():
    with pytest.raises(ValueError, match="date or date_from"):
        _client().list_sessions("ws_1")


def test_list_sessions_query(capture):
    _client().list_sessions("ws_1", date="2026-08-01", url="/pricing", referrer="google")
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/workspaces/ws_1/sessions")
    assert capture["kwargs"]["params"] == {
        "date": "2026-08-01", "url": "/pricing", "referrer": "google"}


def test_list_organisation_sessions_needs_no_date(capture):
    _client().list_organisation_sessions("ws_1", "org_1")
    assert (capture["method"], capture["url"]) == (
        "GET", f"{BASE}/workspaces/ws_1/organisations/org_1/sessions")
    assert capture["kwargs"]["params"] == {}


# --- segments -----------------------------------------------------------------

def test_list_segments(capture):
    _client().list_segments("ws_1")
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/workspaces/ws_1/segments")


# --- custom field definitions -------------------------------------------------

def test_custom_field_definition_crud(capture):
    _client().list_custom_fields("ws_1")
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/workspaces/ws_1/custom-fields")

    _client().create_custom_field("ws_1", "Industry", "text", description="d",
                                  options=[{"key": "tech", "label": "Tech"}])
    assert (capture["method"], capture["url"]) == ("POST", f"{BASE}/workspaces/ws_1/custom-fields")
    assert capture["kwargs"]["json"] == {
        "name": "Industry", "type": "text", "description": "d",
        "options": [{"key": "tech", "label": "Tech"}]}

    _client().get_custom_field("ws_1", "industry")
    assert (capture["method"], capture["url"]) == (
        "GET", f"{BASE}/workspaces/ws_1/custom-fields/industry")

    _client().update_custom_field("ws_1", "industry", name="Sector")
    assert (capture["method"], capture["url"]) == (
        "PATCH", f"{BASE}/workspaces/ws_1/custom-fields/industry")
    assert capture["kwargs"]["json"] == {"name": "Sector"}

    _client().delete_custom_field("ws_1", "industry")
    assert (capture["method"], capture["url"]) == (
        "DELETE", f"{BASE}/workspaces/ws_1/custom-fields/industry")


# --- custom field values ------------------------------------------------------

def test_custom_field_values(capture):
    _client().list_custom_field_values("ws_1", "org_1")
    assert (capture["method"], capture["url"]) == (
        "GET", f"{BASE}/workspaces/ws_1/organisations/org_1/custom-fields")

    _client().set_custom_field_values("ws_1", "org_1", {"deal_size": 50000})
    assert (capture["method"], capture["url"]) == (
        "PATCH", f"{BASE}/workspaces/ws_1/organisations/org_1/custom-fields")
    assert capture["kwargs"]["json"] == {"custom_fields": {"deal_size": 50000}}

    _client().set_custom_field_value("ws_1", "org_1", "account_tier", "enterprise")
    assert (capture["method"], capture["url"]) == (
        "PUT", f"{BASE}/workspaces/ws_1/organisations/org_1/custom-fields/account_tier")
    assert capture["kwargs"]["json"] == {"value": "enterprise"}

    _client().clear_custom_field_value("ws_1", "org_1", "account_tier")
    assert (capture["method"], capture["url"]) == (
        "DELETE", f"{BASE}/workspaces/ws_1/organisations/org_1/custom-fields/account_tier")


# --- erreurs amont ------------------------------------------------------------

def test_upstream_error_is_typed(monkeypatch):
    def fake_request(method, url, **kwargs):
        return _Resp(429, {"message": "Too Many Attempts."})
    monkeypatch.setattr(sc.requests, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as exc:
        _client().get_me()
    assert exc.value.status_code == 429
