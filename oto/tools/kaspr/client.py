"""
Kaspr API Client for LinkedIn profile enrichment.

Requires: requests
"""

import re
from typing import Optional, Dict, Any, List

import requests

from ...config import require_secret

# Kaspr veut le SLUG NU : une URL complète (ou un slash/query) fait un 500
# (vérifié live : `alexislaporte` → 200, `https://.../in/alexislaporte/` → 500).
_LINKEDIN_IN = re.compile(r"/in/([^/?#]+)", re.IGNORECASE)


def linkedin_slug(raw: str) -> str:
    """Normalise un identifiant LinkedIn (slug nu OU URL profil) → slug nu."""
    raw = (raw or "").strip()
    m = _LINKEDIN_IN.search(raw)
    if m:
        return m.group(1)
    return raw.rstrip("/").split("?")[0].split("#")[0]


class KasprClient:
    """
    Kaspr API client for:
    - LinkedIn profile enrichment
    - Email and phone number retrieval
    """

    BASE_URL = "https://api.developers.kaspr.io"

    def __init__(self, api_key: str = None):
        """
        Initialize Kaspr client.

        Args:
            api_key: Kaspr API key (or set KASPR_API_KEY env var)
        """
        self.api_key = api_key or require_secret("KASPR_API_KEY")

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make API request."""
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "accept-version": "v2.0",
        }

        response = requests.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def verify_key(self) -> Dict[str, Any]:
        """
        Validate the API key.

        Kaspr v2.0 n'expose pas d'endpoint `/user` ou `/me` — on vérifie
        l'auth via un POST sentinel sur `/profile/linkedin` avec un id
        manifestement introuvable. L'API authentifie avant de chercher le
        profil donc on obtient 401 si la clé est mauvaise, 200 + body
        vide sinon (vérifié live 22/05).

        Returns: `{"valid": True}` si clé OK, sinon lève la HTTPError.
        """
        self._request(
            "POST", "profile/linkedin",
            json={
                "id": "__oto_verify_key__",
                "name": "__verify__",
                "dataToGet": [],
            },
        )
        return {"valid": True}

    def enrich_linkedin(
        self,
        linkedin_id: str,
        name: str = None,
        is_phone_required: bool = False,
        data_to_get: List[str] = None,
    ) -> Dict[str, Any]:
        """
        Enrich a LinkedIn profile.

        Args:
            linkedin_id: LinkedIn slug ("john-doe-12345") or full profile URL
                ("https://www.linkedin.com/in/john-doe-12345/") — the bare slug
                is extracted automatically (a full URL makes Kaspr 500).
            name: Full name (helps matching)
            is_phone_required: Require phone number
            data_to_get: Data types to retrieve (e.g., ["workEmail", "personalEmail", "phone"])

        Returns:
            Enriched profile with emails and phones
        """
        slug = linkedin_slug(linkedin_id)
        data = {"id": slug, "name": name or slug}
        if is_phone_required:
            data["isPhoneRequired"] = True
        data["dataToGet"] = data_to_get or ["workEmail", "phone"]

        try:
            return self._request("POST", "profile/linkedin", json=data)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 402 and "phone" in data["dataToGet"]:
                data["dataToGet"] = [d for d in data["dataToGet"] if d != "phone"]
                return self._request("POST", "profile/linkedin", json=data)
            raise
