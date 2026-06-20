"""Lever ATS API client.

Auth = **API key** en Basic auth (la clé est le *username*, mot de passe vide).
Créée dans Lever : Settings → Integrations and API → API credentials. Passée en
clair au constructeur (ou `LEVER_API_KEY` en fallback).

Vocabulaire Lever : un candidat dans un pipeline = une **opportunity** ; un poste
= un **posting**. Les écritures (création, note) acceptent un `perform_as` (id
d'un utilisateur Lever au nom de qui agir).

Docs : https://hire.lever.co/developer/documentation

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret


class LeverClient:
    """Client Lever Hire v1 — opportunities (candidats), postings, notes."""

    BASE_URL = "https://api.lever.co/v1"

    def __init__(self, api_key: Optional[str] = None):
        """Initialise le client.

        Args:
            api_key: Lever API key (ou env `LEVER_API_KEY`).
        """
        self.api_key = api_key or require_secret("LEVER_API_KEY")
        self.session = requests.Session()
        self.session.auth = (self.api_key, "")
        self.session.headers.update({"Content-Type": "application/json"})

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise Exception(f"Lever HTTP {resp.status_code}: {body}")
        return resp.json() if resp.content else {}

    # --- Opportunities (candidats dans un pipeline) -------------------------

    def list_opportunities(
        self,
        limit: int = 50,
        offset: Optional[str] = None,
        posting_id: Optional[str] = None,
        stage_id: Optional[str] = None,
        email: Optional[str] = None,
        expand: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Liste les opportunities (candidats). Renvoie `{data, hasNext, next}` —
        passer `next` à `offset` pour la page suivante.

        Args:
            posting_id / stage_id : filtres pipeline.
            email: filtre par email exact du candidat.
            expand: champs à dérouler (ex. ["applications", "stage", "owner"]).
        """
        params: Dict[str, Any] = {"limit": min(limit, 100)}
        if offset:
            params["offset"] = offset
        if posting_id:
            params["posting_id"] = posting_id
        if stage_id:
            params["stage_id"] = stage_id
        if email:
            params["email"] = email
        if expand:
            params["expand"] = expand
        return self._request("GET", "/opportunities", params=params)

    def get_opportunity(
        self, opportunity_id: str, expand: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Récupère une opportunity (candidat) par id."""
        params = {"expand": expand} if expand else None
        return self._request("GET", f"/opportunities/{opportunity_id}", params=params)

    def add_candidate(
        self, candidate: Dict[str, Any], perform_as: str,
        posting_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Crée un candidat (opportunity).

        Args:
            candidate: objet candidat Lever (`name`, `emails`, `phones`, `links`,
                `tags`, `sources`, …).
            perform_as: id de l'utilisateur Lever au nom de qui créer (requis).
            posting_ids: postings auxquels rattacher le candidat.
        """
        body = dict(candidate)
        if posting_ids:
            body["postings"] = posting_ids
        return self._request("POST", "/opportunities", json=body,
                             params={"perform_as": perform_as})

    def add_note(
        self, opportunity_id: str, value: str, perform_as: str,
    ) -> Dict[str, Any]:
        """Ajoute une note à une opportunity (candidat).

        Args:
            perform_as: id de l'utilisateur Lever auteur de la note (requis).
        """
        return self._request(
            "POST", f"/opportunities/{opportunity_id}/notes",
            json={"value": value}, params={"perform_as": perform_as})

    # --- Postings (postes) --------------------------------------------------

    def list_postings(
        self, limit: int = 50, offset: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les postings (postes). `state` : "published" | "internal" |
        "closed" | "draft" | "pending" | "rejected"."""
        params: Dict[str, Any] = {"limit": min(limit, 100)}
        if offset:
            params["offset"] = offset
        if state:
            params["state"] = state
        return self._request("GET", "/postings", params=params)

    def get_posting(self, posting_id: str) -> Dict[str, Any]:
        """Récupère un posting (poste) par id."""
        return self._request("GET", f"/postings/{posting_id}")

    # --- Référentiels -------------------------------------------------------

    def list_stages(self) -> Dict[str, Any]:
        """Liste les stages du pipeline (référentiel)."""
        return self._request("GET", "/stages")

    def list_users(self, limit: int = 50, offset: Optional[str] = None) -> Dict[str, Any]:
        """Liste les utilisateurs Lever (recruteurs) — pour `perform_as`."""
        params: Dict[str, Any] = {"limit": min(limit, 100)}
        if offset:
            params["offset"] = offset
        return self._request("GET", "/users", params=params)
