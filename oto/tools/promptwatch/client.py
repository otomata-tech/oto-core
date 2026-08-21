"""PromptWatch API v2 Client — https://promptwatch.com/docs/api-reference/

AI visibility monitoring: track how brands/products appear in LLM answers
(ChatGPT, Claude, Gemini…), across prompts organized into monitors, with
visibility/sentiment/citation analytics and AI-generated content to close
coverage gaps.

Auth = API key, header `X-API-Key`. Base `https://server.promptwatch.com/api/v2`.
Org-level keys additionally scope to one project via header `X-Project-Id`
(UUID) — project-level keys don't need it (already scoped). `project_id`
passed to the constructor is sent on every request; `list_projects()` is how
an org-level key discovers which id to use.

Full API surface covered (all 25 doc categories — nothing deferred):
- projects          — list
- monitors          — CRUD
- prompts           — CRUD + native bulk (create/delete/activate/deactivate,
                      tag/topic attachment solo+bulk)
- responses         — list/get + summary/sentiment/mentions/competitors analytics
- visibility        — time series (brand, per-prompt) + competitor heatmap
- citations         — analytics, rank, domains, grouped table, LLM sources,
                      self-frequency, top pages
- content           — list/get/create (CREATE or OPTIMIZE mode)
- content gap       — stats, prompt list, latest coverage, recommendations
- tags / topics     — CRUD (rename/delete/list/create)
- brands            — list/create/update
- personas          — CRUD
- publishing        — CMS connections, publish status, set/clear publication,
                      push draft, publish live
- content agent     — settings, scheduled slots (list/get/update/accept/
                      decline/publish now)
- ads radar         — ads, prompts with ads, ad domains + analytics
- shopping          — product appearances/analytics, tracked products CRUD
- site health       — flagged pages (missing title/description, thin content…)
- sitemap           — crawl progress, crawled URLs
- page tracker      — track/untrack URLs, citing prompts/responses
- models            — available LLM models
- action items      — list, update status
- query fanouts     — prompts with query fanouts
- social citations  — Reddit + YouTube

⚠️ Most endpoints below were NOT individually doc-fetched with full param
lists — query params are inferred from PromptWatch's own highly consistent
conventions across the endpoints that WERE verified (page/size pagination,
startDate/endDate ≤90-day windows, llmMonitorId/promptId/models/topicIds
filters, sortBy/sortOrder, from/until for date-range list endpoints). Each
such method also accepts `**extra_params`, merged into the query string, as
an escape hatch if the real param name differs.

**Live-verified against a real account, 2026-08-20** (`fastmcp.Client`
in-memory, real MCP `tools/call`, GET/read paths only): all core-scope
endpoints (list-prompts, list-monitors incl. the bare-array correction
below, list-responses, response_summary, visibility_time_series, citations,
list-content, list-tags, list-personas, list-brands) + all extended-scope
GET endpoints (models, actions, query-fanouts, publishing connections,
content-agent settings, ads list/domains, shopping items/top-products/
tracked/products-over-time/top-merchant-domains, sitemap progress/urls,
site-health, page-tracker list, reddit/youtube citations). Two path guesses
were WRONG and got corrected from the live 404 (`ad_domain_analytics`:
`ads/domain-analytics` singular, not `ads/domains/analytics`;
`shopping_product_position_analytics`: `shopping/product-position-analytics`
singular, not `shopping/position-analytics` — and it takes no item filter,
top-N ranking only). `delete_tracked_product` remains UNVERIFIED (a mutating
call, not exercised against the live account) but follows this API's
otherwise-universal `DELETE .../{id}` convention.
`list_monitors` deviates from its inferred paginated-envelope shape (bare
array, see its own docstring) — the one confirmed surprise among the
core-scope endpoints. Treat anything not explicitly called out above as
inferred-but-unverified.

**Advisor bug-review pass, 2026-08-20** (doc cross-check, no new live calls —
the entire mutating surface remains unexercised against a real request in
either direction; the fixes below are corrections against fetched doc text,
not new live confirmations):
- `update_prompt`: `intent` was optional client-side; the doc states both
  `type` AND `intent` are required (full replace, not a partial patch) — now
  a required positional arg, both call sites (tool layer) updated to match.
- `bulk_create_prompts`/`bulk_delete_prompts`/`activate_prompts`/
  `deactivate_prompts` (1-100) and `add_tracked_products` (1-5000) and
  `add_tracked_pages` (1-100) now enforce PromptWatch's documented item-count
  bounds client-side (`_check_bulk_size`, raises `ValueError`) instead of
  letting an oversized/empty batch round-trip to a 400.
- Added `sentiment_time_series` (`GET /sentiment-time-series`, project-wide
  brand sentiment) — present in the doc index alongside
  `visibility_time_series` but missed in the original build, which only
  covered its response-scoped sibling `response_sentiment_time_series`
  (`/responses/sentiment-time-series`). Unverified against a real response.

**Live write-path smoke test, 2026-08-20 — found and fixed a real, systemic
bug**: `_headers()` unconditionally sent `Content-Type: application/json`,
even on requests with no body. PromptWatch's Fastify server rejects any
bodyless call carrying that header (`FST_ERR_CTP_EMPTY_JSON_BODY`) — this
broke EVERY DELETE endpoint (`delete_monitor`, `delete_prompt`, `delete_tag`,
`delete_topic`, `delete_persona`, `clear_content_publication`,
`delete_tracked_product`, `delete_tracked_page`) and every bodyless POST
action (`push_content_draft_to_cms`, `publish_content_live`,
`accept_content_agent_slot`, `decline_content_agent_slot`,
`publish_content_agent_slot_now` when called with no extra params) — 13
endpoints, unreachable until this fix. Root cause: `requests` already sets
`Content-Type: application/json` automatically whenever a real `json=` body
is passed, so the manual header was both redundant AND actively harmful on
bodyless calls — removed. Verified end-to-end against the real account after
the fix: `create_tags` → `list_tags` → `rename_tag` → `delete_tag` →
re-`delete_tag` (confirms 404 `TAG_NOT_FOUND`, tag fully gone), self-cleaning,
no residue left on the account. One side-finding, not a bug: `list_tags`
returns tags with `promptCount: 0` (the just-created tag) but they don't
appear in `list_tags`'s own response — confirmed reproducible (3 retries,
5s wait), path/params match the doc exactly, so this looks like an upstream
filter on unused tags rather than a client defect; not chased further since
`rename_tag`/`delete_tag` work fine when the id is already known.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


def _clean(**kwargs) -> Dict[str, Any]:
    """Drops None values — PromptWatch treats an explicit empty/absent param
    differently from one that's simply not sent for several filters."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _check_bulk_size(items: List, name: str, max_n: int) -> None:
    """Fails locally against PromptWatch's documented per-request bounds,
    instead of letting an oversized batch round-trip to a 400."""
    if not (1 <= len(items) <= max_n):
        raise ValueError(f"{name} must have between 1 and {max_n} items, got {len(items)}")


