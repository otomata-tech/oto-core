"""Zoho Desk API Client — https://desk.zoho.com/DeskAPIDocument

OAuth scopes required (combine with comma at token-generation time):
  Desk.tickets.ALL
  Desk.contacts.READ
  Desk.basic.READ
  Desk.settings.READ
  Desk.articles.READ          (Help Center / KB articles)

Secrets expected in environment / ~/.otomata/secrets.env:
  ZOHO_DESK_CLIENT_ID
  ZOHO_DESK_CLIENT_SECRET
  ZOHO_DESK_REFRESH_TOKEN
  ZOHO_DESK_ORG_ID            (header `orgId` — OPTIONAL: a mono-portal token resolves
                               the portal on its own; sent only when provided)
  ZOHO_DESK_API_DOMAIN        (default https://desk.zoho.com — use .eu / .in if applicable)
  ZOHO_DESK_ACCOUNTS_URL      (default https://accounts.zoho.com — must match the data center)
"""

import time
from typing import Any, Optional

import requests

from ...config import require_secret, get_secret
from ..common import raise_for_upstream
from ..common.errors import UpstreamHTTPError
from ..zoho.auth import ZohoAuthError, cred_key, get_access_token, invalidate


# Endpoint Desk → scope OAuth qui le débloque. Zoho répond `403 SCOPE_MISMATCH` sans
# JAMAIS dire quel scope manque — or c'est la seule information utile : le remède est de
# régénérer le self-client avec ce scope. Un token Desk peut très bien authentifier avec
# des scopes PARTIELS (cas vécu : les articles répondaient 200 pendant que tickets,
# contacts et départements rendaient un 403 opaque). Signal d'usage #299.
_SCOPE_BY_PREFIX = (
    ("tickets/search", "Desk.search.READ"),
    ("tickets", "Desk.tickets.{rw}"),
    ("contacts", "Desk.contacts.{rw}"),
    ("articles", "Desk.articles.READ"),
    ("departments", "Desk.basic.READ"),
    ("organizations", "Desk.basic.READ"),
)


def _required_scope(endpoint: str, method: str) -> Optional[str]:
    """Scope attendu pour `endpoint`, ou None si l'endpoint n'est pas cartographié."""
    path = (endpoint or "").split("?", 1)[0].lstrip("/")
    rw = "READ" if method.upper() in ("GET", "HEAD") else "WRITE"
    for prefix, scope in _SCOPE_BY_PREFIX:
        if path.startswith(prefix):
            return scope.format(rw=rw)
    return None


