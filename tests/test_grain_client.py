"""Contrat du client Grain (v2, Bearer + Public-Api-Version).

Mocke `requests.Session.request` (et `requests.put` pour l'upload direct,
hors session) : vérifie méthode/URL/query/body, les DEUX headers d'auth, et
le typage des erreurs amont.
"""
from __future__ import annotations

import pytest

from oto.tools.grain import client as gc
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body=None, text: str = "", content: bytes = b""):
        self.status_code = status_code
        self._body = body
        self.text = text if text else str(body)
        self.content = content or (str(body).encode() if body is not None else b"")
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"recordings": []})

    monkeypatch.setattr(gc.requests.Session, "request", fake_request)
    return seen


def _client():
    return gc.GrainClient(api_key="grn-test-token")


def test_auth_headers_are_bearer_and_api_version():
    c = _client()
    assert c.session.headers["Authorization"] == "Bearer grn-test-token"
    assert c.session.headers["Public-Api-Version"] == "2025-10-31"


def test_list_recordings_posts_json_body(capture):
    _client().list_recordings(cursor="abc", filter={"team": "t1"}, include={"highlights": True})
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/recordings"
    assert capture["kwargs"]["json"] == {
        "cursor": "abc", "filter": {"team": "t1"}, "include": {"highlights": True}}


def test_none_kwargs_are_dropped_from_body(capture):
    _client().list_recordings(cursor=None, filter={"team": "t1"})
    assert capture["kwargs"]["json"] == {"filter": {"team": "t1"}}


def test_get_recording_is_a_post(capture):
    _client().get_recording("rec_123", include={"participants": True})
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/recordings/rec_123"
    assert capture["kwargs"]["json"] == {"include": {"participants": True}}


def test_get_transcript_text_returns_text_not_json(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return _Resp(200, text="hello world")
    monkeypatch.setattr(gc.requests.Session, "request", fake_request)
    result = _client().get_transcript_text("rec_123")
    assert result == "hello world"
    assert isinstance(result, str)


def test_download_recording_returns_bytes(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return _Resp(200, content=b"\x00\x01binarydata")
    monkeypatch.setattr(gc.requests.Session, "request", fake_request)
    result = _client().download_recording("rec_123")
    assert result == b"\x00\x01binarydata"
    assert isinstance(result, bytes)


def test_update_recording_is_a_patch_with_title(capture):
    _client().update_recording("rec_123", "New title")
    assert capture["method"] == "PATCH"
    assert capture["kwargs"]["json"] == {"title": "New title"}


def test_add_tag_is_a_put(capture):
    _client().add_tag("rec_123", "important")
    assert capture["method"] == "PUT"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/recordings/rec_123/tags"
    assert capture["kwargs"]["json"] == {"tag": "important"}


def test_remove_tag_builds_path(capture):
    _client().remove_tag("rec_123", "important")
    assert capture["method"] == "DELETE"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/recordings/rec_123/tags/important"


def test_share_with_team_puts_team_id_in_body_not_path(capture):
    """Live-tested 2026-08-20: the path-based shape (team_id in the URL, like
    unshare_from_team's DELETE) 404s for real — the real shape mirrors
    share_with_user instead: plural path, team_id in the body."""
    _client().share_with_team("rec_123", "team_456")
    assert capture["method"] == "PUT"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/recordings/rec_123/teams"
    assert capture["kwargs"]["json"] == {"team_id": "team_456"}


def test_unshare_from_team_has_team_id_in_path_no_body(capture):
    _client().unshare_from_team("rec_123", "team_456")
    assert capture["method"] == "DELETE"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/recordings/rec_123/teams/team_456"
    assert capture["kwargs"]["json"] is None


def test_create_upload_url(capture):
    _client().create_upload_url("meeting.mp4", user_id="usr_1")
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/recordings/upload"
    assert capture["kwargs"]["json"] == {"filename": "meeting.mp4", "user_id": "usr_1"}


def test_upload_recording_file_does_not_use_session_auth(monkeypatch):
    """L'URL d'upload est pré-autorisée (potentiellement un autre host) —
    envoyer le Bearer Grain dessus fuiterait le credential à un tiers."""
    seen = {}

    def fake_put(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        return _Resp(200)

    monkeypatch.setattr(gc.requests, "put", fake_put)
    _client().upload_recording_file("https://s3.example.com/upload/xyz", b"filebytes",
                                     content_type="video/mp4")
    assert seen["url"] == "https://s3.example.com/upload/xyz"
    assert seen["kwargs"]["data"] == b"filebytes"
    assert "Authorization" not in seen["kwargs"].get("headers", {})
    assert seen["kwargs"]["headers"]["Content-Type"] == "video/mp4"


def test_create_hook(capture):
    _client().create_hook("https://example.com/hook", "recording_added",
                           include={"participants": True})
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/hooks/create"
    assert capture["kwargs"]["json"] == {
        "hook_url": "https://example.com/hook", "hook_type": "recording_added",
        "include": {"participants": True}}


def test_delete_hook_builds_path(capture):
    _client().delete_hook("hook_789")
    assert capture["method"] == "DELETE"
    assert capture["url"] == "https://api.grain.com/_/public-api/v2/hooks/hook_789"


def test_list_users_teams_meeting_types_post_empty_body(capture):
    _client().list_users()
    assert capture["kwargs"]["json"] == {}
    _client().list_teams()
    assert capture["kwargs"]["json"] == {}
    _client().list_meeting_types()
    assert capture["kwargs"]["json"] == {}


def test_http_error_is_typed(monkeypatch):
    monkeypatch.setattr(
        gc.requests.Session, "request",
        lambda self, *a, **k: _Resp(401, {"error": "invalid_token"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().list_recordings()
    assert e.value.status_code == 401
    assert e.value.is_client_error
    assert e.value.service == "grain"


def test_upload_error_is_also_typed(monkeypatch):
    monkeypatch.setattr(gc.requests, "put", lambda url, **k: _Resp(403, {"error": "expired"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().upload_recording_file("https://s3.example.com/upload/xyz", b"data")
    assert e.value.status_code == 403
    assert e.value.service == "grain"
