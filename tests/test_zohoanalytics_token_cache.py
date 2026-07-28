"""#233 : le token Zoho Analytics est caché PROCESS-WIDE (keyé par credential) →
une NOUVELLE instance de client par appel serveur ne re-refresh PAS à chaque fois
(sinon rate-limit Zoho sur /oauth/v2/token → 400 intermittent).

Le cache vit désormais dans `oto.tools.zoho.auth` (partagé CRM/Desk/Analytics,
#285) — ces tests gardent la garde AU NIVEAU DU CLIENT : ils échouent si le
client Analytics cesse de passer par le helper commun."""
import pytest

import oto.tools.zoho.auth as zauth
from oto.tools.zohoanalytics.client import ZohoAnalyticsClient


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


@pytest.fixture(autouse=True)
def _clear_cache():
    zauth._TOKEN_CACHE.clear()
    yield
    zauth._TOKEN_CACHE.clear()


def _mk(rt="rt1"):
    return ZohoAnalyticsClient(client_id="cid", client_secret="sec", refresh_token=rt,
                               org_id="org", accounts_url="https://accounts.zoho.eu")


def _count_post(monkeypatch, calls):
    monkeypatch.setattr(zauth.requests, "post",
                        lambda *a, **k: calls.append(1) or _Resp(
                            {"access_token": "TOK", "expires_in": 3600}))


def test_token_cached_across_instances(monkeypatch):
    calls = []
    _count_post(monkeypatch, calls)
    t1 = _mk()._get_access_token()
    t2 = _mk()._get_access_token()          # nouvelle instance, MÊME credential
    assert t1 == t2 == "TOK"
    assert len(calls) == 1                  # un SEUL refresh, pas un par instance


def test_invalidate_forces_refresh(monkeypatch):
    calls = []
    _count_post(monkeypatch, calls)
    c = _mk()
    c._get_access_token()
    c._invalidate_token()                   # ex. après un 401
    c._get_access_token()
    assert len(calls) == 2                  # invalidation → nouveau refresh


def test_distinct_credentials_isolated(monkeypatch):
    calls = []
    _count_post(monkeypatch, calls)
    _mk(rt="rtA")._get_access_token()
    _mk(rt="rtB")._get_access_token()       # credential différent → cache séparé
    assert len(calls) == 2