class PromptWatchClient:
    """Client for the PromptWatch API v2 (AI visibility monitoring)."""

    BASE_URL = "https://server.promptwatch.com/api/v2"
    TIMEOUT = 30

    def __init__(self, api_key: str | None = None, project_id: str | None = None):
        """
        Args:
            api_key: PromptWatch API key (dashboard Settings > API Keys).
                Defaults to env `PROMPTWATCH_API_KEY`.
            project_id: project UUID, sent as `X-Project-Id` on every request.
                Only meaningful for an org-level key targeting >1 project —
                a project-level key ignores it. See `list_projects()`.
        """
        self.api_key = api_key or require_secret("PROMPTWATCH_API_KEY")
        self.project_id = project_id or None

    def _headers(self) -> Dict[str, str]:
        """No static `Content-Type` here — `requests` sets it automatically
        when a `json=` body is actually passed. Fastify (PromptWatch's
        server) rejects DELETE/other bodyless calls with `Content-Type:
        application/json` + an empty body (`FST_ERR_CTP_EMPTY_JSON_BODY`,
        live-confirmed 2026-08-20 on `delete_tag`) — every bodyless mutating
        call in this client was broken by a static header here."""
        headers = {"X-API-Key": self.api_key}
        if self.project_id:
            headers["X-Project-Id"] = self.project_id
        return headers

    def _request(self, method: str, endpoint: str, *,
                 params: Optional[dict] = None, json: Optional[dict] = None) -> Any:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        resp = requests.request(
            method, url, headers=self._headers(), params=params, json=json,
            timeout=self.TIMEOUT,
        )
        raise_for_upstream(resp, service="promptwatch")
        return resp.json() if resp.content else {}

    # --- Projects --------------------------------------------------------

    def list_projects(self) -> List[Dict]:
        """Org-level key only — projects it can target via `project_id`."""
        return self._request("GET", "projects").get("projects", [])

    # --- Monitors ----------------------------------------------------------

    def list_monitors(self, page: int = 1, size: int = 10, **extra_params) -> List[Dict]:
        """⚠️ Live-verified (2026-08-20): unlike prompts/responses/content,
        this endpoint returns a BARE array, no `{total,page,size,totalPages}`
        envelope — `page`/`size` are sent but pagination metadata isn't
        returned, so there's no way to tell from the response alone whether
        the list was truncated."""
        return self._request("GET", "monitors",
                              params=_clean(page=page, size=size, **extra_params))

    def get_monitor(self, monitor_id: str) -> Dict:
        return self._request("GET", f"monitors/{monitor_id}")

    def create_monitor(self, name: str, models: List[str], *,
                        description: str = None, language_code: str = None,
                        country_code: str = None, state_code: str = None,
                        city_code: str = None, prompt_frequency: str = None,
                        persona_id: str = None, persona_stacking_enabled: bool = None,
                        initial_prompts: List[Dict] = None,
                        generate_prompts: List[Dict] = None) -> Dict:
        """`initial_prompts`: [{"prompt","type","intent"?,"keywords"?}, …].
        `generate_prompts` (max 3): [{"amount" (1-50), "type"?, "instructions"?}, …]."""
        body = _clean(
            name=name, models=models, description=description,
            languageCode=language_code, countryCode=country_code,
            stateCode=state_code, cityCode=city_code,
            promptFrequency=prompt_frequency, personaId=persona_id,
            personaStackingEnabled=persona_stacking_enabled,
            initialPrompts=initial_prompts, generatePrompts=generate_prompts,
        )
        return self._request("POST", "monitors", json=body)

    def update_monitor(self, monitor_id: str, **fields) -> Dict:
        """`fields` in PromptWatch's own camelCase (e.g. `promptFrequency=`)."""
        return self._request("PUT", f"monitors/{monitor_id}", json=fields)

    def delete_monitor(self, monitor_id: str) -> Dict:
        """Soft-delete — data retained but hidden."""
        return self._request("DELETE", f"monitors/{monitor_id}")

    # --- Prompts -------------------------------------------------------------

    def list_prompts(self, page: int = 1, size: int = 10, *,
                      llm_monitor_id: str = None, query: str = None,
                      is_active: bool = None, types: List[str] = None,
                      topic_ids=None, sort_by: str = None,
                      sort_order: str = None, **extra_params) -> Dict:
        params = _clean(
            page=page, size=size, llmMonitorId=llm_monitor_id, query=query,
            isActive=is_active, types=types, topicIds=topic_ids,
            sortBy=sort_by, sortOrder=sort_order, **extra_params,
        )
        return self._request("GET", "prompts", params=params)

    def get_prompt(self, prompt_id: str) -> Dict:
        return self._request("GET", f"prompts/{prompt_id}")

    def create_prompt(self, prompt: str, llm_monitor_id: str, type: str, *,
                       intent: str = None, language_code: str = None,
                       keywords: List[str] = None, tags: List[str] = None,
                       is_active: bool = None) -> Dict:
        body = _clean(
            prompt=prompt, llmMonitorId=llm_monitor_id, type=type,
            intent=intent, languageCode=language_code, keywords=keywords,
            tags=tags, isActive=is_active,
        )
        return self._request("POST", "prompts", json=body)

    def update_prompt(self, prompt_id: str, type: str, intent: str) -> Dict:
        """`type` AND `intent` are both required by this endpoint (full
        replace, not a partial patch) — omitting `intent` risks silently
        nulling it out upstream."""
        return self._request("PUT", f"prompts/{prompt_id}",
                              json={"type": type, "intent": intent})

    def delete_prompt(self, prompt_id: str) -> Dict:
        """Soft-delete."""
        return self._request("DELETE", f"prompts/{prompt_id}")

    def bulk_create_prompts(self, llm_monitor_id: str, prompts: List[Dict]) -> Dict:
        """`prompts`: 1-100 items, each `{"prompt","type","intent"?,
        "languageCode"?,"keywords"?,"tags"?,"isActive"?}`."""
        _check_bulk_size(prompts, "prompts", 100)
        return self._request("POST", "prompts/bulk",
                              json={"llmMonitorId": llm_monitor_id, "prompts": prompts})

    def bulk_delete_prompts(self, ids: List[str]) -> Dict:
        """1-100 prompt ids. Soft-delete."""
        _check_bulk_size(ids, "ids", 100)
        return self._request("DELETE", "prompts/bulk", json={"ids": ids})

    def activate_prompts(self, ids: List[str]) -> Dict:
        """1-100 prompt ids."""
        _check_bulk_size(ids, "ids", 100)
        return self._request("PATCH", "prompts/bulk/activate", json={"ids": ids})

    def deactivate_prompts(self, ids: List[str]) -> Dict:
        """1-100 prompt ids."""
        _check_bulk_size(ids, "ids", 100)
        return self._request("PATCH", "prompts/bulk/deactivate", json={"ids": ids})

    def attach_tags(self, prompt_id: str, tags: List[str]) -> Dict:
        """Tag NAMES — auto-created if they don't already exist."""
        return self._request("POST", f"prompts/{prompt_id}/tags", json={"tags": tags})

    def attach_topics(self, prompt_id: str, topics: List[str]) -> Dict:
        """Topic NAMES (not ids) — auto-created if they don't already exist."""
        return self._request("POST", f"prompts/{prompt_id}/topics", json={"topics": topics})

    def bulk_attach_tags(self, prompt_ids: List[str], tags: List[str]) -> Dict:
        """Tag NAMES, auto-created; applied to every id in `prompt_ids`."""
        return self._request("POST", "prompts/bulk/tags",
                              json={"promptIds": prompt_ids, "tags": tags})

    def bulk_attach_topics(self, prompt_ids: List[str], topics: List[str]) -> Dict:
        """Topic NAMES, auto-created; applied to every id in `prompt_ids`."""
        return self._request("POST", "prompts/bulk/topics",
                              json={"promptIds": prompt_ids, "topics": topics})

    # --- Responses -------------------------------------------------------------

    def list_responses(self, page: int = 1, size: int = 10, *,
                        llm_monitor_id: str = None, prompt_id: str = None,
                        models: List[str] = None, sentiment: List[str] = None,
                        mentioned_our_brand: bool = None,
                        prompt_types: List[str] = None, topic_ids=None,
                        from_: str = None, until: str = None,
                        sort_by: str = None, sort_order: str = None,
                        **extra_params) -> Dict:
        params = _clean(
            page=page, size=size, llmMonitorId=llm_monitor_id,
            promptId=prompt_id, models=models, sentiment=sentiment,
            mentionedOurBrand=mentioned_our_brand, promptTypes=prompt_types,
            topicIds=topic_ids, until=until,
            sortBy=sort_by, sortOrder=sort_order, **extra_params,
        )
        if from_ is not None:
            params["from"] = from_
        return self._request("GET", "responses", params=params)

    def get_response(self, response_id: str) -> Dict:
        """Includes full citations, unlike the list view."""
        return self._request("GET", f"responses/{response_id}")

    def response_summary(self, start_date: str = None, end_date: str = None) -> Dict:
        """`{totalResponses, totalBrandMentions, brandMentionRate}`. Date
        range (YYYY-MM-DD) capped at 90 days by PromptWatch."""
        return self._request("GET", "responses/summary",
                              params=_clean(startDate=start_date, endDate=end_date))

    def sentiment_distribution(self, start_date: str = None, end_date: str = None,
                                **extra_params) -> Dict:
        return self._request("GET", "responses/sentiment-distribution",
                              params=_clean(startDate=start_date, endDate=end_date,
                                            **extra_params))

    def response_sentiment_time_series(self, start_date: str = None,
                                        end_date: str = None, **extra_params) -> Any:
        return self._request("GET", "responses/sentiment-time-series",
                              params=_clean(startDate=start_date, endDate=end_date,
                                            **extra_params))

    def mentions_time_series(self, start_date: str = None, end_date: str = None,
                              **extra_params) -> Any:
        """Brand/competitor mentions over time."""
        return self._request("GET", "responses/mentions-time-series",
                              params=_clean(startDate=start_date, endDate=end_date,
                                            **extra_params))

    def top_competitors(self, start_date: str = None, end_date: str = None,
                         **extra_params) -> Any:
        return self._request("GET", "responses/competitors",
                              params=_clean(startDate=start_date, endDate=end_date,
                                            **extra_params))

    # --- Visibility --------------------------------------------------------

    def visibility_time_series(self, start_date: str = None, end_date: str = None,
                                range: str = None, models: List[str] = None,
                                prompt_id: str = None, llm_monitor_id: str = None) -> List[Dict]:
        """`range`: day|week|month (default day). Date span ≤90 days."""
        params = _clean(startDate=start_date, endDate=end_date, range=range,
                         models=models, promptId=prompt_id, llmMonitorId=llm_monitor_id)
        return self._request("GET", "visibility-time-series", params=params)

    def sentiment_time_series(self, start_date: str = None, end_date: str = None,
                               range: str = None, models: List[str] = None,
                               prompt_id: str = None, llm_monitor_id: str = None) -> List[Dict]:
        """Project-wide brand sentiment over time — the top-level
        `/sentiment-time-series` sibling of `visibility_time_series`, distinct
        from `response_sentiment_time_series` (`/responses/sentiment-time-series`,
        response-level). `range`: day|week|month (default day). Not
        live-verified — added from doc listing, unexercised against a real
        response."""
        params = _clean(startDate=start_date, endDate=end_date, range=range,
                         models=models, promptId=prompt_id, llmMonitorId=llm_monitor_id)
        return self._request("GET", "sentiment-time-series", params=params)

    def prompt_visibility_time_series(self, prompt_id: str, start_date: str = None,
                                       end_date: str = None, range: str = None,
                                       models: List[str] = None) -> List[Dict]:
        params = _clean(promptId=prompt_id, startDate=start_date, endDate=end_date,
                         range=range, models=models)
        return self._request("GET", "prompt-visibility-time-series", params=params)

    def competitor_heatmap(self, start_date: str = None, end_date: str = None, *,
                            models: List[str] = None, prompt_id: str = None,
                            llm_monitor_id: str = None, exclude_self: bool = None,
                            hide_ignored_brands: bool = None,
                            relations: List[str] = None, limit: int = None,
                            prompt_types: List[str] = None, tag_ids=None,
                            topic_ids=None) -> Dict:
        """`relations`: DIRECT_COMPETITOR|SELF|OTHER|IGNORED. `limit`: 1-100
        (default 20)."""
        params = _clean(
            startDate=start_date, endDate=end_date, models=models,
            promptId=prompt_id, llmMonitorId=llm_monitor_id,
            excludeSelf=exclude_self, hideIgnoredBrands=hide_ignored_brands,
            relations=relations, limit=limit, promptTypes=prompt_types,
            tagIds=tag_ids, topicIds=topic_ids,
        )
        return self._request("GET", "competitor-heatmap", params=params)

    # --- Citations -----------------------------------------------------------

    def citations(self, start_date: str = None, end_date: str = None, *,
                  models: List[str] = None, prompt_id: str = None,
                  llm_monitor_id: str = None, prompt_types: List[str] = None,
                  domains: List[str] = None, topic_ids=None,
                  page: int = None, size: int = None) -> Dict:
        """Top cited domains/URLs + authority metrics. Date span ≤90 days."""
        params = _clean(
            startDate=start_date, endDate=end_date, models=models,
            promptId=prompt_id, llmMonitorId=llm_monitor_id,
            promptTypes=prompt_types, domains=domains, topicIds=topic_ids,
            page=page, size=size,
        )
        return self._request("GET", "citations", params=params)

    def citation_rank_analysis(self, **extra_params) -> Any:
        return self._request("GET", "citations/rank-analysis", params=_clean(**extra_params))

    def citation_domains_over_time(self, **extra_params) -> Any:
        return self._request("GET", "citations/domains-over-time", params=_clean(**extra_params))

    def citation_domains_by_llm(self, **extra_params) -> Any:
        return self._request("GET", "citations/domains-by-llm", params=_clean(**extra_params))

    def citation_grouped(self, page: int = 1, size: int = 20, **extra_params) -> Dict:
        """Paginated sortable table of cited URLs."""
        return self._request("GET", "citations/grouped",
                              params=_clean(page=page, size=size, **extra_params))

    def citation_llm_sources(self, **extra_params) -> Any:
        return self._request("GET", "citations/llm-sources", params=_clean(**extra_params))

    def citation_self_frequency(self, **extra_params) -> Any:
        return self._request("GET", "citations/self-frequency", params=_clean(**extra_params))

    def citation_top_pages(self, **extra_params) -> Any:
        return self._request("GET", "citations/top-pages", params=_clean(**extra_params))

    # --- Content ---------------------------------------------------------------

    def list_content(self, page: int = 1, size: int = 25, *, order_by: str = None,
                      sort_order: str = None, mode: str = None, status: str = None) -> Dict:
        """`mode`: CREATE|OPTIMIZE. `status`: DRAFT|PENDING|IN_PROGRESS|
        COMPLETED|FAILED|STOPPED."""
        params = _clean(page=page, size=size, orderBy=order_by,
                         sortOrder=sort_order, mode=mode, status=status)
        return self._request("GET", "content", params=params)

    def get_content(self, content_id: str) -> Dict:
        return self._request("GET", f"content/{content_id}")

    def create_content(self, mode: str, prompt_id: str, persona_id: str, *,
                        type: str = None, content_length: str = None,
                        optimization_level: str = None, url: str = None,
                        tone_of_voice: str = None, custom_tone_of_voice: str = None,
                        language_code: str = None, image_artistic_style: str = None,
                        image_prompt_instructions: str = None,
                        blocked_words: List[str] = None, brief_title: str = None,
                        brief_description: str = None, brief_objective: str = None,
                        brief_call_to_action: str = None,
                        brief_key_points: List[str] = None, brief_context: str = None,
                        content_gap_recommendation_id: str = None) -> Dict:
        """Starts async generation, returns `{id, status: "PENDING"}` — poll
        `get_content(id)`.

        `mode="CREATE"` (new content): requires `type` (ARTICLE|BLOG_POST|
        OPINION|LISTICLE|HOW_TO|REVIEW|COMPARISON|CASE_STUDY|INTERVIEW|
        DOCUMENTATION|WIKI|PRODUCT_PAGE|LANDING_PAGE|PRESS_RELEASE|
        GENERIC_CONTENT|PRODUCT_COMPARISON) and `content_length`
        (SHORT|MEDIUM|LONG); optional brief_* fields.

        `mode="OPTIMIZE"` (rewrite an existing sitemap page): requires
        `optimization_level` (LOW|MEDIUM|HIGH) and `url`; no brief_* fields.
        """
        if mode not in ("CREATE", "OPTIMIZE"):
            raise ValueError("create_content: mode must be 'CREATE' or 'OPTIMIZE'")
        if mode == "CREATE" and not (type and content_length):
            raise ValueError("create_content(mode='CREATE') requires `type` and `content_length`")
        if mode == "OPTIMIZE" and not (optimization_level and url):
            raise ValueError("create_content(mode='OPTIMIZE') requires `optimization_level` and `url`")
        body = _clean(
            mode=mode, promptId=prompt_id, personaId=persona_id, type=type,
            contentLength=content_length, optimizationLevel=optimization_level,
            url=url, toneOfVoice=tone_of_voice,
            customToneOfVoice=custom_tone_of_voice, languageCode=language_code,
            imageArtisticStyle=image_artistic_style,
            imagePromptInstructions=image_prompt_instructions,
            blockedWords=blocked_words, briefTitle=brief_title,
            briefDescription=brief_description, briefObjective=brief_objective,
            briefCallToAction=brief_call_to_action, briefKeyPoints=brief_key_points,
            briefContext=brief_context,
            contentGapRecommendationId=content_gap_recommendation_id,
        )
        return self._request("POST", "content/create", json=body)

    # --- Content gap -----------------------------------------------------------

    def content_gap_stats(self, prompt_types: List[str] = None,
                           start_date: str = None, end_date: str = None) -> Dict:
        params = _clean(promptTypes=prompt_types, startDate=start_date, endDate=end_date)
        return self._request("GET", "content-gap/stats", params=params)

    def content_gap_prompts(self, page: int = 1, size: int = 25, *,
                             query: str = None, prompt_types: List[str] = None,
                             intent_types: List[str] = None, tag_ids=None,
                             topic_ids=None, has_coverage: bool = None,
                             sort_by: str = None, sort_order: str = None,
                             start_date: str = None, end_date: str = None) -> Dict:
        params = _clean(
            page=page, size=size, query=query, promptTypes=prompt_types,
            intentTypes=intent_types, tagIds=tag_ids, topicIds=topic_ids,
            hasCoverage=has_coverage, sortBy=sort_by, sortOrder=sort_order,
            startDate=start_date, endDate=end_date,
        )
        return self._request("GET", "content-gap/prompts", params=params)

    def content_gap_latest(self, prompt_id: str) -> Dict:
        """Latest content-coverage analysis for one prompt."""
        return self._request("GET", f"content-gap/prompts/{prompt_id}/latest")

    def content_gap_recommendations(self, prompt_id: str) -> Any:
        """Content recommendations from the prompt's latest coverage analysis."""
        return self._request("GET", f"content-gap/prompts/{prompt_id}/latest/recommendations")

    # --- Tags ------------------------------------------------------------------

    def list_tags(self) -> Any:
        return self._request("GET", "tags")

    def create_tags(self, names: List[str]) -> Dict:
        return self._request("POST", "tags", json={"names": names})

    def delete_tag(self, tag_id: str) -> Dict:
        return self._request("DELETE", f"tags/{tag_id}")

    def rename_tag(self, tag_id: str, name: str) -> Dict:
        return self._request("PATCH", f"tags/{tag_id}", json={"name": name})

    # --- Topics ------------------------------------------------------------

    def list_topics(self) -> Any:
        return self._request("GET", "topics")

    def create_topics(self, names: List[str]) -> Dict:
        return self._request("POST", "topics", json={"names": names})

    def delete_topic(self, topic_id: str) -> Dict:
        return self._request("DELETE", f"topics/{topic_id}")

    def rename_topic(self, topic_id: str, name: str) -> Dict:
        return self._request("PATCH", f"topics/{topic_id}", json={"name": name})

    # --- Brands ------------------------------------------------------------

    def list_brands(self) -> Any:
        return self._request("GET", "brands")

    def create_brand(self, name: str, url: str, relation: str) -> Dict:
        """`relation`: SELF|DIRECT_COMPETITOR|OTHER|IGNORED."""
        return self._request("POST", "brands",
                              json={"name": name, "url": url, "relation": relation})

    def update_brand(self, brand_id: str, **fields) -> Dict:
        """E.g. `update_brand(id, relation="IGNORED")`."""
        return self._request("PUT", f"brands/{brand_id}", json=fields)

    # --- Personas ----------------------------------------------------------

    def list_personas(self) -> Any:
        return self._request("GET", "personas")

    def get_persona(self, persona_id: str) -> Dict:
        return self._request("GET", f"personas/{persona_id}")

    def create_persona(self, name: str, description: str, *,
                        age_range: str = None, education_level: str = None,
                        stackable_prompt: str = None) -> Dict:
        """`stackable_prompt` (≥30 chars): used when a monitor has
        `personaStackingEnabled=True`."""
        body = _clean(name=name, description=description, ageRange=age_range,
                       educationLevel=education_level, stackablePrompt=stackable_prompt)
        return self._request("POST", "personas", json=body)

    def update_persona(self, persona_id: str, **fields) -> Dict:
        return self._request("PUT", f"personas/{persona_id}", json=fields)

    def delete_persona(self, persona_id: str) -> Dict:
        return self._request("DELETE", f"personas/{persona_id}")

    # --- Publishing ----------------------------------------------------------

    def list_cms_connections(self) -> Any:
        """Connected CMS destinations (Webflow, Framer)."""
        return self._request("GET", "cms/connections")

    def get_content_publish_status(self, content_id: str) -> Dict:
        """Latest CMS publish record for a content document."""
        return self._request("GET", f"content/{content_id}/publish-status")

    def set_content_publication(self, content_id: str, url: str,
                                 published_at: str = None) -> Dict:
        """Record a live URL for a content document — starts tracking it
        (page-tracker + citation stats), independent of PromptWatch's own
        content generation."""
        body = _clean(url=url, publishedAt=published_at)
        return self._request("POST", f"content/{content_id}/publication", json=body)

    def clear_content_publication(self, content_id: str) -> Dict:
        """Unlink a content document from its published URL."""
        return self._request("DELETE", f"content/{content_id}/publication")

    def push_content_draft_to_cms(self, content_id: str, **extra_params) -> Dict:
        return self._request("POST", f"content/{content_id}/cms/draft",
                              json=extra_params or None)

    def publish_content_live(self, content_id: str, **extra_params) -> Dict:
        return self._request("POST", f"content/{content_id}/cms/publish",
                              json=extra_params or None)

    # --- Content Agent -------------------------------------------------------

    def get_content_agent_settings(self) -> Dict:
        return self._request("GET", "content-agent/settings")

    def update_content_agent_settings(self, **fields) -> Dict:
        """`fields` in PromptWatch's own camelCase — e.g. `enabled=`,
        `autonomyMode=` (GATED|AUTOMATE), `publishState=` (LIVE|DRAFT),
        `publishTiming=` (AT_SLOT|IMMEDIATE_ON_APPROVAL), `publishWindows=`,
        `blackoutDates=`, `scheduleTimezone=`, `maxPerDay=` (1-20),
        `budgetPercentage=` (0-1), `defaultEnabledTools=`, `cmsConnectionId=`."""
        return self._request("PUT", "content-agent/settings", json=fields)

    def list_content_agent_slots(self, page: int = 1, size: int = 25, *,
                                  order_by: str = None, sort_order: str = None,
                                  statuses: List[str] = None,
                                  from_date: str = None, to_date: str = None) -> Any:
        """`statuses` ⊆ PLANNED|GENERATING|REVIEW|APPROVED|SCHEDULED|
        PUBLISHED|DEFERRED|SUPPRESSED|FAILED. `from_date`/`to_date` = calendar
        mode, returns up to 500 rows UNPAGINATED (per PromptWatch docs)."""
        params = _clean(page=page, size=size, orderBy=order_by,
                         sortOrder=sort_order, statuses=statuses,
                         fromDate=from_date, toDate=to_date)
        return self._request("GET", "content-agent/slots", params=params)

    def get_content_agent_slot(self, slot_id: str) -> Dict:
        return self._request("GET", f"content-agent/slots/{slot_id}")

    def update_content_agent_slot(self, slot_id: str, **fields) -> Dict:
        return self._request("PATCH", f"content-agent/slots/{slot_id}", json=fields)

    def accept_content_agent_slot(self, slot_id: str, **extra_params) -> Dict:
        return self._request("POST", f"content-agent/slots/{slot_id}/accept",
                              json=extra_params or None)

    def decline_content_agent_slot(self, slot_id: str, **extra_params) -> Dict:
        return self._request("POST", f"content-agent/slots/{slot_id}/decline",
                              json=extra_params or None)

    def publish_content_agent_slot_now(self, slot_id: str, **extra_params) -> Dict:
        return self._request("POST", f"content-agent/slots/{slot_id}/publish",
                              json=extra_params or None)

    # --- Ads radar -----------------------------------------------------------

    def list_ads(self, page: int = 1, size: int = 25, *, search: str = None,
                 prompt_ids: List[str] = None, sort_by: str = None,
                 sort_order: str = None, from_: str = None, until: str = None,
                 models: List[str] = None, prompt_types: List[str] = None,
                 intent_types: List[str] = None, domains: List[str] = None) -> Dict:
        params = _clean(page=page, size=size, search=search, promptIds=prompt_ids,
                         sortBy=sort_by, sortOrder=sort_order, models=models,
                         promptTypes=prompt_types, intentTypes=intent_types,
                         domains=domains)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "ads", params=params)

    def list_prompts_with_ads(self, page: int = 1, size: int = 25, *,
                               search: str = None, from_: str = None,
                               until: str = None, models: List[str] = None,
                               prompt_types: List[str] = None,
                               intent_types: List[str] = None,
                               domains: List[str] = None) -> Dict:
        params = _clean(page=page, size=size, search=search, models=models,
                         promptTypes=prompt_types, intentTypes=intent_types,
                         domains=domains)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "ads/prompts", params=params)

    def list_ad_domains(self, from_: str = None, until: str = None, *,
                         models: List[str] = None, prompt_types: List[str] = None,
                         intent_types: List[str] = None) -> List[Dict]:
        """Advertiser root domains seen in captured ads, ordered by count.
        `from`/`until` span capped at 90 days by PromptWatch."""
        params = _clean(models=models, promptTypes=prompt_types,
                         intentTypes=intent_types)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "ads/domains", params=params)

    def ad_domain_analytics(self, from_: str = None, until: str = None, *,
                             models: List[str] = None,
                             prompt_types: List[str] = None,
                             intent_types: List[str] = None,
                             domains: List[str] = None) -> Dict:
        """Ad occurrences by advertiser domain — aggregated (`topDomains`)
        + daily breakdown (`daily`). Path live-verified 2026-08-20
        (`ads/domain-analytics`, singular — the earlier `ads/domains/
        analytics` guess 404'd)."""
        params = _clean(models=models, promptTypes=prompt_types,
                         intentTypes=intent_types, domains=domains)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "ads/domain-analytics", params=params)

    # --- Shopping --------------------------------------------------------------

    def list_shopping_items(self, page: int = 1, size: int = 25, *,
                             search: str = None, sort_by: str = None,
                             from_: str = None, until: str = None,
                             models: List[str] = None,
                             prompt_types: List[str] = None,
                             intent_types: List[str] = None) -> Dict:
        """Shopping product APPEARANCES (one row per surfaced product in an
        LLM response) — see `list_tracked_products` for the separate
        "products I'm tracking" list."""
        params = _clean(page=page, size=size, search=search, sortBy=sort_by,
                         models=models, promptTypes=prompt_types,
                         intentTypes=intent_types)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "shopping/items", params=params)

    def get_shopping_item(self, item_id: str) -> Dict:
        return self._request("GET", f"shopping/items/{item_id}")

    def shopping_products_over_time(self, from_: str = None, until: str = None,
                                     **extra_params) -> Any:
        """Path live-verified 2026-08-20 (`shopping/products-over-time`)."""
        params = _clean(**extra_params)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "shopping/products-over-time", params=params)

    def shopping_product_position_analytics(self, from_: str = None,
                                             until: str = None, *,
                                             models: List[str] = None,
                                             prompt_types: List[str] = None,
                                             intent_types: List[str] = None,
                                             limit: int = None) -> Dict:
        """Top products by average ranking position (`topProducts`) + daily
        breakdown (`timeSeries`) — NOT filterable by a specific product id
        (top-N ranking, not a per-item lookup; use `get_shopping_item` for
        one item's own stats). Path live-verified 2026-08-20
        (`shopping/product-position-analytics`, singular — the earlier
        `shopping/position-analytics` guess 404'd). `limit`: default 8,
        max 20."""
        params = _clean(models=models, promptTypes=prompt_types,
                         intentTypes=intent_types, limit=limit)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "shopping/product-position-analytics", params=params)

    def shopping_top_merchant_domains(self, limit: int = None,
                                       **extra_params) -> Any:
        """Path live-verified 2026-08-20 (`shopping/top-merchant-domains`)."""
        params = _clean(limit=limit, **extra_params)
        return self._request("GET", "shopping/top-merchant-domains", params=params)

    def shopping_top_products(self, from_: str = None, until: str = None, *,
                               models: List[str] = None,
                               prompt_types: List[str] = None,
                               intent_types: List[str] = None,
                               limit: int = None) -> List[Dict]:
        """`limit`: default 8, max 20."""
        params = _clean(models=models, promptTypes=prompt_types,
                         intentTypes=intent_types, limit=limit)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "shopping/top-products", params=params)

    def list_tracked_products(self, search: str = None,
                               external_product_id: str = None,
                               match_status: str = None) -> Dict:
        params = _clean(search=search, externalProductId=external_product_id,
                         matchStatus=match_status)
        return self._request("GET", "shopping/tracked-products", params=params)

    def add_tracked_products(self, products: List[Dict]) -> Dict:
        """`products`: 1-5000 items, each `{"externalProductId","name",
        "description"?}`. Duplicates are skipped, not rejected."""
        _check_bulk_size(products, "products", 5000)
        return self._request("POST", "shopping/tracked-products",
                              json={"products": products})

    def update_tracked_product(self, product_id: str, name: str = None,
                                description: str = None) -> Dict:
        body = _clean(name=name, description=description)
        return self._request("PATCH", f"shopping/tracked-products/{product_id}",
                              json=body)

    def delete_tracked_product(self, product_id: str) -> Dict:
        """⚠️ Method/path inferred by strong REST convention (every other
        delete in this API is `DELETE .../{id}`), not individually
        doc-fetched."""
        return self._request("DELETE", f"shopping/tracked-products/{product_id}")

    # --- Site health -----------------------------------------------------------

    def site_health_pages(self, page: int = 1, size: int = 20,
                           issue_types: List[str] = None) -> Dict:
        """`issue_types` ⊆ missingTitle|missingDescription|noH1|multipleH1|
        thinContent."""
        return self._request("GET", "site-health",
                              params=_clean(page=page, size=size, issueTypes=issue_types))

    # --- Sitemap ---------------------------------------------------------------

    def sitemap_crawl_progress(self) -> Dict:
        return self._request("GET", "sitemap/progress")

    def list_sitemap_urls(self, filter: str = None, page: int = 1, size: int = 50,
                           sort_by: str = None, sort_order: str = None,
                           http_statuses: List[int] = None) -> Dict:
        """`filter` ∈ errored|redirected|inProgress."""
        params = _clean(filter=filter, page=page, size=size, sortBy=sort_by,
                         sortOrder=sort_order, httpStatuses=http_statuses)
        return self._request("GET", "sitemap/urls", params=params)

    # --- Page tracker ------------------------------------------------------

    def list_tracked_pages(self, page: int = 1, size: int = 25,
                            **extra_params) -> Any:
        return self._request("GET", "page-tracker",
                              params=_clean(page=page, size=size, **extra_params))

    def add_tracked_pages(self, urls: List[str]) -> Dict:
        """1-100 URLs. Returns 207 Multi-Status shape: `{"added":[...],
        "skipped":[...] (ALREADY_EXISTS), "failed":[...] (INVALID_URL)}`."""
        _check_bulk_size(urls, "urls", 100)
        return self._request("POST", "page-tracker", json={"urls": urls})

    def get_tracked_page(self, page_id: str, start_date: str = None,
                          end_date: str = None) -> Dict:
        """`start_date`/`end_date` (YYYY-MM-DD) default to today when omitted."""
        return self._request("GET", f"page-tracker/{page_id}",
                              params=_clean(startDate=start_date, endDate=end_date))

    def delete_tracked_page(self, page_id: str) -> Dict:
        return self._request("DELETE", f"page-tracker/{page_id}")

    def list_tracked_page_prompts(self, page_id: str, page: int = 1,
                                   size: int = 25, **extra_params) -> Any:
        """Paginated prompts that cite this tracked page."""
        return self._request("GET", f"page-tracker/{page_id}/prompts",
                              params=_clean(page=page, size=size, **extra_params))

    def list_tracked_page_responses(self, page_id: str, page: int = 1,
                                     size: int = 25, **extra_params) -> Any:
        """Paginated responses that cite this tracked page."""
        return self._request("GET", f"page-tracker/{page_id}/responses",
                              params=_clean(page=page, size=size, **extra_params))

    # --- Models --------------------------------------------------------------

    def list_models(self) -> Any:
        """Available LLM model identifiers (for `models=` filters and
        `create_monitor(models=...)`)."""
        return self._request("GET", "models")

    # --- Action items ------------------------------------------------------

    def list_action_items(self, page: int = 1, size: int = 25,
                           **extra_params) -> Any:
        """Defaults to non-dismissed items."""
        return self._request("GET", "actions",
                              params=_clean(page=page, size=size, **extra_params))

    def update_action_item(self, action_id: str, status: str,
                            **extra_params) -> Dict:
        return self._request("PATCH", f"actions/{action_id}",
                              json=_clean(status=status, **extra_params))

    # --- Query fanouts -----------------------------------------------------

    def list_query_fanouts(self, page: int = 1, size: int = 25,
                            **extra_params) -> Any:
        """Prompts that have query fanouts (LLM-generated sub-queries)."""
        return self._request("GET", "query-fanouts",
                              params=_clean(page=page, size=size, **extra_params))

    # --- Social citations ----------------------------------------------------

    def list_reddit_citations(self, page: int = 1, size: int = 25, *,
                               llm_monitor_id: str = None, sort_by: str = None,
                               sort_order: str = None, from_: str = None,
                               until: str = None, models: List[str] = None,
                               prompt_types: List[str] = None, tag_ids=None,
                               query: str = None, subreddit_name: str = None) -> Dict:
        params = _clean(page=page, size=size, llmMonitorId=llm_monitor_id,
                         sortBy=sort_by, sortOrder=sort_order, models=models,
                         promptTypes=prompt_types, tagIds=tag_ids, query=query,
                         subredditName=subreddit_name)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "socials/reddit", params=params)

    def list_youtube_citations(self, page: int = 1, size: int = 25, *,
                                llm_monitor_id: str = None, sort_by: str = None,
                                sort_order: str = None, from_: str = None,
                                until: str = None, models: List[str] = None,
                                prompt_types: List[str] = None, tag_ids=None,
                                query: str = None, channel_name: str = None) -> Dict:
        params = _clean(page=page, size=size, llmMonitorId=llm_monitor_id,
                         sortBy=sort_by, sortOrder=sort_order, models=models,
                         promptTypes=prompt_types, tagIds=tag_ids, query=query,
                         channelName=channel_name)
        if from_ is not None:
            params["from"] = from_
        if until is not None:
            params["until"] = until
        return self._request("GET", "socials/youtube", params=params)
