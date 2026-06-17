"""Unipile API client — hosted LinkedIn search / scrape / messaging.

Requires: requests

Secrets (résolus via oto.config) :
- UNIPILE_API_KEY            (requis) — clé X-API-KEY du compte Unipile
- UNIPILE_DSN                (def. api25.unipile.com:15555) — host:port de l'instance
- UNIPILE_LINKEDIN_ACCOUNT_ID (optionnel) — sinon, 1er compte LINKEDIN connecté

Doctrine de surface : un seul atome de recherche (`search`) qui résout lui-même
les facettes employeur/localisation par nom — la page company LinkedIn (ex.
79066705) n'est PAS un id de facette people-search ; il faut passer par
/linkedin/search/parameters. Le client masque ce gotcha.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests

from ...config import get_secret, require_secret

DEFAULT_DSN = "api25.unipile.com:15555"


class UnipileError(RuntimeError):
    """Erreur API Unipile, message remonté tel quel."""


class UnipileClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        dsn: Optional[str] = None,
        account_id: Optional[str] = None,
    ):
        self.api_key = api_key or require_secret("UNIPILE_API_KEY")
        self.dsn = dsn or get_secret("UNIPILE_DSN", DEFAULT_DSN)
        self.base_url = f"https://{self.dsn}/api/v1"
        self._account_id = account_id or get_secret("UNIPILE_LINKEDIN_ACCOUNT_ID")
        self.session = requests.Session()
        self.session.headers.update(
            {"X-API-KEY": self.api_key, "accept": "application/json"}
        )

    # ---- transport -------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, params=params, json=json)
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("detail") or body.get("message") or resp.text
            except ValueError:
                msg = resp.text or f"{resp.status_code} {resp.reason}"
            raise UnipileError(f"Unipile {resp.status_code}: {msg}")
        if not resp.text:
            return None
        return resp.json()

    # ---- accounts --------------------------------------------------------

    def list_accounts(self) -> list[dict]:
        """Comptes connectés (LinkedIn, etc.)."""
        return self._request("GET", "/accounts").get("items", [])

    def account_id(self) -> str:
        """Id du compte LinkedIn à utiliser : configuré, sinon 1er LINKEDIN."""
        if self._account_id:
            return self._account_id
        for acc in self.list_accounts():
            if acc.get("type") == "LINKEDIN":
                self._account_id = acc["id"]
                return self._account_id
        raise UnipileError(
            "Aucun compte LinkedIn connecté sur Unipile "
            "(et UNIPILE_LINKEDIN_ACCOUNT_ID non défini)."
        )

    # ---- hosted auth (connexion d'un compte LinkedIn par l'utilisateur) ---

    def hosted_auth_link(
        self,
        notify_url: Optional[str] = None,
        providers: Optional[list[str]] = None,
        name: Optional[str] = None,
        success_redirect_url: Optional[str] = None,
        failure_redirect_url: Optional[str] = None,
        ttl_minutes: int = 60,
    ) -> str:
        """Génère une URL d'auth hébergée Unipile (Hosted Auth Wizard).

        L'utilisateur l'ouvre, se connecte à son compte (LinkedIn par défaut) sur
        la page Unipile — 2FA/checkpoints gérés par Unipile — et au succès Unipile
        crée le compte et **POST le résultat sur `notify_url`** (webhook) avec
        l'`account_id`. Ne consomme PAS de credential côté oto : c'est notre clé
        (compte/abonnement) qui autorise la génération du lien.

        Args:
            notify_url: webhook qui recevra `{account_id, name, status}` au succès.
            providers: comptes autorisés (défaut `["LINKEDIN"]`).
            name: identifiant libre rattaché au compte (souvent le sub de l'user).
            success_redirect_url / failure_redirect_url: redirections post-flow.
            ttl_minutes: durée de validité du lien.

        Returns:
            L'URL hébergée (`https://account.unipile.com/...`).
        """
        expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        body: dict[str, Any] = {
            "type": "create",
            "providers": providers or ["LINKEDIN"],
            "api_url": f"https://{self.dsn}",
            "expiresOn": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if notify_url:
            body["notify_url"] = notify_url
        if name:
            body["name"] = name
        if success_redirect_url:
            body["success_redirect_url"] = success_redirect_url
        if failure_redirect_url:
            body["failure_redirect_url"] = failure_redirect_url
        data = self._request("POST", "/hosted/accounts/link", json=body)
        return (data or {}).get("url", "")

    # ---- facettes (employeur / localisation) -----------------------------

    def resolve_facet(self, facet_type: str, keywords: str) -> list[dict]:
        """Résout un nom en ids de facette LinkedIn.

        facet_type ∈ COMPANY | LOCATION | INDUSTRY | SCHOOL ...
        Retourne [{id, title}, ...]. La page company LinkedIn n'est pas
        forcément une facette employeur valide — utiliser CE résultat.
        """
        params = {
            "account_id": self.account_id(),
            "type": facet_type,
            "keywords": keywords,
        }
        data = self._request(
            "GET", "/linkedin/search/parameters", params=params
        )
        return [
            {"id": it.get("id"), "title": it.get("title")}
            for it in (data or {}).get("items", [])
        ]

    def _as_facet_ids(self, facet_type: str, values: Optional[list[str]]) -> list[str]:
        """Chaque valeur : déjà un id numérique → tel quel ; sinon résolue
        (1er match) en id de facette."""
        if not values:
            return []
        out: list[str] = []
        for v in values:
            v = str(v).strip()
            if v.isdigit():
                out.append(v)
                continue
            matches = self.resolve_facet(facet_type, v)
            if not matches:
                raise UnipileError(f"Facette {facet_type} introuvable pour : {v!r}")
            out.append(str(matches[0]["id"]))
        return out

    # ---- recherche -------------------------------------------------------

    def search(
        self,
        keywords: Optional[str] = None,
        category: str = "people",
        company: Optional[list[str]] = None,
        location: Optional[list[str]] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        """Recherche LinkedIn (classic). `company`/`location` acceptent des
        noms (résolus en facettes) ou des ids numériques.

        Retourne le payload Unipile brut (items + paging + cursor).
        """
        body: dict[str, Any] = {"api": "classic", "category": category}
        if keywords:
            body["keywords"] = keywords
        company_ids = self._as_facet_ids("COMPANY", company)
        location_ids = self._as_facet_ids("LOCATION", location)
        if company_ids:
            body["company"] = company_ids
        if location_ids:
            body["location"] = location_ids
        params = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        return self._request(
            "POST", "/linkedin/search", params=params, json=body
        )

    # ---- profils / sociétés ---------------------------------------------

    def get_profile(self, identifier: str, sections: str = "*") -> dict:
        """Profil complet (carrière, écoles, réseau). `identifier` = public
        identifier ou provider id. `sections="*"` = tout."""
        params = {"account_id": self.account_id(), "linkedin_sections": sections}
        return self._request("GET", f"/users/{quote(identifier, safe='')}", params=params)

    def get_company(self, identifier: str) -> dict:
        params = {"account_id": self.account_id()}
        return self._request(
            "GET", f"/linkedin/company/{quote(identifier, safe='')}", params=params
        )

    # ---- messagerie ------------------------------------------------------

    def list_chats(self, limit: int = 20, cursor: Optional[str] = None) -> dict:
        params: dict[str, Any] = {"account_id": self.account_id(), "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/chats", params=params)

    def list_messages(self, chat_id: str, limit: int = 50) -> dict:
        params = {"limit": limit}
        return self._request("GET", f"/chats/{chat_id}/messages", params=params)

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        attendee_id: Optional[str] = None,
    ) -> dict:
        """Envoie un message. `chat_id` → répond dans un fil existant ;
        sinon `attendee_id` (provider id du destinataire) → ouvre un nouveau fil.
        """
        if chat_id:
            return self._request(
                "POST", f"/chats/{chat_id}/messages", json={"text": text}
            )
        if not attendee_id:
            raise UnipileError("send_message : chat_id ou attendee_id requis.")
        body = {
            "account_id": self.account_id(),
            "attendees_ids": [attendee_id],
            "text": text,
        }
        return self._request("POST", "/chats", json=body)
