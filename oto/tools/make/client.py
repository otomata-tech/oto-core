"""Make (ex-Integromat) REST API v2 client — scénarios + exécutions.

Make est une plateforme d'automatisation de workflows (« scénarios »). L'API REST
v2 expose les organisations, équipes, scénarios, leur exécution et leurs logs.

Auth = **API token** (en-tête `Authorization: Token <token>`) + **base URL** de la
zone du compte (Make est régionalisé : `https://eu1.make.com`, `https://us1.make.com`,
`https://eu2.make.com`…). Le token se crée dans Make : Profile → API/MCP access →
Add token (scoper a minima `scenarios:read`/`scenarios:run`).

Les deux passés au constructeur (ou `MAKE_API_TOKEN` / `MAKE_BASE_URL` en fallback).

⚠️ Lister les scénarios exige un `team_id` (les scénarios appartiennent à une équipe).
`list_organizations` puis `list_teams(organization_id)` permettent de le découvrir.

Docs : https://developers.make.com/api-documentation

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...config import require_secret


class MakeClient:
    """Client Make — organisations, équipes, scénarios, exécutions (API v2)."""

    def __init__(self, api_token: Optional[str] = None,
                 base_url: Optional[str] = None):
        """Initialise le client.

        Args:
            api_token: Make API token (ou env `MAKE_API_TOKEN`).
            base_url: URL de la zone, ex. `https://eu1.make.com` (ou env
                `MAKE_BASE_URL`). Le suffixe `/api/v2` est ajouté.
        """
        self.api_token = api_token or require_secret("MAKE_API_TOKEN")
        base = (base_url or require_secret("MAKE_BASE_URL")).rstrip("/")
        if base.endswith("/api/v2"):
            base = base[: -len("/api/v2")]
        self.base_url = base
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}/api/v2{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise Exception(f"Make HTTP {resp.status_code}: {body}")
        return resp.json() if resp.content else {}

    # --- Découverte (organisations / équipes) -------------------------------

    def list_organizations(self) -> Dict[str, Any]:
        """Liste les organisations accessibles avec ce token."""
        return self._request("GET", "/organizations")

    def list_teams(self, organization_id: int) -> Dict[str, Any]:
        """Liste les équipes d'une organisation (porteuses des scénarios)."""
        return self._request("GET", "/teams",
                             params={"organizationId": organization_id})

    # --- Scénarios ----------------------------------------------------------

    def list_scenarios(
        self,
        team_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Liste les scénarios d'une équipe (paginé).

        Args:
            team_id: identifiant de l'équipe (cf. `list_teams`).
        """
        params: Dict[str, Any] = {
            "teamId": team_id,
            "pg[limit]": min(limit, 100),
            "pg[offset]": offset,
        }
        return self._request("GET", "/scenarios", params=params)

    def get_scenario(self, scenario_id: int) -> Dict[str, Any]:
        """Récupère un scénario (métadonnées, planning, état)."""
        return self._request("GET", f"/scenarios/{scenario_id}")

    def get_scenario_blueprint(self, scenario_id: int) -> Dict[str, Any]:
        """Récupère le blueprint (structure des modules) d'un scénario."""
        return self._request("GET", f"/scenarios/{scenario_id}/blueprint")

    def run_scenario(
        self,
        scenario_id: int,
        data: Optional[Dict[str, Any]] = None,
        responsive: bool = True,
    ) -> Dict[str, Any]:
        """Déclenche l'exécution d'un scénario.

        Args:
            data: payload d'entrée passé au scénario (selon ses modules).
            responsive: attendre la fin de l'exécution (True) ou rendre la main
                immédiatement (False).
        """
        body: Dict[str, Any] = {"responsive": responsive}
        if data is not None:
            body["data"] = data
        return self._request("POST", f"/scenarios/{scenario_id}/run", json=body)

    # --- Exécutions / logs --------------------------------------------------

    def list_scenario_logs(
        self,
        scenario_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Liste les logs d'exécution d'un scénario (paginé)."""
        params: Dict[str, Any] = {
            "pg[limit]": min(limit, 100),
            "pg[offset]": offset,
        }
        return self._request("GET", f"/scenarios/{scenario_id}/logs",
                             params=params)
