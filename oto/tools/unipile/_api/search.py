"""Recherche LinkedIn (classic / sales navigator / recruiter) et facettes.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

from typing import Any, Optional

from ..const import _API_PREFIX, _SCRAPE_TIMEOUT, _URL_SEARCH_TIMEOUT
from ..errors import UnipileError


class _SearchMixin:
    """Recherche LinkedIn (classic / sales navigator / recruiter) et facettes."""

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

