"""Unipile API **v2** client — hosted LinkedIn search / scrape / messaging.

Pourquoi une classe séparée de `client.py` (v1) plutôt qu'un `if version` par
méthode : l'API v2 change **structurellement** (cf. `docs/api-v2` Unipile), pas
juste des noms :

- **base + version** : `https://{dsn}/v2` (v1 = `https://{dsn}/api/v1`).
- **`account_id` dans le PATH** (`/v2/{account_id}/…`), plus en query param — donc
  ne fuite plus dans les query strings, mais peut apparaître dans une URL d'erreur
  → on le **caviarde** dans les messages (`_sanitize`, feedback oto #178).
- **enveloppe de liste** `{data, total_count, next_cursor}` (v1 = `{items, cursor}`).
  On **normalise** chaque réponse de liste vers la forme v1 (`items`/`cursor`) EN
  PLUS de garder `data`/`next_cursor` → tout l'aval oto-mcp (feed sync, wrappers,
  attendus de l'agent) reste inchangé sans réécriture.
- **surface éclatée** : search people/companies séparés + par produit
  (classic/recruiter/sales-navigator) ; invitations = `users/me/relation-requests` ;
  attendees d'un fil = `participants` ; réactions de message sous le chat ;
  solde InMail = `inmail-credits` (corrige la 404 v1 `inmail/balance`, feedback #178).

Ce client **fold-in les fixes feedback** que la v2 seule ne donne pas :
- **garde anti-mismatch** identifier↔réponse sur `get_profile`/`get_company`
  (feedback #144-149/#153 : sous concurrence, l'API a rendu le profil d'un AUTRE
  membre / un CompanyProfile à la place). On vérifie que l'objet rendu correspond
  bien au type ET à l'identifiant demandés, sinon `UnipileError` **actionnable et
  retryable** — jamais de donnée fausse renvoyée en silence.
- **erreurs réseau propres** : les exceptions `requests` (DNS/timeout) sont mappées
  en `UnipileError` stable au lieu de fuiter `net::ERR_NAME_NOT_RESOLVED` (#177).

v2 est **beta** côté Unipile (nouveau compte + migration de données requis) : ce
client est **opt-in** (résolu par config/env côté oto-mcp), v1 reste le défaut.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

import requests

from ...config import get_secret, require_secret
from .client import DEFAULT_DSN, UnipileError, parse_feed

logger = logging.getLogger(__name__)

# Feed = même Magic Route Voyager que v1, exposée en v2 comme le proxy générique
# `POST /v2/{account_id}/linkedin/` (proxyRequest) : on relaie une requête Voyager
# brute. Le queryId GraphQL et le schéma restent ceux de v1 (parse_feed partagé).
FEED_QUERY_ID = "voyagerFeedDashMainFeed.7a50ef8ba5a7865c23ad5df46f735709"

# Préfixe de path par produit LinkedIn (search & co.).
_API_PREFIX = {
    "classic": "/linkedin/search",
    "sales_navigator": "/linkedin/sales-navigator/search",
    "recruiter": "/linkedin/recruiter/search",
}


def _sections_param(sections: str) -> Optional[list[str]]:
    """Map la valeur v1 `sections` vers le param v2 `with_sections`.

    v1 : `"*"` (tout) ou une liste séparée par virgules de noms nus
    (`experience`, `education`…). v2 : `with_sections=linkedin_<nom>` (et
    `linkedin_*` = tout). `"*"`/vide → None (défaut serveur = tout)."""
    s = (sections or "").strip()
    if not s or s in ("*", "linkedin_*"):
        return None
    out: list[str] = []
    for part in s.split(","):
        p = part.strip()
        if not p:
            continue
        out.append(p if p.startswith("linkedin_") else f"linkedin_{p}")
    return out or None


class UnipileClientV2:
    """Client Unipile API v2. Surface publique **identique** à `UnipileClient`
    (v1) pour que les wrappers oto-mcp (`tools/unipile.py`) restent inchangés."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        dsn: Optional[str] = None,
        account_id: Optional[str] = None,
    ):
        self.api_key = api_key or require_secret("UNIPILE_API_KEY")
        self.dsn = dsn or get_secret("UNIPILE_DSN", DEFAULT_DSN)
        self.base_url = f"https://{self.dsn}/v2"
        self._account_id = account_id or get_secret("UNIPILE_LINKEDIN_ACCOUNT_ID")
        self.session = requests.Session()
        self.session.headers.update(
            {"X-API-KEY": self.api_key, "accept": "application/json"}
        )

    # ---- transport -------------------------------------------------------

    def _sanitize(self, msg: str) -> str:
        """Caviarde l'account_id dans un message d'erreur (il vit dans le path v2 →
        remonterait sinon dans une URL 404, feedback #178)."""
        acct = self._account_id
        if acct and isinstance(msg, str):
            return msg.replace(acct, "<account>")
        return msg

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, params=params, json=json)
        except requests.RequestException as e:
            # DNS/timeout/reset : erreur stable au lieu de fuiter net::ERR_* (#177).
            raise UnipileError(
                self._sanitize(f"Unipile: erreur réseau ({type(e).__name__}).")
            ) from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("detail") or body.get("message") or resp.text
            except ValueError:
                msg = resp.text or f"{resp.status_code} {resp.reason}"
            raise UnipileError(self._sanitize(f"Unipile {resp.status_code}: {msg}"),
                               status_code=resp.status_code)
        if not resp.text:
            return None
        return resp.json()

    def _acct(self, sub_path: str) -> str:
        """Préfixe un sous-chemin par `/{account_id}` (path param v2)."""
        return f"/{quote(self.account_id(), safe='')}{sub_path}"

    @staticmethod
    def _norm(data: Any) -> Any:
        """Normalise une enveloppe de liste v2 `{data, next_cursor, total_count}`
        vers la forme v1 attendue par l'aval (`items`, `cursor`) SANS perdre les
        champs natifs. No-op si `data` n'est pas une enveloppe de liste."""
        if not isinstance(data, dict):
            return data
        if "data" in data and isinstance(data.get("data"), list):
            data.setdefault("items", data["data"])
        if "next_cursor" in data:
            data.setdefault("cursor", data.get("next_cursor"))
        return data

    # ---- accounts --------------------------------------------------------

    def list_accounts(self) -> list[dict]:
        data = self._request("GET", "/accounts")
        if isinstance(data, dict):
            return data.get("data") or data.get("items") or []
        return data or []

    def account_id(self) -> str:
        if self._account_id:
            return self._account_id
        for acc in self.list_accounts():
            if (acc.get("type") or acc.get("provider")) == "LINKEDIN":
                self._account_id = acc["id"]
                return self._account_id
        raise UnipileError(
            "Aucun compte LinkedIn connecté sur Unipile "
            "(et UNIPILE_LINKEDIN_ACCOUNT_ID non défini)."
        )

    # ---- hosted auth -----------------------------------------------------

    def hosted_auth_link(
        self,
        notify_url: Optional[str] = None,
        providers: Optional[list[str]] = None,
        name: Optional[str] = None,
        success_redirect_url: Optional[str] = None,
        failure_redirect_url: Optional[str] = None,
        ttl_minutes: int = 60,
    ) -> str:
        """URL d'auth hébergée (v2 : `POST /v2/auth/link`, createAuthLink)."""
        from datetime import datetime, timedelta, timezone

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
        data = self._request("POST", "/auth/link", json=body)
        return (data or {}).get("url", "")

    # ---- facettes --------------------------------------------------------

    def resolve_facet(
        self, facet_type: str, keywords: str, limit: int = 100
    ) -> list[dict]:
        """Résout un nom en ids de facette LinkedIn (v2 :
        `GET /v2/{account}/linkedin/search/parameters`)."""
        params = {"type": facet_type, "keywords": keywords, "limit": limit}
        data = self._request(
            "GET", self._acct("/linkedin/search/parameters"), params=params
        )
        items = (data or {}).get("data") or (data or {}).get("items") or []
        return [{"id": it.get("id"), "title": it.get("title")} for it in items]

    def _as_facet_ids(self, facet_type: str, values: Optional[list[str]]) -> list[str]:
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
        api: str = "classic",
        network_distance: Optional[list[int]] = None,
        url: Optional[str] = None,
        advanced_keywords: Optional[dict] = None,
        industry: Optional[dict] = None,
    ) -> dict:
        """Recherche LinkedIn (v2). `company`/`location`/`industry` = noms
        (résolus en facettes) ou ids numériques. Voir `UnipileClient.search`."""
        prefix = _API_PREFIX.get(api, _API_PREFIX["classic"])
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor

        # Recherche par URL collée : endpoint from-url du produit, corps {url}.
        if url:
            return self._norm(self._request(
                "POST", self._acct(prefix), params=params, json={"url": url}
            ))

        cat = "companies" if category == "companies" else "people"
        path = f"{prefix}/{cat}"
        body: dict[str, Any] = {}
        if keywords:
            body["keywords"] = keywords
        if advanced_keywords:
            ak = {k: v for k, v in advanced_keywords.items() if v}
            if ak:
                body["advanced_keywords"] = ak
        location_ids = self._as_facet_ids("LOCATION", location)
        if location_ids:
            body["location"] = location_ids
        if industry:
            inc = self._as_facet_ids("INDUSTRY", industry.get("include"))
            exc = self._as_facet_ids("INDUSTRY", industry.get("exclude"))
            if inc or exc:
                body["industry"] = inc + exc  # v2 : liste plate d'ids
        company_ids = self._as_facet_ids("COMPANY", company)
        if company_ids:
            # v2 people-search : `current_company` (v1 `company` était l'employeur
            # courant) ; companies-search n'a pas de filtre employeur.
            if cat == "people":
                body["current_company"] = company_ids
        if cat == "people" and network_distance:
            body["network_distance"] = [int(d) for d in network_distance]
        return self._norm(self._request(
            "POST", self._acct(path), params=params, json=body
        ))

    # ---- profils / sociétés (avec garde anti-mismatch #153) --------------

    @staticmethod
    def _identity_ok(requested: str, resp: dict, expect_object: str) -> bool:
        """True si `resp` correspond bien au type ET à l'identifiant demandés.
        Tolère slug↔id (compare requested à `public_identifier`, `id`,
        `provider_id`, insensible à la casse)."""
        if not isinstance(resp, dict):
            return False
        obj = resp.get("object")
        if obj and expect_object and obj != expect_object:
            return False  # ex. demandé UserProfile, reçu CompanyProfile (#148/#149)
        req = str(requested).strip().lower()
        cands = {
            str(resp.get(k, "")).strip().lower()
            for k in ("public_identifier", "id", "provider_id", "member_urn")
        }
        return req in cands if any(cands) else True  # pas d'id à comparer → on laisse

    def get_profile(self, identifier: str, sections: str = "*") -> dict:
        """Profil complet. `identifier` = public identifier (slug) ou provider id.

        Garde #153 : rejette une réponse qui ne correspond pas au membre demandé
        (mauvais appariement observé sous concurrence) → `UnipileError` retryable."""
        params: dict[str, Any] = {}
        secs = _sections_param(sections)
        if secs:
            params["with_sections"] = secs
        data = self._request(
            "GET", self._acct(f"/users/{quote(identifier, safe='')}"), params=params
        )
        if not self._identity_ok(identifier, data, "UserProfile"):
            got = (data or {}).get("public_identifier") or (data or {}).get("id")
            raise UnipileError(
                f"Unipile identity_mismatch: profil demandé {identifier!r}, "
                f"reçu {got!r} (object={(data or {}).get('object')!r}). "
                "Réponse rejetée — réessaie."
            )
        return data

    def get_company(self, identifier: str) -> dict:
        """Fiche société. Garde #153 (idem get_profile, côté CompanyProfile)."""
        data = self._request(
            "GET", self._acct(f"/linkedin/company/{quote(identifier, safe='')}")
        )
        if not self._identity_ok(identifier, data, "CompanyProfile"):
            got = (data or {}).get("public_identifier") or (data or {}).get("id")
            raise UnipileError(
                f"Unipile identity_mismatch: société demandée {identifier!r}, "
                f"reçu {got!r} (object={(data or {}).get('object')!r}). "
                "Réponse rejetée — réessaie."
            )
        return data

    # ---- messagerie ------------------------------------------------------

    def list_chats(self, limit: int = 20, cursor: Optional[str] = None,
                   with_attendee_names: bool = False) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._norm(self._request("GET", self._acct("/chats"), params=params))
        if with_attendee_names:
            self._annotate_chat_attendees(data)
        return data

    def resolve_attendee_names(self, provider_ids, max_pages: int = 10,
                               page_limit: int = 100) -> dict:
        """Résout des `attendee_provider_id` via le carnet de contacts v2
        (`/v2/{account}/contacts`, paginé). Best-effort (cf. v1)."""
        wanted = {str(p) for p in provider_ids if p}
        out: dict[str, dict] = {}
        cursor = None
        for _ in range(max_pages):
            if not wanted - out.keys():
                break
            page = self.list_attendees(cursor=cursor, limit=page_limit)
            items = (page or {}).get("items") or []
            for att in items:
                if not isinstance(att, dict):
                    continue
                pid = str(att.get("provider_id") or att.get("id") or "")
                if pid in wanted:
                    out[pid] = att
            cursor = (page or {}).get("cursor")
            if not items or not cursor:
                break
        return out

    def _annotate_chat_attendees(self, data: Any) -> None:
        """Enrichit in-place les fils d'un `/chats` avec le nom de l'interlocuteur.
        Best-effort : ne lève jamais (la liste prime sur l'enrichissement)."""
        items = (data or {}).get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        ids = {str(it.get("attendee_provider_id"))
               for it in items
               if isinstance(it, dict) and it.get("attendee_provider_id")}
        if not ids:
            return
        try:
            resolved = self.resolve_attendee_names(ids)
        except Exception:  # noqa: BLE001 — enrichissement best-effort voulu
            logger.warning("unipile v2 chats: résolution attendees échouée, "
                           "liste servie sans enrichissement", exc_info=True)
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            att = resolved.get(str(it.get("attendee_provider_id") or ""))
            if not att:
                continue
            it["attendee_name"] = att.get("name")
            it["attendee_headline"] = (att.get("specifics") or {}).get("occupation")
            it["attendee_profile_url"] = att.get("profile_url")

    def list_messages(self, chat_id: str, limit: int = 50) -> dict:
        params = {"limit": limit}
        return self._norm(self._request(
            "GET", self._acct(f"/chats/{quote(chat_id, safe='')}/messages"),
            params=params,
        ))

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        attendee_id: Optional[str] = None,
    ) -> dict:
        if chat_id:
            return self._request(
                "POST", self._acct(f"/chats/{quote(chat_id, safe='')}/messages/send"),
                json={"text": text},
            )
        if not attendee_id:
            raise UnipileError("send_message : chat_id ou attendee_id requis.")
        # v2 : nouveau fil via `POST /v2/{account}/chats/send`, `users_ids`.
        return self._request(
            "POST", self._acct("/chats/send"),
            json={"users_ids": [attendee_id], "text": text},
        )

    # ---- réseau / outreach ----------------------------------------------

    def list_relations(self, cursor: Optional[str] = None,
                       limit: Optional[int] = None) -> dict:
        from .client import cursor_with_limit
        params: dict[str, Any] = {}
        if cursor:
            # Le limit de l'appel prime sur celui figé dans le cursor (#179).
            params["cursor"] = cursor_with_limit(cursor, limit) if limit else cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/users/me/relations"), params=params
        ))

    def list_invitations(self, direction: str = "received",
                         limit: Optional[int] = None,
                         cursor: Optional[str] = None) -> dict:
        """Invitations — v2 : `GET /v2/{account}/users/me/relation-requests`,
        `type=sent|received`. `limit` est un vrai param serveur (plus de curseur
        qui fige le limit, cf. #179)."""
        params: dict[str, Any] = {
            "type": "sent" if direction == "sent" else "received"
        }
        if limit:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return self._norm(self._request(
            "GET", self._acct("/users/me/relation-requests"), params=params
        ))

    def send_invitation(self, provider_id: str,
                        message: Optional[str] = None) -> dict:
        """v2 : `POST /users/me/relation-requests`, corps `{user_id, message}`."""
        body: dict[str, Any] = {"user_id": provider_id}
        if message:
            body["message"] = message
        return self._request(
            "POST", self._acct("/users/me/relation-requests"), json=body
        )

    def handle_invitation(
        self, invitation_id: str, shared_secret: str, action: str = "accept"
    ) -> dict:
        """Accepte/refuse une invitation REÇUE. v2 : `request_id` suffit (plus de
        `shared_secret`, gardé dans la signature pour compat v1). accept →
        `/accept` ; decline → `/cancel`."""
        if action not in ("accept", "decline"):
            raise UnipileError("handle_invitation : action = 'accept' ou 'decline'.")
        verb = "accept" if action == "accept" else "cancel"
        return self._request(
            "POST",
            self._acct(
                f"/users/me/relation-requests/{quote(invitation_id, safe='')}/{verb}"
            ),
        )

    def cancel_invitation(self, invitation_id: str) -> dict:
        """Annule une invitation ENVOYÉE. v2 : `/relation-requests/{id}/cancel`."""
        return self._request(
            "POST",
            self._acct(
                f"/users/me/relation-requests/{quote(invitation_id, safe='')}/cancel"
            ),
        )

    # ---- posts / engagement ---------------------------------------------

    def list_member_posts(self, identifier: str, cursor: Optional[str] = None,
                          limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(identifier, safe='')}/posts"),
            params=params,
        ))

    def get_post(self, post_id: str) -> dict:
        return self._request(
            "GET", self._acct(f"/posts/{quote(post_id, safe='')}")
        )

    def list_comments(self, post_id: str, cursor: Optional[str] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return self._norm(self._request(
            "GET", self._acct(f"/posts/{quote(post_id, safe='')}/comments"),
            params=params,
        ))

    def list_reactions(self, post_id: str, cursor: Optional[str] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return self._norm(self._request(
            "GET", self._acct(f"/posts/{quote(post_id, safe='')}/reactions"),
            params=params,
        ))

    def create_post(self, text: str) -> dict:
        return self._request("POST", self._acct("/posts"), json={"text": text})

    def comment_post(self, post_id: str, text: str) -> dict:
        return self._request(
            "POST", self._acct(f"/posts/{quote(post_id, safe='')}/comments"),
            json={"text": text},
        )

    def react_post(self, post_id: str, value: str = "LIKE") -> dict:
        """Réagit à un post. v2 : corps `{reaction}` (v1 `value`)."""
        return self._request(
            "POST", self._acct(f"/posts/{quote(post_id, safe='')}/reactions"),
            json={"reaction": value},
        )

    # ---- feed (Voyager passthrough via proxyRequest v2) -----------------

    def linkedin_raw(
        self,
        request_url: str,
        method: str = "GET",
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        encoding: bool = False,
        force_api: bool = False,
    ) -> dict:
        """Relaie une requête Voyager brute — v2 : `POST /v2/{account}/linkedin/`
        (proxyRequest), corps `{url, method, bypass_url_encoding, …}`."""
        payload: dict[str, Any] = {
            "url": request_url,
            "method": method,
            "bypass_url_encoding": not encoding,
        }
        if body is not None:
            payload["body"] = body
        if headers:
            payload["headers"] = headers
        return self._request("POST", self._acct("/linkedin/"), json=payload)

    def get_feed(
        self,
        count: int = 20,
        cursor: Optional[str] = None,
        raw: bool = False,
        sort_order: str = "MEMBER_SETTING",
    ) -> dict:
        """Feed d'accueil LinkedIn via la Magic Route Voyager (parse partagé v1)."""
        from .client import _unpack_cursor

        start, token = _unpack_cursor(cursor)
        if token:
            variables = (
                f"(start:{start},count:{count},"
                f"paginationToken:{token},sortOrder:{sort_order})"
            )
            request_url = (
                "https://www.linkedin.com/voyager/api/graphql"
                f"?variables={variables}&queryId={FEED_QUERY_ID}"
            )
        else:
            request_url = (
                "https://www.linkedin.com/voyager/api/graphql"
                f"?queryId={FEED_QUERY_ID}"
            )
        resp = self.linkedin_raw(request_url, method="GET", encoding=False)
        if raw:
            return resp
        return parse_feed(resp, count=count, start=start)

    # ---- moi / followers / activité d'un membre -------------------------

    def get_own_profile(self) -> dict:
        """Profil du compte connecté. v2 : `GET /users/me` (pas de garde #153 :
        l'id rendu ≠ le littéral « me »)."""
        return self._request("GET", self._acct("/users/me"))

    def list_followers(self, user_id: Optional[str] = None,
                      cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        uid = user_id or "me"
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(uid, safe='')}/followers"),
            params=params,
        ))

    def list_following(self, user_id: Optional[str] = None,
                      cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        uid = user_id or "me"
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(uid, safe='')}/following"),
            params=params,
        ))

    def list_member_comments(self, identifier: str,
                            cursor: Optional[str] = None,
                            limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(identifier, safe='')}/comments"),
            params=params,
        ))

    def list_member_reactions(self, identifier: str,
                             cursor: Optional[str] = None,
                             limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(identifier, safe='')}/reactions"),
            params=params,
        ))

    # ---- messagerie : participants / contacts / état du fil -------------

    def list_chat_attendees(self, chat_id: str) -> dict:
        """Participants d'un fil. v2 : `/chats/{chat_id}/participants`."""
        return self._norm(self._request(
            "GET", self._acct(f"/chats/{quote(chat_id, safe='')}/participants")
        ))

    def list_attendees(self, cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        """Carnet de contacts. v2 : `/v2/{account}/contacts`."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/contacts"), params=params
        ))

    # v2 updateChat : champs dédiés (plus le couple {action, value} de v1).
    _CHAT_ACTION_FIELD = {
        "setReadStatus": "read_status",
        "setMuteStatus": "muted_until",
        "setArchiveStatus": "archive_status",
        "setPinnedStatus": "pin_status",
        "setLabel": "label",
    }

    def patch_chat(self, chat_id: str, action: str, value: Any = None) -> dict:
        """Modifie l'état d'un fil. Traduit les `action` v1 vers les champs v2
        (`PATCH /chats/{id}`)."""
        field = self._CHAT_ACTION_FIELD.get(action)
        if field is None:
            raise UnipileError(
                f"patch_chat : action {action!r} non supportée en v2 "
                f"({', '.join(self._CHAT_ACTION_FIELD)})."
            )
        return self._request(
            "PATCH", self._acct(f"/chats/{quote(chat_id, safe='')}"),
            json={field: value},
        )

    def react_message(self, message_id: str, reaction: str,
                      chat_id: Optional[str] = None) -> dict:
        """Réagit à un message. v2 exige le `chat_id` (route sous le fil)."""
        if not chat_id:
            raise UnipileError(
                "react_message : chat_id requis en v2 "
                "(route /chats/{chat_id}/messages/{message_id}/reactions)."
            )
        return self._request(
            "POST",
            self._acct(
                f"/chats/{quote(chat_id, safe='')}"
                f"/messages/{quote(message_id, safe='')}/reactions"
            ),
            json={"reaction": reaction},
        )

    # ---- recruiter / sales navigator ------------------------------------

    def list_contracts(self) -> dict:
        return self._request("GET", self._acct("/linkedin/contracts"))

    def select_contract(self, contract_id: str) -> dict:
        return self._request(
            "POST",
            self._acct(f"/linkedin/contracts/{quote(contract_id, safe='')}/select"),
        )

    def inmail_balance(self) -> dict:
        """Solde InMail. v2 : `GET /linkedin/inmail-credits` (corrige la 404 v1
        `/linkedin/inmail/balance`, feedback #178). Réponse `{object, credits}`."""
        return self._request("GET", self._acct("/linkedin/inmail-credits"))

    def endorse_profile(self, profile_id: str, skill_endorsement_id: int) -> dict:
        """v2 : `POST /linkedin/member/{member_id}/endorse-skill`, corps
        `{skill_id}`."""
        return self._request(
            "POST",
            self._acct(f"/linkedin/member/{quote(profile_id, safe='')}/endorse-skill"),
            json={"skill_id": str(skill_endorsement_id)},
        )

    def member_action(self, user_id: str, api: str, action: str,
                     hiring_project_id: Optional[str] = None,
                     stage: Optional[str] = None,
                     list_id: Optional[str] = None) -> dict:
        """Action premium (sauvegarde lead / pipeline recruteur). v2 éclate ces
        actions par produit ; on mappe les cas courants, sinon erreur claire."""
        if api == "sales_navigator" and action == "saveLead":
            if not list_id:
                raise UnipileError("saveLead (v2) : list_id (lead-list) requis.")
            return self._request(
                "POST",
                self._acct(
                    f"/linkedin/sales-navigator/lead-lists/{quote(list_id, safe='')}/save"
                ),
                json={"user_id": user_id},
            )
        if api == "recruiter" and action in (
            "addCandidateToPipeline", "addApplicantToPipeline"
        ):
            if not hiring_project_id:
                raise UnipileError(
                    "pipeline recruiter (v2) : hiring_project_id requis."
                )
            body: dict[str, Any] = {"user_id": user_id}
            if stage:
                body["stage"] = stage
            return self._request(
                "POST",
                self._acct(
                    f"/linkedin/recruiter/projects/"
                    f"{quote(hiring_project_id, safe='')}/pipeline/candidate/save"
                ),
                json=body,
            )
        raise UnipileError(
            f"member_action (v2) : combinaison api={api!r} action={action!r} "
            "non mappée."
        )

    # ---- recruiter : offres & candidats ---------------------------------

    def list_job_postings(self, cursor: Optional[str] = None,
                         limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/linkedin/jobs"), params=params
        ))

    def get_job_posting(self, job_id: str) -> dict:
        return self._request(
            "GET", self._acct(f"/linkedin/jobs/{quote(job_id, safe='')}")
        )

    def list_job_applicants(self, job_id: str, cursor: Optional[str] = None,
                           limit: Optional[int] = None) -> dict:
        """v2 : `POST /linkedin/jobs/{job_id}/applicants` (getClassicApplicants)."""
        body: dict[str, Any] = {}
        if cursor:
            body["cursor"] = cursor
        if limit:
            body["limit"] = limit
        return self._norm(self._request(
            "POST", self._acct(f"/linkedin/jobs/{quote(job_id, safe='')}/applicants"),
            json=body,
        ))

    def get_job_applicant(self, job_id: str, applicant_id: str) -> dict:
        return self._request(
            "GET",
            self._acct(
                f"/linkedin/jobs/{quote(job_id, safe='')}"
                f"/applicants/{quote(applicant_id, safe='')}"
            ),
        )

    def list_hiring_projects(self, cursor: Optional[str] = None,
                            limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/linkedin/recruiter/projects"), params=params
        ))
