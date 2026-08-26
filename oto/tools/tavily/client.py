"""Tavily client — recherche web et lecture de pages taillées pour un agent (tavily.com).

API REST, auth Bearer `tvly-…`. Quatre surfaces, toutes **synchrones** :
- **search** : recherche web → réponse synthétique optionnelle + extraits cités,
  filtrable par sujet (general/news/finance), période, pays, langue.
- **extract** : N URLs → contenu propre (markdown/texte), reclassé par une intention.
- **crawl** : parcours d'un site guidé en langage naturel → contenu des pages.
- **map** : découverte des URLs d'un site (sans contenu).

Coût (crédits Tavily) : search 1 (2 en `advanced`) ; extract 1 par 5 URLs (2 en
`advanced`) ; crawl/map 1 par 10 pages (2 avec `instructions` ou `advanced`).
`include_usage=True` est toujours envoyé : la réponse porte `usage.credits`.

Les corps de requête passent tels quels à l'API (les `None` sont retirés pour
laisser Tavily appliquer SES défauts) — voir https://docs.tavily.com.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class TavilyClient:
    """Client Tavily (https://api.tavily.com), auth Bearer `tvly-…`."""

    BASE_URL = "https://api.tavily.com"

    def __init__(self, api_key: str = None):
        """
        Args:
            api_key: clé Tavily (ou variable d'env `TAVILY_API_KEY`).
        """
        self.api_key = api_key or require_secret("TAVILY_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *, timeout: int = 60, **kwargs) -> Dict[str, Any]:
        resp = self.session.request(method, f"{self.BASE_URL}{path}", timeout=timeout, **kwargs)
        raise_for_upstream(resp, service="tavily")
        return resp.json() if resp.content else {}

    @staticmethod
    def _compact(body: Dict[str, Any]) -> Dict[str, Any]:
        """Retire les clés à None — l'API applique alors SES défauts."""
        return {k: v for k, v in body.items() if v is not None}

    # --- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        search_depth: Optional[str] = None,
        topic: Optional[str] = None,
        max_results: Optional[int] = None,
        chunks_per_source: Optional[int] = None,
        time_range: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_answer: Optional[Union[bool, str]] = None,
        include_raw_content: Optional[Union[bool, str]] = None,
        include_images: Optional[bool] = None,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        country: Optional[str] = None,
        language: Optional[str] = None,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """POST /search — recherche web. 1 crédit (`advanced` : 2).

        Args:
            query: termes de recherche (max 400 caractères).
            search_depth: `basic` (défaut) | `advanced` | `fast` | `ultra-fast`.
            topic: `general` (défaut) | `news` | `finance`.
            max_results: 0-20 (défaut 5).
            chunks_per_source: 1-3 extraits par source (`advanced` seulement).
            time_range: `day` | `week` | `month` | `year`.
            start_date / end_date: bornes `YYYY-MM-DD`.
            include_answer: `False` (défaut) | `True`/`"basic"` | `"advanced"` —
                réponse synthétique générée par Tavily à partir des résultats.
            include_raw_content: `False` (défaut) | `"markdown"` | `"text"` —
                contenu complet de chaque page (lourd).
            include_domains / exclude_domains: listes de domaines.
            country: nom de pays en anglais (`france`) — booste ce pays.
            language: code ISO 639-1 (`fr`).
            timeout: timeout HTTP local (secondes).

        Returns: `{query, answer?, results: [{title, url, content, score,
            raw_content?, favicon?}], images?, response_time, usage: {credits},
            request_id}`.
        """
        body = self._compact({
            "query": query,
            "search_depth": search_depth,
            "topic": topic,
            "max_results": max_results,
            "chunks_per_source": chunks_per_source,
            "time_range": time_range,
            "start_date": start_date,
            "end_date": end_date,
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
            "include_images": include_images,
            "include_domains": include_domains,
            "exclude_domains": exclude_domains,
            "country": country,
            "language": language,
            "include_usage": True,
        })
        return self._request("POST", "/search", json=body, timeout=timeout)

    # --- extract ------------------------------------------------------------

    def extract(
        self,
        urls: Union[str, List[str]],
        query: Optional[str] = None,
        extract_depth: Optional[str] = None,
        chunks_per_source: Optional[int] = None,
        format: Optional[str] = None,
        include_images: Optional[bool] = None,
        include_favicon: Optional[bool] = None,
        timeout_s: Optional[float] = None,
        timeout: int = 90,
    ) -> Dict[str, Any]:
        """POST /extract — contenu propre de N URLs. 1 crédit par 5 URLs (`advanced` : 2).

        Args:
            urls: une URL ou une liste (max 20).
            query: intention utilisée pour reclasser les extraits.
            extract_depth: `basic` (défaut) | `advanced` (tables, contenu dynamique).
            chunks_per_source: 1-5 extraits par source (avec `query`).
            format: `markdown` (défaut) | `text`.
            timeout_s: budget côté Tavily (1-60 s).
            timeout: timeout HTTP local (secondes).

        Returns: `{results: [{url, raw_content, images?, favicon?}],
            failed_results: [{url, error}], response_time, usage: {credits},
            request_id}`.
        """
        body = self._compact({
            "urls": urls,
            "query": query,
            "extract_depth": extract_depth,
            "chunks_per_source": chunks_per_source,
            "format": format,
            "include_images": include_images,
            "include_favicon": include_favicon,
            "timeout": timeout_s,
            "include_usage": True,
        })
        return self._request("POST", "/extract", json=body, timeout=timeout)

    # --- crawl / map --------------------------------------------------------

    def _site_body(self, url: str, instructions, max_depth, max_breadth, limit,
                   select_paths, select_domains, exclude_paths, exclude_domains,
                   allow_external, timeout_s) -> Dict[str, Any]:
        return self._compact({
            "url": url,
            "instructions": instructions,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "select_paths": select_paths,
            "select_domains": select_domains,
            "exclude_paths": exclude_paths,
            "exclude_domains": exclude_domains,
            "allow_external": allow_external,
            "timeout": timeout_s,
            "include_usage": True,
        })

    def crawl(
        self,
        url: str,
        instructions: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_breadth: Optional[int] = None,
        limit: Optional[int] = None,
        select_paths: Optional[List[str]] = None,
        select_domains: Optional[List[str]] = None,
        exclude_paths: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        allow_external: Optional[bool] = None,
        extract_depth: Optional[str] = None,
        chunks_per_source: Optional[int] = None,
        format: Optional[str] = None,
        include_images: Optional[bool] = None,
        include_favicon: Optional[bool] = None,
        timeout_s: Optional[float] = None,
        timeout: int = 160,
    ) -> Dict[str, Any]:
        """POST /crawl — parcours d'un site avec contenu. **Synchrone**.
        1 crédit par 10 pages (2 avec `instructions` ou `extract_depth=advanced`).

        Args:
            url: URL racine.
            instructions: consigne en langage naturel (« les pages produit et tarifs »).
            max_depth: 1-5 (défaut 1). max_breadth: 1-500 liens par niveau (défaut 20).
            limit: plafond de pages (défaut 50) — première protection contre une
                facture surprise.
            select_paths / select_domains / exclude_paths / exclude_domains: regex.
            allow_external: suivre les liens externes (défaut API : true).
            extract_depth: `basic` (défaut) | `advanced`.
            format: `markdown` (défaut) | `text`.
            timeout_s: budget côté Tavily (10-150 s).
            timeout: timeout HTTP local (secondes).

        Returns: `{base_url, results: [{url, raw_content, favicon?}],
            response_time, usage: {credits}, request_id}`.
        """
        body = self._site_body(url, instructions, max_depth, max_breadth, limit,
                               select_paths, select_domains, exclude_paths,
                               exclude_domains, allow_external, timeout_s)
        body.update(self._compact({
            "extract_depth": extract_depth,
            "chunks_per_source": chunks_per_source,
            "format": format,
            "include_images": include_images,
            "include_favicon": include_favicon,
        }))
        return self._request("POST", "/crawl", json=body, timeout=timeout)

    def map_site(
        self,
        url: str,
        instructions: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_breadth: Optional[int] = None,
        limit: Optional[int] = None,
        select_paths: Optional[List[str]] = None,
        select_domains: Optional[List[str]] = None,
        exclude_paths: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        allow_external: Optional[bool] = None,
        timeout_s: Optional[float] = None,
        timeout: int = 160,
    ) -> Dict[str, Any]:
        """POST /map — URLs d'un site, sans contenu. 1 crédit par 10 pages.

        Mêmes arguments de parcours que `crawl` (sans les options de contenu).

        Returns: `{base_url, results: [url, …], response_time, usage: {credits},
            request_id}`.
        """
        body = self._site_body(url, instructions, max_depth, max_breadth, limit,
                               select_paths, select_domains, exclude_paths,
                               exclude_domains, allow_external, timeout_s)
        return self._request("POST", "/map", json=body, timeout=timeout)
