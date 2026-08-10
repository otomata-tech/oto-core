"""
Bright Data client — scaffold (coquille vide).

Le connecteur est câblé côté plateforme (registre + clé), mais les produits ne
sont **pas encore implémentés**. Cette classe pose l'authentification et l'accès
HTTP ; les méthodes produit restent à écrire.

Produits à brancher (endpoint unifié `https://api.brightdata.com/request`, POST,
auth Bearer) :
- **SERP API** — résultats de recherche structurés (Google/Bing…). Body :
  `{"zone": <serp_zone>, "url": "https://www.google.com/search?q=...", "format": "raw"}`
  + `brd_json=1` (query param de l'`url`) ou `"data_format": "parsed_light"` pour du
  JSON parsé ; `"data_format": "markdown"` pour du Markdown.
- **Web Unlocker** — fetch de n'importe quelle URL protégée (anti-bot) → HTML brut
  ou Markdown. Body : `{"zone": <unlocker_zone>, "url": ..., "format": "raw"}`.
- **Web Scraper / Datasets** — datasets structurés (LinkedIn, Amazon…) via flux
  asynchrone trigger→snapshot (endpoints `/datasets/v3/*`, polling).

Requires: requests
"""

from typing import Any, Dict

import requests

from ...config import require_secret

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class BrightDataClient:
    """Client Bright Data (scaffold). Auth Bearer + endpoint `/request` posés ;
    aucune méthode produit publique pour l'instant (cf. docstring du module)."""

    BASE_URL = "https://api.brightdata.com/request"

    def __init__(self, api_key: str = None):
        """
        Initialize Bright Data client.

        Args:
            api_key: Bright Data API token (or set BRIGHTDATA_API_KEY env var).
        """
        self.api_key = api_key or require_secret("BRIGHTDATA_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _post(self, payload: Dict[str, Any]) -> requests.Response:
        """POST sur l'endpoint unifié `/request`. Helper bas-niveau prêt pour les
        futures méthodes produit (SERP / Web Unlocker)."""
        response = self.session.post(self.BASE_URL, json=payload, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response

    # TODO — méthodes produit à implémenter (cf. docstring du module) :
    #   serp(query, engine="google", parse=True, ...)  -> JSON SERP parsé
    #   unlock(url, data_format=None, ...)              -> HTML / Markdown
    #   dataset_trigger(...) / dataset_snapshot(...)    -> datasets async
