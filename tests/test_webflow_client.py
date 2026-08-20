"""WebflowClient — verrouille method+endpoint+body de chaque opération CMS.

Mocke `requests.request` : on vérifie le CONTRAT HTTP que le client construit,
sans réseau ni clé réelle (même patron que test_folk_client.py).
"""
import json

import pytest

from oto.tools.webflow import client as webflow_client
from oto.tools.webflow.client import WebflowClient

BASE = "https://api.webflow.com/v2"
SITE_ID = "site_123"


class _Resp:
    def __init__(self, payload, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def fake_request(method, url, headers=None, **kwargs):
        captured.append({"method": method, "url": url, "headers": headers, **kwargs})
        return _Resp({"items": [], "collections": [], "pagination": {"total": 0}})

    monkeypatch.setattr(webflow_client.requests, "request", fake_request)
    return captured


@pytest.fixture
def c():
    return WebflowClient(api_key="test-token", site_id=SITE_ID)


def test_constructor_requires_no_env_when_args_given():
    c = WebflowClient(api_key="k", site_id="s")
    assert c.api_key == "k"
    assert c.site_id == "s"


# --- site_id lazy resolution (no site_id passed to the constructor) ---------

def test_site_id_resolves_lazily_via_sites_list(monkeypatch):
    captured = []

    def fake_request(method, url, headers=None, **kwargs):
        captured.append(url)
        if url == f"{BASE}/sites":
            return _Resp({"sites": [{"id": "site_resolved"}]})
        return _Resp({"id": "site_resolved"})

    monkeypatch.setattr(webflow_client.requests, "request", fake_request)
    c = WebflowClient(api_key="k")
    assert c.get_site() == {"id": "site_resolved"}
    assert captured == [f"{BASE}/sites", f"{BASE}/sites/site_resolved"]


def test_site_id_resolution_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_request(method, url, headers=None, **kwargs):
        if url == f"{BASE}/sites":
            calls["n"] += 1
            return _Resp({"sites": [{"id": "site_resolved"}]})
        return _Resp({"collections": []})

    monkeypatch.setattr(webflow_client.requests, "request", fake_request)
    c = WebflowClient(api_key="k")
    c.list_collections()
    c.list_collections()
    assert calls["n"] == 1


def test_site_id_zero_sites_raises_value_error(monkeypatch):
    monkeypatch.setattr(
        webflow_client.requests, "request",
        lambda method, url, headers=None, **kwargs: _Resp({"sites": []}))
    c = WebflowClient(api_key="k")
    with pytest.raises(ValueError, match="aucun site"):
        c.get_site()


def test_site_id_multiple_sites_raises_value_error(monkeypatch):
    monkeypatch.setattr(
        webflow_client.requests, "request",
        lambda method, url, headers=None, **kwargs: _Resp(
            {"sites": [{"id": "a"}, {"id": "b"}]}))
    c = WebflowClient(api_key="k")
    with pytest.raises(ValueError, match="2 sites"):
        c.get_site()


def test_explicit_site_id_skips_resolution(monkeypatch):
    def fake_request(method, url, headers=None, **kwargs):
        assert url != f"{BASE}/sites", "ne doit jamais résoudre — site_id est déjà fourni"
        return _Resp({"id": SITE_ID})

    monkeypatch.setattr(webflow_client.requests, "request", fake_request)
    c = WebflowClient(api_key="k", site_id=SITE_ID)
    c.get_site()


# --- Site ------------------------------------------------------------------

def test_get_site(c, calls):
    c.get_site()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/sites/{SITE_ID}"
    assert calls[-1]["headers"]["Authorization"] == "Bearer test-token"


# --- Collections -------------------------------------------------------------

def test_list_collections(c, calls):
    c.list_collections()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/sites/{SITE_ID}/collections"


def test_get_collection(c, calls):
    c.get_collection("coll_1")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/collections/coll_1"


# --- Items -------------------------------------------------------------------

def test_list_items_default_params(c, calls):
    c.list_items("coll_1")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/collections/coll_1/items"
    assert calls[-1]["params"] == {"offset": 0, "limit": 100}


def test_list_items_caps_limit_at_100(c, calls):
    c.list_items("coll_1", limit=500)
    assert calls[-1]["params"]["limit"] == 100


def test_list_items_sort_and_locale(c, calls):
    c.list_items("coll_1", sort_by="lastUpdated", sort_order="desc",
                  cms_locale_id="loc_fr")
    params = calls[-1]["params"]
    assert params["sortBy"] == "lastUpdated"
    assert params["sortOrder"] == "desc"
    assert params["cmsLocaleId"] == "loc_fr"


def test_list_all_items_paginates_until_total(monkeypatch, c):
    pages = [
        {"items": [{"id": "1"}, {"id": "2"}], "pagination": {"total": 3}},
        {"items": [{"id": "3"}], "pagination": {"total": 3}},
    ]

    def fake_list_items(collection_id, *, offset=0, limit=100, **kwargs):
        return pages[offset // 2]

    monkeypatch.setattr(c, "list_items", fake_list_items)
    items = c.list_all_items("coll_1")
    assert [i["id"] for i in items] == ["1", "2", "3"]


def test_list_all_items_respects_cap(monkeypatch, c):
    def fake_list_items(collection_id, *, offset=0, limit=100, **kwargs):
        start = offset
        batch = [{"id": str(i)} for i in range(start, start + 100)]
        return {"items": batch, "pagination": {"total": 10_000}}

    monkeypatch.setattr(c, "list_items", fake_list_items)
    items = c.list_all_items("coll_1", cap=150)
    assert len(items) == 150


def test_get_item(c, calls):
    c.get_item("coll_1", "item_1")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/collections/coll_1/items/item_1"


def test_create_items_single(c, calls):
    c.create_items("coll_1", [{"fieldData": {"name": "Post", "slug": "post"}}])
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/collections/coll_1/items"
    assert calls[-1]["json"] == {"items": [{"fieldData": {"name": "Post", "slug": "post"}}]}


def test_create_items_bulk_single_call(c, calls):
    c.create_items("coll_1", [{"fieldData": {"name": "A", "slug": "a"}},
                               {"fieldData": {"name": "B", "slug": "b"}}])
    assert len(calls) == 1
    assert len(calls[-1]["json"]["items"]) == 2


def test_update_items(c, calls):
    c.update_items("coll_1", [{"id": "item_1", "fieldData": {"name": "New"}}])
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/collections/coll_1/items"
    assert calls[-1]["json"] == {"items": [{"id": "item_1", "fieldData": {"name": "New"}}]}


def test_delete_items(c, calls):
    c.delete_items("coll_1", ["item_1", "item_2"])
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/collections/coll_1/items"
    assert calls[-1]["json"] == {"items": [{"id": "item_1"}, {"id": "item_2"}]}


def test_publish_items(c, calls):
    c.publish_items("coll_1", ["item_1", "item_2"])
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/collections/coll_1/items/publish"
    assert calls[-1]["json"] == {"itemIds": ["item_1", "item_2"]}


# --- Webhooks ------------------------------------------------------------------

def test_list_webhooks(c, calls):
    c.list_webhooks()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/sites/{SITE_ID}/webhooks"


def test_get_webhook_has_no_site_id_in_path(c, calls):
    """get/delete sont scopés au webhook seul (pas de site_id dans le chemin) —
    contrairement à list/create, scopés au site. Vérifié live 2026-08-20."""
    c.get_webhook("wh_1")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/webhooks/wh_1"


def test_create_webhook_without_filter(c, calls):
    c.create_webhook("collection_item_created", "https://example.com/hook")
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/sites/{SITE_ID}/webhooks"
    assert calls[-1]["json"] == {
        "triggerType": "collection_item_created", "url": "https://example.com/hook"}


def test_create_webhook_with_filter(c, calls):
    c.create_webhook("form_submission", "https://example.com/hook",
                     filter={"name": "Contact Form"})
    assert calls[-1]["json"] == {
        "triggerType": "form_submission", "url": "https://example.com/hook",
        "filter": {"name": "Contact Form"}}


def test_delete_webhook(c, calls):
    c.delete_webhook("wh_1")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/webhooks/wh_1"


def test_webhook_trigger_types_match_confirmed_doc_enum():
    assert webflow_client.WebflowClient.WEBHOOK_TRIGGER_TYPES == {
        "form_submission", "site_publish",
        "page_created", "page_metadata_updated", "page_deleted",
        "ecomm_new_order", "ecomm_order_changed", "ecomm_inventory_changed",
        "collection_item_created", "collection_item_changed",
        "collection_item_deleted", "collection_item_published",
        "collection_item_unpublished", "comment_created",
    }


# --- Errors / rate limit ------------------------------------------------------

def test_raises_on_4xx(monkeypatch, c):
    from oto.tools.common import UpstreamHTTPError

    def fake_request(method, url, headers=None, **kwargs):
        return _Resp({"message": "not found"}, status_code=404)

    monkeypatch.setattr(webflow_client.requests, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as exc_info:
        c.get_item("coll_1", "missing")
    assert exc_info.value.status_code == 404


def test_retries_on_429_then_succeeds(monkeypatch, c):
    responses = [
        _Resp({}, status_code=429, headers={"Retry-After": "0"}),
        _Resp({"id": "coll_1"}, status_code=200),
    ]
    seen = []

    def fake_request(method, url, headers=None, **kwargs):
        seen.append(1)
        return responses.pop(0)

    monkeypatch.setattr(webflow_client.requests, "request", fake_request)
    monkeypatch.setattr(webflow_client.time, "sleep", lambda s: None)
    result = c.get_collection("coll_1")
    assert result == {"id": "coll_1"}
    assert len(seen) == 2
