"""Contrat du client Tavily (Bearer, search/extract/crawl/map).

Mocke `requests.Session.request` : vérifie chemin, corps, compaction des options
non renseignées (une clé à None ne doit PAS partir — sinon elle écrase le défaut
de l'API par un null) et la présence systématique de `include_usage` (la réponse
doit porter `usage.credits` pour la facturation).
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.tavily import client as tv


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
        return _Resp(200, {"results": []})

    monkeypatch.setattr(tv.requests.Session, "request", fake_request)
    return seen


def _client():
    return tv.TavilyClient(api_key="tvly-test")


def test_auth_header_is_bearer():
    assert _client().session.headers["Authorization"] == "Bearer tvly-test"


def test_search_compacts_and_always_asks_usage(capture):
    _client().search("acme pricing", topic="news", max_results=3)
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.tavily.com/search"
    body = capture["kwargs"]["json"]
    assert body == {"query": "acme pricing", "topic": "news", "max_results": 3,
                    "include_usage": True}
    assert "search_depth" not in body


def test_extract_accepts_single_or_list(capture):
    _client().extract("https://a.com", query="prix")
    assert capture["url"].endswith("/extract")
    assert capture["kwargs"]["json"]["urls"] == "https://a.com"
    _client().extract(["https://a.com", "https://b.com"], timeout_s=30)
    body = capture["kwargs"]["json"]
    assert body["urls"] == ["https://a.com", "https://b.com"]
    assert body["timeout"] == 30            # `timeout_s` → clé API `timeout`
    assert body["include_usage"] is True


def test_crawl_and_map_share_traversal_body(capture):
    _client().crawl("https://a.com", instructions="pages tarifs", limit=10,
                    extract_depth="advanced")
    body = capture["kwargs"]["json"]
    assert capture["url"].endswith("/crawl")
    assert body["instructions"] == "pages tarifs" and body["limit"] == 10
    assert body["extract_depth"] == "advanced"

    _client().map_site("https://a.com", limit=10, select_paths=["/blog/.*"])
    body = capture["kwargs"]["json"]
    assert capture["url"].endswith("/map")
    assert body == {"url": "https://a.com", "limit": 10,
                    "select_paths": ["/blog/.*"], "include_usage": True}
    assert "extract_depth" not in body


def test_upstream_error_is_typed(monkeypatch):
    monkeypatch.setattr(tv.requests.Session, "request",
                        lambda self, m, u, **kw: _Resp(432, {"detail": "plan limit"}))
    with pytest.raises(UpstreamHTTPError) as ei:
        _client().search("x")
    assert ei.value.status_code == 432
    assert ei.value.service == "tavily"
