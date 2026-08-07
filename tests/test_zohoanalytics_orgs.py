"""ZohoAnalyticsClient.list_orgs — le seul appel qui ne connaît pas encore son org.

Toutes les requêtes Analytics portent l'en-tête `ZANALYTICS-ORGID`. Celle-ci ne peut
pas : elle sert justement à découvrir l'organisation, après un consentement OAuth, pour
éviter de faire chercher un identifiant à onze chiffres dans l'interface Zoho.

Mocke `requests.request` : pas de réseau, pas de vrai credential.
"""
import pytest

from oto.tools.zohoanalytics import client as za_client
from oto.tools.zohoanalytics.client import ZohoAnalyticsClient


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.text = "" if payload is None else str(payload)
        self.headers = {}

    def json(self):
        return self._payload


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setattr(ZohoAnalyticsClient, "_get_access_token", lambda self: "tok")
    return ZohoAnalyticsClient(client_id="cid", client_secret="sec",
                               refresh_token="rt", org_id=None,
                               api_domain="https://analyticsapi.zoho.eu",
                               accounts_url="https://accounts.zoho.eu")


def test_list_orgs_omits_the_org_header(c, monkeypatch):
    """Le point du test : envoyer ZANALYTICS-ORGID ici serait circulaire — on ne l'a
    pas encore, c'est ce qu'on vient chercher."""
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, headers=headers)
        return _Resp({"data": {"orgs": [
            {"orgId": "20068608403", "orgName": "movinmotion", "role": "Organization Admin"},
        ]}})

    monkeypatch.setattr(za_client.requests, "request", fake_request)
    orgs = c.list_orgs()

    assert captured["url"] == "https://analyticsapi.zoho.eu/restapi/v2/orgs"
    assert "ZANALYTICS-ORGID" not in captured["headers"]
    assert captured["headers"]["Authorization"] == "Zoho-oauthtoken tok"
    assert orgs == [{"org_id": "20068608403", "name": "movinmotion",
                     "role": "Organization Admin"}]


def test_list_orgs_returns_every_org(c, monkeypatch):
    """Un compte en voit souvent plusieurs (workspaces partagés) et la réponse n'en
    désigne aucune par défaut : au-delà d'une seule, l'appelant doit faire CHOISIR.
    Cas réel — le premier compte testé en rendait deux."""
    monkeypatch.setattr(za_client.requests, "request", lambda *a, **k: _Resp({"data": {"orgs": [
        {"orgId": "20072252845", "orgName": "Marvin", "role": "Account Admin"},
        {"orgId": "20068608403", "orgName": "movinmotion", "role": "Organization Admin"},
    ]}}))
    assert [o["name"] for o in c.list_orgs()] == ["Marvin", "movinmotion"]


def test_list_orgs_tolerates_an_empty_payload(c, monkeypatch):
    monkeypatch.setattr(za_client.requests, "request", lambda *a, **k: _Resp({}))
    assert c.list_orgs() == []


def test_other_calls_still_demand_the_org(c, monkeypatch):
    """Sans org, un appel normal doit dire QUOI faire — pas partir sans l'en-tête et
    se faire rejeter par Zoho avec un message opaque."""
    monkeypatch.setattr(za_client.requests, "request",
                        lambda *a, **k: pytest.fail("aucune requête ne doit partir"))
    with pytest.raises(ValueError) as e:
        c.list_workspaces()
    assert "list_orgs" in str(e.value)


def test_org_header_is_sent_when_known(monkeypatch):
    monkeypatch.setattr(ZohoAnalyticsClient, "_get_access_token", lambda self: "tok")
    client = ZohoAnalyticsClient(client_id="cid", client_secret="sec", refresh_token="rt",
                                 org_id="20068608403",
                                 api_domain="https://analyticsapi.zoho.eu",
                                 accounts_url="https://accounts.zoho.eu")
    captured = {}
    monkeypatch.setattr(za_client.requests, "request",
                        lambda method, url, headers=None, **k: (
                            captured.update(headers=headers), _Resp({"data": {}}))[1])
    client.list_workspaces()
    assert captured["headers"]["ZANALYTICS-ORGID"] == "20068608403"
