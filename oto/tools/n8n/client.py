"""n8n public REST API client — workflows + executions.

n8n est une plateforme d'automatisation de workflows (open source, self-hostée
OU n8n Cloud). L'**API publique** expose les workflows, leurs exécutions, les
credentials et les tags.

Auth = **API key** (en-tête `X-N8N-API-KEY`) + **base URL** de l'instance (le
self-hosting impose une URL propre — n8n Cloud : `https://<sub>.app.n8n.cloud`).
La clé se crée dans n8n : Settings → n8n API → Create an API key.

Les deux passés au constructeur (ou `N8N_API_KEY` / `N8N_BASE_URL` en fallback).

Docs : https://docs.n8n.io/api/

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...config import require_secret


class N8nClient:
    """Client n8n — workflows, exécutions, tags (API publique v1)."""

    def __init__(self, api_key: Optional[str] = None,
                 base_url: Optional[str] = None):
        """Initialise le client.

        Args:
            api_key: n8n API key (ou env `N8N_API_KEY`).
            base_url: URL de l'instance, ex. `https://acme.app.n8n.cloud`
                (ou env `N8N_BASE_URL`). Le suffixe `/api/v1` est ajouté.
        """
        self.api_key = api_key or require_secret("N8N_API_KEY")
        base = (base_url or require_secret("N8N_BASE_URL")).rstrip("/")
        # Tolère qu'on passe déjà l'URL avec /api/v1.
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        self.base_url = base
        self.session = requests.Session()
        self.session.headers.update({
            "X-N8N-API-KEY": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise Exception(f"n8n HTTP {resp.status_code}: {body}")
        return resp.json() if resp.content else {}

    # --- Workflows ----------------------------------------------------------

    def list_workflows(
        self,
        limit: int = 50,
        active: Optional[bool] = None,
        tags: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les workflows (paginé via `nextCursor`).

        Args:
            active: ne garder que les workflows actifs/inactifs.
            tags: liste de tags séparés par des virgules.
            cursor: curseur de pagination (`nextCursor` de la page précédente).
        """
        params: Dict[str, Any] = {"limit": min(limit, 250)}
        if active is not None:
            params["active"] = str(active).lower()
        if tags:
            params["tags"] = tags
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/workflows", params=params)

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Récupère un workflow (nodes, connections, settings)."""
        return self._request("GET", f"/workflows/{workflow_id}")

    def activate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Active un workflow (déclencheurs/cron mis en route)."""
        return self._request("POST", f"/workflows/{workflow_id}/activate")

    def deactivate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Désactive un workflow."""
        return self._request("POST", f"/workflows/{workflow_id}/deactivate")

    # --- Exécutions ---------------------------------------------------------

    def list_executions(
        self,
        limit: int = 50,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les exécutions (paginé).

        Args:
            workflow_id: filtre par workflow.
            status: `success` | `error` | `waiting`.
            cursor: curseur de pagination.
        """
        params: Dict[str, Any] = {"limit": min(limit, 250)}
        if workflow_id:
            params["workflowId"] = workflow_id
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/executions", params=params)

    def get_execution(self, execution_id: int,
                      include_data: bool = False) -> Dict[str, Any]:
        """Récupère une exécution. `include_data` inclut les données détaillées
        des nodes (volumineux)."""
        params = {"includeData": "true"} if include_data else None
        return self._request("GET", f"/executions/{execution_id}", params=params)

    # --- Tags ---------------------------------------------------------------

    def list_tags(self, limit: int = 50,
                  cursor: Optional[str] = None) -> Dict[str, Any]:
        """Liste les tags."""
        params: Dict[str, Any] = {"limit": min(limit, 250)}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/tags", params=params)
