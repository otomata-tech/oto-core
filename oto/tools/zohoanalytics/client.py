"""Zoho Analytics API v2 client — https://www.zoho.com/analytics/api/v2/

Lecture des données d'un workspace : métadonnées (workspaces, vues) + export
des données d'une vue (synchrone) et exécution de requêtes SQL SELECT (flux
d'export asynchrone : create job → poll → download).

OAuth2 self-client (client_id/client_secret/refresh_token), même mécanique de
token que Zoho CRM. Toutes les requêtes portent l'en-tête `ZANALYTICS-ORGID`.
"""

import json
import time
from typing import Any, Optional

import requests

from ...config import require_secret, get_secret
from ..common import raise_for_upstream
from ..zoho.auth import ZohoAuthError, cred_key, get_access_token, invalidate

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


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
        # FACULTATIF à la construction : en mode server-based il est obtenu par le
        # flux de consentement, pas collé. Son absence est signalée au moment
        # du refresh (message actionnable) plutôt que par une erreur de config.
        self.refresh_token = refresh_token or get_secret("ZOHO_ANALYTICS_REFRESH_TOKEN", None)
        # FACULTATIF à la construction, comme `refresh_token` et pour la même raison :
        # en mode server-based, l'organisation n'est connue qu'APRÈS le consentement —
        # c'est `list_orgs()` qui la découvre. Son absence est signalée au moment du
        # premier appel qui en a besoin (message actionnable), pas à la construction.
        self.org_id = org_id or get_secret("ZOHO_ANALYTICS_ORG_ID", None)
        self.api_domain = api_domain or get_secret(
            "ZOHO_ANALYTICS_API_DOMAIN", "https://analyticsapi.zoho.com")
        self.accounts_url = accounts_url or get_secret(
            "ZOHO_ANALYTICS_ACCOUNTS_URL", "https://accounts.zoho.com")
        self._cred_key = cred_key(
            self.accounts_url, self.client_id, self.refresh_token)

    # --- Auth ---

    def _get_access_token(self) -> str:
        """Token d'accès valide, rafraîchi au besoin. Cache PROCESS-WIDE keyé par
        credential (#233) : une nouvelle instance de client par appel serveur ne
        re-refresh PAS si un token valide est déjà en cache (sinon rate-limit Zoho)."""
        return get_access_token(self.accounts_url, self.client_id,
                                self.client_secret, self.refresh_token,
                                key=self._cred_key)

    def _invalidate_token(self):
        invalidate(self._cred_key)

    # --- HTTP ---

    def _auth_headers(self) -> dict:
        """Authentification seule. Un seul endpoint s'en contente — `list_orgs`, qui
        sert justement à découvrir l'organisation qu'on ne connaît pas encore."""
        return {"Authorization": f"Zoho-oauthtoken {self._get_access_token()}"}

    def _headers(self) -> dict:
        if not self.org_id:
            raise ValueError(
                "Org ID Zoho Analytics manquant : chaque requête porte l'en-tête "
                "ZANALYTICS-ORGID. Découvre les organisations du compte avec "
                "`list_orgs()`, puis renseigne celle qui porte tes workspaces.")
        return {**self._auth_headers(), "ZANALYTICS-ORGID": self.org_id}

    def _request(self, method: str, url: str, *, parse_json: bool = True,
                 with_org: bool = True, **kwargs) -> Any:
        """Requête authentifiée avec refresh du token sur 401 et backoff sur 429.

        `url` est absolu (les endpoints Analytics mélangent `/restapi/v2/…` et
        des URL de download déjà pleines renvoyées par l'API).

        `with_org=False` omet l'en-tête d'organisation — réservé à `list_orgs`, seul
        endpoint qui répond sans savoir dans quelle organisation chercher."""
        build = self._headers if with_org else self._auth_headers
        headers = build()
        for attempt in range(3):
            resp = requests.request(method, url, headers=headers, timeout=_HTTP_TIMEOUT, **kwargs)

            if resp.status_code == 401 and attempt == 0:
                self._invalidate_token()
                headers = build()
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

    def list_orgs(self) -> list[dict]:
        """Organisations Analytics visibles par ce compte : `{org_id, name, role}`.

        Le SEUL endpoint qui ne réclame pas `ZANALYTICS-ORGID` — d'où son intérêt :
        après un consentement OAuth, il permet de renseigner l'organisation au lieu
        d'envoyer l'utilisateur chercher un identifiant à onze chiffres dans
        l'interface Zoho.

        ⚠️ **Un compte en voit souvent PLUSIEURS** (workspaces partagés), et la
        réponse ne désigne aucune organisation par défaut. Au-delà d'une seule, c'est
        donc un CHOIX à faire faire — pas à deviner : sur le premier compte réel
        testé, deux organisations remontaient, et prendre « la première » aurait
        désigné la mauvaise."""
        payload = self._request("GET", self._v2("orgs"), with_org=False)
        orgs = (payload or {}).get("data", {}).get("orgs") or []
        return [{"org_id": str(o.get("orgId")), "name": o.get("orgName"),
                 "role": o.get("role")} for o in orgs if o.get("orgId")]

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
