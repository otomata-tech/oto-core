"""Teamtailor ATS API client.

Auth = **API key** dans l'en-tête `Authorization: Token token=<key>`, plus un
en-tête de version d'API obligatoire (`X-Api-Version`). Clé créée dans
Teamtailor : Settings → API keys (Admin). Passée en clair au constructeur (ou
`TEAMTAILOR_API_KEY` en fallback).

L'API suit la convention **JSON:API** : les ressources ont `{type, id,
attributes, relationships}` et le filtrage passe par `filter[...]`, la
pagination par `page[number]`/`page[size]`. Les helpers exposent une surface
simple (candidats, jobs, candidatures) ; `call` reste l'échappatoire générique.

Docs : https://docs.teamtailor.com/

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...config import require_secret

# Version d'API Teamtailor figée (en-tête obligatoire). À bumper consciemment.
_API_VERSION = "20210218"


class TeamtailorClient:
    """Client Teamtailor v1 (JSON:API) — candidats, jobs, candidatures."""

    BASE_URL = "https://api.teamtailor.com/v1"

    def __init__(self, api_key: Optional[str] = None,
                 api_version: str = _API_VERSION):
        """Initialise le client.

        Args:
            api_key: Teamtailor API key (ou env `TEAMTAILOR_API_KEY`).
            api_version: valeur de l'en-tête `X-Api-Version` (date figée).
        """
        self.api_key = api_key or require_secret("TEAMTAILOR_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token token={self.api_key}",
            "X-Api-Version": api_version,
            "Content-Type": "application/vnd.api+json",
        })

    def call(self, method: str, path: str, **kwargs) -> Any:
        """Appel brut JSON:API (échappatoire générique). `path` commence par `/`."""
        url = f"{self.BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise Exception(f"Teamtailor HTTP {resp.status_code}: {body}")
        return resp.json() if resp.content else {}

    @staticmethod
    def _page(page_size: int, page_number: int,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "page[size]": min(page_size, 30), "page[number]": page_number,
        }
        if extra:
            params.update(extra)
        return params

    # --- Candidats ----------------------------------------------------------

    def list_candidates(
        self, page_size: int = 30, page_number: int = 1,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les candidats (paginé). `email` filtre par email exact."""
        extra = {"filter[email]": email} if email else None
        return self.call("GET", "/candidates",
                        params=self._page(page_size, page_number, extra))

    def get_candidate(self, candidate_id: str) -> Dict[str, Any]:
        """Récupère un candidat par id."""
        return self.call("GET", f"/candidates/{candidate_id}")

    def create_candidate(self, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Crée un candidat.

        Args:
            attributes: attributs JSON:API (`first-name`, `last-name`, `email`,
                `phone`, `pitch`, `tags`, …). Encapsulés en `{data:{type, attributes}}`.
        """
        body = {"data": {"type": "candidates", "attributes": attributes}}
        return self.call("POST", "/candidates", json=body)

    # --- Jobs ---------------------------------------------------------------

    def list_jobs(
        self, page_size: int = 30, page_number: int = 1,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les jobs (postes). `status` : "open" | "draft" | "archived" |
        "unlisted"."""
        extra = {"filter[status]": status} if status else None
        return self.call("GET", "/jobs",
                        params=self._page(page_size, page_number, extra))

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Récupère un job par id."""
        return self.call("GET", f"/jobs/{job_id}")

    # --- Candidatures -------------------------------------------------------

    def list_job_applications(
        self, page_size: int = 30, page_number: int = 1,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les candidatures. `job_id` filtre par poste."""
        extra = {"filter[job-id]": job_id} if job_id else None
        return self.call("GET", "/job-applications",
                        params=self._page(page_size, page_number, extra))
