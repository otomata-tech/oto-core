"""
Cloro client — AI-search monitoring & SERP-in-JSON (cloro.dev).

Une seule API (Bearer) qui interroge les moteurs IA (ChatGPT, Gemini, Perplexity,
Copilot, Grok, Google AI Mode) et Google (SERP organique + AI Overview + PAA,
Google News), et renvoie du JSON structuré (texte/markdown + sources/citations).
Usage métier : veille de marque « AI SEO » (ce que les moteurs IA disent d'une
marque/produit), intelligence concurrentielle, SERP propre en JSON.

Requires: requests
"""

from typing import Any, Dict, Optional

import requests

from ...config import require_secret


class CloroClient:
    """Client cloro.dev. Auth Bearer ; endpoints sync `POST /v1/monitor/{provider}`.

    Les appels moteurs IA peuvent prendre ~30-45 s (timeout large par défaut).
    """

    BASE_URL = "https://api.cloro.dev/v1"

    # Moteurs IA conversationnels (corps `prompt`).
    AI_PROVIDERS = ("chatgpt", "gemini", "grok", "copilot", "perplexity", "aimode")

    def __init__(self, api_key: str = None):
        """
        Initialize Cloro client.

        Args:
            api_key: Cloro API key (or set CLORO_API_KEY env var).
        """
        self.api_key = api_key or require_secret("CLORO_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _post(self, path: str, body: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        response = self.session.post(f"{self.BASE_URL}{path}", json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def monitor(
        self,
        provider: str,
        prompt: str,
        country: Optional[str] = None,
        include: Optional[Dict[str, bool]] = None,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """Interroge un moteur IA (`provider` ∈ AI_PROVIDERS) avec `prompt`.

        Args:
            provider: 'chatgpt' | 'gemini' | 'perplexity' | 'copilot' | 'grok' | 'aimode'.
            prompt: question/requête (1–10 000 caractères).
            country: code pays ISO (ex. 'US', 'FR').
            include: flags d'extraction (ex. {'markdown': True, 'searchQueries': True}).

        Returns: payload Cloro `{success, result: {text, markdown, sources, ...}}`.
        """
        body: Dict[str, Any] = {"prompt": prompt}
        if country:
            body["country"] = country
        if include:
            body["include"] = include
        return self._post(f"/monitor/{provider}", body, timeout)

    def google(
        self,
        query: str,
        country: Optional[str] = None,
        include: Optional[Dict[str, bool]] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Google SERP en JSON via Cloro (organique + AI Overview + People Also Ask).

        Args:
            query: requête de recherche.
            country: code pays ISO.
            include: flags ex. {'aiOverview': True, 'organicResults': True,
                'peopleAlsoAsk': True}.
        """
        body: Dict[str, Any] = {"query": query}
        if country:
            body["country"] = country
        if include:
            body["include"] = include
        return self._post("/monitor/google", body, timeout)

    def google_news(
        self,
        query: str,
        country: Optional[str] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Google News en JSON via Cloro.

        Args:
            query: requête.
            country: code pays ISO.
        """
        body: Dict[str, Any] = {"query": query}
        if country:
            body["country"] = country
        return self._post("/monitor/google/news", body, timeout)
