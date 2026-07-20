"""Zoho Analytics API v2 client — https://www.zoho.com/analytics/api/v2/

Lecture des données d'un workspace : métadonnées (workspaces, vues) + export
des données d'une vue (synchrone) et exécution de requêtes SQL SELECT (flux
d'export asynchrone : create job → poll → download).

OAuth2 self-client (client_id/client_secret/refresh_token), même mécanique de
token que Zoho CRM. Toutes les requêtes portent l'en-tête `ZANALYTICS-ORGID`.
"""

import hashlib
import json
import time
from typing import Any, Optional

import requests

from ...config import require_secret, get_secret
from ..common import raise_for_upstream
from ..zoho import ZohoAuthError

# Cache de token PROCESS-WIDE, keyé par credential (hash de accounts_url|client_id|
# refresh_token) — #233. Le token Zoho vit ~1h, mais côté serveur une NOUVELLE
# instance de client est créée à CHAQUE appel MCP → un cache d'INSTANCE seul refait
# un refresh à chaque appel → rate-limit Zoho sur /oauth/v2/token (400 intermittent).
# Le cache partagé survit aux instances. Clé = HASH du refresh_token (secret) →
# isolation entre credentials/utilisateurs, jamais de secret en clair comme clé.
_TOKEN_CACHE: dict[str, "tuple[str, float]"] = {}


