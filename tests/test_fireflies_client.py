"""Contrat du client Fireflies (GraphQL, un seul endpoint POST).

Mocke `requests.Session.post` : vérifie la query envoyée (nom d'opération),
les variables, le header Bearer, et le typage double des erreurs — HTTP >=
400 (`UpstreamHTTPError`) vs. `errors[]` sur un HTTP 200 (`FirefliesGraphQLError`).
"""
from __future__ import annotations

import pytest

from oto.tools.fireflies import client as fc
from oto.tools.fireflies import FirefliesGraphQLError
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _AnyKeyDict(dict):
    """Every lookup returns an empty dict — lets capture-only tests ignore
    which top-level GraphQL response key a given method unwraps."""

    def __getitem__(self, key):
        return self.get(key, _AnyKeyDict())


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_post(self, url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        return _Resp(200, {"data": _AnyKeyDict()})

    monkeypatch.setattr(fc.requests.Session, "post", fake_post)
    return seen


def _client():
    return fc.FirefliesClient(api_key="ff-test-token")


def test_auth_header_is_bearer():
    c = _client()
    assert c.session.headers["Authorization"] == "Bearer ff-test-token"
    assert c.session.headers["Content-Type"] == "application/json"


def test_execute_posts_to_single_graphql_endpoint(capture):
    _client().get_transcript("t1")
    assert capture["url"] == "https://api.fireflies.ai/graphql"
    body = capture["kwargs"]["json"]
    assert "query Transcript" in body["query"]
    assert body["variables"] == {"transcriptId": "t1"}


def test_none_variables_are_dropped(capture):
    _client().list_transcripts(mine=True, keyword=None, limit=None)
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"mine": True}


def test_get_transcript_extracts_data_key(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _Resp(200, {"data": {"transcript": {"id": "t1", "title": "Weekly sync"}}})
    monkeypatch.setattr(fc.requests.Session, "post", fake_post)
    result = _client().get_transcript("t1")
    assert result == {"id": "t1", "title": "Weekly sync"}


def test_get_transcript_custom_fields_override(capture):
    _client().get_transcript("t1", fields="id title")
    body = capture["kwargs"]["json"]
    assert "id title" in body["query"]
    assert "sentences" not in body["query"]


def test_list_transcripts_uses_modern_params_only(capture):
    _client().list_transcripts(keyword="budget", mine=True, organizers=["a@x.com"])
    body = capture["kwargs"]["json"]
    assert "$title" not in body["query"]
    assert "$date:" not in body["query"]
    assert body["variables"] == {"keyword": "budget", "mine": True, "organizers": ["a@x.com"]}


def test_list_transcripts_uses_the_real_graphql_types_for_scope_and_lists(capture):
    """Live-confirmed 2026-08-20: `scope`'s doc-named type `TranscriptsQueryScope`
    doesn't exist on the real schema (real type: plain `String`), and
    `organizers`/`participants` are `[String!]` not `[String]` — sending the
    wrong type is a GRAPHQL_VALIDATION_FAILED (HTTP 400) at call time. Locks
    the fix so a future edit can't silently reintroduce either mismatch."""
    _client().list_transcripts(keyword="x", scope="title", organizers=["a@x.com"], participants=["b@x.com"])
    query = capture["kwargs"]["json"]["query"]
    assert "$scope: String" in query
    assert "TranscriptsQueryScope" not in query
    assert "$organizers: [String!]" in query
    assert "$participants: [String!]" in query


def test_delete_transcript(capture):
    _client().delete_transcript("t1")
    body = capture["kwargs"]["json"]
    assert "mutation deleteTranscript" in body["query"]
    assert body["variables"] == {"id": "t1"}


def test_upload_audio_wraps_url_in_input(capture):
    _client().upload_audio("https://example.com/a.mp3", title="Call", save_video=True)
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {
        "url": "https://example.com/a.mp3", "title": "Call", "save_video": True}


def test_update_meeting_privacy(capture):
    _client().update_meeting_privacy("t1", "teammates")
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"id": "t1", "privacy": "teammates"}


def test_update_meeting_channel_bulk_ids(capture):
    _client().update_meeting_channel(["t1", "t2"], "chan1")
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"transcript_ids": ["t1", "t2"], "channel_id": "chan1"}


def test_share_meeting_drops_none_expiry(capture):
    _client().share_meeting("m1", ["a@x.com", "b@x.com"])
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"meeting_id": "m1", "emails": ["a@x.com", "b@x.com"]}


def test_share_meeting_with_expiry(capture):
    _client().share_meeting("m1", ["a@x.com"], expiry_days=7)
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"meeting_id": "m1", "emails": ["a@x.com"], "expiry_days": 7}


def test_add_to_live_meeting_minimal(capture):
    _client().add_to_live_meeting("https://zoom.us/j/123")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"meeting_link": "https://zoom.us/j/123"}


