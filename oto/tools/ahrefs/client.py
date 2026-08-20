"""Ahrefs API client — SEO data (v3, https://docs.ahrefs.com/en/api).

Bearer token (`Authorization: Bearer <key>`), confirmed on the site-audit
endpoint pages ("Authorization: string (header) required — Bearer token API
key"). One method per REST endpoint (1 call = 1 endpoint) — the shared
`select`/`where`/`order_by` mini query language (Ahrefs' own "filter syntax")
is NOT re-typed per method: required identifiers are explicit kwargs, every
other documented query/body param passes through **params, same convention as
`TheirStackClient`'s untyped filter DSL.

`output` is force-checked to `json` (or absent) — `_request` raises before
calling `resp.json()` on a `csv`/`xml`/`php` body, which would otherwise be a
confusing parse error far from the real mistake.

Almost every report defaults to `limit=1000` rows server-side when omitted;
`site-audit/issues` and `site-audit/page-content` cost 50 API units per
request regardless of `limit`. Pass `limit` explicitly to bound spend —
`subscription_info_limits_and_usage()` is free and shows current consumption.

Write endpoints (project/competitor/keyword-list mutations, brand-radar
report/prompt mutations) ARE implemented here for full API coverage, but no
oto-backend tool reaches the destructive ones (delete/patch) — see
`oto_mcp/tools/ahrefs.py`, which mirrors the Silae doctrine: exposing a write
is a deliberate tool-layer choice, never a side effect of client coverage.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connect, read) — never an unbounded wait
_BASE_URL = "https://api.ahrefs.com/v3"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drops `None` values — an omitted kwarg must not become the literal
    string 'None' in the querystring."""
    return {k: v for k, v in params.items() if v is not None}


