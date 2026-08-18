"""Contrat du client Origami (v2, Bearer `og_live_…`, 1 méthode = 1 endpoint).

Mocke `requests.Session.request` : vérifie chemin, méthode, corps camelCase, query
params (`dryRun`/`confirm` sérialisés `true`, params à None retirés), et les
validations locales qui évitent un aller-retour voué à l'échec (upsert vide ou >100,
matchColumns manquant, list_sequences sans scope).
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.origami import client as og


class _Resp:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"x" if body is not None else b""
        self.text = str(body)
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"object": "list", "items": [], "nextCursor": None})

    monkeypatch.setattr(og.requests.Session, "request", fake_request)
    return seen


def _client(**kw):
    return og.OrigamiClient(api_key="og_live_test", **kw)


# --- auth / transport ----------------------------------------------------------

def test_auth_header_is_bearer():
    assert _client().session.headers["Authorization"] == "Bearer og_live_test"


def test_project_header_is_optional():
    assert "x-origami-project" not in _client().session.headers
    assert _client(project_id="proj-1").session.headers["x-origami-project"] == "proj-1"


def test_none_params_are_dropped_and_timeout_is_set(capture):
    _client().list_tables()
    assert capture["method"] == "GET"
    assert capture["url"] == "https://origami.chat/api/v2/tables"
    assert capture["kwargs"]["params"] is None
    assert capture["kwargs"]["timeout"] == og._HTTP_TIMEOUT

    _client().list_tables(workspace_id="ws-1", cursor="c2")
    assert capture["kwargs"]["params"] == {"workspaceId": "ws-1", "cursor": "c2"}


# --- workspaces / upload -------------------------------------------------------

def test_workspaces(capture):
    _client().list_workspaces(search="tulina")
    assert capture["url"].endswith("/workspaces")
    assert capture["kwargs"]["params"] == {"search": "tulina"}

    _client().create_workspace("Pilote")
    assert capture["method"] == "POST"
    assert capture["kwargs"]["json"] == {"name": "Pilote"}


def test_upload_documents_is_json_never_multipart(capture):
    files = [{"filename": "leads.csv", "content": "aGVsbG8=", "mode": "table"}]
    _client().upload_documents("ws-1", files)
    assert capture["method"] == "POST"
    assert capture["url"] == "https://origami.chat/api/v2/workspaces/ws-1/documents"
    assert capture["kwargs"]["json"] == {"files": files}
    assert "files" not in capture["kwargs"]  # pas de multipart


def test_upload_documents_validates_shape():
    with pytest.raises(ValueError):
        _client().upload_documents("ws-1", [])
    with pytest.raises(ValueError):
        _client().upload_documents("ws-1", [{"filename": "x.csv"}])  # content manquant


# --- tables / rows -------------------------------------------------------------

def test_table_reads(capture):
    _client().get_table("t-1", include="stats")
    assert capture["url"].endswith("/tables/t-1")
    assert capture["kwargs"]["params"] == {"include": "stats"}

    _client().list_columns("t-1")
    assert capture["url"].endswith("/tables/t-1/columns")


def test_list_rows_flat_and_cursor(capture):
    _client().list_rows("t-1")
    assert capture["url"].endswith("/tables/t-1/rows")
    assert capture["kwargs"]["params"] == {"cells": "flat"}

    _client().list_rows("t-1", cursor="abc", limit=200,
                        filters=[{"column": "email", "operator": "is_not_empty"}],
                        sort={"column": "company", "direction": "asc"})
    p = capture["kwargs"]["params"]
    assert p["cursor"] == "abc" and p["limit"] == 200 and p["cells"] == "flat"
    assert p["filters"] == '[{"column": "email", "operator": "is_not_empty"}]'
    assert p["sort"] == '{"column": "company", "direction": "asc"}'


def test_upsert_rows_body_and_enrich_default_false(capture):
    rows = [{"email": "a@b.fr", "first-name": "A"}]
    _client().upsert_rows("t-1", rows, ["email"])
    assert capture["method"] == "POST"
    assert capture["url"].endswith("/tables/t-1/rows/upsert")
    assert capture["kwargs"]["json"] == {"rows": rows, "matchColumns": ["email"],
                                         "enrich": False}

    _client().upsert_rows("t-1", rows, ["email"], enrich=True, reenrich_updated=True,
                          batch_id="b-1")
    body = capture["kwargs"]["json"]
    assert body["enrich"] is True and body["reenrichUpdated"] is True
    assert body["batchId"] == "b-1"


def test_upsert_rows_local_guards():
    c = _client()
    with pytest.raises(ValueError):
        c.upsert_rows("t-1", [], ["email"])
    with pytest.raises(ValueError):
        c.upsert_rows("t-1", [{"email": "x"}] * 101, ["email"])
    with pytest.raises(ValueError):
        c.upsert_rows("t-1", [{"email": "x"}], [])


# --- campaigns -----------------------------------------------------------------

def test_campaign_reads(capture):
    c = _client()
    c.list_campaigns("t-1")
    assert capture["url"].endswith("/tables/t-1/campaigns")
    c.get_campaign("cmp-1")
    assert capture["url"].endswith("/campaigns/cmp-1")
    c.campaign_stats("cmp-1")
    assert capture["url"].endswith("/campaigns/cmp-1/stats")
    c.campaign_people("cmp-1", cursor="k", status="sent,replied")
    assert capture["url"].endswith("/campaigns/cmp-1/people")
    assert capture["kwargs"]["params"] == {"cursor": "k", "status": "sent,replied"}


def test_create_campaign_body_with_settings(capture):
    _client().create_campaign("t-1", "Relance des grossistes",
                              settings={"blockPriorContacts": True,
                                        "blockActiveDuplicates": False})
    assert capture["method"] == "POST"
    assert capture["url"].endswith("/tables/t-1/campaigns")
    assert capture["kwargs"]["json"] == {
        "instructions": "Relance des grossistes",
        "settings": {"blockPriorContacts": True, "blockActiveDuplicates": False}}

    _client().create_campaign("t-1", "Sans settings")
    assert capture["kwargs"]["json"] == {"instructions": "Sans settings"}


def test_create_campaign_requires_instructions():
    with pytest.raises(ValueError):
        _client().create_campaign("t-1", "   ")


def test_get_run_is_under_agents(capture):
    _client().get_run("ag-1", "run-1")
    assert capture["method"] == "GET"
    assert capture["url"] == "https://origami.chat/api/v2/agents/ag-1/runs/run-1"


def test_launch_pause_resume_dry_run_query(capture):
    c = _client()
    c.launch_campaign("cmp-1")
    assert capture["method"] == "POST"
    assert capture["url"].endswith("/campaigns/cmp-1/launch")
    assert capture["kwargs"]["params"] is None

    c.launch_campaign("cmp-1", dry_run=True)
    assert capture["kwargs"]["params"] == {"dryRun": "true"}

    c.pause_campaign("cmp-1", dry_run=True)
    assert capture["url"].endswith("/campaigns/cmp-1/pause")
    assert capture["kwargs"]["params"] == {"dryRun": "true"}

    c.resume_campaign("cmp-1")
    assert capture["url"].endswith("/campaigns/cmp-1/resume")
    assert capture["kwargs"]["params"] is None


def test_delete_campaign_two_step(capture):
    c = _client()
    c.delete_campaign("cmp-1")
    assert capture["method"] == "DELETE"
    assert capture["url"].endswith("/campaigns/cmp-1")
    assert capture["kwargs"]["params"] is None  # aperçu d'impact

    c.delete_campaign("cmp-1", confirm=True)
    assert capture["kwargs"]["params"] == {"confirm": "true"}

    c.delete_campaign("cmp-1", confirm=True, dry_run=True)
    assert capture["kwargs"]["params"] == {"confirm": "true", "dryRun": "true"}


# --- sequences -----------------------------------------------------------------

def test_sequences(capture):
    c = _client()
    c.list_sequences("ws-1", status="active", channel="email")
    assert capture["url"].endswith("/sequences")
    assert capture["kwargs"]["params"] == {"workspaceId": "ws-1", "status": "active",
                                           "channel": "email"}
    c.get_sequence("seq-1")
    assert capture["url"].endswith("/sequences/seq-1")


def test_list_sequences_requires_workspace():
    with pytest.raises(ValueError):
        _client().list_sequences("")


# --- erreurs -------------------------------------------------------------------

def test_http_error_is_typed_and_carries_the_code(monkeypatch):
    monkeypatch.setattr(og.requests.Session, "request",
                        lambda self, *a, **k: _Resp(400, {"error": "Unknown fields",
                                                          "code": "UNKNOWN_FIELDS",
                                                          "details": {"fields": ["Email"]}}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().upsert_rows("t-1", [{"Email": "x"}], ["Email"])
    assert e.value.status_code == 400
    assert e.value.body["code"] == "UNKNOWN_FIELDS"
    assert e.value.service == "origami"


def test_empty_body_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(og.requests.Session, "request",
                        lambda self, *a, **k: _Resp(200, None))
    assert _client().get_campaign("cmp-1") == {}