class ZohoDeskClient:
    API_VERSION = "v1"

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

        Credentials passés explicitement (serveur multi-user) ou résolus via
        `require_secret` (CLI). Token d'accès caché **en mémoire** sur
        l'instance — jamais sur un fichier partagé (fuite cross-user)."""
        self.client_id = client_id or require_secret("ZOHO_DESK_CLIENT_ID")
        self.client_secret = client_secret or require_secret("ZOHO_DESK_CLIENT_SECRET")
        # FACULTATIF à la construction : en mode server-based il est obtenu par le
        # flux de consentement, pas collé. Son absence est signalée au moment
        # du refresh (message actionnable) plutôt que par une erreur de config.
        self.refresh_token = refresh_token or get_secret("ZOHO_DESK_REFRESH_TOKEN", None)
        # org_id (en-tête `orgId`) est OPTIONNEL : les endpoints KB articles
        # résolvent le portail depuis le token mono-org (vérifié empiriquement) →
        # pas de require_secret qui forcerait le champ. Fourni si un endpoint le
        # réclame (tickets…), omis de l'en-tête sinon.
        self.org_id = org_id or get_secret("ZOHO_DESK_ORG_ID", None)
        self.api_domain = api_domain or get_secret(
            "ZOHO_DESK_API_DOMAIN", "https://desk.zoho.com")
        self.accounts_url = accounts_url or get_secret(
            "ZOHO_DESK_ACCOUNTS_URL", "https://accounts.zoho.com")
        self._cred_key = cred_key(
            self.accounts_url, self.client_id, self.refresh_token)

    # --- Auth ---

    def _get_access_token(self) -> str:
        """Token d'accès valide, rafraîchi au besoin — cache process-wide keyé par
        credential (cf. `..zoho.auth`, #285)."""
        return get_access_token(self.accounts_url, self.client_id,
                                self.client_secret, self.refresh_token,
                                key=self._cred_key)

    def _invalidate_token(self):
        invalidate(self._cred_key)

    # --- HTTP ---

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.api_domain}/api/{self.API_VERSION}/{endpoint}"
        token = self._get_access_token()
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        if self.org_id:
            headers["orgId"] = self.org_id
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"

        for attempt in range(3):
            resp = requests.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 401 and attempt == 0:
                self._invalidate_token()
                token = self._get_access_token()
                headers["Authorization"] = f"Zoho-oauthtoken {token}"
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue

            # `403 SCOPE_MISMATCH` : nommer le scope manquant plutôt que de relayer le
            # code opaque de Zoho (cf. `_SCOPE_BY_PREFIX`). L'appelant sait alors quoi
            # régénérer, au lieu de deviner lequel des scopes `Desk.*` fait défaut.
            if resp.status_code == 403:
                try:
                    code = (resp.json() or {}).get("errorCode")
                except Exception:  # noqa: BLE001 — corps illisible : on relaie tel quel
                    code = None
                if code == "SCOPE_MISMATCH":
                    scope = _required_scope(endpoint, method)
                    detail = (f"il manque le scope `{scope}`" if scope
                              else "il manque un scope `Desk.*` pour cet appel")
                    raise UpstreamHTTPError(
                        403,
                        f"Zoho Desk refuse cet appel : {detail}. Régénère le self-client "
                        f"avec ce scope (console api-console.zoho.com), puis rééchange le "
                        f"grant token contre un refresh token.",
                        service="zohodesk",
                    )

            raise_for_upstream(resp, service="zohodesk")

            return resp.json() if resp.content else {}

        raise Exception("Request failed after retries")

    # --- Tickets ---

    def list_tickets(
        self,
        from_index: int = 1,
        limit: int = 50,
        department_id: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
        fields: Optional[str] = None,
    ) -> dict:
        """List tickets. status: Open / On Hold / Escalated / Closed."""
        params: dict[str, Any] = {"from": from_index, "limit": min(limit, 100)}
        if department_id:
            params["departmentId"] = department_id
        if status:
            params["status"] = status
        if sort_by:
            params["sortBy"] = sort_by
        if fields:
            params["fields"] = fields
        return self._request("GET", "tickets", params=params)

    def get_ticket(self, ticket_id: str, include: Optional[str] = None) -> dict:
        """Get a single ticket. include: contacts,products,assignee,team,..."""
        params = {"include": include} if include else None
        return self._request("GET", f"tickets/{ticket_id}", params=params)

    def create_ticket(self, data: dict) -> dict:
        """Create a ticket. Required: subject, departmentId, contactId (or contact dict)."""
        return self._request("POST", "tickets", json=data)

    def update_ticket(self, ticket_id: str, data: dict) -> dict:
        """Patch ticket fields (status, priority, assignee, customFields, ...)."""
        return self._request("PATCH", f"tickets/{ticket_id}", json=data)

    def delete_ticket(self, ticket_id: str) -> dict:
        """Move a single ticket to trash (uses the bulk endpoint with one id)."""
        return self._request("POST", "tickets/moveToTrash", json={"ticketIds": [ticket_id]})

    def move_tickets_to_trash(self, ticket_ids: list[str]) -> dict:
        """Move multiple tickets to trash in one call (max 50 per Zoho)."""
        return self._request("POST", "tickets/moveToTrash", json={"ticketIds": ticket_ids})

    def search_tickets(self, query: dict, from_index: int = 1, limit: int = 50) -> dict:
        """Search tickets via /tickets/search. query: dict of field=value pairs."""
        params: dict[str, Any] = {"from": from_index, "limit": min(limit, 100), **query}
        return self._request("GET", "tickets/search", params=params)

    # --- Threads (replies / comments on a ticket) ---

    def list_threads(self, ticket_id: str) -> dict:
        return self._request("GET", f"tickets/{ticket_id}/threads")

    def get_thread(self, ticket_id: str, thread_id: str) -> dict:
        return self._request("GET", f"tickets/{ticket_id}/threads/{thread_id}")

    # --- Contacts ---

    def list_contacts(self, from_index: int = 1, limit: int = 50) -> dict:
        params = {"from": from_index, "limit": min(limit, 100)}
        return self._request("GET", "contacts", params=params)

    def get_contact(self, contact_id: str) -> dict:
        return self._request("GET", f"contacts/{contact_id}")

    def create_contact(self, data: dict) -> dict:
        """Create a contact. Required: lastName. Optional: firstName, email, phone, accountId."""
        return self._request("POST", "contacts", json=data)

    def search_contacts(self, query: dict) -> dict:
        return self._request("GET", "contacts/search", params=query)

    # --- Departments ---

    def list_departments(self) -> dict:
        return self._request("GET", "departments")

    # --- Agents ---

    def list_agents(self) -> dict:
        return self._request("GET", "agents")

    # --- Articles (Help Center / Knowledge Base) ---

    def list_articles(
        self,
        from_index: int = 1,
        limit: int = 50,
        department_id: Optional[str] = None,
        category_id: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """List Help Center articles (metadata only — the HTML body comes from
        `get_article`). status: Published / Draft / Review / Expired.
        sortBy: e.g. modifiedTime, createdTime, viewCount (prefix "-" for desc)."""
        params: dict[str, Any] = {"from": from_index, "limit": min(limit, 100)}
        if department_id:
            params["departmentId"] = department_id
        if category_id:
            params["categoryId"] = category_id
        if status:
            params["status"] = status
        if sort_by:
            params["sortBy"] = sort_by
        return self._request("GET", "articles", params=params)

    def get_article(self, article_id: str) -> dict:
        """Get a single article, including its full HTML body (`answer`)."""
        return self._request("GET", f"articles/{article_id}")