class AhrefsClient:
    """Ahrefs API v3 client (https://api.ahrefs.com/v3), Bearer auth."""

    BASE_URL = _BASE_URL

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: Ahrefs API key (or env var `AHREFS_API_KEY`). Created in
                the Ahrefs webapp under API Access — one key per Ahrefs seat,
                billed against that account's subscription (byo-only, no
                platform-shared key: Ahrefs seats are expensive and per-org).
        """
        self.api_key = api_key or require_secret("AHREFS_API_KEY")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 json_body: Any = None) -> Any:
        query = _clean(params or {})
        output = query.get("output")
        if output is not None and output != "json":
            raise ValueError(
                f"AhrefsClient only parses output='json' (got {output!r}) — "
                "csv/xml/php responses aren't handled here; omit `output` or pass 'json'.")
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}", params=query, json=json_body,
            timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="ahrefs")
        return resp.json()

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Dict[str, Any], *, query: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("POST", path, params=query, json_body=_clean(body))

    def _put(self, path: str, body: Any, *, query: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("PUT", path, params=query, json_body=body)

    def _patch(self, path: str, body: Dict[str, Any], *, query: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("PATCH", path, params=query, json_body=_clean(body))

    def _delete(self, path: str, **params: Any) -> Any:
        return self._request("DELETE", path, params=params)

    # ================================================================
    # Site Explorer — /site-explorer/*
    # ================================================================

    # --- Overview ---------------------------------------------------

    def domain_rating(self, target: str, date: str, **params: Any) -> Any:
        """GET /site-explorer/domain-rating — Ahrefs Rank + Domain Rating (0-100 log scale)."""
        return self._get("/site-explorer/domain-rating", target=target, date=date, **params)

    def backlinks_stats(self, target: str, date: str, **params: Any) -> Any:
        """GET /site-explorer/backlinks-stats — total backlinks + referring domains."""
        return self._get("/site-explorer/backlinks-stats", target=target, date=date, **params)

    def outlinks_stats(self, target: str, **params: Any) -> Any:
        """GET /site-explorer/outlinks-stats — outgoing links + linked domains (beta)."""
        return self._get("/site-explorer/outlinks-stats", target=target, **params)

    def site_metrics(self, target: str, date: str, **params: Any) -> Any:
        """GET /site-explorer/metrics — organic + paid traffic metrics."""
        return self._get("/site-explorer/metrics", target=target, date=date, **params)

    def ai_responses_count(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/ai-responses-count — citations of target across AI platforms."""
        return self._get("/site-explorer/ai-responses-count", target=target, select=select, **params)

    def refdomains_history(self, target: str, date_from: str, **params: Any) -> Any:
        """GET /site-explorer/refdomains-history — referring-domains count over time."""
        return self._get("/site-explorer/refdomains-history", target=target, date_from=date_from, **params)

    def domain_rating_history(self, target: str, date_from: str, **params: Any) -> Any:
        """GET /site-explorer/domain-rating-history — Domain Rating over time."""
        return self._get("/site-explorer/domain-rating-history", target=target, date_from=date_from, **params)

    def url_rating_history(self, target: str, date_from: str, **params: Any) -> Any:
        """GET /site-explorer/url-rating-history — URL Rating over time."""
        return self._get("/site-explorer/url-rating-history", target=target, date_from=date_from, **params)

    def pages_history(self, target: str, date_from: str, **params: Any) -> Any:
        """GET /site-explorer/pages-history — count of pages ranking in top positions over time."""
        return self._get("/site-explorer/pages-history", target=target, date_from=date_from, **params)

    def metrics_history(self, target: str, date_from: str, **params: Any) -> Any:
        """GET /site-explorer/metrics-history — organic/paid traffic + cost over time.
        Default `select` if omitted upstream: date,org_cost,org_traffic,paid_cost,paid_traffic."""
        return self._get("/site-explorer/metrics-history", target=target, date_from=date_from, **params)

    def keywords_history(self, target: str, date_from: str, **params: Any) -> Any:
        """GET /site-explorer/keywords-history — ranking-position-tier counts over time.
        Default `select` if omitted upstream: date,top3,top4_10,top11_plus."""
        return self._get("/site-explorer/keywords-history", target=target, date_from=date_from, **params)

    def metrics_by_country(self, target: str, date: str, **params: Any) -> Any:
        """GET /site-explorer/metrics-by-country — organic/paid metrics broken down by country."""
        return self._get("/site-explorer/metrics-by-country", target=target, date=date, **params)

    def pages_by_traffic(self, target: str, **params: Any) -> Any:
        """GET /site-explorer/pages-by-traffic — organic traffic distribution across pages."""
        return self._get("/site-explorer/pages-by-traffic", target=target, **params)

    def total_search_volume_history(self, target: str, date_from: str, **params: Any) -> Any:
        """GET /site-explorer/total-search-volume-history — search volume of ranked keywords over time."""
        return self._get("/site-explorer/total-search-volume-history", target=target, date_from=date_from, **params)

    # --- Backlinks profile -------------------------------------------

    def all_backlinks(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/all-backlinks — every backlink to target."""
        return self._get("/site-explorer/all-backlinks", target=target, select=select, **params)

    def broken_backlinks(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/broken-backlinks — backlinks currently returning an error status."""
        return self._get("/site-explorer/broken-backlinks", target=target, select=select, **params)

    def refdomains(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/refdomains — referring domains + their authority metrics."""
        return self._get("/site-explorer/refdomains", target=target, select=select, **params)

    def anchors(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/anchors — anchor text of backlinks pointing to target."""
        return self._get("/site-explorer/anchors", target=target, select=select, **params)

    # --- Organic search ------------------------------------------------

    def organic_keywords(self, target: str, select: str, date: str, **params: Any) -> Any:
        """GET /site-explorer/organic-keywords — keywords target ranks for organically."""
        return self._get("/site-explorer/organic-keywords", target=target, select=select, date=date, **params)

    def organic_competitors(self, target: str, country: str, date: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/organic-competitors — domains competing in organic search."""
        return self._get("/site-explorer/organic-competitors", target=target, country=country,
                          date=date, select=select, **params)

    def top_pages(self, target: str, select: str, date: str, **params: Any) -> Any:
        """GET /site-explorer/top-pages — best-performing pages in organic search."""
        return self._get("/site-explorer/top-pages", target=target, select=select, date=date, **params)

    # --- Paid search -----------------------------------------------------

    def paid_pages(self, target: str, select: str, date: str, **params: Any) -> Any:
        """GET /site-explorer/paid-pages — pages ranking in paid search."""
        return self._get("/site-explorer/paid-pages", target=target, select=select, date=date, **params)

    # --- Pages -------------------------------------------------------------

    def pages_by_backlinks(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/pages-by-backlinks — pages ranked by backlink profile strength."""
        return self._get("/site-explorer/pages-by-backlinks", target=target, select=select, **params)

    def pages_by_internal_links(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/pages-by-internal-links — pages ranked by internal link count."""
        return self._get("/site-explorer/pages-by-internal-links", target=target, select=select, **params)

    def crawled_pages(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/crawled-pages — pages crawled by Ahrefs' bot (status, title, UR)."""
        return self._get("/site-explorer/crawled-pages", target=target, select=select, **params)

    # --- Outgoing links ------------------------------------------------------

    def linked_domains(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/linkeddomains — domains that target links out to."""
        return self._get("/site-explorer/linkeddomains", target=target, select=select, **params)

    def linked_anchors_external(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/linked-anchors-external — anchor text of target's outbound links."""
        return self._get("/site-explorer/linked-anchors-external", target=target, select=select, **params)

    def linked_anchors_internal(self, target: str, select: str, **params: Any) -> Any:
        """GET /site-explorer/linked-anchors-internal — anchor text of target's internal links."""
        return self._get("/site-explorer/linked-anchors-internal", target=target, select=select, **params)

    # ================================================================
    # Keywords Explorer — /keywords-explorer/*
    # ================================================================

    def keywords_overview(self, select: str, country: str, **params: Any) -> Any:
        """GET /keywords-explorer/overview — volume/difficulty/clicks/SERP-features for keywords.
        Needs one of `keywords`, `keyword_list_id` or `target` (**params) to scope the query."""
        return self._get("/keywords-explorer/overview", select=select, country=country, **params)

    def volume_history(self, keyword: str, country: str, **params: Any) -> Any:
        """GET /keywords-explorer/volume-history — monthly search volume history for one keyword."""
        return self._get("/keywords-explorer/volume-history", keyword=keyword, country=country, **params)

    def volume_by_country(self, keyword: str, **params: Any) -> Any:
        """GET /keywords-explorer/volume-by-country — average monthly volume broken down by country."""
        return self._get("/keywords-explorer/volume-by-country", keyword=keyword, **params)

    def matching_terms(self, select: str, country: str, **params: Any) -> Any:
        """GET /keywords-explorer/matching-terms — keyword ideas matching seed keyword(s)/list.
        Needs `keywords` or `keyword_list_id` (**params)."""
        return self._get("/keywords-explorer/matching-terms", select=select, country=country, **params)

    def related_terms(self, select: str, country: str, **params: Any) -> Any:
        """GET /keywords-explorer/related-terms — keywords top-ranking pages also rank/talk about.
        Needs `keywords` or `keyword_list_id` (**params)."""
        return self._get("/keywords-explorer/related-terms", select=select, country=country, **params)

    def search_suggestions(self, select: str, country: str, **params: Any) -> Any:
        """GET /keywords-explorer/search-suggestions — autocomplete-style keyword ideas.
        Needs `keywords` or `keyword_list_id` (**params)."""
        return self._get("/keywords-explorer/search-suggestions", select=select, country=country, **params)

    # ================================================================
    # Site Audit — /site-audit/*
    # ================================================================

    def audit_projects(self, **params: Any) -> Any:
        """GET /site-audit/projects — health scores of Site Audit projects (optionally `project_id`/`project_name`/`project_url`)."""
        return self._get("/site-audit/projects", **params)

    def audit_issues(self, project_id: int, **params: Any) -> Any:
        """GET /site-audit/issues — issues found in a Site Audit project's latest crawl. 50 units/request."""
        return self._get("/site-audit/issues", project_id=project_id, **params)

    def audit_page_content(self, project_id: int, target_url: str, select: str, **params: Any) -> Any:
        """GET /site-audit/page-content — text/HTML/metadata for one crawled page. 50 units/request.
        `select` values: crawl_datetime, page_text, page_text_md, raw_html, rendered_html."""
        return self._get("/site-audit/page-content", project_id=project_id, target_url=target_url,
                          select=select, **params)

    def audit_page_explorer(self, project_id: int, **params: Any) -> Any:
        """GET /site-audit/page-explorer — per-page crawl metrics (indexability, links, structured data…)."""
        return self._get("/site-audit/page-explorer", project_id=project_id, **params)

    # ================================================================
    # Rank Tracker — /rank-tracker/*
    # ================================================================

    def rank_overview(self, project_id: int, date: str, device: str, select: str, **params: Any) -> Any:
        """GET /rank-tracker/overview — tracked-keyword positions/traffic/SERP-features."""
        return self._get("/rank-tracker/overview", project_id=project_id, date=date, device=device,
                          select=select, **params)

    def rank_serp_overview(self, project_id: int, keyword: str, country: str, device: str, **params: Any) -> Any:
        """GET /rank-tracker/serp-overview — organic+paid SERP for one tracked keyword."""
        return self._get("/rank-tracker/serp-overview", project_id=project_id, keyword=keyword,
                          country=country, device=device, **params)

    def rank_competitors_overview(self, project_id: int, date: str, device: str, select: str, **params: Any) -> Any:
        """GET /rank-tracker/competitors-overview — competitor rankings on tracked keywords."""
        return self._get("/rank-tracker/competitors-overview", project_id=project_id, date=date,
                          device=device, select=select, **params)

    def rank_competitors_pages(self, project_id: int, date: str, device: str, select: str, **params: Any) -> Any:
        """GET /rank-tracker/competitors-pages — competitor pages ranking on tracked keywords."""
        return self._get("/rank-tracker/competitors-pages", project_id=project_id, date=date,
                          device=device, select=select, **params)

    def rank_competitors_domains(self, project_id: int, date: str, device: str, select: str, **params: Any) -> Any:
        """GET /rank-tracker/competitors-domains — competitor domain rankings + traffic."""
        return self._get("/rank-tracker/competitors-domains", project_id=project_id, date=date,
                          device=device, select=select, **params)

    def rank_competitors_stats(self, project_id: int, date: str, device: str, select: str, **params: Any) -> Any:
        """GET /rank-tracker/competitors-stats — competitive position-distribution + SERP-feature counts."""
        return self._get("/rank-tracker/competitors-stats", project_id=project_id, date=date,
                          device=device, select=select, **params)

    # ================================================================
    # SERP Overview — /serp-overview/* (standalone: ANY keyword, no tracking project)
    # ================================================================

    def serp_overview(self, keyword: str, country: str, select: str, **params: Any) -> Any:
        """GET /serp-overview/serp-overview — SERP for any keyword (no Rank Tracker project needed)."""
        return self._get("/serp-overview/serp-overview", keyword=keyword, country=country,
                          select=select, **params)

    # ================================================================
    # Batch Analysis — /batch-analysis/*
    # ================================================================

    def batch_analysis(self, targets: List[Dict[str, Any]], select: List[str], **body: Any) -> Any:
        """POST /batch-analysis/batch-analysis — SEO metrics for up to many targets in one call.

        Args:
            targets: list of {"url": ..., "mode": "exact"|"prefix"|"domain"|"subdomains",
                "protocol": "both"|"http"|"https"}.
            select: field names to return per target.
            **body: optional `order_by`, `country`, `volume_mode`.
        """
        return self._post("/batch-analysis/batch-analysis", {"targets": targets, "select": select, **body})

    # ================================================================
    # Subscription Info — /subscription-info/*
    # ================================================================

    def subscription_info_limits_and_usage(self, **params: Any) -> Any:
        """GET /subscription-info/limits-and-usage — API-unit consumption + subscription limits. Free."""
        return self._get("/subscription-info/limits-and-usage", **params)

    # ================================================================
    # Management — /management/* (Rank Tracker projects, keyword lists, Brand
    # Radar reports/prompts). Read + create implemented for tool-layer use;
    # delete/patch implemented too (full API coverage) but deliberately NOT
    # wired to any oto-backend tool — see module docstring.
    # ================================================================

    # --- Rank Tracker projects --------------------------------------------

    def list_projects(self, **params: Any) -> Any:
        """GET /management/projects — Rank Tracker projects reachable with this key. Free."""
        return self._get("/management/projects", **params)

    def create_project(self, project_name: str, url: str, mode: str, protocol: str, **body: Any) -> Any:
        """POST /management/projects — create a Rank Tracker project.
        **body: optional `access` ('private'|'shared'), `owned_by` (email), `folder_id`."""
        return self._post("/management/projects",
                           {"project_name": project_name, "url": url, "mode": mode,
                            "protocol": protocol, **body})

    def delete_projects(self, project_ids: str, **params: Any) -> Any:
        """DELETE /management/projects — delete one or more projects (comma-separated `project_ids`). Free."""
        return self._delete("/management/projects", project_ids=project_ids, **params)

    def update_project(self, project_id: int, **body: Any) -> Any:
        """PATCH /management/update-project — change `access` and/or `folder` of a project.
        At least one of `access`/`folder` (**body) must be set."""
        return self._patch("/management/update-project", {"project_id": project_id, **body})

    # --- Rank Tracker project keywords --------------------------------------

    def project_keywords(self, project_id: int, **params: Any) -> Any:
        """GET /management/project-keywords — keywords tracked in a project (languages, locations, tags)."""
        return self._get("/management/project-keywords", project_id=project_id, **params)

    def add_project_keywords(self, project_id: int, keywords: List[Dict[str, Any]],
                              locations: List[Dict[str, Any]]) -> Any:
        """PUT /management/project-keywords — add keywords to a project.

        Verified against Ahrefs' OpenAPI spec (docs.ahrefs.com/openapi.json,
        2026-08-20): TWO parallel arrays, not one merged list —
        `keywords`: [{"keyword", "tags"?: [str]}], `locations`: [{"country",
        "location_id"?, "language"?}], matched by POSITION (keywords[i] takes
        locations[i]). `country` is required within each `locations[]` entry."""
        return self._put("/management/project-keywords", {"keywords": keywords, "locations": locations},
                          query={"project_id": project_id})

    def delete_project_keywords(self, project_id: int, keywords: List[Dict[str, Any]]) -> Any:
        """PUT /management/project-keywords-delete — remove keywords from a project. Free."""
        return self._put("/management/project-keywords-delete", {"keywords": keywords},
                          query={"project_id": project_id})

    def tag_project_keywords(self, project_id: int, keywords: List[Dict[str, Any]], tags: List[str],
                              **body: Any) -> Any:
        """PUT /management/project-keywords-tags — add/replace tags on project keywords.
        `project_id` is a BODY field here (verified against spec — this endpoint,
        unlike its siblings, takes no query params at all), not a query param.
        **body: `update_mode` ('add' default | 'replace')."""
        return self._put("/management/project-keywords-tags",
                          {"project_id": project_id, "keywords": keywords, "tags": tags, **body})

    def untag_project_keywords(self, project_id: int, keywords: List[Dict[str, Any]], tags: List[str]) -> Any:
        """PUT /management/project-keywords-tags-delete — remove tags from project keywords. Free.
        `project_id` is a BODY field here (verified against spec), not a query param."""
        return self._put("/management/project-keywords-tags-delete",
                          {"project_id": project_id, "keywords": keywords, "tags": tags})

    # --- Rank Tracker project competitors -----------------------------------

    def project_competitors(self, project_id: int, **params: Any) -> Any:
        """GET /management/project-competitors — competitors configured on a project. Free."""
        return self._get("/management/project-competitors", project_id=project_id, **params)

    def add_project_competitors(self, project_id: int, competitors: List[Dict[str, Any]]) -> Any:
        """POST /management/project-competitors — add competitors ({"url", "mode"})."""
        return self._post("/management/project-competitors", {"competitors": competitors},
                           query={"project_id": project_id})

    def delete_project_competitors(self, project_id: int, competitors: List[Dict[str, Any]]) -> Any:
        """POST /management/project-competitors-delete — remove competitors from a project. Free."""
        return self._post("/management/project-competitors-delete", {"competitors": competitors},
                           query={"project_id": project_id})

    # --- Lookups -------------------------------------------------------------

    def locations(self, country_code: str, **params: Any) -> Any:
        """GET /management/locations — location IDs + language codes for a country
        (feeds `location_id`/`language_code` on `rank_serp_overview`). `us_state`
        (**params) required when `country_code='us'`."""
        return self._get("/management/locations", country_code=country_code, **params)

    # --- Keyword lists ---------------------------------------------------------

    def keyword_list_keywords(self, keyword_list_id: int, **params: Any) -> Any:
        """GET /management/keyword-list-keywords — keywords in a keyword list. Free."""
        return self._get("/management/keyword-list-keywords", keyword_list_id=keyword_list_id, **params)

    def add_keyword_list_keywords(self, keyword_list_id: int, keywords: List[str]) -> Any:
        """PUT /management/keyword-list-keywords — add keywords to a keyword list."""
        return self._put("/management/keyword-list-keywords", {"keywords": keywords},
                          query={"keyword_list_id": keyword_list_id})

    def delete_keyword_list_keywords(self, keyword_list_id: int, keywords: List[str]) -> Any:
        """PUT /management/keyword-list-keywords-delete — remove keywords from a keyword list."""
        return self._put("/management/keyword-list-keywords-delete", {"keywords": keywords},
                          query={"keyword_list_id": keyword_list_id})

    # --- Brand Radar reports & prompts ------------------------------------------

    def brand_radar_prompts(self, report_id: str, **params: Any) -> Any:
        """GET /management/brand-radar-prompts — custom prompts configured on a Brand Radar report. Free."""
        return self._get("/management/brand-radar-prompts", report_id=report_id, **params)

    def create_brand_radar_prompts(self, report_id: str, countries: List[str], prompts: List[str]) -> Any:
        """POST /management/brand-radar-prompts — add custom prompts (max 400 chars each). Free."""
        return self._post("/management/brand-radar-prompts", {"countries": countries, "prompts": prompts},
                           query={"report_id": report_id})

    def delete_brand_radar_prompts(self, report_id: str, prompts: List[str], **body: Any) -> Any:
        """PUT /management/brand-radar-prompts-delete — remove custom prompts.
        **body: `countries` to scope the deletion (omit = all countries)."""
        return self._put("/management/brand-radar-prompts-delete", {"prompts": prompts, **body},
                          query={"report_id": report_id})

    def brand_radar_reports(self, **params: Any) -> Any:
        """GET /management/brand-radar-reports — Brand Radar reports on this account. Free."""
        return self._get("/management/brand-radar-reports", **params)

    def create_brand_radar_report(self, prompts_frequency: List[Dict[str, str]], **body: Any) -> Any:
        """POST /management/brand-radar-reports — create a Brand Radar report monitoring
        brand/competitor visibility across AI platforms.

        Verified against Ahrefs' OpenAPI spec (docs.ahrefs.com/openapi.json,
        2026-08-20): there is NO top-level `data_source`/`frequency` field on this
        endpoint (unlike the brand-radar DATA reports) — each platform + cadence
        pair lives INSIDE `prompts_frequency`.

        Args:
            prompts_frequency: [{"data_source": "chatgpt", "frequency": "daily"}, ...].
            **body: `project_id`, `name`, `brand`, `competitors` ({"names"?, "url_groups"?}).
        """
        return self._post("/management/brand-radar-reports",
                           {"prompts_frequency": prompts_frequency, **body})

    def update_brand_radar_report(self, report_id: str, prompts_frequency: List[Dict[str, str]]) -> Any:
        """PATCH /management/brand-radar-reports — update a report's data-source/frequency settings. Free."""
        return self._patch("/management/brand-radar-reports",
                            {"report_id": report_id, "prompts_frequency": prompts_frequency})

    # ================================================================
    # Brand Radar (data) — /brand-radar/* — AI-chatbot visibility of a brand.
    # GET variant implemented for each report except the two POST-only ones
    # (citations-overview, citations-history), whose filters are inherently
    # object/array-shaped (brand_filter, tags_filter, brands[with url_groups]).
    # The richer POST body shape (for GET-having reports too) is reachable via
    # `extra`/**params passthrough at the tool layer for power users.
    # ================================================================

    def brand_ai_responses(self, data_source: str, select: str, **params: Any) -> Any:
        """GET /brand-radar/ai-responses — AI-generated responses mentioning tracked brand(s).
        Custom-only prompts (`prompts='custom'`) are free; Ahrefs prompt data = standard pricing.
        Needs one of `brand`/`competitors`/`market`/`where` (**params)."""
        return self._get("/brand-radar/ai-responses", data_source=data_source, select=select, **params)

    def brand_cited_pages(self, data_source: str, select: str, **params: Any) -> Any:
        """GET /brand-radar/cited-pages — pages cited by AI chatbots for tracked brand(s)."""
        return self._get("/brand-radar/cited-pages", data_source=data_source, select=select, **params)

    def brand_cited_domains(self, data_source: str, select: str, **params: Any) -> Any:
        """GET /brand-radar/cited-domains — domains cited by AI chatbots for tracked brand(s)."""
        return self._get("/brand-radar/cited-domains", data_source=data_source, select=select, **params)

    def brand_impressions_overview(self, data_source: str, select: str, **params: Any) -> Any:
        """GET /brand-radar/impressions-overview — estimated AI-chatbot impressions by mention pattern."""
        return self._get("/brand-radar/impressions-overview", data_source=data_source, select=select, **params)

    def brand_citations_overview(self, data_source: List[str], select: List[str], **body: Any) -> Any:
        """POST /brand-radar/citations-overview — estimated citation counts across AI chatbots.
        Every entity in `brands`/`competitors` (**body) needs at least one `url_groups` value."""
        return self._post("/brand-radar/citations-overview", {"data_source": data_source, "select": select, **body})

    def brand_mentions_overview(self, data_source: str, select: str, **params: Any) -> Any:
        """GET /brand-radar/mentions-overview — mention counts split by brand-only/competitor-only/both."""
        return self._get("/brand-radar/mentions-overview", data_source=data_source, select=select, **params)

    def brand_sov_overview(self, data_source: str, **params: Any) -> Any:
        """GET /brand-radar/sov-overview — share of voice across AI chatbot platforms.
        Needs one of `brand`/`competitors`/`market`/`where` (**params)."""
        return self._get("/brand-radar/sov-overview", data_source=data_source, **params)

    def brand_impressions_history(self, brand: str, data_source: str, date_from: str, **params: Any) -> Any:
        """GET /brand-radar/impressions-history — brand impressions over a historical period."""
        return self._get("/brand-radar/impressions-history", brand=brand, data_source=data_source,
                          date_from=date_from, **params)

    def brand_citations_history(self, data_source: List[str], date_from: str, **body: Any) -> Any:
        """POST /brand-radar/citations-history — brand-URL citations over a historical period."""
        return self._post("/brand-radar/citations-history", {"data_source": data_source, "date_from": date_from, **body})

    def brand_mentions_history(self, brand: str, data_source: str, date_from: str, **params: Any) -> Any:
        """GET /brand-radar/mentions-history — brand mention counts over a historical period."""
        return self._get("/brand-radar/mentions-history", brand=brand, data_source=data_source,
                          date_from=date_from, **params)

    def brand_sov_history(self, data_source: str, date_from: str, **params: Any) -> Any:
        """GET /brand-radar/sov-history — share of voice across AI chatbots over a historical period.
        Needs one of `brand`/`competitors`/`market`/`where` (**params)."""
        return self._get("/brand-radar/sov-history", data_source=data_source, date_from=date_from, **params)

    # ================================================================
    # Web Analytics — /web-analytics/* — Ahrefs' own on-site analytics (needs
    # the Ahrefs JS snippet installed on the tracked site; separate from
    # Site Explorer's crawl-based estimates). 34 endpoints, ALL sharing the
    # identical shape (`project_id` + optional `from`/`to`/`where`/`output`,
    # `chart` variants add a required `granularity`) except for one
    # per-dimension chart-series filter param whose NAME differs per report
    # (`sources_to_chart`, `source_channels_to_chart`, `source_referers_to_chart`…
    # — not a typo, Ahrefs' own docs are inconsistent here). One parameterized
    # method rather than 34 near-duplicate ones — the deliberate exception to
    # this client's 1-method-1-endpoint convention; `report` is validated
    # against the real path list so a typo fails fast, not as a silent 404.
    # ================================================================

    WEB_ANALYTICS_REPORTS = frozenset({
        "stats", "chart",
        "source-channels", "source-channels-chart", "sources", "sources-chart",
        "referrers", "referrers-chart", "utm-params", "utm-params-chart",
        "entry-pages", "entry-pages-chart", "exit-pages", "exit-pages-chart",
        "top-pages", "top-pages-chart", "cities", "cities-chart",
        "continents", "continents-chart", "countries", "countries-chart",
        "languages", "languages-chart", "browsers", "browsers-chart",
        "browser-versions", "browser-versions-chart", "devices", "devices-chart",
        "operating-systems", "operating-systems-chart",
        "operating-systems-versions", "operating-systems-versions-chart",
    })

    def web_analytics(self, report: str, project_id: int, **params: Any) -> Any:
        """GET /web-analytics/<report> — on-site analytics for one project. Free.

        Args:
            report: one of `AhrefsClient.WEB_ANALYTICS_REPORTS`. `*-chart` variants
                require `granularity` ('hourly'|'daily'|'weekly'|'monthly') in
                **params; the non-chart ones accept `limit`/`order_by` instead.
            project_id: the Web Analytics project (from Ahrefs, distinct from a
                Rank Tracker `project_id` even though both are called "project").
            **params: `from`/`to` (ISO 8601, bound the date range), `where`
                (filter expression), and that report's chart-series filter under
                whatever name Ahrefs' docs give it for `report`.
        """
        if report not in self.WEB_ANALYTICS_REPORTS:
            raise ValueError(f"Unknown Web Analytics report {report!r} — "
                              f"valid: {sorted(self.WEB_ANALYTICS_REPORTS)}")
        return self._get(f"/web-analytics/{report}", project_id=project_id, **params)

    # ================================================================
    # GSC Insights — /gsc/* — Google Search Console data proxied through
    # Ahrefs (the Ahrefs project must have GSC connected in the app). 11 of
    # 12 endpoints share the same shape (`date_from` + one of `project_id`/
    # `portfolio_id`) — same rationale as Web Analytics for one parameterized
    # method; `anonymous-queries` has a genuinely different required shape
    # (`select`+`project_id`+`country`, no `portfolio_id`) so it stays separate.
    # ================================================================

    GSC_REPORTS = frozenset({
        "performance-history", "positions-history", "pages-history",
        "performance-by-device", "metrics-by-country", "ctr-by-position",
        "performance-by-position", "keyword-history", "keywords",
        "page-history", "pages",
    })

    def gsc_report(self, report: str, date_from: str, **params: Any) -> Any:
        """GET /gsc/<report> — Search Console data for a project/portfolio. Free.

        Args:
            report: one of `AhrefsClient.GSC_REPORTS`.
            date_from: period start, YYYY-MM-DD.
            **params: one of `project_id`/`portfolio_id` REQUIRED (Ahrefs accepts
                either) — omitting both is rejected upstream, not caught here.
                Also `date_to`, `history_grouping`, `search_type`, `country`,
                `device`, `where`, `keywords`(keyword-history)/`keyword_list_id`
                (keywords), `pages` (page-history), `limit` (keywords/pages).
        """
        if report not in self.GSC_REPORTS:
            raise ValueError(f"Unknown GSC report {report!r} — valid: {sorted(self.GSC_REPORTS)}")
        return self._get(f"/gsc/{report}", date_from=date_from, **params)

    def gsc_anonymous_queries(self, select: str, project_id: int, date_from: str, country: str,
                               **params: Any) -> Any:
        """GET /gsc/anonymous-queries — GSC queries flagged as anonymous (privacy-redacted). Free."""
        return self._get("/gsc/anonymous-queries", select=select, project_id=project_id,
                          date_from=date_from, country=country, **params)

    # ================================================================
    # Social Media — /social-media/* — Ahrefs' social publishing/listening
    # product (connected channels post/schedule content and read engagement).
    # ================================================================

    def social_channels(self, **params: Any) -> Any:
        """GET /social-media/channels — connected social channels + auth status. Free."""
        return self._get("/social-media/channels", **params)

    def social_channel_metrics(self, channel_id: str, date_from: str, **params: Any) -> Any:
        """GET /social-media/channel-metrics — follower-count history for a channel. Free."""
        return self._get("/social-media/channel-metrics", channel_id=channel_id, date_from=date_from, **params)

    def social_authors(self, **params: Any) -> Any:
        """GET /social-media/authors — social-media authors (id + name). Free."""
        return self._get("/social-media/authors", **params)

    def social_activity_history(self, post_id: int, **params: Any) -> Any:
        """GET /social-media/activity-history — who changed a post, and when. Free."""
        return self._get("/social-media/activity-history", post_id=post_id, **params)

    def social_posts(self, status: str, **params: Any) -> Any:
        """GET /social-media/posts — posts by status ('published'|'scheduled'|'draft'|'failed'|'deleted')."""
        return self._get("/social-media/posts", status=status, **params)

    def social_post_metrics(self, external_post_id: str, channel_id: str, date_from: str, **params: Any) -> Any:
        """GET /social-media/post-metrics — engagement metrics for one published post. Free."""
        return self._get("/social-media/post-metrics", external_post_id=external_post_id,
                          channel_id=channel_id, date_from=date_from, **params)

    def create_social_post(self, channel_ids: List[str], text_content: str, timing: str, **body: Any) -> Any:
        """POST /social-media/post — publish/schedule/draft a post.
        `timing`: 'publish_now'|'scheduled'|'draft'. **body: `scheduled_at` (ISO 8601,
        required iff timing='scheduled'), `auto_comment`."""
        return self._post("/social-media/post",
                           {"channel_ids": channel_ids, "text_content": text_content,
                            "timing": timing, **body})

    def delete_social_post(self, post_id: int) -> Any:
        """DELETE /social-media/post — remove a post."""
        return self._delete("/social-media/post", post_id=post_id)

    def update_social_post(self, post_id: int, **body: Any) -> Any:
        """PATCH /social-media/post — edit content/timing/channels/auto-comment of a post.
        **body: `channel_ids`, `text_content`, `timing`, `scheduled_at`, `auto_comment`."""
        return self._patch("/social-media/post", {"post_id": post_id, **body})

    # ================================================================
    # Public — /public/* — no-auth crawler IPs + free-tier Domain Rating.
    # ================================================================

    def crawler_ips(self, **params: Any) -> Any:
        """GET /public/crawler-ips — Ahrefs crawler bot's IP addresses. Free, no API key needed."""
        return self._get("/public/crawler-ips", **params)

    def crawler_ip_ranges(self, **params: Any) -> Any:
        """GET /public/crawler-ip-ranges — Ahrefs crawler bot's IPv4 CIDR ranges. Free, no API key needed."""
        return self._get("/public/crawler-ip-ranges", **params)

    def domain_rating_free(self, target: str, **params: Any) -> Any:
        """GET /public/domain-rating-free — free-tier Domain Rating (0-100).
        Usage requires crediting "Domain Rating by Ahrefs" per Ahrefs' license."""
        return self._get("/public/domain-rating-free", target=target, **params)

    def domain_rating_top_domains(self, **params: Any) -> Any:
        """GET /public/domain-rating-top-domains — top-1M domains by Domain Rating.
        **params: `from`/`to` rank position (default 1/100, max 250k rows/request)."""
        return self._get("/public/domain-rating-top-domains", **params)
