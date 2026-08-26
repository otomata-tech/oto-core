"""Contrat du client Waalaxy (developers.waalaxy.com, Bearer wa_live_…).

Mocke `requests.request` : méthode/URL/body des 4 endpoints de l'API
publique, le header d'auth, les gardes ValueError d'`add_prospects` et le
typage des erreurs amont.
"""
from __future__ import annotations

import pytest

from oto.tools.waalaxy import client as wc
from oto.tools.common.errors import UpstreamHTTPError

BASE = "https://developers.waalaxy.com"


class _Resp:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        self.content = str(body).encode() if body is not None else b""

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, seen.get("_body", {"result": []}))

    monkeypatch.setattr(wc.requests, "request", fake_request)
    return seen


def _client():
    return wc.WaalaxyClient(api_key="wa_live_test")


def test_auth_header_is_bearer(capture):
    _client().test_connection()
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer wa_live_test"
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/integrations/test")


def test_list_endpoints(capture):
    _client().list_prospect_lists()
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/prospectLists/getProspectLists")
    _client().list_campaigns()
    assert (capture["method"], capture["url"]) == ("GET", f"{BASE}/campaigns/getAll")


def test_add_prospects_body(capture):
    _client().add_prospects(
        [{"url": "https://www.linkedin.com/in/x", "customVariables": [{"label": "a", "value": "b"}]}],
        "list_1", campaign_id="camp_1", can_create_duplicates=True,
        should_overwrite_custom_profile_data=False,
    )
    assert (capture["method"], capture["url"]) == ("POST", f"{BASE}/prospects/addProspectFromIntegration")
    body = capture["kwargs"]["json"]
    assert body["prospectListId"] == "list_1"
    assert body["campaignId"] == "camp_1"
    assert body["origin"] == {"name": "oto"}
    assert body["canCreateDuplicates"] is True
    assert body["shouldOverwriteCustomProfileData"] is False
    assert "moveDuplicatesToOtherList" not in body
    assert "addExistingProspectInCampaign" not in body
    assert body["prospects"][0]["url"] == "https://www.linkedin.com/in/x"


def test_add_prospects_omits_campaign_when_absent(capture):
    _client().add_prospects([{"url": "https://www.linkedin.com/in/x"}], "list_1", origin="tulina")
    body = capture["kwargs"]["json"]
    assert "campaignId" not in body
    assert body["origin"] == {"name": "tulina"}


@pytest.mark.parametrize("prospects,list_id,msg", [
    ([], "l", "at least one"),
    ([{"url": "https://www.linkedin.com/in/x"}], "", "prospect_list_id"),
    ([{"customProfile": {}}], "l", "url"),
    ([{"url": "u", "customVariables": [{"label": "a"}]}], "l", "label"),
    ([{"url": "u", "customVariables": [{"label": "a", "value": "x" * 1001}]}], "l", "1000"),
])
def test_add_prospects_guards(capture, prospects, list_id, msg):
    with pytest.raises(ValueError, match=msg):
        _client().add_prospects(prospects, list_id)
    assert "method" not in capture


def test_upstream_error_is_typed(monkeypatch):
    monkeypatch.setattr(
        wc.requests, "request",
        lambda *a, **k: _Resp(401, {"title": "Unauthorized", "status": 401}))
    with pytest.raises(UpstreamHTTPError) as ei:
        _client().test_connection()
    assert ei.value.status_code == 401
    assert ei.value.service == "waalaxy"
