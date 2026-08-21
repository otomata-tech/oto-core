"""PromptWatchClient — locks the HTTP contract (verb, URL, header, query
params, body shape) for a representative sample of endpoints across the v1
scope, the X-Project-Id header behavior for org-level keys, `create_content`'s
CREATE/OPTIMIZE mode validation (raised client-side, no network call), and
error translation via `raise_for_upstream`.

Mocks `requests.request`: HTTP contract only, no network, no real key.
"""
import json

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.promptwatch import client as pw_client
from oto.tools.promptwatch.client import PromptWatchClient

BASE = "https://server.promptwatch.com/api/v2"


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        captured.append({
            "method": method, "url": url, "headers": headers,
            "params": params, "json": json,
        })
        return _Resp({"prompts": [], "total": 0, "page": 1, "size": 10, "totalPages": 0})

    monkeypatch.setattr(pw_client.requests, "request", fake_request)
    return captured


@pytest.fixture
def c():
    return PromptWatchClient(api_key="test-key")


# --- auth / headers -----------------------------------------------------------

def test_sends_x_api_key(c, calls):
    c.list_projects()
    assert calls[0]["headers"]["X-API-Key"] == "test-key"


def test_no_project_id_header_by_default(c, calls):
    c.list_projects()
    assert "X-Project-Id" not in calls[0]["headers"]


def test_never_sets_content_type_header_itself(c, calls):
    """Live-confirmed 2026-08-20: PromptWatch's Fastify server rejects a
    bodyless DELETE that carries `Content-Type: application/json`
    (`FST_ERR_CTP_EMPTY_JSON_BODY`). The client must never set this header
    itself, on ANY call — `requests` adds it automatically, and only when a
    real `json=` body is passed, so the safe invariant to lock down here is
    "we never construct it ourselves" rather than "absent on DELETE only"."""
    c.delete_tag("t-1")
    assert "Content-Type" not in calls[0]["headers"]
    c.create_tags(["a"])
    assert "Content-Type" not in calls[1]["headers"]


def test_project_id_sent_when_configured(calls):
    c = PromptWatchClient(api_key="test-key", project_id="proj-123")
    c.list_projects()
    assert calls[0]["headers"]["X-Project-Id"] == "proj-123"


# --- prompts -------------------------------------------------------------------

def test_list_prompts_url_and_params(c, calls):
    c.list_prompts(page=2, size=50, llm_monitor_id="mon-1", is_active=True,
                    types=["ORGANIC"], sort_by="createdAt", sort_order="desc")
    call = calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE}/prompts"
    assert call["params"] == {
        "page": 2, "size": 50, "llmMonitorId": "mon-1", "isActive": True,
        "types": ["ORGANIC"], "sortBy": "createdAt", "sortOrder": "desc",
    }


def test_create_prompt_body_shape(c, calls):
    c.create_prompt("How do I export a report?", "mon-1", "ORGANIC",
                     intent="INFORMATIONAL", keywords=["export", "report"])
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/prompts"
    assert call["json"] == {
        "prompt": "How do I export a report?", "llmMonitorId": "mon-1",
        "type": "ORGANIC", "intent": "INFORMATIONAL",
        "keywords": ["export", "report"],
    }


def test_update_prompt_requires_intent_and_sends_both_fields(c, calls):
    c.update_prompt("p-1", "BRAND_SPECIFIC", "BRANDED")
    call = calls[0]
    assert call["method"] == "PUT"
    assert call["url"] == f"{BASE}/prompts/p-1"
    assert call["json"] == {"type": "BRAND_SPECIFIC", "intent": "BRANDED"}


def test_bulk_create_prompts_rejects_oversized_batch(c, calls):
    with pytest.raises(ValueError):
        c.bulk_create_prompts("mon-1", [{"prompt": "x", "type": "ORGANIC"}] * 101)
    assert calls == []


def test_bulk_create_prompts_rejects_empty_batch(c, calls):
    with pytest.raises(ValueError):
        c.bulk_create_prompts("mon-1", [])
    assert calls == []


def test_add_tracked_products_rejects_oversized_batch(c, calls):
    with pytest.raises(ValueError):
        c.add_tracked_products([{"externalProductId": "x", "name": "x"}] * 5001)
    assert calls == []


