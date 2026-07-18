"""SalesforceClient — verrouille le contrat d'auth OAuth (refresh, erreur, cache
d'`instance_url`) et le contrat HTTP des sObjects.

Mocke `requests.post`/`requests.request` : pas de réseau, pas de credential réel.

Le test `test_refresh_400_invalid_grant_raises_salesforce_auth_error` est celui qui
aurait attrapé le bug initial (ordre `raise_for_status()` avant le check du corps,
copié de Zoho sans tenir compte du fait que Salesforce répond HTTP 400 — pas 200
comme Zoho — sur un refus OAuth) : il exerce le VRAI chemin `_get_access_token`,
pas juste la construction directe de l'exception.
"""
import base64
import json

import pytest

from oto.tools.salesforce import client as sf_client
from oto.tools.salesforce.client import SalesforceAuthError, SalesforceClient

LOGIN = "https://login.salesforce.com"
INSTANCE = "https://my-org.my.salesforce.com"


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.headers = {}
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


@pytest.fixture
def c():
    return SalesforceClient(
        client_id="cid", client_secret="csecret", refresh_token="rtok", login_url=LOGIN)


def test_refresh_400_invalid_grant_raises_salesforce_auth_error(c, monkeypatch):
    """Salesforce (contrairement à Zoho) répond HTTP 400 sur un refus OAuth — le
    corps doit être inspecté AVANT `raise_for_status()`."""
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"error": "invalid_grant", "error_description": "expired access/refresh token"},
        status_code=400,
    ))
    with pytest.raises(SalesforceAuthError) as exc_info:
        c._get_access_token()
    assert exc_info.value.status_code == 401
    assert "invalid_grant" in str(exc_info.value)


def test_refresh_200_with_error_body_also_raises(c, monkeypatch):
    """Défensif : si un jour Salesforce (ou un proxy) renvoie 200 + erreur dans le
    corps, le check reste couvert (comme Zoho)."""
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"error": "invalid_client"}, status_code=200))
    with pytest.raises(SalesforceAuthError):
        c._get_access_token()


def test_refresh_success_caches_instance_url_and_token(c, monkeypatch):
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "tok123", "instance_url": INSTANCE}, status_code=200))
    token, instance_url = c._get_access_token()
    assert token == "tok123"
    assert instance_url == INSTANCE

    # 2e appel : pas de nouveau POST (cache mémoire) — on invalide le mock post pour le prouver
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("ne devrait pas re-poster : token en cache")))
    token2, instance_url2 = c._get_access_token()
    assert (token2, instance_url2) == (token, instance_url)


def test_request_uses_instance_url_from_refresh(c, monkeypatch):
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "tok123", "instance_url": INSTANCE}, status_code=200))
    captured = []

    def fake_request(method, url, headers=None, **kwargs):
        captured.append({"method": method, "url": url, "headers": headers, **kwargs})
        return _Resp({"totalSize": 0, "done": True, "records": []})

    monkeypatch.setattr(sf_client.requests, "request", fake_request)

    c.query("SELECT Id FROM Contact LIMIT 1")
    assert captured[-1]["method"] == "GET"
    assert captured[-1]["url"] == f"{INSTANCE}/services/data/v60.0/query/"
    assert captured[-1]["headers"]["Authorization"] == "Bearer tok123"


def test_query_more_routes_through_request_absolute_path(c, monkeypatch):
    """`query_more` doit passer par `_request` (refresh-and-retry-once inclus),
    pas par un `requests.get` séparé — vérifie que le path absolu est utilisé tel
    quel contre `instance_url` (pas de double préfixe /services/data/...)."""
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "tok123", "instance_url": INSTANCE}, status_code=200))
    captured = []

    def fake_request(method, url, headers=None, **kwargs):
        captured.append({"method": method, "url": url})
        return _Resp({"totalSize": 0, "done": True, "records": []})

    monkeypatch.setattr(sf_client.requests, "request", fake_request)

    c.query_more("/services/data/v60.0/query/01g000000000001-2000")
    assert captured[-1]["url"] == f"{INSTANCE}/services/data/v60.0/query/01g000000000001-2000"


def test_update_and_delete_synthesize_success_on_204(c, monkeypatch):
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "tok123", "instance_url": INSTANCE}, status_code=200))
    monkeypatch.setattr(sf_client.requests, "request",
                         lambda *a, **k: _Resp(None, status_code=204))

    assert c.update_record("Contact", "003xx", {"LastName": "X"}) == {
        "id": "003xx", "success": True}
    assert c.delete_record("Contact", "003xx") == {"id": "003xx", "success": True}


# --- Notes (Enhanced Notes: ContentNote + ContentDocumentLink) -------------

def test_list_notes_escapes_quotes_in_parent_id(c, monkeypatch):
    """La 1re requête (ContentDocumentLink) doit échapper `parent_id`."""
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "tok123", "instance_url": INSTANCE}, status_code=200))
    captured = []

    def fake_request(method, url, headers=None, **kwargs):
        captured.append(kwargs.get("params", {}))
        return _Resp({"totalSize": 0, "done": True, "records": []})

    monkeypatch.setattr(sf_client.requests, "request", fake_request)

    assert c.list_notes("003xx'; DROP") == []
    assert "\\'" in captured[-1]["q"]
    assert "ContentDocumentLink" in captured[-1]["q"]


def test_list_notes_two_step_query_decodes_content(c, monkeypatch):
    """ContentDocumentLink → ContentNote (pas de semi-join direct possible entre
    les deux) ; `Content` (base64) est décodé en texte lisible."""
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "tok123", "instance_url": INSTANCE}, status_code=200))
    calls = {"n": 0}

    def fake_request(method, url, headers=None, **kwargs):
        calls["n"] += 1
        q = kwargs.get("params", {}).get("q", "")
        if "ContentDocumentLink" in q:
            return _Resp({"records": [{"ContentDocumentId": "069AAA"}]})
        assert "ContentNote" in q and "069AAA" in q
        return _Resp({"records": [{
            "Id": "069AAA", "Title": "Suivi",
            "Content": base64.b64encode("Appelé le client".encode()).decode(),
            "CreatedDate": "2026-07-18T00:00:00.000+0000",
        }]})

    monkeypatch.setattr(sf_client.requests, "request", fake_request)

    notes = c.list_notes("003xx")
    assert calls["n"] == 2
    assert notes[0]["Content"] == "Appelé le client"


def test_create_note_creates_contentnote_then_links_it(c, monkeypatch):
    monkeypatch.setattr(sf_client.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "tok123", "instance_url": INSTANCE}, status_code=200))
    captured = []

    def fake_request(method, url, headers=None, **kwargs):
        captured.append({"method": method, "url": url, **kwargs})
        if "ContentNote" in url:
            return _Resp({"id": "069AAA", "success": True, "errors": []}, status_code=201)
        return _Resp({"id": "06AAAA", "success": True, "errors": []}, status_code=201)

    monkeypatch.setattr(sf_client.requests, "request", fake_request)

    result = c.create_note("003xx", "Suivi", "Appelé le client")
    assert result["id"] == "069AAA"

    note_call, link_call = captured
    assert note_call["url"].endswith("/sobjects/ContentNote/")
    assert base64.b64decode(note_call["json"]["Content"]).decode() == "Appelé le client"
    assert link_call["url"].endswith("/sobjects/ContentDocumentLink/")
    assert link_call["json"] == {
        "ContentDocumentId": "069AAA", "LinkedEntityId": "003xx",
        "ShareType": "V", "Visibility": "AllUsers",
    }
