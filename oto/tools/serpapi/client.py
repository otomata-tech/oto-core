"""
SerpAPI Client for Google Jobs and search.

Requires: requests
"""

import time
from typing import Optional, Dict, Any

import requests

from ...config import require_secret

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class SerpAPIClient:
    """
    SerpAPI client — accès générique à **tous** les moteurs SerpApi.

    `search(engine, params)` est l'entrée générique (engine = 'google', 'bing',
    'google_trends', 'youtube', 'walmart', 'google_jobs'…). `search_jobs` /
    `get_job_details` sont des raccourcis typés Google Jobs construits par-dessus.
    """

    BASE_URL = "https://serpapi.com/search"

    def __init__(self, api_key: str = None):
        """
        Initialize SerpAPI client.

        Args:
            api_key: SerpAPI key (or set SERPAPI_API_KEY env var)
        """
        self.api_key = api_key or require_secret("SERPAPI_API_KEY")
        self.session = requests.Session()
        self._last_request = 0.0
        self._min_interval = 1.0

    def _rate_limit(self):
        """Ensure minimum time between requests."""
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    def _request(self, params: Dict) -> Dict:
        """Make API request."""
        self._rate_limit()
        params["api_key"] = self.api_key

        response = self.session.get(self.BASE_URL, params=params, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def _paginate(
        self,
        params: Dict,
        result: Dict,
        results_key: str,
        max_results: int,
    ) -> Dict:
        """Suit `serpapi_pagination.next_page_token` jusqu'à `max_results`.

        Concatène `result[results_key]` au fil des pages et tronque à
        `max_results`. Mute et renvoie `result` (la dernière page sert de
        métadonnées). No-op si le moteur ne renvoie pas de token.
        """
        all_items = list(result.get(results_key, []))
        while len(all_items) < max_results:
            next_token = result.get("serpapi_pagination", {}).get("next_page_token")
            if not next_token:
                break
            params["next_page_token"] = next_token
            result = self._request(params)
            new_items = result.get(results_key, [])
            if not new_items:
                break
            all_items.extend(new_items)

        result[results_key] = all_items[:max_results]
        return result

    def search(
        self,
        engine: str,
        params: Dict[str, Any] = None,
        max_results: int = None,
        results_key: str = None,
        **extra,
    ) -> Dict[str, Any]:
        """
        Generic SerpApi call — reach ANY engine.

        Args:
            engine: SerpApi engine id, e.g. 'google', 'bing', 'duckduckgo',
                'youtube', 'walmart', 'amazon', 'ebay', 'google_trends',
                'google_finance', 'google_flights', 'google_hotels',
                'google_events', 'google_jobs'… (full list: serpapi.com).
            params: engine-specific parameters (e.g. {'q': 'pizza', 'gl': 'us'}).
            max_results: if set with `results_key`, auto-paginate up to this many.
            results_key: the result array to paginate/cap (e.g. 'organic_results',
                'jobs_results'). Required to enable pagination.
            **extra: extra params merged into the query (convenience).

        Returns:
            Raw SerpApi JSON payload.
        """
        payload: Dict[str, Any] = {"engine": engine}
        if params:
            payload.update(params)
        if extra:
            payload.update(extra)

        result = self._request(payload)
        if max_results and results_key:
            result = self._paginate(payload, result, results_key, max_results)
        return result

    def search_jobs(
        self,
        query: str = None,
        company: str = None,
        location: str = None,
        country: str = None,
        language: str = "en",
        max_results: int = 100,
        no_cache: bool = False,
    ) -> Dict[str, Any]:
        """
        Search Google Jobs.

        Args:
            query: free-text job query (e.g. 'data engineer Paris', 'senior
                python remote'). Use this for general job sourcing.
            company: convenience shortcut — if `query` is omitted, searches
                "<company> jobs" (backward-compatible).
            location: Geographic location (e.g., 'Paris, France')
            country: Country code (e.g., 'fr', 'us')
            language: Language code
            max_results: Maximum results (handles pagination)
            no_cache: Force fresh results

        Returns:
            Dict with jobs_results array
        """
        q = query or (f"{company} jobs" if company else None)
        if not q:
            raise ValueError("search_jobs requires `query` or `company`")

        params: Dict[str, Any] = {"q": q, "hl": language, "no_cache": str(no_cache).lower()}
        if location:
            params["location"] = location
        if country:
            params["gl"] = country

        return self.search(
            "google_jobs", params=params,
            max_results=max_results, results_key="jobs_results",
        )

    def get_job_details(self, job_id: str) -> Dict[str, Any]:
        """
        Get detailed job information.

        Args:
            job_id: Google Jobs job ID

        Returns:
            Detailed job information
        """
        return self.search("google_jobs_listing", params={"q": job_id})
