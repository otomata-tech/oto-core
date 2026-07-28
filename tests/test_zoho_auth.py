"""Auth Zoho partagée : non-fuite des secrets (#284) + cache process-wide (#285)."""
import time

import pytest

from oto.tools.zoho import auth as zauth

CID, SECRET, RT = "1000.CLIENTID", "s3cr3t-client-secret", "1000.refresh.token"
ACC = "https://accounts.zoho.eu"


@pytest.fixture(autouse=True)
def _clear_cache():
    zauth._TOKEN_CACHE.clear()
    yield
    zauth._TOKEN_CACHE.clear()


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_secrets_go_in_body_never_in_url(monkeypatch):
    """Les credentials passent en `data=` (corps), jamais en `params=` (URL) — sinon
    ils fuitent dans les messages d'erreur, les logs et les access logs Zoho (#284)."""
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return _Resp(200, {"access_token": "AT", "expires_in": 3600})

    monkeypatch.setattr(zauth.requests, "post", fake_post)
    assert zauth.get_access_token(ACC, CID, SECRET, RT) == "AT"

    assert "params" not in seen["kw"], "les secrets ne doivent JAMAIS être en query string"
    assert seen["kw"]["data"]["client_secret"] == SECRET
    assert SECRET not in seen["url"] and RT not in seen["url"]


@pytest.mark.parametrize("resp", [
    _Resp(400, {"error": "invalid_client"}),
    _Resp(401, None, "boom"),
    _Resp(500, {"message": "server error"}),
])
def test_error_message_never_carries_secrets(monkeypatch, resp):
    """Sur échec, le message d'exception ne contient aucun secret (#284)."""
    monkeypatch.setattr(zauth.requests, "post", lambda url, **kw: resp)
    with pytest.raises(zauth.ZohoAuthError) as e:
        zauth.get_access_token(ACC, CID, SECRET, RT)
    msg = str(e.value)
    assert SECRET not in msg and RT not in msg and CID not in msg
    assert "accounts.zoho.eu" in msg or "invalid_client" in msg


def test_http_200_with_error_body_is_auth_error(monkeypatch):
    """Zoho répond 200 + {"error": ...} sur une région/client faux."""
    monkeypatch.setattr(zauth.requests, "post",
                        lambda url, **kw: _Resp(200, {"error": "invalid_code"}))
    with pytest.raises(zauth.ZohoAuthError, match="invalid_code"):
        zauth.get_access_token(ACC, CID, SECRET, RT)


def test_cache_is_shared_across_instances(monkeypatch):
    """Un token valide est réutilisé : le serveur recrée un client par appel MCP,
    sans cache partagé Zoho rate-limite /oauth/v2/token (#285)."""
    calls = []
    monkeypatch.setattr(zauth.requests, "post", lambda url, **kw: (
        calls.append(1), _Resp(200, {"access_token": "AT", "expires_in": 3600}))[1])

    for _ in range(5):
        assert zauth.get_access_token(ACC, CID, SECRET, RT) == "AT"
    assert len(calls) == 1, "un seul refresh pour 5 appels du même credential"


def test_cache_isolates_distinct_credentials(monkeypatch):
    """Deux credentials distincts ne partagent jamais un token."""
    tokens = iter(["AT-A", "AT-B"])
    monkeypatch.setattr(zauth.requests, "post", lambda url, **kw: _Resp(
        200, {"access_token": next(tokens), "expires_in": 3600}))
    a = zauth.get_access_token(ACC, CID, SECRET, "rt-A")
    b = zauth.get_access_token(ACC, CID, SECRET, "rt-B")
    assert (a, b) == ("AT-A", "AT-B")


def test_expired_token_is_refreshed(monkeypatch):
    calls = []
    monkeypatch.setattr(zauth.requests, "post", lambda url, **kw: (
        calls.append(1), _Resp(200, {"access_token": "AT2", "expires_in": 3600}))[1])
    key = zauth.cred_key(ACC, CID, RT)
    zauth._TOKEN_CACHE[key] = ("OLD", time.time() - 1)  # périmé
    assert zauth.get_access_token(ACC, CID, SECRET, RT) == "AT2"
    assert len(calls) == 1


def test_invalidate_forces_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(zauth.requests, "post", lambda url, **kw: (
        calls.append(1), _Resp(200, {"access_token": "AT", "expires_in": 3600}))[1])
    key = zauth.cred_key(ACC, CID, RT)
    zauth.get_access_token(ACC, CID, SECRET, RT, key=key)
    zauth.invalidate(key)
    zauth.get_access_token(ACC, CID, SECRET, RT, key=key)
    assert len(calls) == 2


def test_cred_key_never_exposes_the_secret():
    key = zauth.cred_key(ACC, CID, RT)
    assert RT not in key and SECRET not in key and len(key) == 64


def test_all_three_clients_share_the_cache(monkeypatch):
    """CRM, Desk et Analytics passent par le MÊME helper : un credential commun ne
    déclenche qu'un refresh (le correctif #233 ne couvrait qu'Analytics)."""
    from oto.tools.zoho.client import ZohoClient
    from oto.tools.zohodesk.client import ZohoDeskClient
    from oto.tools.zohoanalytics.client import ZohoAnalyticsClient

    calls = []
    monkeypatch.setattr(zauth.requests, "post", lambda url, **kw: (
        calls.append(1), _Resp(200, {"access_token": "AT", "expires_in": 3600}))[1])

    kw = dict(client_id=CID, client_secret=SECRET, refresh_token=RT, accounts_url=ACC)
    assert ZohoClient(**kw)._get_access_token() == "AT"
    assert ZohoDeskClient(org_id="800", **kw)._get_access_token() == "AT"
    assert ZohoAnalyticsClient(org_id="800", **kw)._get_access_token() == "AT"
    assert len(calls) == 1
