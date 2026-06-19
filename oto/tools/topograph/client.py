"""Topograph API Client — KYB data & documents for European public registers.

Topograph (https://www.topograph.co) normalise 100+ registres publics européens
derrière une seule API REST (KYB onboarding + vérification). Doc :
https://docs.topograph.co.

Auth : clé API dans l'en-tête `x-api-key` (Dashboard → Settings → API Keys).

Requires: requests
"""

import time
from typing import Any, Dict, Optional

import requests

from ...config import require_secret


class TopographClient:
    """Client de l'API Topograph v2.

    - `search` : recherche d'entreprise par nom ou n° d'immatriculation (GET /v2/search).
    - `company` : données entreprise normalisées (POST /v2/company), mode
      `onboarding` (rapide/économique) ou `verification` (rigoureux).
    """

    BASE_URL = "https://api.topograph.co/v2"

    def __init__(self, api_key: str = None):
        """
        Args:
            api_key: clé API Topograph (ou variable d'env `TOPOGRAPH_API_KEY`).
        """
        self.api_key = api_key or require_secret("TOPOGRAPH_API_KEY")

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        if method.upper() != "GET":
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise Exception(f"HTTP {resp.status_code}: {body}")
            return resp.json() if resp.content else {}
        raise Exception("Topograph: rate-limited (429) after retries")

    def search(
        self,
        query: str,
        country: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Recherche d'entreprises par nom ou n° d'immatriculation.

        Args:
            query: nom de société ou numéro d'immatriculation.
            country: code pays ISO 3166-1 alpha-2 (ex. "FR", "GB", "DE").
            limit: nombre maximum de résultats.

        Returns:
            Résultats de recherche (candidats avec identité + n° d'immatriculation).
        """
        params: Dict[str, Any] = {"query": query}
        if country:
            params["country"] = country
        if limit is not None:
            params["limit"] = limit
        return self._request("GET", "search", params=params)

    def company(
        self,
        country: Optional[str] = None,
        registration_number: Optional[str] = None,
        company_id: Optional[str] = None,
        mode: str = "onboarding",
    ) -> Dict[str, Any]:
        """Données entreprise normalisées (POST /v2/company).

        Identifier l'entreprise par (`country` + `registration_number`) — les deux
        renvoyés par `search` — ou par `company_id`.

        Args:
            country: code pays ISO 3166-1 alpha-2.
            registration_number: numéro d'immatriculation (SIREN/SIRET, etc.).
            company_id: identifiant Topograph (alternative au n° d'immatriculation).
            mode: "onboarding" (rapide/économique) ou "verification" (rigoureux).

        Returns:
            Fiche entreprise normalisée (identité, forme juridique, dirigeants…).
        """
        body: Dict[str, Any] = {"mode": mode}
        if country:
            body["country"] = country
        if registration_number:
            body["registrationNumber"] = registration_number
        if company_id:
            body["companyId"] = company_id
        return self._request("POST", "company", json=body)
