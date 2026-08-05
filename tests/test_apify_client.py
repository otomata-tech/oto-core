"""Contrat du client Apify (v2, Bearer, store/actors/runs/datasets).

Mocke `requests.Session.request` : vérifie surtout deux pièges — la forme d'URL de
l'identifiant d'actor (`user/name` → `user~name`) et le passage des garde-fous de
coût en QUERY (pas dans le corps, qui est l'input de l'actor).
"""
from __future__ import annotations

import pytest

from oto.tools.apify import client as ap
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
        return _Resp(200, {"data": {"id": "run-1"}})

    monkeypatch.setattr(ap.requests.Session, "request", fake_request)
    return seen


def _client():
    return ap.ApifyClient(api_key="apify_api_test")


def test_auth_header_is_bearer():
    assert _client().session.headers["Authorization"] == "Bearer apify_api_test"


def test_store_search_params(capture):
    _client().store_search(search="google maps", limit=5)

    assert capture["method"] == "GET"
    assert capture["url"] == "https://api.apify.com/v2/store"
    assert capture["kwargs"]["params"] == {"search": "google maps", "limit": 5}


def test_actor_slug_is_converted_to_tilde(capture):
    """Le Store affiche `apify/x`, l'API attend `apify~x` — sinon 404."""
    _client().actor("apify/website-content-crawler")

    assert capture["url"].endswith("/actors/apify~website-content-crawler")


def test_run_sync_sends_input_as_body_and_guards_as_query(capture):
    _client().run_sync_dataset_items(
        "apify/google-maps-scraper",
        run_input={"searchStringsArray": ["boulangerie Marseille"]},
        max_items=20, fields=["title", "url"],
    )

    assert capture["method"] == "POST"
    assert capture["url"].endswith(
        "/actors/apify~google-maps-scraper/run-sync-get-dataset-items")
    assert capture["kwargs"]["json"] == {"searchStringsArray": ["boulangerie Marseille"]}
    assert capture["kwargs"]["params"] == {"maxItems": 20, "fields": "title,url"}


def test_run_sync_input_defaults_to_empty_object(capture):
    """Un POST sans corps ferait échouer l'actor : `{}` est le neutre attendu."""
    _client().run_sync_dataset_items("apify/x")

    assert capture["kwargs"]["json"] == {}


def test_run_and_status(capture):
    _client().run("apify/x", run_input={"a": 1}, timeout_secs=120, memory_mbytes=2048)
    assert capture["url"].endswith("/actors/apify~x/runs")
    assert capture["kwargs"]["params"] == {"timeout": 120, "memory": 2048}

    _client().run_status("run-1", wait_for_finish=10)
    assert capture["method"] == "GET"
    assert capture["url"].endswith("/actor-runs/run-1")
    assert capture["kwargs"]["params"] == {"waitForFinish": 10}


def test_abort_run(capture):
    _client().abort_run("run-1", gracefully=True)

    assert capture["method"] == "POST"
    assert capture["url"].endswith("/actor-runs/run-1/abort")
    assert capture["kwargs"]["params"] == {"gracefully": "true"}


def test_dataset_items_projection(capture):
    _client().dataset_items("ds-1", limit=100, fields=["title"], omit=["html"], clean=True)

    assert capture["url"].endswith("/datasets/ds-1/items")
    assert capture["kwargs"]["params"] == {
        "limit": 100, "fields": "title", "omit": "html", "clean": "true"}


def test_http_error_is_typed(monkeypatch):
    monkeypatch.setattr(ap.requests.Session, "request",
                        lambda self, *a, **k: _Resp(404, {"error": {"type": "record-not-found"}}))

    with pytest.raises(UpstreamHTTPError) as e:
        _client().run_status("nope")
    assert e.value.status_code == 404
