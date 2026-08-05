"""Contrat du client Sellsy (API v2, OAuth2 client_credentials).

Mocke `requests.Session.request` / `.post` : vérifie le flux de jeton (frappe,
cache mémoire, ré-auth sur 401), l'encodage PHP des paramètres de liste
(`field[]`, `embed[]`) et la pagination « seek ».
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.sellsy import client as sc


class _Resp:
    def __init__(self, status_code: int, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.content = b"x"
        self.text = str(body)
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _clear_token_cache():
    sc._TOKEN_CACHE.clear()
    yield
    sc._TOKEN_CACHE.clear()


@pytest.fixture()
def capture(monkeypatch):
    """Auth toujours OK, appels API capturés (dernier appel + compteur d'auth)."""
    seen = {"auth_calls": 0, "calls": []}

    def fake_post(self, url, **kwargs):
        seen["auth_calls"] += 1
        seen["auth_body"] = kwargs.get("json")
        return _Resp(200, {"access_token": "tok-1", "expires_in": 86400,
                           "token_type": "Bearer"})

    def fake_request(self, method, url, **kwargs):
        seen["calls"].append({"method": method, "url": url, **kwargs})
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"data": [], "pagination": {"total": 0}})

    monkeypatch.setattr(sc.requests.Session, "post", fake_post)
    monkeypatch.setattr(sc.requests.Session, "request", fake_request)
    return seen


def _client():
    return sc.SellsyClient(client_id="id-1", client_secret="secret-1")


# --- auth ---------------------------------------------------------------------

def test_token_is_minted_with_client_credentials(capture):
    _client().list_records("companies")

    assert capture["auth_body"] == {"grant_type": "client_credentials",
                                    "client_id": "id-1",
                                    "client_secret": "secret-1"}
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer tok-1"


def test_token_is_reused_across_client_instances(capture):
    """Chaque appel MCP construit un client neuf : sans cache, une frappe de jeton
    par appel (et le quota d'auth Sellsy avec)."""
    _client().list_records("companies")
    _client().list_records("invoices")

    assert capture["auth_calls"] == 1


def test_token_cache_is_keyed_by_credentials(capture):
    """Deux comptes dans le même processus ⟹ deux jetons, jamais l'un pour l'autre."""
    _client().list_records("companies")
    sc.SellsyClient(client_id="id-2", client_secret="secret-2").list_records("companies")

    assert capture["auth_calls"] == 2


