"""Unipile API client — hosted LinkedIn search / scrape / messaging (API v2).

Unipile maintient la session LinkedIn côté serveur (vrai Chrome + proxy
résidentiel), ce qui contourne les deux contraintes du browser local : empreinte
TLS et isolation de session (le cookie ne vit pas sur notre IP datacenter, donc
n'expose ni ne déconnecte la session de l'utilisateur). Cf. oto-mcp#5.

Requires: requests

Secrets (résolus via oto.config) :
- UNIPILE_API_KEY            (requis) — clé X-API-KEY du compte Unipile
- UNIPILE_DSN                (def. api.unipile.com) — host de l'instance
- UNIPILE_LINKEDIN_ACCOUNT_ID (optionnel) — sinon, 1er compte LINKEDIN connecté

Spécificités API v2 (par rapport à l'ancienne API v1, retirée) :
- **base** : `https://{dsn}/v2`.
- **`account_id` dans le PATH** (`/v2/{account_id}/…`), plus en query param — donc
  ne fuite plus dans les query strings, mais peut apparaître dans une URL d'erreur
  → on le **caviarde** dans les messages (`_sanitize`, feedback oto #178).
- **enveloppe de liste** `{data, total_count, next_cursor}`. On **normalise** chaque
  réponse de liste vers `items`/`cursor` EN PLUS de garder `data`/`next_cursor` →
  l'aval oto-mcp (feed sync, wrappers, attendus de l'agent) reste stable.
- **surface éclatée** : search people/companies séparés + par produit
  (classic/recruiter/sales-navigator) ; invitations = `users/me/relation-requests` ;
  attendees d'un fil = `participants` ; réactions de message sous le chat ;
  solde InMail = `inmail-credits`.

Fixes feedback fold-in (au-delà de l'API brute) :
- **garde anti-mismatch** identifier↔réponse sur `get_profile`/`get_company`
  (feedback #144-149/#153 : sous concurrence, l'API a rendu le profil d'un AUTRE
  membre / un CompanyProfile à la place). On vérifie que l'objet rendu correspond
  bien au type ET à l'identifiant demandés, sinon `UnipileError` **actionnable et
  retryable** — jamais de donnée fausse renvoyée en silence.
- **erreurs réseau propres** : les exceptions `requests` (DNS/timeout) sont mappées
  en `UnipileError` stable au lieu de fuiter `net::ERR_NAME_NOT_RESOLVED` (#177).
- **résolution de slug tolérante** sur `get_company` (#176) : un nom de marque
  passé comme slug (`mooniz`) qui tombe en 404 est retenté via une recherche
  société → `public_identifier` canonique (`mooniz1`) ; échec → 404 propre
  enrichi des candidats proches, jamais l'erreur brute.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests

from ...config import get_secret, require_secret

logger = logging.getLogger(__name__)

DEFAULT_DSN = "api.unipile.com"
# (connect, read) en secondes — borne le blocage : un socket amont muet faisait
# pendre l'appel jusqu'au cutoff 300s du client MCP (unipile_me, #114).
_REQUEST_TIMEOUT = (10, 120)
# #238 : la recherche Recruiter PAR URL (talent/search) peut PENDRE indéfiniment
# quand le `searchContextId` de l'URL est expiré/mort côté LinkedIn — l'endpoint ne
# répond ni erreur ni vide → timeout MCP à 180s, opaque. Read timeout court dédié :
# on échoue AVANT le plafond MCP avec une erreur PROPRE et actionnable.
_URL_SEARCH_TIMEOUT = (10, 75)
# Scrape (recherche structurée + fiche société) : LinkedIn/Unipile peut faire
# PENDRE l'appel ~120s (surcharge / rate-limit qui queue) — vécu 2026-07-21, 166
# ReadTimeout de 121s qui gelaient l'agent 2 min chacun. Read timeout court dédié :
# échouer vite (60s) avec une erreur actionnable plutôt que geler.
_SCRAPE_TIMEOUT = (10, 60)

# Feed d'accueil LinkedIn : LinkedIn n'expose AUCUN endpoint feed côté API
# Unipile. Le seul chemin est la Magic Route Voyager, exposée en v2 comme le proxy
# générique `POST /v2/{account_id}/linkedin/` (proxyRequest) : on relaie une
# requête Voyager brute. ⚠️ Voyager n'est PAS contractuel : ce queryId GraphQL et
# le schéma JSON peuvent casser quand LinkedIn fait évoluer son API interne
# (capture devtools sur linkedin.com/feed pour le rafraîchir). Source du queryId :
# https://developer.unipile.com/docs/get-raw-data-example
FEED_QUERY_ID = "voyagerFeedDashMainFeed.7a50ef8ba5a7865c23ad5df46f735709"

# Providers dont la messagerie est rangée par INBOX. Unipile documente DEUX formes
# d'endpoint pour la même opération — « Use `GET /v2/:account_id/chats` or
# `GET /v2/:account_id/inboxes/:inbox_id/chats` **if the provider uses inboxes** »
# (guide de migration messaging v2) — et répond **501** à la mauvaise, DANS LES DEUX
# SENS. Le client ne servait que LinkedIn quand la forme inbox est arrivée (delta live
# 2026-07-06) : la bascule a été faite en dur, donc appliquée aussi à WhatsApp/Telegram/
# Instagram/Messenger/Twitter, qui n'ont pas d'inbox → 501 sur `op="list"` pour ces cinq
# canaux, alors que leur compte est bien connecté. La forme est donc DÉCLARÉE par
# provider (ci-dessous) — pas devinée par canal appelant, pas figée à un seul modèle.
_INBOX_PROVIDERS = {"LINKEDIN"}
# Provider supposé quand l'appelant n'en déclare pas : le client est historiquement
# LinkedIn-first (`UNIPILE_LINKEDIN_ACCOUNT_ID`, découverte du 1er compte linkedin), et
# un appelant qui ne dit rien attend le comportement d'avant.
_DEFAULT_PROVIDER = "LINKEDIN"

# Préfixe de path par produit LinkedIn (search & co.).
_API_PREFIX = {
    "classic": "/linkedin/search",
    "sales_navigator": "/linkedin/sales-navigator/search",
    "recruiter": "/linkedin/recruiter/search",
}


def cursor_with_limit(cursor: str, limit: int) -> str:
    """Réécrit `limit` DANS un cursor Unipile (base64 de `{limit, startIndex}`).

    L'API Unipile fige le `limit` du 1er appel dans le cursor et IGNORE ensuite
    le param `limit` (feedback #179 : une pagination entamée à limit=3 restait
    bloquée à 3/page — des centaines d'appels pour un réseau entier). Le limit
    de l'appel courant doit primer. Forme de cursor inattendue (non-base64,
    non-JSON, pas de clé limit) → rendu tel quel, l'API tranche."""
    import base64
    import json
    try:
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4)))
        if isinstance(data, dict) and "limit" in data:
            data["limit"] = int(limit)
            return base64.b64encode(json.dumps(data).encode()).decode()
    except Exception:  # noqa: BLE001 — cursor opaque : jamais bloquant
        pass
    return cursor