def test_add_tracked_pages_rejects_oversized_batch(c, calls):
    with pytest.raises(ValueError):
        c.add_tracked_pages(["https://example.com"] * 101)
    assert calls == []


def test_sentiment_time_series_dispatch(c, calls):
    c.sentiment_time_series(start_date="2026-07-01", end_date="2026-08-01")
    call = calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE}/sentiment-time-series"
    assert call["params"] == {"startDate": "2026-07-01", "endDate": "2026-08-01"}


def test_bulk_create_prompts_body_shape(c, calls):
    prompts = [{"prompt": "a", "type": "ORGANIC"}, {"prompt": "b", "type": "ORGANIC"}]
    c.bulk_create_prompts("mon-1", prompts)
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/prompts/bulk"
    assert call["json"] == {"llmMonitorId": "mon-1", "prompts": prompts}


def test_bulk_delete_prompts_body_shape(c, calls):
    c.bulk_delete_prompts(["p-1", "p-2"])
    call = calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == f"{BASE}/prompts/bulk"
    assert call["json"] == {"ids": ["p-1", "p-2"]}


def test_activate_prompts_body_shape(c, calls):
    c.activate_prompts(["p-1"])
    call = calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == f"{BASE}/prompts/bulk/activate"
    assert call["json"] == {"ids": ["p-1"]}


def test_bulk_attach_tags_body_shape(c, calls):
    c.bulk_attach_tags(["p-1", "p-2"], ["seo", "brand"])
    call = calls[0]
    assert call["url"] == f"{BASE}/prompts/bulk/tags"
    assert call["json"] == {"promptIds": ["p-1", "p-2"], "tags": ["seo", "brand"]}


def test_attach_topics_uses_names(c, calls):
    c.attach_topics("p-1", ["pricing", "onboarding"])
    call = calls[0]
    assert call["url"] == f"{BASE}/prompts/p-1/topics"
    assert call["json"] == {"topics": ["pricing", "onboarding"]}


# --- monitors --------------------------------------------------------------

def test_create_monitor_body_shape(c, calls):
    c.create_monitor("EU monitor", ["openai/gpt-4.1"], country_code="FR")
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/monitors"
    assert call["json"] == {
        "name": "EU monitor", "models": ["openai/gpt-4.1"], "countryCode": "FR",
    }


def test_delete_monitor(c, calls):
    c.delete_monitor("mon-1")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == f"{BASE}/monitors/mon-1"


# --- content: CREATE/OPTIMIZE mode validation (client-side, no network) -------

def test_create_content_create_mode_requires_type_and_length(c, calls):
    with pytest.raises(ValueError, match="type.*content_length"):
        c.create_content("CREATE", "prompt-1", "persona-1")
    assert calls == []


def test_create_content_optimize_mode_requires_level_and_url(c, calls):
    with pytest.raises(ValueError, match="optimization_level.*url"):
        c.create_content("OPTIMIZE", "prompt-1", "persona-1")
    assert calls == []


def test_create_content_rejects_unknown_mode(c, calls):
    with pytest.raises(ValueError):
        c.create_content("BOGUS", "prompt-1", "persona-1")
    assert calls == []


def test_create_content_create_mode_body_shape(c, calls):
    c.create_content("CREATE", "prompt-1", "persona-1", type="BLOG_POST",
                      content_length="MEDIUM")
    call = calls[0]
    assert call["url"] == f"{BASE}/content/create"
    assert call["json"] == {
        "mode": "CREATE", "promptId": "prompt-1", "personaId": "persona-1",
        "type": "BLOG_POST", "contentLength": "MEDIUM",
    }


# --- errors -----------------------------------------------------------------

