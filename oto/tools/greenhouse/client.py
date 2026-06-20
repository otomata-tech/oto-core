"""Greenhouse Harvest API client (ATS).

Auth = **Harvest API key** en Basic auth (la clé est le *username*, mot de passe
vide). Créée dans Greenhouse : Configure → Dev Center → API Credentials →
Harvest. Passée en clair au constructeur (ou `GREENHOUSE_API_KEY` en fallback).

Surface lecture (candidats, jobs, candidatures, users) + écriture ciblée
(création de candidat, note d'activité). Les écritures Greenhouse exigent un
**`On-Behalf-Of`** (id d'un utilisateur Greenhouse) — passé par `on_behalf_of`.

Docs : https://developers.greenhouse.io/harvest.html

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret


class GreenhouseClient:
    """Client Greenhouse Harvest v1 — sourcing & suivi de candidats (ATS)."""

    BASE_URL = "https://harvest.greenhouse.io/v1"

    def __init__(self, api_key: Optional[str] = None):
        """Initialise le client.

        Args:
            api_key: Harvest API key (ou env `GREENHOUSE_API_KEY`).
        """
        self.api_key = api_key or require_secret("GREENHOUSE_API_KEY")
        self.session = requests.Session()
        # Basic auth : la clé est le username, mot de passe vide.
        self.session.auth = (self.api_key, "")
        self.session.headers.update({"Content-Type": "application/json"})

    def _request(self, method: str, path: str,
                 on_behalf_of: Optional[int] = None, **kwargs) -> Any:
        url = f"{self.BASE_URL}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        if on_behalf_of is not None:
            headers["On-Behalf-Of"] = str(on_behalf_of)
        resp = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise Exception(f"Greenhouse HTTP {resp.status_code}: {body}")
        return resp.json() if resp.content else {}

    # --- Candidats ----------------------------------------------------------

    def list_candidates(
        self,
        per_page: int = 50,
        page: int = 1,
        job_id: Optional[int] = None,
        email: Optional[str] = None,
        created_after: Optional[str] = None,
        updated_after: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Liste les candidats (paginé). Filtres : `job_id`, `email`,
        `created_after`/`updated_after` (ISO 8601)."""
        params: Dict[str, Any] = {"per_page": min(per_page, 500), "page": page}
        if job_id:
            params["job_id"] = job_id
        if email:
            params["email"] = email
        if created_after:
            params["created_after"] = created_after
        if updated_after:
            params["updated_after"] = updated_after
        return self._request("GET", "/candidates", params=params)

    def get_candidate(self, candidate_id: int) -> Dict[str, Any]:
        """Récupère un candidat par id (avec ses candidatures)."""
        return self._request("GET", f"/candidates/{candidate_id}")

    def add_candidate(
        self, candidate: Dict[str, Any], on_behalf_of: int,
    ) -> Dict[str, Any]:
        """Crée un candidat (ou prospect).

        Args:
            candidate: objet candidat Greenhouse (`first_name`, `last_name`,
                `email_addresses`, `applications`, `phone_numbers`, …).
            on_behalf_of: id de l'utilisateur Greenhouse au nom de qui créer
                (header `On-Behalf-Of`, obligatoire en écriture).
        """
        return self._request("POST", "/candidates", json=candidate,
                             on_behalf_of=on_behalf_of)

    def add_note(
        self, candidate_id: int, body: str, user_id: int,
        visibility: str = "public",
    ) -> Dict[str, Any]:
        """Ajoute une note au fil d'activité d'un candidat.

        Args:
            user_id: id de l'utilisateur Greenhouse auteur de la note (aussi
                utilisé comme `On-Behalf-Of`).
            visibility: "admin_only" | "private" | "public".
        """
        body_obj = {"user_id": user_id, "body": body, "visibility": visibility}
        return self._request(
            "POST", f"/candidates/{candidate_id}/activity_feed/notes",
            json=body_obj, on_behalf_of=user_id)

    # --- Jobs ---------------------------------------------------------------

    def list_jobs(
        self, per_page: int = 50, page: int = 1, status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Liste les jobs (postes). `status` : "open" | "closed" | "draft"."""
        params: Dict[str, Any] = {"per_page": min(per_page, 500), "page": page}
        if status:
            params["status"] = status
        return self._request("GET", "/jobs", params=params)

    def get_job(self, job_id: int) -> Dict[str, Any]:
        """Récupère un job par id."""
        return self._request("GET", f"/jobs/{job_id}")

    # --- Candidatures -------------------------------------------------------

    def list_applications(
        self, per_page: int = 50, page: int = 1, job_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Liste les candidatures. `status` : "active" | "rejected" | "hired"."""
        params: Dict[str, Any] = {"per_page": min(per_page, 500), "page": page}
        if job_id:
            params["job_id"] = job_id
        if status:
            params["status"] = status
        return self._request("GET", "/applications", params=params)

    def get_application(self, application_id: int) -> Dict[str, Any]:
        """Récupère une candidature par id."""
        return self._request("GET", f"/applications/{application_id}")

    # --- Users (recruteurs) -------------------------------------------------

    def list_users(self, per_page: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """Liste les utilisateurs Greenhouse (recruteurs) — pour `on_behalf_of`."""
        return self._request("GET", "/users",
                             params={"per_page": min(per_page, 500), "page": page})
