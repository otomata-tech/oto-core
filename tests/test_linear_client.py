"""Contrat du client Linear (GraphQL, un seul endpoint POST).

Mocke `requests.Session.post` : vérifie la query envoyée (nom d'opération),
les variables, l'absence de préfixe `Bearer` sur le header d'auth (spécificité
Linear), et le typage à trois niveaux des erreurs — HTTP >= 400 sans corps
`errors[]` (`UpstreamHTTPError`), `errors[]` générique sur un HTTP 200/400
(`LinearGraphQLError`), et `errors[].extensions.code == "RATELIMITED"`
(`LinearRateLimited`, avec `reset_at` lu depuis le header
`X-RateLimit-Requests-Reset`).
"""
from __future__ import annotations

import pytest

from oto.tools.linear import client as lc
from oto.tools.linear import LinearGraphQLError, LinearRateLimited
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        self.headers = headers or {}

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

    monkeypatch.setattr(lc.requests.Session, "post", fake_post)
    return seen


def _client():
    return lc.LinearClient(api_key="lin-test-token")


def test_auth_header_has_no_bearer_prefix():
    c = _client()
    assert c.session.headers["Authorization"] == "lin-test-token"
    assert c.session.headers["Content-Type"] == "application/json"


def test_execute_posts_to_single_graphql_endpoint(capture):
    _client().get_issue("i1")
    assert capture["url"] == "https://api.linear.app/graphql"
    body = capture["kwargs"]["json"]
    assert "query Issue" in body["query"]
    assert body["variables"] == {"id": "i1"}


def test_none_variables_are_dropped(capture):
    _client().list_issues(team_id=None, project_id=None, first=50, after=None)
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"first": 50}


def test_get_issue_extracts_data_key(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _Resp(200, {"data": {"issue": {"id": "i1", "title": "Fix bug"}}})
    monkeypatch.setattr(lc.requests.Session, "post", fake_post)
    result = _client().get_issue("i1")
    assert result == {"id": "i1", "title": "Fix bug"}


def test_get_issue_custom_fields_override(capture):
    _client().get_issue("i1", fields="id title")
    body = capture["kwargs"]["json"]
    assert "id title" in body["query"]
    assert "description" not in body["query"]


def test_list_issues_filter_omitted_when_no_filters(capture):
    _client().list_issues()
    body = capture["kwargs"]["json"]
    assert "filter:" not in body["query"]


def test_list_issues_filter_built_from_given_ids(capture):
    _client().list_issues(team_id="t1", state_id="s1")
    body = capture["kwargs"]["json"]
    assert "team: { id: { eq: $teamId } }" in body["query"]
    assert "state: { id: { eq: $stateId } }" in body["query"]
    assert "project:" not in body["query"]
    assert body["variables"] == {"teamId": "t1", "stateId": "s1", "first": 50}


def test_search_issues_without_team_id_has_no_filter_clause(capture):
    """Regression: an earlier draft embedded a Python-style ternary directly
    into the GraphQL query text (`filter: $teamId != null ? {...} : null`),
    which is not valid GraphQL — the filter clause must be built in Python
    and either included whole or omitted, never conditional inline."""
    _client().search_issues("bug")
    body = capture["kwargs"]["json"]
    assert "filter:" not in body["query"]
    assert "?" not in body["query"]
    assert body["variables"] == {"query": "bug", "first": 50}


def test_search_issues_with_team_id_includes_filter_clause(capture):
    _client().search_issues("bug", team_id="t1")
    body = capture["kwargs"]["json"]
    assert "filter: { team: { id: { eq: $teamId } } }" in body["query"]
    assert body["variables"] == {"query": "bug", "teamId": "t1", "first": 50}


def test_create_issue_wraps_input(capture):
    _client().create_issue("New bug", "t1", priority=2)
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"title": "New bug", "teamId": "t1", "priority": 2}


def test_update_issue_wraps_input(capture):
    _client().update_issue("i1", state_id="s2")
    body = capture["kwargs"]["json"]
    assert body["variables"] == {"id": "i1", "input": {"stateId": "s2"}}


def test_archive_issue(capture):
    _client().archive_issue("i1")
    body = capture["kwargs"]["json"]
    assert "issueArchive" in body["query"]
    assert body["variables"] == {"id": "i1"}


def test_create_comment_wraps_input(capture):
    _client().create_comment("i1", "looks good")
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {"issueId": "i1", "body": "looks good"}


def test_create_webhook_wraps_input(capture):
    _client().create_webhook("https://example.com/hook", team_id="t1",
                              resource_types=["Issue", "Comment"])
    body = capture["kwargs"]["json"]
    assert body["variables"]["input"] == {
        "url": "https://example.com/hook", "teamId": "t1",
        "resourceTypes": ["Issue", "Comment"], "enabled": True,
    }


def test_http_error_with_no_errors_body_is_typed(monkeypatch):
    monkeypatch.setattr(
        lc.requests.Session, "post",
        lambda self, *a, **k: _Resp(401, {"error": "invalid_token"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().get_issue("i1")
    assert e.value.status_code == 401
    assert e.value.service == "linear"


def test_generic_graphql_error_is_typed(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _Resp(200, {
            "data": None,
            "errors": [{"message": "Entity not found.",
                        "extensions": {"code": "ENTITY_NOT_FOUND"}}],
        })
    monkeypatch.setattr(lc.requests.Session, "post", fake_post)
    with pytest.raises(LinearGraphQLError) as e:
        _client().get_issue("bad-id")
    assert e.value.code == "ENTITY_NOT_FOUND"
    assert "not found" in str(e.value)
    assert len(e.value.errors) == 1
    assert not isinstance(e.value, LinearRateLimited)


def test_rate_limited_error_is_typed_and_carries_reset(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _Resp(
            400,
            {"data": None, "errors": [{"message": "Rate limit exceeded",
                                        "extensions": {"code": "RATELIMITED"}}]},
            headers={"X-RateLimit-Requests-Reset": "1735689600000"},
        )
    monkeypatch.setattr(lc.requests.Session, "post", fake_post)
    with pytest.raises(LinearRateLimited) as e:
        _client().get_issue("i1")
    assert e.value.reset_at == 1735689600000
    assert e.value.status_code == 400


def test_rate_limited_error_without_reset_header_defaults_to_none(monkeypatch):
    def fake_post(self, url, **kwargs):
        return _Resp(400, {"data": None, "errors": [
            {"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED"}}]})
    monkeypatch.setattr(lc.requests.Session, "post", fake_post)
    with pytest.raises(LinearRateLimited) as e:
        _client().get_issue("i1")
    assert e.value.reset_at is None