def _sections_param(sections: str) -> Optional[list[str]]:
    """Map la valeur `sections` vers le param v2 `with_sections`.

    Entrée : `"*"` (tout) ou une liste séparée par virgules de noms nus
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


def _slug_from_company_url(url: str) -> Optional[str]:
    """Extrait le slug d'une URL LinkedIn société (`…/company/<slug>[/…]`)."""
    if not url:
        return None
    m = re.search(r"/company/([^/?#]+)", url)
    return m.group(1) if m else None


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


class UnipileRateLimited(UnipileError):
    """429 Unipile : quota amont atteint. LinkedIn cappe les fiches société/profil
    à ~100/12h PAR COMPTE (« We only allow 100 requests. Retry in N hours »). Type
    dédié + délai parsé → l'appelant STOPPE au lieu de marteler (251 appels perdus
    en 12h vécu 2026-07-21). `retry_after` = secondes avant réessai, None si illisible."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


_RETRY_RE = re.compile(r"retry in\s+(\d+)\s*(hour|hr|minute|min|second|sec)", re.I)


def _parse_retry_after(msg: str) -> Optional[int]:
    """Secondes avant réessai depuis un corps 429 (« Retry in 12 hours »). None sinon."""
    m = _RETRY_RE.search(msg or "")
    if not m:
        return None
    return int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2).lower()[0]]


class UnipileClient:
    """Client Unipile API v2 — hosted LinkedIn (et autres IM)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        dsn: Optional[str] = None,
        account_id: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self.api_key = api_key or require_secret("UNIPILE_API_KEY")
        self.dsn = dsn or get_secret("UNIPILE_DSN", DEFAULT_DSN)
        self.base_url = f"https://{self.dsn}/v2"
        self._account_id = account_id or get_secret("UNIPILE_LINKEDIN_ACCOUNT_ID")
        # Canal du compte opéré (LINKEDIN, WHATSAPP, …). Sert la forme d'endpoint de
        # messagerie (cf. `_INBOX_PROVIDERS`) ; None = supposé LinkedIn (compat).
        self.provider = (provider or "").strip().upper() or None
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
        timeout: Optional[tuple] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method, url, params=params, json=json,
                timeout=timeout or _REQUEST_TIMEOUT)
        except requests.RequestException as e:
            # DNS/timeout/reset : erreur stable au lieu de fuiter net::ERR_* (#177).
            raise UnipileError(
                self._sanitize(f"Unipile: erreur réseau ({type(e).__name__}).")
            ) from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = (body.get("detail") or body.get("message")
                       or body.get("title") or resp.text)
            except (ValueError, AttributeError):
                msg = resp.text or f"{resp.status_code} {resp.reason}"
            full = self._sanitize(f"Unipile {resp.status_code}: {msg}")
            # 429 = quota amont (LinkedIn cappe fiches société/profil ~100/12h par
            # compte) → type dédié + délai parsé, l'appelant STOPPE (cf. UnipileRateLimited).
            if resp.status_code == 429:
                raise UnipileRateLimited(full, retry_after=_parse_retry_after(msg))
            raise UnipileError(full, status_code=resp.status_code)
        if not resp.text:
            return None
        return resp.json()

    def _acct(self, sub_path: str) -> str:
        """Préfixe un sous-chemin par `/{account_id}` (path param v2)."""
        return f"/{quote(self.account_id(), safe='')}{sub_path}"

    def uses_inboxes(self) -> bool:
        """Le provider de ce compte range-t-il sa messagerie par inbox ? (cf.
        `_INBOX_PROVIDERS`). Provider non déclaré → supposé LinkedIn."""
        return (self.provider or _DEFAULT_PROVIDER) in _INBOX_PROVIDERS

    def _by_shape(self, inbox_call, plain_call, what: str) -> Any:
        """Appelle la forme d'endpoint DÉCLARÉE pour ce provider, et bascule sur
        l'autre si Unipile répond **501**.

        Le 501 d'Unipile n'est pas une panne : c'est l'amont qui NOMME la forme
        attendue (« Use List inbox Chats endpoint », « Use Start a Chat in the given
        inbox endpoint for this provider », et le symétrique pour un provider sans
        inbox). Le rattrapage vaut donc dans les deux sens et ne masque rien
        d'autre — tout autre statut remonte tel quel, et la bascule est
        JOURNALISÉE : si Unipile reclasse un provider, ça se lit dans les logs au
        lieu de casser un canal en silence, comme le 2026-07-06 côté LinkedIn puis
        ce jour-là côté WhatsApp. L'identité (`account_id`) est dans le path des
        deux formes : rien ne bascule ici que la ROUTE."""
        inbox_first = self.uses_inboxes()
        first, second = ((inbox_call, plain_call) if inbox_first
                         else (plain_call, inbox_call))
        try:
            return first()
        except UnipileError as e:
            if e.status_code != 501:
                raise
            logger.warning(
                "unipile %s: 501 sur la forme %s pour provider=%s — bascule sur "
                "la forme %s. Si ça se répète, `_INBOX_PROVIDERS` a dérivé du "
                "modèle Unipile.",
                what, "inbox" if inbox_first else "plate",
                self.provider or f"{_DEFAULT_PROVIDER} (supposé)",
                "plate" if inbox_first else "inbox")
            return second()

    @staticmethod
    def _norm(data: Any) -> Any:
        """Normalise une enveloppe de liste v2 `{data, next_cursor, total_count}`
        vers la forme `items`/`cursor` attendue par l'aval SANS perdre les champs
        natifs. No-op si `data` n'est pas une enveloppe de liste."""
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
        """`account_id` LinkedIn : celui fourni, sinon le 1er compte LinkedIn du compte
        Unipile.

        ⚠️ La casse du provider a CHANGÉ en v2 : un compte porte `provider:"linkedin"`
        (minuscules) et **plus de champ `type`** (champs v2 relevés en live :
        application_id, created_at, id, is_locked, metadata, name, object, provider,
        proxy, status, user_id). L'ancien test `== "LINKEDIN"` ne pouvait donc plus
        JAMAIS être vrai → la découverte automatique tombait toujours dans le « aucun
        compte LinkedIn connecté » alors qu'un compte opérationnel existait : un
        diagnostic qui MENT coûte des heures. Comparaison insensible à la casse, sur
        `provider` (v2) avec repli `type` (v1), et le message d'échec ÉNUMÈRE les
        providers réellement vus."""
        if self._account_id:
            return self._account_id
        seen: list[str] = []
        for acc in self.list_accounts():
            if not isinstance(acc, dict):
                continue
            provider = str(acc.get("provider") or acc.get("type") or "").strip()
            if provider:
                seen.append(provider)
            if provider.lower() == "linkedin" and acc.get("id"):
                self._account_id = str(acc["id"])
                return self._account_id
        inventory = (f" Comptes connectés : {', '.join(sorted(set(seen)))}."
                     if seen else " Aucun compte connecté sur cette clé Unipile.")
        raise UnipileError(
            "Aucun compte LinkedIn connecté sur Unipile "
            "(et UNIPILE_LINKEDIN_ACCOUNT_ID non défini)." + inventory
        )

    def account_alive(self, account_id: str) -> bool:
        """La SESSION du compte est-elle vivante ? `GET /v2/{account_id}/users/me` :
        200 = utilisable, 401 = déconnecté (checkpoint / login avorté / cookie mort).
        Distinct de `status:'running'` du compte, qui peut mentir sur un compte
        mort-né (wizard abandonné). Sert à ne binder qu'un compte RÉELLEMENT
        utilisable (un compte mort-né préféré à l'ancien sain = incident vécu)."""
        try:
            resp = self.session.request(
                "GET", f"{self.base_url}/{quote(account_id, safe='')}/users/me",
                timeout=_REQUEST_TIMEOUT)
        except requests.RequestException:
            return False
        return resp.status_code == 200

    # ---- hosted auth -----------------------------------------------------

    # Produits LinkedIn activables au lien hosted-auth (`config.linkedin.products`).
    # `classic` = la base, toujours incluse. Les deux PREMIUM sont EXCLUSIFS : un
    # compte ne peut en activer qu'UN (contrainte Unipile documentée).
    LINKEDIN_PREMIUM_PRODUCTS = ("recruiter", "sales_navigator")

    def hosted_auth_link(
        self,
        notify_url: Optional[str] = None,
        providers: Optional[list[str]] = None,
        name: Optional[str] = None,
        success_redirect_url: Optional[str] = None,
        failure_redirect_url: Optional[str] = None,
        ttl_minutes: int = 60,
        premium: Optional[str] = None,
        allow_cookies: bool = False,
        reconnect_account: Optional[str] = None,
    ) -> str:
        """URL d'auth hébergée (v2 : `POST /v2/auth/link`, createAuthLink).

        Schéma v2 : `expires_on` (snake) ; `providers` = liste de codes
        **minuscules** (`["linkedin"]`) ou `"*"` (tous) ; **un seul** `redirect_uri`
        (v2 ne sépare plus succès/échec) ; la réponse porte le lien sur **`link`**.
        `name`/`notify_url` restent acceptés (corrélation webhook du hosted-auth #131).

        ⚠️ **C'est à l'app d'activer les produits premium** : sans
        `config.linkedin.products`, Unipile ne connecte que `classic` → les
        endpoints Recruiter/Sales Navigator répondent 403 « out of your scope » et
        le wizard n'offre AUCUNE case premium (confirmé par le support Unipile).
        - `premium` : `"recruiter"` | `"sales_navigator"` | None. **Exclusifs** — un
          compte ne peut activer qu'un seul des deux.
        - `allow_cookies` : ajoute la connexion par cookies aux méthodes du wizard
          (sans lui, seul identifiant/mot de passe est proposé). **Recommandé par
          Unipile pour les produits premium.**
        - `reconnect_account` : `account_id` d'un compte EXISTANT → `type=reconnect`
          (rattache le produit/répare la session SUR ce compte) au lieu de `create`
          (qui ferait un DOUBLON). À utiliser pour activer un premium sur un compte
          déjà connecté."""
        expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        body: dict[str, Any] = {
            "type": "reconnect" if reconnect_account else "create",
            "providers": [str(p).lower() for p in providers] if providers else "*",
            "api_url": f"https://{self.dsn}",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        # v2 = un seul redirect_uri (l'échec n'a plus d'URL dédiée) ; on prend le
        # succès, sinon l'échec en repli.
        redirect = success_redirect_url or failure_redirect_url
        if redirect:
            body["redirect_uri"] = redirect
        if reconnect_account:
            body["reconnect_account"] = reconnect_account
        if notify_url:
            body["notify_url"] = notify_url
        if name:
            body["name"] = name
        # config.linkedin : produits à activer + méthodes de connexion offertes.
        # N'est posé que si on demande quelque chose de non-défaut (sinon Unipile
        # garde son comportement d'origine : classic + credentials).
        if premium or allow_cookies:
            if premium and premium not in self.LINKEDIN_PREMIUM_PRODUCTS:
                raise UnipileError(
                    f"premium invalide : {premium!r} (attendu "
                    f"{' ou '.join(map(repr, self.LINKEDIN_PREMIUM_PRODUCTS))}). "
                    "Un compte ne peut activer qu'UN produit premium."
                )
            cfg: dict[str, Any] = {}
            if premium:
                cfg["products"] = ["classic", premium]
            if allow_cookies:
                cfg["allow_methods"] = ["credentials", "cookies"]
            body["config"] = {"linkedin": cfg}
        data = self._request("POST", "/auth/link", json=body)
        return (data or {}).get("link") or (data or {}).get("url", "")

    # ---- facettes --------------------------------------------------------

    def resolve_facet(
        self, facet_type: str, keywords: str, limit: int = 100
    ) -> list[dict]:
        """Résout un nom en candidats de facette LinkedIn (v2 :
        `GET /v2/{account}/linkedin/search/parameters`). Renvoie `[{id, name}]` —
        le `name` est le LIBELLÉ lisible (« Microsoft Excel »), indispensable pour
        qu'un agent DÉSAMBIGÜISE (ex. 6 candidats pour « Microsoft Excel ») : la
        réponse porte le libellé sous `name` (pas `title`, historiquement null)."""
        params = {"type": facet_type, "keywords": keywords, "limit": limit}
        data = self._request(
            "GET", self._acct("/linkedin/search/parameters"), params=params
        )
        items = (data or {}).get("data") or (data or {}).get("items") or []
        return [{"id": it.get("id"),
                 "name": it.get("name") or it.get("title")} for it in items]

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
        skills: Optional[list] = None,
    ) -> dict:
        """Recherche LinkedIn. `company`/`location`/`industry`/`skills` = noms (résolus
        en facettes) ou ids numériques ; `industry`/`skills` acceptent aussi un dict
        `{include?, exclude?}`. Les formes d'encodage varient par PRODUIT et par
        FACETTE (vérifiées live, cf. `_facet_field`) — l'appelant passe juste noms/ids."""
        prefix = _API_PREFIX.get(api, _API_PREFIX["classic"])
        # #238 : pagination CURSOR-ONLY. Le cursor encode DÉJÀ toute la requête
        # (mots-clés + facettes). On NE reconstruit PAS le body et on ne re-résout
        # PAS les facettes (chaque nom→id = un GET amont ; empilés, ils faisaient
        # timeouter les pages Recruiter à 180s). On renvoie juste le cursor sur
        # l'endpoint structuré du produit. (Une recherche par `url` ne produit pas de
        # cursor → toute pagination est structurée.)
        if cursor:
            cat = "companies" if category == "companies" else "people"
            return self._norm(self._request(
                "POST", self._acct(f"{prefix}/{cat}"),
                params={"cursor": cursor}, json={}, timeout=_SCRAPE_TIMEOUT))
        params: dict[str, Any] = {}

        # Recherche par URL collée : endpoint from-url du produit, corps {url}.
        if url:
            try:
                return self._norm(self._request(
                    "POST", self._acct(prefix), params=params, json={"url": url},
                    timeout=_URL_SEARCH_TIMEOUT))
            except UnipileError as e:
                # Réseau/timeout SANS status HTTP = l'endpoint from-url n'a jamais
                # répondu → très probablement un searchContextId expiré/mort (#238).
                # Erreur PROPRE et actionnable au lieu d'un timeout MCP opaque.
                if getattr(e, "status_code", None) is None:
                    raise UnipileError(
                        "Recherche Recruiter par URL injoignable — le contexte de "
                        "recherche (searchContextId de l'URL) est probablement expiré "
                        "côté LinkedIn. Régénère l'URL depuis ton historique Recruiter, "
                        "ou passe à la recherche STRUCTURÉE (api='recruiter' + "
                        "keywords/company/location) puis pagine par cursor.") from e
                raise

        cat = "companies" if category == "companies" else "people"
        api_norm = api if api in _API_PREFIX else "classic"
        path = f"{prefix}/{cat}"
        body: dict[str, Any] = {}
        if keywords:
            body["keywords"] = keywords
        if advanced_keywords:
            ak = {k: v for k, v in advanced_keywords.items() if v}
            if ak:
                body["advanced_keywords"] = ak
        # ⚠️ La FORME des facettes (location/company/industry) diffère par produit
        # (contrat API v2 vérifié en live) — voir `_facet_field` :
        #   classic          : liste plate d'ids ["123"] (inclusion seule)
        #   sales_navigator  : {include:[ids], exclude:[ids]}
        #   recruiter        : [{id, ...}] (objets)
        loc = self._facet_field("LOCATION", location, api_norm)
        if loc is not None:
            body["location"] = loc
        ind = self._facet_field(
            "INDUSTRY", industry, api_norm,
            dict_input=True,  # `industry` est un dict {include?, exclude?}
        )
        if ind is not None:
            body["industry"] = ind
        comp = self._facet_field("COMPANY", company, api_norm)
        if comp is not None:
            # people-search : filtre EMPLOYEUR courant (`current_company`) ;
            # companies-search : le filtre société n'existe pas (on l'omet).
            if cat == "people":
                body["current_company"] = comp
        if cat == "people" and network_distance:
            body["network_distance"] = [int(d) for d in network_distance]
        if cat == "people":
            # `skills` = MÊME encodage de facette par produit que location/industry
            # (`_facet_field`) : recruiter → `[{id}]` (MUST_HAVE implicite) et
            # `[{id, priority:"DOESNT_HAVE"}]` pour l'exclusion — forme confirmée par la
            # doc Unipile (Recruiter people search). Accepte noms/ids OU dict
            # `{include?, exclude?}` (comme industry).
            sk = self._facet_field("SKILL", skills, api_norm,
                                   dict_input=isinstance(skills, dict))
            if sk is not None:
                body["skills"] = sk
        return self._norm(self._request(
            "POST", self._acct(path), params=params, json=body,
            timeout=_SCRAPE_TIMEOUT,
        ))

    def _facet_field(self, facet_type: str, value, api: str,
                     dict_input: bool = False):
        """Encode un filtre de facette selon le PRODUIT (contrat v2 vérifié en live).

        `value` = liste de noms/ids (défaut) OU dict `{include?, exclude?}` de
        noms/ids (`dict_input=True`, pour `industry`). Renvoie la valeur prête pour
        le corps, ou None si rien. `exclude` sur `classic` LÈVE (l'API classic n'a
        pas d'exclusion — concaténer include+exclude renvoyait les EXCLUS, faux en
        silence)."""
        if dict_input:
            inc = self._as_facet_ids(facet_type, (value or {}).get("include"))
            exc = self._as_facet_ids(facet_type, (value or {}).get("exclude"))
        else:
            inc = self._as_facet_ids(facet_type, value)
            exc = []
        if not inc and not exc:
            return None
        if api == "classic":
            if exc:
                raise UnipileError(
                    f"exclusion non supportée par api='classic' pour {facet_type.lower()} : "
                    "l'API LinkedIn classic n'accepte qu'une liste à INCLURE. Retire "
                    "`exclude`, ou utilise api='sales_navigator' / 'recruiter'.")
            return inc  # liste plate d'ids
        if api == "sales_navigator":
            out: dict[str, Any] = {}
            if inc:
                out["include"] = inc
            if exc:
                out["exclude"] = exc
            return out
        # recruiter : la forme dépend de la FACETTE (vérifié LIVE, contrat sélectionné) :
        #   INDUSTRY → objet `{include:[ids], exclude:[ids]}` (comme sales_navigator) ;
        #   SKILL    → `[{name: <id>}]` — ⚠️ le champ s'appelle `name` mais porte l'ID
        #              (un `name`=libellé ne filtre PAS ; `{id,...}` lève 400) ;
        #   LOCATION/COMPANY (défaut) → `[{id}]`.
        # Exclusion partout via `priority: "DOESNT_HAVE"`.
        if facet_type == "INDUSTRY":
            out2: dict[str, Any] = {}
            if inc:
                out2["include"] = inc
            if exc:
                out2["exclude"] = exc
            return out2
        key = "name" if facet_type == "SKILL" else "id"
        objs = [{key: i} for i in inc]
        objs += [{key: i, "priority": "DOESNT_HAVE"} for i in exc]
        return objs

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

    def _get_company_raw(self, identifier: str) -> dict:
        """GET société brut + garde anti-mismatch #153. Lève telle quelle
        (404 inclus) — le fallback de résolution vit dans `get_company`."""
        data = self._request(
            "GET", self._acct(f"/linkedin/company/{quote(identifier, safe='')}"),
            timeout=_SCRAPE_TIMEOUT,
        )
        if not self._identity_ok(identifier, data, "CompanyProfile"):
            got = (data or {}).get("public_identifier") or (data or {}).get("id")
            raise UnipileError(
                f"Unipile identity_mismatch: société demandée {identifier!r}, "
                f"reçu {got!r} (object={(data or {}).get('object')!r}). "
                "Réponse rejetée — réessaie."
            )
        return data

    def _resolve_company_slugs(self, name: str, limit: int = 5) -> list[str]:
        """#176 : cherche des sociétés par nom → `public_identifier` candidats,
        par ordre de pertinence. Best-effort : ne doit jamais masquer le 404
        d'origine (toute erreur de recherche → aucun candidat)."""
        try:
            res = self.search(category="companies", keywords=name)
        except Exception:  # noqa: BLE001 — résolution best-effort, jamais fatale
            return []
        items = (res or {}).get("items") or (res or {}).get("data") or []
        out: list[str] = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            slug = it.get("public_identifier") or _slug_from_company_url(
                it.get("public_profile_url") or it.get("profile_url") or ""
            )
            if slug:
                out.append(slug)
        return list(dict.fromkeys(out))  # dédup en conservant l'ordre

    def get_company(self, identifier: str, resolve: bool = True) -> dict:
        """Fiche société. `identifier` = slug (`public_identifier`) ou id numérique.

        Garde #153 : rejette une réponse d'un autre objet/identifiant.
        Résolution tolérante #176 : si le slug fourni est introuvable (404) et
        non numérique, on tente une recherche société par nom pour retrouver le
        `public_identifier` canonique (ex. `mooniz` → `mooniz1`) et on réessaie.
        Échec → 404 propre enrichi des candidats proches (`resolve=False` coupe
        le fallback)."""
        try:
            return self._get_company_raw(identifier)
        except UnipileError as e:
            ident = str(identifier).strip()
            if not resolve or e.status_code != 404 or ident.isdigit():
                raise
            candidates = self._resolve_company_slugs(ident)
            for slug in candidates:
                if slug.strip().lower() != ident.lower():
                    try:
                        return self._get_company_raw(slug)
                    except UnipileError:
                        continue
            if candidates:
                raise UnipileError(
                    f"Unipile 404: société {identifier!r} introuvable. "
                    f"Slugs candidats proches : {', '.join(candidates)}.",
                    status_code=404,
                ) from e
            raise

    # ---- messagerie ------------------------------------------------------

    def list_inboxes(self) -> dict:
        """Inboxes du compte (v2 : `GET /v2/{account}/inboxes`). LinkedIn classic :
        `CLASSIC_PRIMARY` (principale), `CLASSIC_ARCHIVED`, `CLASSIC_SPAM`,
        `CLASSIC_JOBS`, `CLASSIC_INMAIL`, `CLASSIC_STARRED`."""
        return self._norm(self._request("GET", self._acct("/inboxes")))

    def list_chats(self, limit: int = 20, cursor: Optional[str] = None,
                   with_attendee_names: bool = False,
                   inbox: str = "CLASSIC_PRIMARY") -> dict:
        """Fils de messagerie, dans la forme d'endpoint du provider (`_by_shape`) :
        **par inbox** pour LinkedIn (`GET /v2/{account}/inboxes/{inbox}/chats` —
        l'ancien `/chats` y renvoie 501 « Use List inbox Chats endpoint », delta live
        2026-07-06), **à plat** pour les providers sans inbox (WhatsApp, Telegram,
        Instagram, Messenger, Twitter), où c'est la forme inbox qui rend 501.
        `inbox` (LinkedIn) défaut = `CLASSIC_PRIMARY` ; autres via `list_inboxes`."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._norm(self._by_shape(
            lambda: self._request(
                "GET", self._acct(f"/inboxes/{quote(inbox, safe='')}/chats"),
                params=params),
            lambda: self._request("GET", self._acct("/chats"), params=params),
            "list_chats"))
        if with_attendee_names:
            self._annotate_chat_attendees(data)
        return data

    def resolve_attendee_names(self, provider_ids, max_pages: int = 10,
                               page_limit: int = 100) -> dict:
        """Résout des `attendee_provider_id` via le carnet de contacts v2
        (`/v2/{account}/contacts`, paginé). Best-effort."""
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
            logger.warning("unipile chats: résolution attendees échouée, "
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
        inbox: str = "CLASSIC_PRIMARY",
    ) -> dict:
        if chat_id:
            return self._request(
                "POST", self._acct(f"/chats/{quote(chat_id, safe='')}/messages/send"),
                json={"text": text},
            )
        if not attendee_id:
            raise UnipileError("send_message : chat_id ou attendee_id requis.")
        # v2 : pour un provider à INBOX (LinkedIn), le nouveau fil passe par l'inbox —
        # `POST /v2/{account}/inboxes/{inbox}/chats/send`. Le `/chats/send` générique y
        # renvoie 501 « Use Start a Chat in the given inbox endpoint for this provider »
        # (relevé live 2026-07-08 — même modèle inbox que list_chats, signal #199/#200) ;
        # sans inbox (WhatsApp & co.), c'est l'inverse. Même corps des deux côtés
        # (`users_ids`, qui remplace `attendees_ids` de v1) : seule la route change.
        body = {"users_ids": [attendee_id], "text": text}
        return self._by_shape(
            lambda: self._request(
                "POST", self._acct(f"/inboxes/{quote(inbox, safe='')}/chats/send"),
                json=body),
            lambda: self._request("POST", self._acct("/chats/send"), json=body),
            "send_message",
        )

    # ---- réseau / outreach ----------------------------------------------

    def list_relations(self, cursor: Optional[str] = None,
                       limit: Optional[int] = None) -> dict:
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
        `shared_secret`, gardé dans la signature pour compat appelant). accept →
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

    def _member_id(self, identifier: str) -> str:
        """Résout un identifiant de membre vers le **provider_id (URN, `ACoAA…`)**
        attendu par les endpoints posts/comments/reactions v2 : le slug public y
        renvoie 400 « Invalid User ID » (delta v2 relevé en live 2026-07-06). URN
        déjà opaque → tel quel ; slug → résolu via le profil (1 appel)."""
        ident = str(identifier).strip()
        if ident.startswith(("ACoA", "urn:")):
            return ident
        prof = self.get_profile(ident)
        return str((prof or {}).get("provider_id") or (prof or {}).get("id") or ident)

    def list_member_posts(self, identifier: str, cursor: Optional[str] = None,
                          limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(self._member_id(identifier), safe='')}/posts"),
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
        """Réagit à un post. v2 : corps `{reaction}`."""
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
        """Feed d'accueil LinkedIn via la Magic Route Voyager."""
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
            "GET", self._acct(f"/users/{quote(self._member_id(identifier), safe='')}/comments"),
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
            "GET", self._acct(f"/users/{quote(self._member_id(identifier), safe='')}/reactions"),
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

    # v2 updateChat : champs dédiés (plus le couple {action, value}).
    _CHAT_ACTION_FIELD = {
        "setReadStatus": "read_status",
        "setMuteStatus": "muted_until",
        "setArchiveStatus": "archive_status",
        "setPinnedStatus": "pin_status",
        "setLabel": "label",
    }

    def patch_chat(self, chat_id: str, action: str, value: Any = None) -> dict:
        """Modifie l'état d'un fil (`PATCH /chats/{id}`)."""
        field = self._CHAT_ACTION_FIELD.get(action)
        if field is None:
            raise UnipileError(
                f"patch_chat : action {action!r} non supportée "
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
                "react_message : chat_id requis "
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
        """Solde InMail. v2 : `GET /linkedin/inmail-credits`. Réponse `{object, credits}`."""
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
                raise UnipileError("saveLead : list_id (lead-list) requis.")
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
                    "pipeline recruiter : hiring_project_id requis."
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
            f"member_action : combinaison api={api!r} action={action!r} "
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


def _annotated_entity(node: Any) -> Optional[str]:
    """Nom de la PREMIÈRE entité annotée d'un texte Voyager. Voyager livre ses
    libellés en texte annoté — `{text: "Jean Dupont a commenté ceci", attributes:
    [{start, length, …}]}` — où la 1re annotation couvre l'acteur. On en découpe
    la tranche plutôt que de deviner par expression régulière (indépendant de la
    langue de l'interface). None si la forme n'est pas celle-là."""
    if not isinstance(node, dict):
        return None
    text = node.get("text")
    if isinstance(text, dict):  # `{text: {text, attributes}}`
        return _annotated_entity(text)
    attrs = node.get("attributes")
    if not isinstance(text, str) or not isinstance(attrs, list) or not attrs:
        return None
    first = attrs[0]
    if not isinstance(first, dict):
        return None
    start, length = first.get("start"), first.get("length")
    if not isinstance(start, int) or not isinstance(length, int) or length <= 0:
        return None
    name = text[start:start + length].strip()
    return name or None


def _feed_context(el: dict) -> tuple[Optional[str], Optional[str]]:
    """(feed_reason, surfaced_by) — POURQUOI ce post remonte dans MON feed.

    Un post d'inconnu apparaît presque toujours par REBOND d'une relation : « X a
    commenté ceci », « X a réagi », repartage. Cette raison est le cœur du social
    selling par rebond (qui de mon réseau interagit avec qui) et elle était perdue
    au mapping (feedback #280) : `feed_reason` = le libellé Voyager verbatim,
    `surfaced_by` = le nom de la relation à l'origine de la remontée.

    Best-effort : `header` (emplacement usuel du libellé de rebond) puis
    `socialContext`. Aucune des deux ⇒ (None, None) = post remonté directement."""
    for node in (el.get("header"), el.get("socialContext")):
        reason = _text_of(node)
        if reason:
            return reason, _annotated_entity(node)
    return None, None


def _comment_authors(el: dict, included_by_urn: dict,
                     activity_urn: Optional[str]) -> list[str]:
    """Auteurs des commentaires visibles sur cet update, dans l'ordre de rencontre.

    Le feed ne porte pas les commentaires complets, mais Voyager y joint les
    commentaires MIS EN AVANT (ceux qui font remonter le post) : à défaut du fil
    entier, garder QUI a commenté suffit à répondre « qui de mon réseau interagit
    avec qui » (feedback #280). Deux pistes : le `socialDetail` (inline ou
    déréférencé) puis les objets `comment` d'`included` rattachés à cette activité
    (leur `entityUrn` porte l'id d'activité). Best-effort, dédupliqué."""
    names: list[str] = []

    def _add(commenter: Any) -> None:
        if isinstance(commenter, str):  # référence `*commenter` → included
            commenter = included_by_urn.get(commenter)
        name = (_text_of(_deep_get(commenter, "name"))
                or _text_of(_deep_get(commenter, "title"))
                or _text_of(commenter))
        if name and name not in names:
            names.append(name)

    sd = el.get("socialDetail")
    if sd is None:
        ref = el.get("*socialDetail")
        if isinstance(ref, str):
            sd = included_by_urn.get(ref)
    for c in _deep_get(sd, "comments", "elements", default=[]) or []:
        if isinstance(c, dict):
            _add(c.get("commenter") or c.get("*commenter"))

    if activity_urn:
        for urn, obj in included_by_urn.items():
            if "comment" in urn.lower() and activity_urn in urn and isinstance(obj, dict):
                _add(obj.get("commenter") or obj.get("*commenter"))
    return names


# --- DE QUOI un post est fait (bloc `content` de l'update) -------------------
# Voyager range le média d'un post dans `content`, sous une clé qui NOMME le type de
# composant (`imageComponent`, `pollComponent`, `carouselContent`… — 42 noms relevés
# sur un feed réel). Ce bloc était intégralement jeté au mapping : un post à 2 775
# réactions dont le texte se réduit à « 🧐 » (tout le propos est dans l'image) devenait
# INCLASSABLE pour un agent — le post le plus engageant d'une page, invisible.
# Le bloc brut pèse ~4 700 caractères (images en 4 résolutions + tracking) : on n'en
# garde que le TYPE normalisé + l'intitulé porteur de sens quand il est là, ~100
# caractères. Le type est DÉRIVÉ du nom de la clé (suffixe `Component`/`Content`
# retiré, camelCase → snake_case), pas d'une table exhaustive à maintenir : un
# composant jamais vu rend son propre nom normalisé plutôt qu'un « unknown » muet.
# La table ci-dessous ne porte donc QUE les synonymes à replier.
_CONTENT_ALIASES = {
    "linked_in_video": "video",     # vidéo native LinkedIn
    "external_video": "video",      # YouTube & co. embarqués
    "native_video": "video",
    "slideshow": "carousel",        # diaporama d'images = un carrousel
}
# Ordre de DOMINANCE quand un update porte plusieurs composants : le premier de cette
# liste gagne. Classement par pouvoir de tri décroissant — ce qui appelle une action
# précise (sondage, document, article) avant le simple habillage (image). Un type
# inconnu passe après tous les connus (il informe, mais on ne sait pas encore combien).
_CONTENT_PRIORITY = ("poll", "document", "article", "newsletter", "event", "job",
                     "celebration", "video", "carousel", "image", "entity")
# Clés d'intitulé sondées sur le composant dominant (titre d'article, question d'un
# sondage, titre d'un document, texte alternatif d'une image…).
_CONTENT_LABEL_KEYS = ("title", "question", "headline", "name",
                       "altText", "accessibilityText")
_CONTENT_LABEL_MAX = 140   # borne le pire cas (≈ la limite d'une question de sondage)
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _content_key_to_type(key: str) -> Optional[str]:
    """`imageComponent` → `image`, `linkedInVideoComponent` → `video`,
    `carouselContent` → `carousel`. None si la clé n'est pas un composant de contenu
    (`resharedUpdate`, `$type`… restent hors du compte)."""
    for suffix in ("Component", "Content"):
        if key.endswith(suffix) and len(key) > len(suffix):
            base = _CAMEL_SPLIT.sub("_", key[: -len(suffix)]).lower()
            return _CONTENT_ALIASES.get(base, base)
    return None


def _content_label(node: Any) -> Optional[str]:
    """Intitulé porteur de sens d'un composant, s'il est disponible SANS COÛT (déjà
    dans la charge utile) : titre d'article, question de sondage, titre de document,
    texte alternatif d'une image. Sondé sur le composant, puis UN cran plus bas — ses
    sous-objets (`document.title`) et le 1er élément de ses listes
    (`images[0].accessibilityText`), là où Voyager range ces intitulés.
    Tronqué à `_CONTENT_LABEL_MAX`. None si le composant n'en porte pas."""
    if not isinstance(node, dict):
        return None
    candidates = [node]
    for value in node.values():
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            candidates.append(value[0])
    for obj in candidates:
        for key in _CONTENT_LABEL_KEYS:
            label = _text_of(obj.get(key))
            if isinstance(label, str) and label.strip():
                return label.strip()[:_CONTENT_LABEL_MAX]
    return None


def _content_facets(content: Any) -> tuple[str, Optional[str]]:
    """(content_type, content_title) d'un bloc `content` Voyager.

    Pas de bloc / aucun composant reconnaissable → `("text", None)` : le post ne porte
    que son texte, ce n'est pas un échec de mapping (un vrai schéma inattendu, lui,
    fait lever `_map_feed_item` et l'item est journalisé puis ignoré).
    Plusieurs composants → le DOMINANT (`_CONTENT_PRIORITY`, puis ordre d'apparition)
    donne le type ET l'intitulé : un champ scalaire reste filtrable à l'égalité en aval
    (miroir datastore), là où une liste ou un « image+article » ne l'est pas."""
    if not isinstance(content, dict):
        return "text", None
    found: list[tuple[int, int, str, Any]] = []
    for i, (key, node) in enumerate(content.items()):
        # ⚠️ Voyager déclare TOUTES les clés de son schéma GraphQL, la quasi-totalité à
        # `null` : la PRÉSENCE d'une clé ne dit rien, seule sa VALEUR compte. Sans ce
        # test, `dynamicPollComponent: null` faisait un sondage de n'importe quel post —
        # et `poll` étant en tête de la dominance, 48 posts sur 60 sont sortis en `poll`
        # au premier run réel (12/08). Une donnée fausse écrite à chaque sync est pire
        # que pas de donnée : l'agent trie dessus sans pouvoir en douter.
        if node is None or node == {} or node == [] or node == "":
            continue
        ctype = _content_key_to_type(key)
        if not ctype:
            continue
        rank = (_CONTENT_PRIORITY.index(ctype) if ctype in _CONTENT_PRIORITY
                else len(_CONTENT_PRIORITY))
        found.append((rank, i, ctype, node))
    if not found:
        return "text", None
    found.sort(key=lambda f: (f[0], f[1]))
    _, _, ctype, node = found[0]
    if ctype not in _CONTENT_PRIORITY:
        # Composant jamais vu : on rend son nom tel que Voyager le nomme (normalisé)
        # plutôt qu'un « unknown » muet — traçable quand LinkedIn en ajoute un.
        logger.debug("unipile feed: composant de contenu inconnu (%s)", ctype)
    return ctype, _content_label(node)


def _map_feed_item(el: dict, included_by_urn: dict) -> dict:
    """Un update Voyager → item normalisé. Lève si `el` n'est pas un update
    exploitable (ni actor ni commentary) — l'appelant gère le fallback."""
    actor = el.get("actor") if isinstance(el.get("actor"), dict) else {}
    commentary = el.get("commentary") if isinstance(el.get("commentary"), dict) else {}
    if not actor and not commentary:
        raise ValueError("element sans actor/commentary (pas un update feed)")

    activity_urn = _activity_urn_from(el)
    reactions, comments = _social_counts(el, included_by_urn)
    # POURQUOI ce post remonte dans MON feed (« Untel a commenté ceci », « Untel a
    # réagi ») : c'est le souvenir le plus fréquent de l'utilisateur — il se rappelle
    # QUI a fait remonter le post, pas son auteur. Sans ce champ, un post retrouvé
    # « par rebond » est introuvable dans le miroir (signal #280 : recherche d'un post
    # vu via le commentaire d'une relation → 0 résultat sur 710 posts miroir).
    # `_feed_context` lit `header` PUIS `socialContext` (repli) et rend aussi le NOM de
    # la relation à l'origine de la remontée — une lecture du seul `header` perdait les
    # deux.
    feed_reason, surfaced_by = _feed_context(el)
    post_url = (
        f"https://www.linkedin.com/feed/update/{activity_urn}"
        if activity_urn else None
    )
    # REPOST : `author_name` est alors le re-partageur et `text` son commentaire de
    # partage — l'auteur ORIGINAL, celui qu'on cherche, se perdait entièrement.
    reshared = el.get("resharedUpdate") if isinstance(el.get("resharedUpdate"), dict) else {}
    if not reshared:
        reshared = _deep_get(el, "content", "resharedUpdate", default={}) or {}
    reshared_actor = reshared.get("actor") if isinstance(reshared.get("actor"), dict) else {}
    # …et son COMMENTAIRE aussi : sur un repost, `text` porte le mot du re-partageur —
    # souvent vide ou « 👏 » — pendant que le contenu réel, celui sur lequel la règle de
    # tri veut juger, restait introuvable. Même traitement de type que le post porteur.
    reshared_commentary = (reshared.get("commentary")
                           if isinstance(reshared.get("commentary"), dict) else {})
    content_type, content_title = _content_facets(el.get("content"))
    return {
        "urn": activity_urn or el.get("entityUrn"),
        "author_name": _text_of(actor.get("name")),
        "author_headline": _text_of(actor.get("description")),
        "text": _text_of(commentary.get("text")) or _text_of(commentary),
        "posted_at": _posted_at_from_activity(activity_urn),
        "posted_relative": _text_of(actor.get("subDescription")),
        "reactions_count": reactions,
        "comments_count": comments,
        # Pourquoi ce post remonte + qui l'a fait remonter + qui a commenté
        # (feedback #280 : le rebond par une relation était perdu au mapping).
        "feed_reason": feed_reason,
        "surfaced_by": surfaced_by,
        "comment_authors": _comment_authors(el, included_by_urn, activity_urn),
        # DE QUOI le post est fait : sans ça, un post dont tout le propos est dans
        # l'image (texte = « 🧐 », 2 775 réactions) est inclassable — le type normalisé
        # + l'intitulé gratuit (titre d'article, question de sondage) le rendent triable
        # sans rapatrier le bloc `content` (~4 700 caractères, 93 % de tracking et de
        # miniatures). `content_type` vaut toujours quelque chose (`text` = post nu).
        "content_type": content_type,
        "content_title": content_title,
        "post_url": post_url,
        "is_repost": bool(reshared),
        "original_author_name": _text_of(reshared_actor.get("name")) or None,
        # Sur un repost, la substance est dans l'ORIGINAL : son texte et la nature de
        # son contenu. None hors repost (le champ reste présent : le miroir aval
        # projette des colonnes fixes).
        "original_text": (_text_of(reshared_commentary.get("text"))
                          or _text_of(reshared_commentary) or None) if reshared else None,
        "original_content_type": (_content_facets(reshared.get("content"))[0]
                                  if reshared else None),
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
