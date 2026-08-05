"""Contrat du client Firecrawl (v2, Bearer, scrape/crawl/map/search/extract).

Mocke `requests.Session.request` : vérifie chemin, corps camelCase et compaction
des options non renseignées (une clé à None ne doit PAS partir — sinon elle écrase
le défaut de l'API par un null).
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.firecrawl import client as fc


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
        return _Resp(200, {"success": True})

    monkeypatch.setattr(fc.requests.Session, "request", fake_request)
    return seen


def _client():
    return fc.FirecrawlClient(api_key="fc-test")


def test_auth_header_is_bearer():
    assert _client().session.headers["Authorization"] == "Bearer fc-test"


def test_scrape_posts_camel_case_and_drops_unset(capture):
    _client().scrape("https://acme.com", formats=["markdown"], only_main_content=True)

    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.firecrawl.dev/v2/scrape"
    body = capture["kwargs"]["json"]
    assert body == {"url": "https://acme.com", "formats": ["markdown"],
                    "onlyMainContent": True}
    assert "waitFor" not in body and "excludeTags" not in body


def test_crawl_maps_every_option(capture):
    _client().crawl("https://acme.com", limit=50, include_paths=["/blog/.*"],
                    max_discovery_depth=2, crawl_entire_domain=True,
                    scrape_options={"formats": ["markdown"]})

    body = capture["kwargs"]["json"]
    assert body["limit"] == 50
    assert body["includePaths"] == ["/blog/.*"]
    assert body["maxDiscoveryDepth"] == 2
    assert body["crawlEntireDomain"] is True
    assert body["scrapeOptions"] == {"formats": ["markdown"]}


def test_crawl_status_by_id(capture):
    _client().crawl_status("job-1")

    assert capture["method"] == "GET"
    assert capture["url"] == "https://api.firecrawl.dev/v2/crawl/job-1"


def test_crawl_status_follows_absolute_next_url(capture):
    """`next` est une URL ABSOLUE : la préfixer de BASE_URL la casserait."""
    _client().crawl_status(next_url="https://api.firecrawl.dev/v2/crawl/job-1?skip=10")

    assert capture["url"] == "https://api.firecrawl.dev/v2/crawl/job-1?skip=10"


def test_crawl_status_requires_an_identifier():
    with pytest.raises(ValueError):
        _client().crawl_status()


def test_map_and_search_paths(capture):
    _client().map_site("https://acme.com", search="pricing")
    assert capture["url"].endswith("/map")
    assert capture["kwargs"]["json"] == {"url": "https://acme.com", "search": "pricing"}

    _client().search("agence IA marseille", limit=5,
                     scrape_options={"formats": ["markdown"]})
    assert capture["url"].endswith("/search")
    assert capture["kwargs"]["json"]["query"] == "agence IA marseille"
    assert capture["kwargs"]["json"]["limit"] == 5


def test_extract_start_and_status(capture):
    _client().extract(["https://acme.com/*"], prompt="les tarifs",
                      schema={"type": "object"})
    assert capture["url"].endswith("/extract")
    assert capture["kwargs"]["json"]["urls"] == ["https://acme.com/*"]
    assert capture["kwargs"]["json"]["schema"] == {"type": "object"}

    _client().extract_status("x-1")
    assert capture["method"] == "GET"
    assert capture["url"].endswith("/extract/x-1")


def test_http_error_is_typed(monkeypatch):
    monkeypatch.setattr(fc.requests.Session, "request",
                        lambda self, *a, **k: _Resp(402, {"error": "Payment required"}))

    with pytest.raises(UpstreamHTTPError) as e:
        _client().scrape("https://acme.com")
    assert e.value.status_code == 402
    assert e.value.is_client_error
