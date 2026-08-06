"""LushaClient — locks the request shape (api_key header, body assembly) and
the upstream-error contract (raise_for_upstream, no hand-rolled status check).

Mocks `requests.request`: no network, no real credential.
"""
import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.lusha import client as lusha_client
from oto.tools.lusha.client import LushaClient


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.text = "" if payload is None else str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def c():
    return LushaClient(api_key="lusha-key-123")


def test_search_and_enrich_sends_api_key_header_and_body(c, monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = kwargs.get("json")
        return _Resp({"requestId": "r1", "results": [], "billing": {"creditsCharged": 0}})

    monkeypatch.setattr(lusha_client.requests, "request", fake_request)

    c.search_and_enrich([{"email": "orit.shilvock@lusha.com"}])

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.lusha.com/v3/contacts/search-and-enrich"
    assert captured["headers"]["api_key"] == "lusha-key-123"
    assert "Authorization" not in captured["headers"]  # pas de scheme Bearer
    assert captured["json"] == {"contacts": [{"email": "orit.shilvock@lusha.com"}]}


def test_reveal_and_options_are_omitted_when_not_given(c, monkeypatch):
    captured = {}
    monkeypatch.setattr(lusha_client.requests, "request", lambda *a, **k: (
        captured.update(json=k.get("json")), _Resp({"results": []}))[1])

    c.search_and_enrich([{"email": "a@b.com"}])
    assert "reveal" not in captured["json"]
    assert "options" not in captured["json"]


def test_reveal_and_include_partial_profiles_are_forwarded(c, monkeypatch):
    captured = {}
    monkeypatch.setattr(lusha_client.requests, "request", lambda *a, **k: (
        captured.update(json=k.get("json")), _Resp({"results": []}))[1])

    c.search_and_enrich(
        [{"email": "a@b.com"}], reveal=["emails", "phones"],
        include_partial_profiles=True)
    assert captured["json"]["reveal"] == ["emails", "phones"]
    assert captured["json"]["options"] == {"includePartialProfiles": True}


def test_returns_parsed_response_body(c, monkeypatch):
    payload = {"requestId": "r1", "results": [{"id": "1", "firstName": "Orit"}],
               "billing": {"creditsCharged": 3, "resultsReturned": 1}}
    monkeypatch.setattr(lusha_client.requests, "request",
                        lambda *a, **k: _Resp(payload))
    assert c.search_and_enrich([{"email": "a@b.com"}]) == payload


def test_over_100_contacts_rejected_without_a_network_call(c, monkeypatch):
    monkeypatch.setattr(
        lusha_client.requests, "request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP call expected")))

    with pytest.raises(ValueError, match="100"):
        c.search_and_enrich([{"email": f"{i}@b.com"} for i in range(101)])


def test_exactly_100_contacts_is_accepted(c, monkeypatch):
    monkeypatch.setattr(lusha_client.requests, "request",
                        lambda *a, **k: _Resp({"results": []}))
    c.search_and_enrich([{"email": f"{i}@b.com"} for i in range(100)])  # no raise


def test_upstream_4xx_raises_upstream_http_error(c, monkeypatch):
    monkeypatch.setattr(
        lusha_client.requests, "request",
        lambda *a, **k: _Resp({"statusCode": 400, "message": "Validation failed",
                               "errors": ["entityType must be one of: contact, company"]},
                              status_code=400))
    with pytest.raises(UpstreamHTTPError) as exc_info:
        c.search_and_enrich([{"email": "a@b.com"}])
    assert exc_info.value.status_code == 400
