"""Recruitee ATS API client.

Auth = **API token** (Bearer) + **company id** (le sous-domaine/identifiant de la
société, présent dans l'URL de l'app Recruitee). Token créé dans Recruitee :
Settings → Apps and plugins → Personal API tokens. Les deux passés au
constructeur (ou `RECRUITEE_API_TOKEN` / `RECRUITEE_COMPANY_ID` en fallback).

Vocabulaire Recruitee : un poste = une **offer** ; un candidat = un **candidate**
(rattaché à une ou plusieurs offers).

Docs : https://docs.recruitee.com/reference

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret


class RecruiteeClient:
    """Client Recruitee — candidats, offers, notes."""

    BASE_URL = "https://api.recruitee.com"

    def __init__(self, api_token: Optional[str] = None,
                 company_id: Optional[str] = None):
        """Initialise le client.

        Args:
            api_token: Recruitee API token (ou env `RECRUITEE_API_TOKEN`).
            company_id: identifiant de la société (ou env `RECRUITEE_COMPANY_ID`).
        """
        self.api_token = api_token or require_secret("RECRUITEE_API_TOKEN")
        self.company_id = company_id or require_secret("RECRUITEE_COMPANY_ID")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}/c/{self.company_id}{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise Exception(f"Recruitee HTTP {resp.status_code}: {body}")
        return resp.json() if resp.content else {}

    # --- Candidats ----------------------------------------------------------

    def list_candidates(
        self,
        limit: int = 50,
        offset: int = 0,
        offer_id: Optional[int] = None,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les candidats (paginé). `offer_id` filtre par poste, `query`
        cherche par nom/email."""
        params: Dict[str, Any] = {"limit": min(limit, 100), "offset": offset}
        if offer_id:
            params["offer_id"] = offer_id
        if query:
            params["query"] = query
        return self._request("GET", "/candidates", params=params)

    def get_candidate(self, candidate_id: int) -> Dict[str, Any]:
        """Récupère un candidat par id."""
        return self._request("GET", f"/candidates/{candidate_id}")

    def create_candidate(
        self, candidate: Dict[str, Any], offer_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Crée un candidat.

        Args:
            candidate: objet candidat (`name`, `emails`, `phones`, `social_links`,
                `links`, `cover_letter`, …).
            offer_ids: postes auxquels rattacher le candidat.
        """
        body: Dict[str, Any] = {"candidate": candidate}
        if offer_ids:
            body["offers"] = offer_ids
        return self._request("POST", "/candidates", json=body)

    def add_note(self, candidate_id: int, body: str) -> Dict[str, Any]:
        """Ajoute une note à un candidat."""
        return self._request(
            "POST", f"/candidates/{candidate_id}/notes",
            json={"note": {"body": body}})

    # --- Offers (postes) ----------------------------------------------------

    def list_offers(
        self, scope: Optional[str] = None, kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les offers (postes). `scope` : "active" | "archived" | "not_archived" ;
        `kind` : "job" | "talent_pool"."""
        params: Dict[str, Any] = {}
        if scope:
            params["scope"] = scope
        if kind:
            params["kind"] = kind
        return self._request("GET", "/offers", params=params or None)

    def get_offer(self, offer_id: int) -> Dict[str, Any]:
        """Récupère un offer (poste) par id."""
        return self._request("GET", f"/offers/{offer_id}")
