"""Contrat du client Granola (v1, Bearer).

Mocke `requests.Session.request` : vérifie méthode/URL/query/body, le header
Bearer, et le typage des erreurs amont.
"""
from __future__ import annotations

import pytest

from oto.tools.granola import client as gr
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"x"
        self.text = str(body)
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"notes": []})

    monkeypatch.setattr(gr.requests.Session, "request", fake_request)
    return seen


def _client():
    return gr.GranolaClient(api_key="grn_test")


def test_auth_header_is_bearer():
    c = _client()
    assert c.session.headers["Authorization"] == "Bearer grn_test"


def test_list_notes_builds_url_and_query(capture):
    _client().list_notes(folder_id="fol_abc", page_size=5)
    assert capture["method"] == "GET"
    assert capture["url"] == "https://public-api.granola.ai/v1/notes"
    assert capture["kwargs"]["params"] == {"folder_id": "fol_abc", "page_size": 5}
    assert capture["kwargs"]["timeout"] == gr._HTTP_TIMEOUT


def test_none_params_are_dropped_from_query(capture):
    _client().list_notes(folder_id=None, cursor="abc")
    assert "folder_id" not in capture["kwargs"]["params"]
    assert capture["kwargs"]["params"] == {"cursor": "abc"}


def test_get_note_builds_path(capture):
    _client().get_note("not_1d3tmYTlCICgjy", include="transcript")
    assert capture["url"] == "https://public-api.granola.ai/v1/notes/not_1d3tmYTlCICgjy"
    assert capture["kwargs"]["params"] == {"include": "transcript"}


def test_get_transcript_builds_path(capture):
    _client().get_transcript("not_1d3tmYTlCICgjy", page_size=100)
    assert capture["url"] == "https://public-api.granola.ai/v1/notes/not_1d3tmYTlCICgjy/transcript"
    assert capture["kwargs"]["params"] == {"page_size": 100}


def test_create_webhook_endpoint_sends_json_body(capture):
    _client().create_webhook_endpoint(
        url="https://example.com/hook", scopes=["personal", "public"],
        events=["note.generated"])
    assert capture["method"] == "POST"
    assert capture["url"] == "https://public-api.granola.ai/v1/webhook-endpoints"
    assert capture["kwargs"]["json"] == {
        "url": "https://example.com/hook", "scopes": ["personal", "public"],
        "events": ["note.generated"]}


def test_update_webhook_endpoint_is_a_patch(capture):
    _client().update_webhook_endpoint("whe_2mKr8fQxLp7Ta3", enabled=False)
    assert capture["method"] == "PATCH"
    assert capture["url"] == "https://public-api.granola.ai/v1/webhook-endpoints/whe_2mKr8fQxLp7Ta3"
    assert capture["kwargs"]["json"] == {"enabled": False}


def test_delete_webhook_endpoint_sends_no_body(capture):
    _client().delete_webhook_endpoint("whe_2mKr8fQxLp7Ta3")
    assert capture["method"] == "DELETE"
    assert capture["url"] == "https://public-api.granola.ai/v1/webhook-endpoints/whe_2mKr8fQxLp7Ta3"
    assert capture["kwargs"]["json"] is None


def test_http_error_is_typed(monkeypatch):
    monkeypatch.setattr(
        gr.requests.Session, "request",
        lambda self, *a, **k: _Resp(401, {"error": "invalid_token"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().list_notes()
    assert e.value.status_code == 401
    assert e.value.is_client_error
    assert e.value.service == "granola"


def test_rate_limit_error_is_typed(monkeypatch):
    monkeypatch.setattr(
        gr.requests.Session, "request",
        lambda self, *a, **k: _Resp(429, {"error": "rate_limited"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().list_notes()
    assert e.value.status_code == 429