class ZohoAnalyticsClient:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        org_id: Optional[str] = None,
        api_domain: Optional[str] = None,
        accounts_url: Optional[str] = None,
    ):
        """Initialise le client.

        Credentials passés explicitement (usage serveur multi-utilisateur) ou
        résolus via `require_secret` (usage CLI). Le token d'accès est mis en cache
        **en mémoire de process**, keyé par credential (`_TOKEN_CACHE`) — jamais sur
        disque, jamais partagé entre credentials distincts (clé = hash du secret).
        """
        self.client_id = client_id or require_secret("ZOHO_ANALYTICS_CLIENT_ID")
        self.client_secret = client_secret or require_secret("ZOHO_ANALYTICS_CLIENT_SECRET")
        self.refresh_token = refresh_token or require_secret("ZOHO_ANALYTICS_REFRESH_TOKEN")
        self.org_id = org_id or require_secret("ZOHO_ANALYTICS_ORG_ID")
        self.api_domain = api_domain or get_secret(
            "ZOHO_ANALYTICS_API_DOMAIN", "https://analyticsapi.zoho.com")
        self.accounts_url = accounts_url or get_secret(
            "ZOHO_ANALYTICS_ACCOUNTS_URL", "https://accounts.zoho.com")
        self._cred_key = hashlib.sha256(
            f"{self.accounts_url}|{self.client_id}|{self.refresh_token}".encode()
        ).hexdigest()

    # --- Auth ---

    def _get_access_token(self) -> str:
        """Token d'accès valide, rafraîchi au besoin. Cache PROCESS-WIDE keyé par
        credential (#233) : une nouvelle instance de client par appel serveur ne
        re-refresh PAS si un token valide est déjà en cache (sinon rate-limit Zoho)."""
        cached = _TOKEN_CACHE.get(self._cred_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]

        resp = requests.post(
            f"{self.accounts_url}/oauth/v2/token",
            params={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
        )
        resp.raise_for_status()
        token_data = resp.json()
        if "error" in token_data:
            raise ZohoAuthError(f"Zoho OAuth error: {token_data['error']}")

        token = token_data["access_token"]
        _TOKEN_CACHE[self._cred_key] = (
            token, time.time() + token_data.get("expires_in", 3600))
        return token

    def _invalidate_token(self):
        _TOKEN_CACHE.pop(self._cred_key, None)

    # --- HTTP ---

    def _headers(self) -> dict:
        return {
            "Authorization": f"Zoho-oauthtoken {self._get_access_token()}",
            "ZANALYTICS-ORGID": self.org_id,
        }

    def _request(self, method: str, url: str, *, parse_json: bool = True, **kwargs) -> Any:
        """Requête authentifiée avec refresh du token sur 401 et backoff sur 429.

        `url` est absolu (les endpoints Analytics mélangent `/restapi/v2/…` et
        des URL de download déjà pleines renvoyées par l'API)."""
        headers = self._headers()
        for attempt in range(3):
            resp = requests.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 401 and attempt == 0:
                self._invalidate_token()
                headers = self._headers()
                continue
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 2)))
                continue

            raise_for_upstream(resp, service="zohoanalytics")

            if not resp.content:
                return {}
            if parse_json:
                return resp.json()
            return resp.text

        raise Exception("Request failed after retries")

    def _v2(self, endpoint: str) -> str:
        return f"{self.api_domain}/restapi/v2/{endpoint}"

    # --- Métadonnées ---

    def list_workspaces(self) -> dict:
        """List all workspaces accessible to the user (owned + shared)."""
        return self._request("GET", self._v2("workspaces"))

    def list_views(
        self, workspace_id: str, view_types: Optional[list[int]] = None,
    ) -> dict:
        """List views of a workspace. `view_types` filtre par code Zoho
        (0 Table, 2 Chart, 3 Pivot, 4 Summary, 6 QueryTable, 7 Dashboard)."""
        params = {}
        if view_types:
            params["CONFIG"] = json.dumps({"viewTypes": view_types})
        return self._request(
            "GET", self._v2(f"workspaces/{workspace_id}/views"), params=params)

    def get_view_details(self, view_id: str, *, with_meta: bool = True) -> dict:
        """Get metadata of one view (columns, type, folder…).

        L'API v2 keye le détail d'une vue sur le `view_id` **globalement unique** —
        l'endpoint est `/restapi/v2/views/<view-id>`, **PAS** imbriqué sous le
        workspace : un GET sur `/workspaces/<ws>/views/<view-id>` renvoie
        `INVALID_METHOD` (errorCode 8541), ce chemin n'accepte pas GET.
        `with_meta` (CONFIG `withInvolvedMetaInfo`) ramène le détail des colonnes
        + vues impliquées — sinon on n'obtient que l'entête de la vue."""
        params = {}
        if with_meta:
            params["CONFIG"] = json.dumps({"withInvolvedMetaInfo": True})
        return self._request("GET", self._v2(f"views/{view_id}"), params=params)

    # --- Export des données ---

    def export_view(
        self,
        workspace_id: str,
        view_id: str,
        response_format: str = "json",
        criteria: Optional[str] = None,
        selected_columns: Optional[list[str]] = None,
    ) -> Any:
        """Export synchrone des données d'une vue.

        `response_format` ∈ csv/json/xml/xls/pdf/html/image. `criteria` = filtre
        SQL-like Zoho (ex. `"Sales" > 500`). Renvoie le JSON parsé pour `json`,
        sinon le texte brut."""
        config: dict[str, Any] = {"responseFormat": response_format}
        if criteria:
            config["criteria"] = criteria
        if selected_columns:
            config["selectedColumns"] = selected_columns
        return self._request(
            "GET",
            self._v2(f"workspaces/{workspace_id}/views/{view_id}/data"),
            params={"CONFIG": json.dumps(config)},
            parse_json=(response_format == "json"),
        )

    def query_sql(
        self,
        workspace_id: str,
        sql_query: str,
        response_format: str = "json",
        poll_interval: float = 1.5,
        max_polls: int = 40,
    ) -> Any:
        """Exécute une requête SQL SELECT sur un workspace via le flux d'export
        asynchrone (create job → poll jusqu'à `JOB COMPLETED` → download).

        Renvoie le JSON parsé pour `json`, sinon le texte brut. Lève si le job
        échoue ou n'aboutit pas dans `max_polls` itérations."""
        created = self._request(
            "GET",
            self._v2(f"bulk/workspaces/{workspace_id}/data"),
            params={"CONFIG": json.dumps(
                {"responseFormat": response_format, "sqlQuery": sql_query})},
        )
        job_id = created.get("data", {}).get("jobId")
        if not job_id:
            raise ValueError(f"Zoho Analytics: no jobId in create-export response: {created}")

        job_url = self._v2(f"bulk/workspaces/{workspace_id}/exportjobs/{job_id}")
        for _ in range(max_polls):
            info = self._request("GET", job_url).get("data", {})
            status = info.get("jobStatus", "")
            if status == "JOB COMPLETED":
                download_url = info.get("downloadUrl")
                if not download_url:
                    raise ValueError(
                        f"Zoho Analytics: job completed without downloadUrl: {info}")
                return self._request(
                    "GET", download_url, parse_json=(response_format == "json"))
            if status in ("JOB FAILED", "JOB REPEATED"):
                raise ValueError(f"Zoho Analytics export job {job_id} failed: {info}")
            time.sleep(poll_interval)

        raise TimeoutError(
            f"Zoho Analytics export job {job_id} not done after {max_polls} polls")
