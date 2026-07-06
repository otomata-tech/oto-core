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

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests

from ...config import get_secret, require_secret

DEFAULT_DSN = "api25.unipile.com:15555"

logger = logging.getLogger(__name__)

# Feed d'accueil LinkedIn : LinkedIn n'expose AUCUN endpoint feed côté API
# Unipile. Le seul chemin est la Magic Route raw data d'Unipile (POST
# /api/v1/linkedin) qui relaie une requête Voyager arbitraire avec la session
# du compte connecté. ⚠️ Voyager n'est PAS contractuel : ce queryId GraphQL et
# le schéma JSON peuvent casser quand LinkedIn fait évoluer son API interne
# (capture devtools sur linkedin.com/feed pour le rafraîchir). Source du queryId :
# https://developer.unipile.com/docs/get-raw-data-example
FEED_QUERY_ID = "voyagerFeedDashMainFeed.7a50ef8ba5a7865c23ad5df46f735709"


class UnipileError(RuntimeError):
    """Erreur API Unipile, message remonté tel quel.

    `status_code` = code HTTP amont quand l'erreur vient d'une réponse Unipile
    (même contrat que `oto.tools.common.UpstreamHTTPError` : permet aux
    consommateurs de router un 4xx comme erreur gérée, pas un bug), None sinon
    (erreur réseau, config, identity mismatch).
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


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
            raise UnipileError(f"Unipile {resp.status_code}: {msg}",
                               status_code=resp.status_code)
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

    def resolve_facet(
        self, facet_type: str, keywords: str, limit: int = 100
    ) -> list[dict]:
        """Résout un nom en ids de facette LinkedIn.

        facet_type ∈ COMPANY | LOCATION | INDUSTRY | SCHOOL | SKILLS ...
        Retourne [{id, title}, ...]. La page company LinkedIn n'est pas
        forcément une facette employeur valide — utiliser CE résultat.
        """
        params = {
            "account_id": self.account_id(),
            "type": facet_type,
            "keywords": keywords,
            "limit": limit,
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
        api: str = "classic",
        network_distance: Optional[list[int]] = None,
        url: Optional[str] = None,
        advanced_keywords: Optional[dict] = None,
        industry: Optional[dict] = None,
    ) -> dict:
        """Recherche LinkedIn. `company`/`location`/`industry` acceptent des
        noms (résolus en facettes) ou des ids numériques.

        Args:
            keywords: mots-clés (nom, intitulé de poste…).
            category: "people" ou "companies".
            company: employeur(s) — noms ou ids de facette.
            location: localisation(s) — noms ou ids de facette.
            cursor: curseur de pagination.
            api: "classic" | "sales_navigator" | "recruiter" (défaut classic).
                Certains filtres (tenure, langue, role/skills) ne marchent que
                sur sales_navigator/recruiter et dépendent de l'abonnement.
            network_distance: degré(s) de relation — [1]=N1, [2]=N2, [3]=N3.
            url: URL de recherche LinkedIn/Sales Nav collée du navigateur. Si
                fournie, les autres filtres structurés sont ignorés.
            advanced_keywords: recherche people ciblée — dict
                {first_name?, last_name?, title?, company?, school?}.
            industry: filtre secteur — dict {include?: [...], exclude?: [...]}
                (noms ou ids de facette).

        Retourne le payload Unipile brut (items + paging + cursor).
        """
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor

        # Recherche par URL collée : mutuellement exclusive des filtres.
        if url:
            return self._request(
                "POST", "/linkedin/search", params=params, json={"url": url}
            )

        body: dict[str, Any] = {"api": api, "category": category}
        if keywords:
            body["keywords"] = keywords
        if advanced_keywords:
            ak = {k: v for k, v in advanced_keywords.items() if v}
            if ak:
                body["advanced_keywords"] = ak
        company_ids = self._as_facet_ids("COMPANY", company)
        location_ids = self._as_facet_ids("LOCATION", location)
        if company_ids:
            body["company"] = company_ids
        if location_ids:
            body["location"] = location_ids
        if industry:
            inc = self._as_facet_ids("INDUSTRY", industry.get("include"))
            exc = self._as_facet_ids("INDUSTRY", industry.get("exclude"))
            if inc or exc:
                body["industry"] = {"include": inc, "exclude": exc}
        if network_distance:
            body["network_distance"] = [int(d) for d in network_distance]
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

    def list_chats(self, limit: int = 20, cursor: Optional[str] = None,
                   with_attendee_names: bool = False) -> dict:
        """Fils de messagerie du compte connecté. Paginé (`limit` + `cursor`).

        `with_attendee_names=True` enrichit chaque fil 1-à-1 de champs
        `attendee_name`/`attendee_headline`/`attendee_profile_url` résolus en
        batch via le carnet `/attendees` (les fils 1-à-1 LinkedIn arrivent avec
        `name: null` et un `attendee_provider_id` opaque — impossible de savoir
        QUI sans ça). Enrichissement best-effort : un id non résolu laisse les
        champs absents, une erreur du carnet n'empêche pas la liste."""
        params: dict[str, Any] = {"account_id": self.account_id(), "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", "/chats", params=params)
        if with_attendee_names:
            self._annotate_chat_attendees(data)
        return data

    def resolve_attendee_names(self, provider_ids, max_pages: int = 10,
                               page_limit: int = 100) -> dict:
        """Résout des `attendee_provider_id` en fiches attendee via le carnet
        `/attendees` paginé (1 famille d'appels batch, PAS un appel par fil).

        Retourne `{provider_id: attendee}` (name, picture_url, profile_url,
        specifics…). Arrêt anticipé dès que tous les ids demandés sont résolus ;
        borné à `max_pages` pages de `page_limit` — les ids au-delà restent
        simplement absents de la map (best-effort)."""
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
                pid = str(att.get("provider_id") or "")
                if pid in wanted:
                    out[pid] = att
            cursor = (page or {}).get("cursor")
            if not items or not cursor:
                break
        return out

    def _annotate_chat_attendees(self, data: Any) -> None:
        """Enrichit in-place les items d'un payload `/chats` avec le nom de leur
        interlocuteur (`attendee_name`/`attendee_headline`/`attendee_profile_url`).
        Best-effort : ne lève jamais (la liste des fils prime sur l'enrichissement)."""
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
            logger.warning("unipile chats: résolution des attendees échouée, "
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

    # ---- réseau / outreach (LinkedIn) -----------------------------------

    def list_relations(self, cursor: Optional[str] = None,
                       limit: Optional[int] = None) -> dict:
        """Relations de 1er degré (N1) du compte connecté."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request("GET", "/users/relations", params=params)

    def list_invitations(self, direction: str = "received",
                         limit: Optional[int] = None,
                         cursor: Optional[str] = None) -> dict:
        """Invitations de connexion — `direction` = 'received' (reçues) ou 'sent'.
        Paginé (`limit` + `cursor`) : sans borne l'endpoint renvoie TOUT le
        backlog (vécu : ~72k chars, réponse inexploitable par un agent)."""
        d = "sent" if direction == "sent" else "received"
        params: dict[str, Any] = {"account_id": self.account_id()}
        if limit:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", f"/users/invite/{d}", params=params)
        # Garde-fou : si l'API ignore `limit`, on tronque côté client en le
        # signalant (`truncated`) plutôt que de resservir le backlog entier.
        if limit and isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list) and len(items) > limit:
                data["items"] = items[:limit]
                data["truncated"] = True
        return data

    def send_invitation(self, provider_id: str,
                        message: Optional[str] = None) -> dict:
        """Envoie une demande de connexion LinkedIn à `provider_id` (+ note ≤300c).
        `provider_id` = le champ `provider_id` d'un profil/résultat de recherche."""
        body: dict[str, Any] = {
            "account_id": self.account_id(),
            "provider_id": provider_id,
        }
        if message:
            body["message"] = message
        return self._request("POST", "/users/invite", json=body)

    # ---- posts / engagement (LinkedIn) ----------------------------------
    # Lectures vérifiées en live ; POST = chemins inférés (convention REST + index
    # Unipile), non sondés en dev pour ne pas publier sous l'identité du compte.

    def list_member_posts(self, identifier: str, cursor: Optional[str] = None,
                          limit: Optional[int] = None) -> dict:
        """Posts publiés par un membre (`identifier` = provider id ou slug)."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request("GET", f"/users/{quote(identifier, safe='')}/posts", params=params)

    def get_post(self, post_id: str) -> dict:
        return self._request("GET", f"/posts/{quote(post_id, safe='')}",
                             params={"account_id": self.account_id()})

    def list_comments(self, post_id: str, cursor: Optional[str] = None) -> dict:
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"/posts/{quote(post_id, safe='')}/comments", params=params)

    def list_reactions(self, post_id: str, cursor: Optional[str] = None) -> dict:
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"/posts/{quote(post_id, safe='')}/reactions", params=params)

    def create_post(self, text: str) -> dict:
        """Publie un post (chemin POST inféré)."""
        return self._request("POST", "/posts",
                             json={"account_id": self.account_id(), "text": text})

    def comment_post(self, post_id: str, text: str) -> dict:
        """Commente un post (chemin POST inféré)."""
        return self._request("POST", f"/posts/{quote(post_id, safe='')}/comments",
                             json={"account_id": self.account_id(), "text": text})

    def react_post(self, post_id: str, value: str = "LIKE") -> dict:
        """Réagit à un post (chemin POST inféré). value LIKE|PRAISE|EMPATHY|INTEREST|
        APPRECIATION|ENTERTAINMENT."""
        return self._request("POST", f"/posts/{quote(post_id, safe='')}/reactions",
                             json={"account_id": self.account_id(), "value": value})

    # ---- raw data route (Magic Route → Voyager passthrough) --------------
    # Pas un endpoint « propre » : on relaie une requête Voyager brute via Unipile.
    # Cf. https://developer.unipile.com/reference/linkedincontroller_getrawdata

    def linkedin_raw(
        self,
        request_url: str,
        method: str = "GET",
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        encoding: bool = False,
        force_api: bool = False,
    ) -> dict:
        """Relaie une requête arbitraire vers l'API interne Voyager de LinkedIn
        via la Magic Route Unipile (`POST /api/v1/linkedin`), exécutée avec la
        session du compte connecté.

        Args:
            request_url: URL Voyager (`https://www.linkedin.com/voyager/api/...`).
            method: verbe HTTP relayé (défaut GET).
            body: payload pour les requêtes POST/PUT/PATCH.
            headers: en-têtes HTTP custom.
            encoding: encode query params/form body côté Unipile (défaut False —
                les query Voyager GraphQL sont déjà formées à la main).
            force_api: force l'usage d'une API sans abonnement actif.

        Retourne l'enveloppe Unipile `{object: "LinkedinRawData", data: {...}}`,
        où `data` est le JSON brut renvoyé par Voyager.
        """
        payload: dict[str, Any] = {
            "account_id": self.account_id(),
            "request_url": request_url,
            "method": method,
            "encoding": encoding,
        }
        if body is not None:
            payload["body"] = body
        if headers:
            payload["headers"] = headers
        if force_api:
            payload["force_api"] = True
        return self._request("POST", "/linkedin", json=payload)

    def get_feed(
        self,
        count: int = 20,
        cursor: Optional[str] = None,
        raw: bool = False,
        sort_order: str = "MEMBER_SETTING",
    ) -> dict:
        """Feed d'accueil LinkedIn (la page d'accueil) du compte connecté, via la
        Magic Route raw data → Voyager (cf. FEED_QUERY_ID).

        ⚠️ L'ordre n'est PAS un chrono garanti : `sort_order=MEMBER_SETTING`
        (défaut) **respecte le réglage de tri choisi sur ta home LinkedIn** (« Les
        plus pertinents » / « Plus récents »). Pour un miroir chronologique, règle
        ta home LinkedIn sur « Plus récents ». Quel que soit l'ordre de service,
        chaque post porte `posted_at` (décodé de l'id d'activité) → l'appelant peut
        toujours re-trier de façon stable. Les encarts sponsorisés/promo sont exclus.

        Args:
            count: nombre d'items voulus (le feed Voyager pagine par lots ;
                on tronque la page courante à `count`).
            cursor: curseur opaque renvoyé par un appel précédent
                (`"<start>|<paginationToken>"`) pour la page suivante. None = 1re page.
            raw: True → renvoie l'enveloppe Unipile brute sans mapping (debug).
            sort_order: valeur de tri Voyager pour les pages paginées
                (`MEMBER_SETTING` par défaut). N'affecte que les pages suivantes :
                la 1re page suit toujours le tri par défaut de la home.

        Retourne `{items: [...], cursor: str|None, count: int}` où chaque item est
        normalisé par `parse_feed` (posts organiques seulement).
        """
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
            # 1re page : forme exacte de l'exemple Unipile (sans variables).
            request_url = (
                "https://www.linkedin.com/voyager/api/graphql"
                f"?queryId={FEED_QUERY_ID}"
            )
        resp = self.linkedin_raw(request_url, method="GET", encoding=False)
        if raw:
            return resp
        return parse_feed(resp, count=count, start=start)

    # ---- réseau : invitations (handle / cancel) -------------------------

    def handle_invitation(
        self, invitation_id: str, shared_secret: str, action: str = "accept"
    ) -> dict:
        """Accepte ou refuse une invitation LinkedIn REÇUE.

        Args:
            invitation_id: id de l'invitation (champ d'un item `list_invitations
                ('received')`).
            shared_secret: token fourni par LinkedIn sur le même item
                (obligatoire côté API pour traiter une invitation reçue).
            action: 'accept' ou 'decline'.
        """
        if action not in ("accept", "decline"):
            raise UnipileError("handle_invitation : action = 'accept' ou 'decline'.")
        body = {
            "provider": "LINKEDIN",
            "account_id": self.account_id(),
            "shared_secret": shared_secret,
            "action": action,
        }
        return self._request(
            "POST", f"/users/invite/received/{quote(invitation_id, safe='')}", json=body
        )

    def cancel_invitation(self, invitation_id: str) -> dict:
        """Annule une invitation LinkedIn ENVOYÉE (en attente). `invitation_id` =
        id d'un item `list_invitations('sent')`."""
        return self._request(
            "DELETE", f"/users/invite/sent/{quote(invitation_id, safe='')}",
            params={"account_id": self.account_id()},
        )

    # ---- réseau : followers / following / activité d'un membre ----------

    def get_own_profile(self) -> dict:
        """Profil du compte connecté lui-même (le « moi » LinkedIn)."""
        return self._request("GET", "/users/me",
                             params={"account_id": self.account_id()})

    def list_followers(self, user_id: Optional[str] = None,
                      cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        """Followers (LinkedIn : du compte connecté ; `user_id` = autre membre
        selon le provider). Paginé."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if user_id:
            params["user_id"] = user_id
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request("GET", "/users/followers", params=params)

    def list_following(self, user_id: Optional[str] = None,
                      cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        """Comptes suivis. Paginé."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if user_id:
            params["user_id"] = user_id
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request("GET", "/users/following", params=params)

    def list_member_comments(self, identifier: str,
                            cursor: Optional[str] = None,
                            limit: Optional[int] = None) -> dict:
        """Commentaires laissés par un membre (`identifier` = provider id). Pour
        repérer ce qu'un prospect engage → accroche social-selling."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request(
            "GET", f"/users/{quote(identifier, safe='')}/comments", params=params
        )

    def list_member_reactions(self, identifier: str,
                             cursor: Optional[str] = None,
                             limit: Optional[int] = None) -> dict:
        """Réactions d'un membre (`identifier` = provider id) — posts qu'il a likés."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request(
            "GET", f"/users/{quote(identifier, safe='')}/reactions", params=params
        )

    # ---- messagerie : participants / contacts / état du fil -------------

    def list_chat_attendees(self, chat_id: str) -> dict:
        """Participants d'un fil de messagerie (`chat_id` d'un `list_chats`)."""
        return self._request(
            "GET", f"/chats/{quote(chat_id, safe='')}/attendees",
            params={"account_id": self.account_id()},
        )

    def list_attendees(self, cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        """Carnet de contacts de messagerie (tous les interlocuteurs). Paginé."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request("GET", "/attendees", params=params)

    def patch_chat(self, chat_id: str, action: str, value: Any = None) -> dict:
        """Modifie l'état d'un fil. `action` ∈ setReadStatus | setMuteStatus |
        setArchiveStatus | setPinnedStatus | addParticipant | removeParticipant |
        setLabel | getInviteLink. `value` = booléen (statuts) ou string
        (participant/label) ; omis pour getInviteLink."""
        body: dict[str, Any] = {"action": action}
        if value is not None:
            body["value"] = value
        return self._request(
            "PATCH", f"/chats/{quote(chat_id, safe='')}", json=body
        )

    def react_message(self, message_id: str, reaction: str,
                      chat_id: Optional[str] = None) -> dict:
        """Réagit à un message (DM) avec un emoji natif (ex. '👍'). `message_id` =
        id d'un message de `list_messages`. `chat_id` n'est requis qu'en v2
        (accepté et ignoré ici pour une signature commune aux deux clients)."""
        return self._request(
            "POST", f"/messages/{quote(message_id, safe='')}/reaction",
            json={"reaction": reaction},
        )

    # ---- LinkedIn recruiter / sales navigator ---------------------------
    # Nécessitent un abonnement Recruiter / Sales Navigator sur le compte
    # connecté ; sinon l'API Unipile renvoie une erreur (remontée telle quelle).

    def list_contracts(self) -> dict:
        """Contrats LinkedIn premium disponibles (Recruiter / Sales Navigator) du
        compte — id à passer à `select_contract` pour activer la bonne ardoise."""
        return self._request("GET", "/linkedin/contracts",
                             params={"account_id": self.account_id()})

    def select_contract(self, contract_id: str) -> dict:
        """Active un contrat Recruiter / Sales Navigator (`contract_id` de
        `list_contracts`) pour les appels premium qui suivent."""
        return self._request(
            "POST", f"/linkedin/contracts/{quote(contract_id, safe='')}/select",
            params={"account_id": self.account_id()},
        )

    def inmail_balance(self) -> dict:
        """Solde de crédits InMail (messages premium) du compte connecté."""
        return self._request("GET", "/linkedin/inmail/balance",
                             params={"account_id": self.account_id()})

    def endorse_profile(self, profile_id: str, skill_endorsement_id: int) -> dict:
        """Recommande une compétence d'un membre.

        Args:
            profile_id: provider id du membre (commence par ACo/ADo).
            skill_endorsement_id: `endorsement_id` d'une compétence, renvoyé dans le
                profil (`get_profile`).
        """
        return self._request("POST", "/linkedin/profile/endorse", json={
            "account_id": self.account_id(),
            "profile_id": profile_id,
            "skill_endorsement_id": skill_endorsement_id,
        })

    def member_action(self, user_id: str, api: str, action: str,
                     hiring_project_id: Optional[str] = None,
                     stage: Optional[str] = None,
                     list_id: Optional[str] = None) -> dict:
        """Action premium sur un membre (sauvegarde lead / pipeline recruteur).

        Args:
            user_id: provider id du membre.
            api: 'sales_navigator' ou 'recruiter'.
            action: sales_navigator → 'saveLead' ; recruiter →
                'addCandidateToPipeline' | 'addApplicantToPipeline' |
                'changeCandidatePipeline' | 'rejectApplicant'.
            hiring_project_id: requis pour les actions pipeline recruiter.
            stage: pipeline recruiter — 'UNCONTACTED' | 'CONTACTED' | 'REPLIED'.
            list_id: liste Sales Navigator cible (optionnel pour saveLead).
        """
        body: dict[str, Any] = {
            "account_id": self.account_id(),
            "api": api,
            "action": action,
        }
        if hiring_project_id:
            body["hiring_project_id"] = hiring_project_id
        if stage:
            body["stage"] = stage
        if list_id:
            body["list_id"] = list_id
        return self._request(
            "POST", f"/linkedin/user/{quote(user_id, safe='')}", json=body
        )

    # ---- LinkedIn recruiter : offres d'emploi & candidats (lectures) ----
    # Chemins REST de l'inventaire Unipile (best-effort, gatés Recruiter).

    def list_job_postings(self, cursor: Optional[str] = None,
                         limit: Optional[int] = None) -> dict:
        """Offres d'emploi (job postings) du compte recruteur. Paginé."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request("GET", "/linkedin/job-postings", params=params)

    def get_job_posting(self, job_id: str) -> dict:
        """Détail d'une offre d'emploi (`job_id` de `list_job_postings`)."""
        return self._request(
            "GET", f"/linkedin/job-postings/{quote(job_id, safe='')}",
            params={"account_id": self.account_id()},
        )

    def list_job_applicants(self, job_id: str, cursor: Optional[str] = None,
                           limit: Optional[int] = None) -> dict:
        """Candidats d'une offre d'emploi. Paginé."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request(
            "GET", f"/linkedin/job-postings/{quote(job_id, safe='')}/applicants",
            params=params,
        )

    def get_job_applicant(self, job_id: str, applicant_id: str) -> dict:
        """Détail d'un candidat d'une offre."""
        return self._request(
            "GET",
            f"/linkedin/job-postings/{quote(job_id, safe='')}"
            f"/applicants/{quote(applicant_id, safe='')}",
            params={"account_id": self.account_id()},
        )

    def list_hiring_projects(self, cursor: Optional[str] = None,
                            limit: Optional[int] = None) -> dict:
        """Projets de recrutement (hiring projects) du compte Recruiter. Paginé.
        Le `hiring_project_id` alimente `member_action` (pipeline)."""
        params: dict[str, Any] = {"account_id": self.account_id()}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request("GET", "/linkedin/hiring-projects", params=params)


# ---- feed parsing (Voyager graphe normalisé) ----------------------------
# Voyager renvoie un graphe NORMALISÉ : `data.feedDashMainFeedByMainFeed.elements[]`
# (les updates) + `data.included[]` (entités déréférencées par URN, ex. le
# socialDetail qui porte les compteurs). Le mapping est DÉFENSIF par conception :
# le schéma Voyager n'est pas contractuel, donc chaque champ est extrait en
# best-effort (accès imbriqué tolérant aux clés absentes) et un item qui casse
# le mapping est journalisé + renvoyé en mode dégradé plutôt que de tout faire
# échouer. Si la forme globale est inattendue, on remonte le payload brut.


def _unpack_cursor(cursor: Optional[str]) -> tuple[int, Optional[str]]:
    """Curseur opaque `"<start>|<paginationToken>"` → (start, token). Tolérant :
    cursor None/vide → (0, None) ; sans `|` → traité comme un token nu (start 0)."""
    if not cursor:
        return 0, None
    if "|" in cursor:
        start_s, token = cursor.split("|", 1)
        try:
            start = int(start_s)
        except (TypeError, ValueError):
            start = 0
        return start, (token or None)
    return 0, cursor


def _deep_get(obj: Any, *keys: str, default: Any = None) -> Any:
    """Accès imbriqué tolérant : retourne `default` dès qu'un maillon manque ou
    n'est pas un dict (jamais de KeyError/TypeError sur un graphe Voyager partiel)."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _text_of(node: Any) -> Optional[str]:
    """Voyager enveloppe souvent le texte dans `{text: "..."}` (parfois imbriqué).
    Accepte une string nue, `{text: str}` ou `{text: {text: str}}`."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        t = node.get("text")
        if isinstance(t, str):
            return t
        if isinstance(t, dict) and isinstance(t.get("text"), str):
            return t["text"]
    return None


def _activity_urn_from(el: dict) -> Optional[str]:
    """Extrait `urn:li:activity:<id>` d'un update Voyager.

    Pistes (dans l'ordre) : updateMetadata.urn / updateMetadata.shareUrn /
    le `entityUrn` de l'update (`urn:li:fsd_update:(urn:li:activity:...,...)`)."""
    for path in (("updateMetadata", "urn"), ("updateMetadata", "shareUrn")):
        v = _deep_get(el, *path)
        if isinstance(v, str) and "urn:li:activity:" in v:
            return _extract_activity(v)
    eu = el.get("entityUrn")
    if isinstance(eu, str):
        return _extract_activity(eu)
    return None


def _extract_activity(s: str) -> Optional[str]:
    """Isole `urn:li:activity:<id>` d'une chaîne (URN composé ou nu)."""
    marker = "urn:li:activity:"
    idx = s.find(marker)
    if idx < 0:
        return None
    rest = s[idx + len(marker):]
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    return f"{marker}{digits}" if digits else None


def _posted_at_from_activity(activity_urn: Optional[str]) -> Optional[str]:
    """Décode l'horodatage encodé dans l'id d'activité LinkedIn : les 41 bits de
    poids fort de l'id 64-bit = un timestamp en ms (`id >> 22`). Astuce robuste,
    indépendante du libellé relatif ('2h') affiché par Voyager."""
    if not activity_urn:
        return None
    try:
        aid = int(activity_urn.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return None
    ms = aid >> 22
    # garde-fou : un epoch ms plausible (> 2001-09, < 2100)
    if not (1_000_000_000_000 < ms < 4_102_444_800_000):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _social_counts(el: dict, included_by_urn: dict) -> tuple[Optional[int], Optional[int]]:
    """(reactions_count, comments_count) depuis le socialDetail — inliné ou
    déréférencé via `*socialDetail` dans `included`. Best-effort."""
    sd = el.get("socialDetail")
    if sd is None:
        ref = el.get("*socialDetail")
        if isinstance(ref, str):
            sd = included_by_urn.get(ref)
    counts = _deep_get(sd, "totalSocialActivityCounts", default={}) or {}
    comments = counts.get("numComments")
    reactions = None
    rtc = counts.get("reactionTypeCounts")
    if isinstance(rtc, list) and rtc:
        try:
            reactions = sum(int(r.get("count", 0)) for r in rtc if isinstance(r, dict))
        except (TypeError, ValueError):
            reactions = None
    if reactions is None:
        reactions = counts.get("numLikes")
    return reactions, comments


def _map_feed_item(el: dict, included_by_urn: dict) -> dict:
    """Un update Voyager → item normalisé. Lève si `el` n'est pas un update
    exploitable (ni actor ni commentary) — l'appelant gère le fallback."""
    actor = el.get("actor") if isinstance(el.get("actor"), dict) else {}
    commentary = el.get("commentary") if isinstance(el.get("commentary"), dict) else {}
    if not actor and not commentary:
        raise ValueError("element sans actor/commentary (pas un update feed)")

    activity_urn = _activity_urn_from(el)
    reactions, comments = _social_counts(el, included_by_urn)
    post_url = (
        f"https://www.linkedin.com/feed/update/{activity_urn}"
        if activity_urn else None
    )
    return {
        "urn": activity_urn or el.get("entityUrn"),
        "author_name": _text_of(actor.get("name")),
        "author_headline": _text_of(actor.get("description")),
        "text": _text_of(commentary.get("text")) or _text_of(commentary),
        "posted_at": _posted_at_from_activity(activity_urn),
        "posted_relative": _text_of(actor.get("subDescription")),
        "reactions_count": reactions,
        "comments_count": comments,
        "post_url": post_url,
    }


def _is_promo(el: dict) -> bool:
    """True si l'update est un encart sponsorisé/promotionnel (pub LinkedIn,
    « Hiring Pro », posts Promoted…) plutôt qu'un post organique — à exclure du
    feed. Plusieurs repères Voyager, best-effort : urn `inAppPromotion`, un
    `promoComponent` dans le contenu, `actionsPosition=PROMO_COMPONENT`, ou un
    bloc `sponsoredTracking` dans les métadonnées de tracking."""
    eu = el.get("entityUrn")
    if isinstance(eu, str) and "inAppPromotion" in eu:
        return True
    if _deep_get(el, "content", "promoComponent") is not None:
        return True
    if _deep_get(el, "metadata", "actionsPosition") == "PROMO_COMPONENT":
        return True
    if _deep_get(el, "metadata", "trackingData", "sponsoredTracking") is not None:
        return True
    return False


def parse_feed(resp: Any, count: int = 20, start: int = 0) -> dict:
    """Mappe l'enveloppe Unipile raw data du feed → `{items, cursor, count}`.

    Ne renvoie QUE des posts organiques normalisés : les encarts sponsorisés/promo
    (`_is_promo`) sont écartés silencieusement, et un update au schéma inattendu est
    **journalisé (warning) puis ignoré** (jamais de `_raw` verbeux dans la sortie).
    Si la structure globale est inattendue (pas d'`elements`), on remonte
    `{items: [], cursor: None, count: 0, _raw: resp}` + log error.
    """
    # Enveloppe Unipile {object, data} → JSON Voyager {data, included}.
    voyager = resp.get("data") if isinstance(resp, dict) else None
    feed = _deep_get(voyager, "data", "feedDashMainFeedByMainFeed")
    elements = feed.get("elements") if isinstance(feed, dict) else None
    if not isinstance(elements, list):
        logger.error(
            "unipile feed: structure inattendue (pas d'elements) — payload brut remonté"
        )
        return {"items": [], "cursor": None, "count": 0, "_raw": resp}

    included = _deep_get(voyager, "included", default=[])
    included_by_urn = {
        it["entityUrn"]: it
        for it in included
        if isinstance(it, dict) and isinstance(it.get("entityUrn"), str)
    }

    items: list[dict] = []
    for el in elements:
        if not isinstance(el, dict) or _is_promo(el):
            continue  # non-dict ou encart sponsorisé/promo → jamais renvoyé
        try:
            items.append(_map_feed_item(el, included_by_urn))
        except Exception:  # noqa: BLE001 — parsing défensif voulu
            logger.warning(
                "unipile feed: mapping d'un item échoué, ignoré", exc_info=True
            )
            continue

    items = items[:count]
    token = _deep_get(feed, "metadata", "paginationToken")
    next_cursor = f"{start + len(items)}|{token}" if token else None
    return {"items": items, "cursor": next_cursor, "count": len(items)}
