"""Ashby ATS API client.

Auth = **API key** en Basic auth (la clé est le *username*, mot de passe vide).
Créée dans Ashby : Settings → Integrations → Ashby API. Passée en clair au
constructeur (ou `ASHBY_API_KEY` en fallback).

Particularité Ashby : **tout est POST** sur des endpoints RPC (`candidate.list`,
`candidate.info`, `job.list`, …), le corps JSON porte les paramètres. La
pagination se fait par `cursor` (curseur de page suivante dans
`nextCursor` quand `moreDataAvailable` est vrai).

Docs : https://developers.ashbyhq.com/

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret


class AshbyClient:
    """Client Ashby — RPC POST (candidate.*, job.*, application.*)."""

    BASE_URL = "https://api.ashbyhq.com"

    def __init__(self, api_key: Optional[str] = None):
        """Initialise le client.

        Args:
            api_key: Ashby API key (ou env `ASHBY_API_KEY`).
        """
        self.api_key = api_key or require_secret("ASHBY_API_KEY")
        self.session = requests.Session()
        self.session.auth = (self.api_key, "")
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def call(self, endpoint: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Appel RPC brut (POST `endpoint`). Échappatoire pour tout endpoint Ashby
        non couvert par un helper. Lève si `success` est faux."""
        url = f"{self.BASE_URL}/{endpoint}"
        resp = self.session.post(url, json=body or {}, timeout=30)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            raise Exception(f"Ashby HTTP {resp.status_code}: {payload}")
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("success") is False:
            raise Exception(f"Ashby error: {data.get('errors') or data}")
        return data

    # --- Candidats ----------------------------------------------------------

    def list_candidates(
        self, limit: int = 50, cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les candidats (paginé). `cursor` = `nextCursor` de la page
        précédente. Renvoie `{results, moreDataAvailable, nextCursor}`."""
        body: Dict[str, Any] = {"limit": min(limit, 100)}
        if cursor:
            body["cursor"] = cursor
        return self.call("candidate.list", body)

    def get_candidate(self, candidate_id: str) -> Dict[str, Any]:
        """Récupère un candidat par id (`candidate.info`)."""
        return self.call("candidate.info", {"id": candidate_id})

    def search_candidates(
        self, email: Optional[str] = None, name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Recherche de candidats par `email` et/ou `name` (`candidate.search`)."""
        body: Dict[str, Any] = {}
        if email:
            body["email"] = email
        if name:
            body["name"] = name
        return self.call("candidate.search", body)

    def add_note(self, candidate_id: str, note: str) -> Dict[str, Any]:
        """Ajoute une note à un candidat (`candidate.createNote`)."""
        return self.call("candidate.createNote",
                         {"candidateId": candidate_id, "note": note})

    # --- Jobs ---------------------------------------------------------------

    def list_jobs(
        self, limit: int = 50, cursor: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les jobs (`job.list`). `status` : "Open" | "Closed" | "Draft" |
        "Archived"."""
        body: Dict[str, Any] = {"limit": min(limit, 100)}
        if cursor:
            body["cursor"] = cursor
        if status:
            body["status"] = status
        return self.call("job.list", body)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Récupère un job par id (`job.info`)."""
        return self.call("job.info", {"id": job_id})

    # --- Candidatures -------------------------------------------------------

    def list_applications(
        self, limit: int = 50, cursor: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les candidatures (`application.list`), filtrable par `job_id`."""
        body: Dict[str, Any] = {"limit": min(limit, 100)}
        if cursor:
            body["cursor"] = cursor
        if job_id:
            body["jobId"] = job_id
        return self.call("application.list", body)

    def get_application(self, application_id: str) -> Dict[str, Any]:
        """Récupère une candidature par id (`application.info`)."""
        return self.call("application.info", {"id": application_id})