def test_create_live_action_item(capture):
    _client().create_live_action_item("m1", "Follow up with the client")
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"meeting_id": "m1", "prompt": "Follow up with the client"}


def test_list_active_meetings(capture):
    _client().list_active_meetings(email="user@example.com", states=["active"])
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"email": "user@example.com", "states": ["active"]}


def test_create_askfred_thread(capture):
    _client().create_askfred_thread("What were the action items?", transcript_id="t1")
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"query": "What were the action items?", "transcript_id": "t1"}


def test_create_askfred_thread_extracts_message(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _Resp(200, {"data": {"createAskFredThread": {"message": {"id": "msg1"}}}})
    monkeypatch.setattr(fc.requests.Session, "post", fake_post)
    result = _client().create_askfred_thread("q")
    assert result == {"id": "msg1"}


def test_continue_askfred_thread(capture):
    _client().continue_askfred_thread("thread1", "And who owns it?")
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"thread_id": "thread1", "query": "And who owns it?"}


def test_delete_askfred_thread(capture):
    _client().delete_askfred_thread("thread1")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"id": "thread1"}


def test_get_askfred_thread_does_not_select_the_nonexistent_error_field(capture):
    """Live-confirmed 2026-08-20: the doc's own example query selects
    `messages.error`, which does not exist on `AskFredMessage`
    (`Cannot query field "error"`, GRAPHQL_VALIDATION_FAILED) — confirmed via
    introspection too. `updated_at`, also in the doc's selection, DOES exist
    and stays. Locks the removal so it can't quietly come back."""
    _client().get_askfred_thread("thread1")
    query = capture["kwargs"]["json"]["query"]
    assert "updated_at" in query
    assert "error" not in query


def test_get_user_omits_id_for_self(capture):
    _client().get_user()
    body = capture["kwargs"]["json"]
    assert body["variables"] == {}


def test_get_user_with_id(capture):
    _client().get_user("user_123")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"userId": "user_123"}


def test_list_user_groups_mine(capture):
    _client().list_user_groups(mine=True)
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"mine": True}


def test_get_channel(capture):
    _client().get_channel("chan1")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"channelId": "chan1"}


def test_list_bites_requires_no_client_side_validation(capture):
    """The 'at least one of mine/transcript_id/my_team' constraint is
    server-enforced (not documented as a formal input type) — the client
    passes through and lets Fireflies reject it via the errors[] path."""
    _client().list_bites(mine=True, limit=10)
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"mine": True, "limit": 10}


def test_get_analytics(capture):
    _client().get_analytics(start_time="2024-01-01", end_time="2024-01-31")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"startTime": "2024-01-01", "endTime": "2024-01-31"}


def test_list_audit_events_category_required(capture):
    _client().list_audit_events("MEETING_OPERATIONS", limit=10)
    body = capture["kwargs"]["json"]
    assert body["variables"]["filters"] == {"category": "MEETING_OPERATIONS"}
    assert body["variables"]["limit"] == 10


def test_set_user_role(capture):
    _client().set_user_role("user_123", "admin")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"userId": "user_123", "role": "admin"}


def test_create_bite(capture):
    _client().create_bite("t1", 10.5, 25.0, name="Great point", media_type="video")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {
        "transcriptId": "t1", "startTime": 10.5, "endTime": 25.0,
        "name": "Great point", "mediaType": "video",
    }


def test_create_bite_uses_the_capital_i_arg_name(capture):
    """Live-confirmed 2026-08-20 via introspection: Fireflies' OWN schema
    spells the mutation arg `transcript_Id` (capital I) — inconsistent with
    every other `transcript_id` in this API, and easy to "fix" back to
    lowercase by someone who hasn't seen the live error. Locks the real
    (mis-)spelling so it survives a refactor."""
    _client().create_bite("t1", 10.5, 25.0)
    query = capture["kwargs"]["json"]["query"]
    assert "createBite(transcript_Id: $transcriptId" in query


def test_http_error_is_typed(monkeypatch):
    monkeypatch.setattr(
        fc.requests.Session, "post",
        lambda self, *a, **k: _Resp(401, {"error": "invalid_token"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().get_transcript("t1")
    assert e.value.status_code == 401
    assert e.value.service == "fireflies"


def test_graphql_error_on_http_200_is_typed(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _Resp(200, {
            "data": {},
            "errors": [{"message": "The transcript ID you are trying to query does not exist.",
                        "extensions": {"code": "object_not_found"}}],
        })
    monkeypatch.setattr(fc.requests.Session, "post", fake_post)
    with pytest.raises(FirefliesGraphQLError) as e:
        _client().get_transcript("bad-id")
    assert e.value.code == "object_not_found"
    assert "does not exist" in e.value.message
    assert len(e.value.errors) == 1
