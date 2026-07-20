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

# Feed d'accueil LinkedIn : LinkedIn n'expose AUCUN endpoint feed côté API
# Unipile. Le seul chemin est la Magic Route Voyager, exposée en v2 comme le proxy
# générique `POST /v2/{account_id}/linkedin/` (proxyRequest) : on relaie une
# requête Voyager brute. ⚠️ Voyager n'est PAS contractuel : ce queryId GraphQL et
# le schéma JSON peuvent casser quand LinkedIn fait évoluer son API interne
# (capture devtools sur linkedin.com/feed pour le rafraîchir). Source du queryId :
# https://developer.unipile.com/docs/get-raw-data-example
FEED_QUERY_ID = "voyagerFeedDashMainFeed.7a50ef8ba5a7865c23ad5df46f735709"

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


class UnipileClient:
    """Client Unipile API v2 — hosted LinkedIn (et autres IM)."""

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
        """Recherche LinkedIn. `company`/`location`/`industry` = noms (résolus en
        facettes) ou ids numériques."""
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
                params={"cursor": cursor}, json={}))
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
        return self._norm(self._request(
            "POST", self._acct(path), params=params, json=body
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
        # recruiter : liste d'objets {id} (priority/scope optionnels, non exposés ici).
        # L'exclusion recruiter se fait via priority=DOESNT_HAVE.
        objs = [{"id": i} for i in inc]
        objs += [{"id": i, "priority": "DOESNT_HAVE"} for i in exc]
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
        """Fils de messagerie. v2 : les chats sont rangés **par inbox**
        (`GET /v2/{account}/inboxes/{inbox}/chats`) — l'ancien `/chats` renvoie
        501 « Use List inbox Chats endpoint » pour LinkedIn (delta live 2026-07-06).
        `inbox` défaut = `CLASSIC_PRIMARY` (boîte principale) ; autres inboxes via
        `list_inboxes`."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._norm(self._request(
            "GET", self._acct(f"/inboxes/{quote(inbox, safe='')}/chats"),
            params=params))
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
        # `POST /v2/{account}/inboxes/{inbox}/chats/send`, `users_ids`. Le `/chats/send`
        # générique renvoie 501 « Use Start a Chat in the given inbox endpoint for this
        # provider » (relevé live 2026-07-08 — même modèle inbox que list_chats). Signal #199/#200.
        return self._request(
            "POST", self._acct(f"/inboxes/{quote(inbox, safe='')}/chats/send"),
            json={"users_ids": [attendee_id], "text": text},
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
