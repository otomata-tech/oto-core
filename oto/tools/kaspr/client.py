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

# Les SEULS noms que Kaspr accepte dans `dataToGet` (API v2.0). Un nom inconnu ne
# rend pas un 400 lisible : le parser amont plante et l'appelant reçoit un **500**
# (`TypeError: Cannot read properties of undefined (reading 'push')`) — c'est-à-dire
# une panne, là où il a une faute de frappe. Reproduit le 2026-09-01 sur un profil
# sentinelle, sans consommer de crédit :
#   ["emails", "phones", "company"] → 500 ;  ["workEmail", "phone"] → 402 ;  [] → 200.
#
# Ces trois noms-là n'étaient pas un hasard : la docstring du tool MCP
# `kaspr_enrich_linkedin` les donnait en EXEMPLE depuis la création du tool
# (2026-05-22), et un agent qui lit le schéma applique ce qu'il y lit. D'où le refus
# LOCAL ci-dessous, qui NOMME les noms acceptés : un premier essai corrigeable au
# lieu d'une panne qu'on croit amont — et qu'on réessaie donc en boucle.
DATA_TO_GET = ("workEmail", "personalEmail", "phone")

# Ce que Kaspr reçoit quand l'appelant ne demande rien : PAS « tous les champs ».
DATA_TO_GET_DEFAUT = ["workEmail", "phone"]


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
    # (connect, read) — Kaspr répond <1s en nominal ; sans read-timeout un blip
    # amont laisse l'appel suspendu POUR TOUJOURS (thread perdu, jamais loggé —
    # vécu 2026-07-22, signal #252 : appel invisible du calllog, client MCP parti
    # à 60s, serveur pendu). Un timeout transforme le blip en erreur actionnable.
    TIMEOUT = (10, 50)

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

        kwargs.setdefault("timeout", self.TIMEOUT)
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
            data_to_get: field names to retrieve — only `DATA_TO_GET` values are
                accepted ("workEmail", "personalEmail", "phone"); anything else is
                refused HERE, because Kaspr answers 500 on an unknown name.
                Omitted → `DATA_TO_GET_DEFAUT` (NOT every field).

        Returns:
            Enriched profile with emails and phones

        Raises:
            ValueError: `data_to_get` carries a name Kaspr does not know.
        """
        if data_to_get is not None:
            inconnus = [str(d) for d in data_to_get if d not in DATA_TO_GET]
            if inconnus:
                raise ValueError(
                    "Kaspr n'accepte dans `dataToGet` que "
                    + ", ".join(DATA_TO_GET)
                    + " — reçu : " + ", ".join(inconnus)
                    + ". (Un nom inconnu ne fait pas un refus chez Kaspr : "
                      "il fait une erreur serveur 500.)")
        slug = linkedin_slug(linkedin_id)
        data = {"id": slug, "name": name or slug}
        if is_phone_required:
            data["isPhoneRequired"] = True
        data["dataToGet"] = data_to_get or list(DATA_TO_GET_DEFAUT)

        try:
            return self._request("POST", "profile/linkedin", json=data)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 402 and "phone" in data["dataToGet"]:
                data["dataToGet"] = [d for d in data["dataToGet"] if d != "phone"]
                return self._request("POST", "profile/linkedin", json=data)
            raise