def test_401_raises_upstream_http_error(c, monkeypatch):
    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        return _Resp({"code": "UNAUTHORIZED"}, status_code=401)

    monkeypatch.setattr(pw_client.requests, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as exc:
        c.list_projects()
    assert exc.value.status_code == 401


# --- extended surface (publishing/content-agent/ads/shopping/site-health/
# sitemap/page-tracker/models/actions/query-fanouts/social) --------------------

def test_set_content_publication_body_shape(c, calls):
    c.set_content_publication("cid-1", "https://example.com/post", published_at="2026-08-20")
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/content/cid-1/publication"
    assert call["json"] == {"url": "https://example.com/post", "publishedAt": "2026-08-20"}


def test_clear_content_publication(c, calls):
    c.clear_content_publication("cid-1")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == f"{BASE}/content/cid-1/publication"


def test_update_content_agent_settings_passes_camelcase_fields(c, calls):
    c.update_content_agent_settings(autonomyMode="GATED", maxPerDay=5)
    call = calls[0]
    assert call["method"] == "PUT"
    assert call["url"] == f"{BASE}/content-agent/settings"
    assert call["json"] == {"autonomyMode": "GATED", "maxPerDay": 5}


def test_list_content_agent_slots_params(c, calls):
    c.list_content_agent_slots(page=1, size=10, statuses=["REVIEW", "APPROVED"])
    call = calls[0]
    assert call["url"] == f"{BASE}/content-agent/slots"
    assert call["params"] == {"page": 1, "size": 10, "statuses": ["REVIEW", "APPROVED"]}


def test_accept_content_agent_slot(c, calls):
    c.accept_content_agent_slot("slot-1")
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/content-agent/slots/slot-1/accept"


def test_list_ads_from_until_params(c, calls):
    c.list_ads(page=1, size=10, from_="2026-01-01", until="2026-02-01")
    call = calls[0]
    assert call["url"] == f"{BASE}/ads"
    assert call["params"]["from"] == "2026-01-01"
    assert call["params"]["until"] == "2026-02-01"


def test_list_ad_domains_url(c, calls):
    c.list_ad_domains()
    assert calls[0]["url"] == f"{BASE}/ads/domains"


def test_add_tracked_products_body_shape(c, calls):
    products = [{"externalProductId": "sku-1", "name": "Widget"}]
    c.add_tracked_products(products)
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/shopping/tracked-products"
    assert call["json"] == {"products": products}


def test_update_tracked_product_body_shape(c, calls):
    c.update_tracked_product("prod-1", name="New name")
    call = calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == f"{BASE}/shopping/tracked-products/prod-1"
    assert call["json"] == {"name": "New name"}


def test_delete_tracked_product(c, calls):
    c.delete_tracked_product("prod-1")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == f"{BASE}/shopping/tracked-products/prod-1"


def test_site_health_pages_params(c, calls):
    c.site_health_pages(page=1, size=20, issue_types=["missingTitle"])
    call = calls[0]
    assert call["url"] == f"{BASE}/site-health"
    assert call["params"] == {"page": 1, "size": 20, "issueTypes": ["missingTitle"]}


def test_sitemap_crawl_progress_url(c, calls):
    c.sitemap_crawl_progress()
    assert calls[0]["url"] == f"{BASE}/sitemap/progress"


def test_add_tracked_pages_body_shape(c, calls):
    c.add_tracked_pages(["https://example.com/a", "https://example.com/b"])
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/page-tracker"
    assert call["json"] == {"urls": ["https://example.com/a", "https://example.com/b"]}


def test_delete_tracked_page(c, calls):
    c.delete_tracked_page("pg-1")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == f"{BASE}/page-tracker/pg-1"


def test_list_models_url(c, calls):
    c.list_models()
    assert calls[0]["url"] == f"{BASE}/models"


def test_update_action_item_body_shape(c, calls):
    c.update_action_item("act-1", status="DISMISSED")
    call = calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == f"{BASE}/actions/act-1"
    assert call["json"] == {"status": "DISMISSED"}


def test_list_query_fanouts_url(c, calls):
    c.list_query_fanouts()
    assert calls[0]["url"] == f"{BASE}/query-fanouts"


def test_list_reddit_citations_params(c, calls):
    c.list_reddit_citations(subreddit_name="crm", from_="2026-01-01")
    call = calls[0]
    assert call["url"] == f"{BASE}/socials/reddit"
    assert call["params"]["subredditName"] == "crm"
    assert call["params"]["from"] == "2026-01-01"


def test_list_youtube_citations_url(c, calls):
    c.list_youtube_citations(channel_name="Folk")
    call = calls[0]
    assert call["url"] == f"{BASE}/socials/youtube"
    assert call["params"]["channelName"] == "Folk"