def test_401_invalidates_the_cached_token_and_retries_once(monkeypatch):
    """Un jeton peut être révoqué avant son expiration annoncée : rejouer une fois
    avec un jeton neuf, plutôt que renvoyer un 401 à l'agent."""
    auth = {"n": 0}
    calls = {"n": 0}

    def fake_post(self, url, **kwargs):
        auth["n"] += 1
        return _Resp(200, {"access_token": f"tok-{auth['n']}", "expires_in": 86400})

    def fake_request(self, method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(401, {"error": "invalid_token"})
        return _Resp(200, {"data": [{"id": 1}]})

    monkeypatch.setattr(sc.requests.Session, "post", fake_post)
    monkeypatch.setattr(sc.requests.Session, "request", fake_request)

    assert _client().list_records("companies") == {"data": [{"id": 1}]}
    assert auth["n"] == 2 and calls["n"] == 2


def test_401_twice_surfaces_the_upstream_error(monkeypatch):
    monkeypatch.setattr(sc.requests.Session, "post", lambda self, url, **kw: _Resp(
        200, {"access_token": "tok", "expires_in": 86400}))
    monkeypatch.setattr(sc.requests.Session, "request",
                        lambda self, method, url, **kw: _Resp(401, {"error": "nope"}))

    with pytest.raises(UpstreamHTTPError) as e:
        _client().list_records("companies")
    assert e.value.status_code == 401


def test_missing_access_token_in_auth_response_is_explicit(monkeypatch):
    monkeypatch.setattr(sc.requests.Session, "post",
                        lambda self, url, **kw: _Resp(200, {"token_type": "Bearer"}))
    with pytest.raises(ValueError, match="access_token"):
        _client().access_token()


# --- encodage des paramètres --------------------------------------------------

def test_list_uses_php_style_arrays_and_singular_field(capture):
    """`field[]=id` : `fields=` serait ignoré EN SILENCE par Sellsy."""
    _client().list_records("companies", limit=100, order="created",
                           fields=["id", "name"], embed=["smart_tags"])

    params = capture["kwargs"]["params"]
    assert params["field[]"] == ["id", "name"]
    assert params["embed[]"] == ["smart_tags"]
    assert params["limit"] == 100 and params["order"] == "created"
    assert "field" not in params and "fields" not in params


def test_unset_params_are_dropped(capture):
    _client().list_records("companies", limit=10)

    assert set(capture["kwargs"]["params"]) == {"limit"}


def test_booleans_go_as_strings(capture):
    _client().create_record("companies", {"name": "Acme"}, verify=True)

    assert capture["kwargs"]["params"]["verify"] == "true"


def test_search_wraps_filters(capture):
    _client().search_records("invoices", {"status": ["due"]}, limit=50)

    assert capture["method"] == "POST"
    assert capture["url"].endswith("/v2/invoices/search")
    assert capture["kwargs"]["json"] == {"filters": {"status": ["due"]}}


def test_search_without_filters_sends_an_empty_object(capture):
    _client().search_records("invoices")

    assert capture["kwargs"]["json"] == {"filters": {}}


def test_resource_name_is_validated():
    with pytest.raises(ValueError, match="ressource"):
        _client().list_records("../accounts")


# --- verbes -------------------------------------------------------------------

def test_crud_paths(capture):
    c = _client()
    c.get_record("companies", 42)
    assert capture["url"].endswith("/v2/companies/42") and capture["method"] == "GET"

    c.update_record("companies", 42, {"name": "Acme"})
    assert capture["method"] == "PUT" and capture["kwargs"]["json"] == {"name": "Acme"}

    c.delete_record("companies", 42)
    assert capture["method"] == "DELETE"

    c.patch_record("opportunities", 7, {"status": "won"})
    assert capture["method"] == "PATCH" and capture["url"].endswith("/opportunities/7")


def test_sub_resources_and_actions(capture):
    c = _client()
    c.list_sub("invoices", 9, "payments")
    assert capture["url"].endswith("/v2/invoices/9/payments")

    c.act("invoices", 9, "validate", payload={"date": "2026-08-05"})
    assert capture["method"] == "POST" and capture["url"].endswith("/invoices/9/validate")

    c.act("estimates", 3, "status", payload={"status": "sent"}, method="PUT")
    assert capture["method"] == "PUT" and capture["url"].endswith("/estimates/3/status")

    c.set_custom_fields("companies", 42, [{"id": 12, "value": "x"}])
    assert capture["kwargs"]["json"] == {"custom_fields": [{"id": 12, "value": "x"}]}

    c.link_contact_to_company(42, 7)
    assert capture["url"].endswith("/companies/42/contacts/7")

    c.global_search("acme", types=["company", "contact"])
    assert capture["url"].endswith("/v2/search")
    assert capture["kwargs"]["params"]["type[]"] == ["company", "contact"]


# --- pagination « seek » ------------------------------------------------------

def test_list_all_follows_the_seek_cursor(monkeypatch):
    pages = [
        _Resp(200, {"data": [{"id": 1}], "pagination": {"total": 3, "offset": "cur-1"}}),
        _Resp(200, {"data": [{"id": 2}], "pagination": {"total": 3, "offset": "cur-2"}}),
        _Resp(200, {"data": [{"id": 3}], "pagination": {"total": 3, "offset": "cur-3"}}),
    ]
    seen = []

    monkeypatch.setattr(sc.requests.Session, "post", lambda self, url, **kw: _Resp(
        200, {"access_token": "tok", "expires_in": 86400}))

    def fake_request(self, method, url, **kwargs):
        seen.append((kwargs.get("params") or {}).get("offset"))
        return pages[len(seen) - 1]

    monkeypatch.setattr(sc.requests.Session, "request", fake_request)

    out = _client().list_all("companies", max_pages=5)
    assert [r["id"] for r in out["data"]] == [1, 2, 3]
    assert seen == [None, "cur-1", "cur-2"]
    assert out["pages"] == 3 and out["truncated"] is False


def test_list_all_reports_truncation(monkeypatch):
    monkeypatch.setattr(sc.requests.Session, "post", lambda self, url, **kw: _Resp(
        200, {"access_token": "tok", "expires_in": 86400}))
    monkeypatch.setattr(sc.requests.Session, "request",
                        lambda self, method, url, **kw: _Resp(
                            200, {"data": [{"id": 1}],
                                  "pagination": {"total": 999, "offset": "cur"}}))

    out = _client().list_all("companies", max_pages=2)
    assert out["pages"] == 2 and out["truncated"] is True


def test_list_all_switches_to_search_when_filtered(capture):
    _client().list_all("invoices", filters={"status": ["due"]}, max_pages=1)

    assert capture["method"] == "POST" and capture["url"].endswith("/invoices/search")


# --- quotas -------------------------------------------------------------------

def test_remaining_quota_is_exposed(monkeypatch):
    monkeypatch.setattr(sc.requests.Session, "post", lambda self, url, **kw: _Resp(
        200, {"access_token": "tok", "expires_in": 86400}))
    monkeypatch.setattr(sc.requests.Session, "request",
                        lambda self, method, url, **kw: _Resp(
                            200, {"data": []},
                            headers={"X-Quota-Remaining-By-Minute": "42",
                                     "X-Quota-Remaining-By-Day": "1000"}))

    c = _client()
    c.list_records("companies")
    assert c.last_quota == {"minute": 42, "day": 1000}
