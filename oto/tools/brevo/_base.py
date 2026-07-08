"""Socle HTTP du client Brevo — auth `api-key`, requêtes, pagination.

Séparé du client pour tenir chaque module sous ~200 lignes : les mixins
métier (contacts, email, campaigns, crm) héritent de `_BrevoBase` et n'ont
plus qu'à appeler `self._request(...)`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class _BrevoBase:
    """Transport commun : session `requests`, header `api-key`, erreurs typées."""

    BASE_URL = "https://api.brevo.com/v3"

    def __init__(self, api_key: Optional[str] = None):
        """Initialise le client.

        Args:
            api_key: clé API v3 Brevo (ou env `BREVO_API_KEY` en fallback CLI).
        """
        self.api_key = api_key or require_secret("BREVO_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "api-key": self.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        raise_for_upstream(resp, service="brevo")
        # 204 (PUT/PATCH Brevo) et corps vides → dict vide plutôt qu'un crash JSON.
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    @staticmethod
    def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
        """Retire les `None` — Brevo rejette `?limit=None` et prend mal `?sort=`."""
        return {k: v for k, v in params.items() if v is not None}
