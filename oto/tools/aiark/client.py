"""
AI Ark API Client — B2B company & people data (search + contact enrichment).

Synchronous REST API (docs.ai-ark.com). Auth = API key in the `X-TOKEN` header.
Base: https://api.ai-ark.com/api/developer-portal

Endpoints couverts (v1 = SYNCHRONES uniquement) :
- POST /v1/companies              — recherche de sociétés (filtres firmographiques)
- POST /v1/people                 — recherche de personnes (filtres société + contact)
- POST /v1/people/export/single   — export d'UNE personne + recherche d'email (sync)
- POST /v1/people/reverse-lookup  — retrouver une personne depuis email/téléphone
- POST /v1/people/mobile-phone-finder — trouver le mobile d'une personne
- GET  /v1/payments/credits       — crédits restants

Les exports/find-emails EN LOT répondent par webhook (asynchrone) : hors périmètre
v1 (itération suivante si besoin). Le single-person export ci-dessus est synchrone.

Requires: requests
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret


class AiArkClient:
    """Client pour l'API AI Ark (Company & People Data)."""

    BASE_URL = "https://api.ai-ark.com/api/developer-portal"
    TIMEOUT = 30

    def __init__(self, api_key: str | None = None):
        """
        Args:
            api_key: clé AI Ark (`X-TOKEN`). À défaut, lue de l'env `AIARK_API_KEY`.
        """
        self.api_key = api_key or require_secret("AIARK_API_KEY")

    def _headers(self) -> Dict[str, str]:
        return {
            "X-TOKEN": self.api_key,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Optional[dict] = None,
        allow_404: bool = False,
    ) -> Any:
        """Appel API. `allow_404=True` renvoie None sur 404 (lookup infructueux =
        cas normal, pas une erreur) au lieu de lever."""
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        resp = requests.request(
            method, url, headers=self._headers(), json=json, timeout=self.TIMEOUT
        )
        if allow_404 and resp.status_code == 404:
            return None
        resp.raise_for_status()
        # Certains endpoints (204/corps vide) ne renvoient pas de JSON.
        if not resp.content:
            return None
        return resp.json()

    # ---- crédits / auth ----------------------------------------------------

    def credits(self) -> Dict[str, Any]:
        """Crédits restants du compte : `{"total": <int>}`."""
        return self._request("GET", "v1/payments/credits")

    def verify_key(self) -> Dict[str, Any]:
        """Valide la clé via un appel crédits. `{"valid": True, "credits": <int>}`
        si OK, sinon lève la HTTPError (401 = clé invalide)."""
        data = self.credits() or {}
        return {"valid": True, "credits": data.get("total")}

    # ---- recherche ---------------------------------------------------------

    def search_companies(
        self,
        *,
        account: Optional[dict] = None,
        lists: Optional[dict] = None,
        lookalike_domains: Optional[List[str]] = None,
        page: int = 0,
        size: int = 10,
    ) -> Dict[str, Any]:
        """Recherche de sociétés (firmographie). Renvoie la page brute AI Ark
        (`content[]`, `totalElements`, `totalPages`, `pageable`, …).

        Args:
            account: filtres firmographiques (nom, domaine, secteur, localisation,
                effectif, CA, technologies, funding…). Structure AI Ark, ex.
                `{"name": {"any": {"include": {"mode": "SMART", "content": ["Amazon"]}}}}`.
            lists: exclusion de sociétés déjà dans des listes sauvegardées.
            lookalike_domains: jusqu'à 5 URLs pour trouver des sociétés similaires.
            page: numéro de page (0-based). size: 0-100.
        """
        body: Dict[str, Any] = {"page": page, "size": size}
        if account is not None:
            body["account"] = account
        if lists is not None:
            body["lists"] = lists
        if lookalike_domains:
            body["lookalikeDomains"] = lookalike_domains
        return self._request("POST", "v1/companies", json=body)

    def search_people(
        self,
        *,
        account: Optional[dict] = None,
        contact: Optional[dict] = None,
        lists: Optional[dict] = None,
        page: int = 0,
        size: int = 10,
    ) -> Dict[str, Any]:
        """Recherche de personnes. Renvoie la page brute AI Ark (`content[]`,
        `totalElements`, `totalPages`, `trackId`, …).

        Args:
            account: filtres sur la société de rattachement (domaine, secteur,
                effectif…), même DSL que `search_companies`.
            contact: filtres sur la personne (séniorité, département, poste,
                localisation…), ex. `{"seniority": {"any": {"include": ["founder"]}}}`.
            lists: exclusion de personnes déjà dans des listes sauvegardées.
            page: numéro de page (0-based). size: 0-100.
        """
        body: Dict[str, Any] = {"page": page, "size": size}
        if account is not None:
            body["account"] = account
        if contact is not None:
            body["contact"] = contact
        if lists is not None:
            body["lists"] = lists
        return self._request("POST", "v1/people", json=body)

    # ---- enrichissement (synchrone) ---------------------------------------

    def export_person(
        self, *, id: Optional[str] = None, url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Export d'UNE personne + recherche d'email (synchrone). Renvoie le profil
        avec `email.output[]` (`address`/`status`/`domainType`), ou None si aucun
        email/profil trouvé (404).

        Args:
            id: AI Ark id d'une personne (issu d'une recherche `search_people`).
            url: OU une URL de profil LinkedIn. Au moins l'un des deux requis.
        """
        if not id and not url:
            raise ValueError("export_person exige `id` ou `url`.")
        body: Dict[str, Any] = {}
        if id:
            body["id"] = id
        if url:
            body["url"] = url
        return self._request(
            "POST", "v1/people/export/single", json=body, allow_404=True
        )

    def reverse_lookup(self, search: str) -> Optional[Dict[str, Any]]:
        """Retrouve une personne depuis une info de contact (email, téléphone…).
        Renvoie le profil complet, ou None si introuvable (404).

        Args:
            search: l'info de contact à résoudre (email, téléphone…).
        """
        return self._request(
            "POST",
            "v1/people/reverse-lookup",
            json={"search": search},
            allow_404=True,
        )

    def mobile_phone(
        self,
        *,
        linkedin: Optional[str] = None,
        domain: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Trouve le(s) mobile(s) d'une personne. Renvoie `{"id", "linkedin",
        "data": [["+..."]]}` ou None si introuvable (404).

        Args:
            linkedin: URL du profil LinkedIn (seul), OU…
            domain + name: domaine de la société ET nom de la personne (ensemble).
        """
        if not linkedin and not (domain and name):
            raise ValueError(
                "mobile_phone exige `linkedin` OU (`domain` ET `name`)."
            )
        body: Dict[str, Any] = {}
        if linkedin:
            body["linkedin"] = linkedin
        if domain:
            body["domain"] = domain
        if name:
            body["name"] = name
        return self._request(
            "POST",
            "v1/people/mobile-phone-finder",
            json=body,
            allow_404=True,
        )
