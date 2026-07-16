"""
Cognism Search API Client — B2B contact & account search, reveal, and
identity-based enrichment.

Synchronous REST API (developers.cognism.com). Auth = API key as Bearer token
(`Authorization: Bearer <key>`). Base: https://app.cognism.com/api/search

Endpoints couverts :
- POST /contact/search   — recherche de contacts (preview, pas d'email/téléphone réel)
- POST /account/search   — recherche de sociétés (preview)
- POST /contact/redeem   — reveal complet par id/redeemId (consomme des crédits)
- POST /account/redeem   — reveal société par id/redeemId
- POST /contact/enrich   — retrouve UN contact depuis des critères d'identité
- POST /account/enrich   — retrouve UNE société depuis des critères d'identité
- GET  /entitlement/contactEntitlementSubscription — champs visibles par la clé (contact)
- GET  /entitlement/accountEntitlementSubscription — champs visibles par la clé (account)
- GET  /filter/{kind}    — valeurs autorisées pour les champs à liste dynamique
  (regions, countries, states, industries, sic, isic, naics, skills,
  technologies, companySizes, companyTypes, jobFunctions, managementLevels,
  seniority)

Pagination : curseur (`lastReturnedKey`), PAS un offset — Cognism ne permet
pas de sauter une page (il faut paginer séquentiellement depuis le début).

La DSL de filtre (`filters`) est volontairement un dict opaque passé tel quel
(même forme que le JSON attendu par Cognism) plutôt que modélisée champ par
champ : ~150 champs, nombreux niveaux d'imbrication (`account.*`,
`previousAccounts.*`, `account.hiringEvent.*`, `account.fundingEvent.*`,
`locationMoveEvent.*`, `jobJoinEvent.*`, `jobLeaveEvent.*`,
`searchOptions.*`) — la doc complète vit dans le guide `cognism-filters`
côté oto-backend, pas ici. Les champs à valeurs FERMÉES (seniority,
jobFunctions, managementLevel, account.types, funding type/series, hiring
department, sort_fields, les enums de accountSearchOptions) sont validés
côté client (`enums.validate_enum_filters`) : une valeur hors liste lève une
`ValueError` explicite plutôt que de laisser filer une requête qui répond 200
avec une page vide (le mode d'échec silencieux le plus probable ici).

Requires: requests
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from .enums import validate_enum_filters


class CognismClient:
    """Client pour l'API Search de Cognism (Contacts & Accounts)."""

    BASE_URL = "https://app.cognism.com/api/search"
    TIMEOUT = 30

    _FILTER_ENDPOINTS = {
        "technologies": "technologiesSearch",
        "managementLevels": "managementLevels",
        "companySizes": "companySizes",
        "industries": "industries",
        "jobFunctions": "jobFunctions",
        "regions": "regions",
        "countries": "countries",
        "states": "states",
        "sic": "sic",
        "isic": "isic",
        "naics": "naics",
        "skills": "skills",
        "companyTypes": "companyTypes",
        "seniority": "seniority",
    }

    def __init__(self, api_key: str | None = None):
        """
        Args:
            api_key: clé Cognism (Bearer). À défaut, lue de l'env
                `COGNISM_API_KEY`.
        """
        self.api_key = api_key or require_secret("COGNISM_API_KEY")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        resp = requests.request(
            method, url, headers=self._headers(), json=json, params=params,
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    # ---- recherche (preview — pas d'email/téléphone réel) -------------------

    def search_contacts(
        self,
        filters: Optional[Dict[str, Any]] = None,
        *,
        index_size: int = 25,
        last_returned_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Recherche de contacts (preview data). `filters` = dict au format
        JSON exact attendu par Cognism (champs top-level type firstName/
        jobTitles/seniority/…, plus `account`/`previousAccounts`/
        `searchOptions`/`locationMoveEvent`/`jobJoinEvent`/`jobLeaveEvent`
        imbriqués) — voir le guide `cognism-filters` pour le détail complet.

        Renvoie la page brute Cognism : `results[]` (contacts avec des flags
        booléens `has*`, PAS d'email/téléphone réel — utiliser
        `redeem_contacts` pour le reveal), `totalResults`, `lastReturnedKey`
        (curseur pour la page suivante — pagination SÉQUENTIELLE uniquement,
        impossible de sauter une page).

        Args:
            index_size: taille de page, défaut 25, max 100.
            last_returned_key: curseur de la page précédente. Vide = 1ère page.
        """
        validate_enum_filters(filters, scope="contact")
        return self._request(
            "POST", "contact/search",
            json=filters or {},
            params={"indexSize": index_size, "lastReturnedKey": last_returned_key or ""},
        )

    def search_accounts(
        self,
        filters: Optional[Dict[str, Any]] = None,
        *,
        index_size: int = 100,
        last_returned_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Recherche de sociétés (preview data). `filters` = dict au format
        JSON exact attendu par Cognism (names/domains/industries/headcount/
        technologies/… + `accountSearchOptions`) — voir le guide
        `cognism-filters`.

        Renvoie la page brute Cognism : `results[]` (flags `has*`),
        `totalResults`, `lastReturnedKey` (curseur, pagination séquentielle).

        Args:
            index_size: taille de page, défaut 100, max 100.
            last_returned_key: curseur de la page précédente. Vide = 1ère page.
        """
        validate_enum_filters(filters, scope="account")
        return self._request(
            "POST", "account/search",
            json=filters or {},
            params={"indexSize": index_size, "lastReturnedKey": last_returned_key or ""},
        )

    # ---- reveal (consomme des crédits) --------------------------------------

    def redeem_contacts(
        self,
        *,
        ids: Optional[List[str]] = None,
        redeem_ids: Optional[List[str]] = None,
        merge_phones_and_locations: bool = False,
    ) -> Dict[str, Any]:
        """Reveal complet (email/téléphone réels) d'un lot de contacts par
        `id` OU `redeemId` (issus d'un `search_contacts` précédent) — mix des
        deux dans un même appel non supporté par Cognism. CONSOMME DES
        CRÉDITS (contrairement à `search_contacts`).

        Args:
            ids: contact ids. OU…
            redeem_ids: redeemIds (identifient contact+poste+société à un
                instant donné — cf. doc Cognism sur la dérive de `redeemId`).
                Exactement un des deux requis.
            merge_phones_and_locations: fusionne les tableaux phones/locations
                dans la réponse.
        """
        if bool(ids) == bool(redeem_ids):
            raise ValueError(
                "redeem_contacts requires exactly one of `ids` or `redeem_ids`."
            )
        body = {"ids": ids} if ids else {"redeemIds": redeem_ids}
        return self._request(
            "POST", "contact/redeem",
            json=body,
            params={"mergePhonesAndLocations": str(merge_phones_and_locations).lower()},
        )

    def redeem_accounts(
        self,
        *,
        ids: Optional[List[str]] = None,
        redeem_ids: Optional[List[str]] = None,
        merge_phones_and_locations: bool = False,
    ) -> Dict[str, Any]:
        """Reveal complet d'un lot de sociétés par `id` OU `redeemId`.
        CONSOMME DES CRÉDITS. Voir `redeem_contacts` pour la sémantique.
        """
        if bool(ids) == bool(redeem_ids):
            raise ValueError(
                "redeem_accounts requires exactly one of `ids` or `redeem_ids`."
            )
        body = {"ids": ids} if ids else {"redeemIds": redeem_ids}
        return self._request(
            "POST", "account/redeem",
            json=body,
            params={"mergePhonesAndLocations": str(merge_phones_and_locations).lower()},
        )

    # ---- enrichissement (identité -> meilleur match, scoré) -----------------

    def enrich_contact(
        self,
        *,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        sha256: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        phone_number: Optional[str] = None,
        job_title: Optional[str] = None,
        account_name: Optional[str] = None,
        account_website: Optional[str] = None,
        anchor_fields: Optional[List[str]] = None,
        min_match_score: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Retrouve UN contact depuis des critères d'identité (best match,
        scoré). Meilleure précision avec un identifiant unique (`email`/
        `sha256`/`linkedin_url`), OU la combinaison `first_name`+`last_name`+
        `job_title` avec `account_name`/`account_website`. Fournir le plus de
        champs possible — Cognism renvoie le meilleur match trouvé.

        `min_match_score` : score minimum pour renvoyer un résultat (défaut
        Cognism = 30 ; <27 = match de faible qualité).

        Lève `ValueError` si AUCUN champ d'identité n'est fourni (appel vide,
        n'a pas de sens côté API).
        """
        body: Dict[str, Any] = {}
        if first_name: body["firstName"] = first_name
        if last_name: body["lastName"] = last_name
        if email: body["email"] = email
        if sha256: body["sha256"] = sha256
        if linkedin_url: body["linkedinUrl"] = linkedin_url
        if phone_number: body["phoneNumber"] = phone_number
        if job_title: body["jobTitle"] = job_title
        if account_name: body["accountName"] = account_name
        if account_website: body["accountWebsite"] = account_website
        if anchor_fields: body["anchorFields"] = anchor_fields
        if min_match_score is not None: body["minMatchScore"] = min_match_score
        if not body:
            raise ValueError("enrich_contact requires at least one identity field.")
        return self._request("POST", "contact/enrich", json=body)

    def enrich_account(
        self,
        *,
        name: Optional[str] = None,
        website: Optional[str] = None,
        domain: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        anchor_fields: Optional[List[str]] = None,
        min_match_score: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Retrouve UNE société depuis des critères d'identité (best match,
        scoré). Meilleure précision avec un identifiant unique (`website`/
        `domain`/`linkedin_url`), OU `name` combiné à `country`/`city` (HQ ou
        bureau). Défaut Cognism `minMatchScore` = 40 (<35 = match de faible
        qualité — seuil différent de `enrich_contact`, où le défaut est 30).
        Lève `ValueError` si aucun champ n'est fourni.
        """
        body: Dict[str, Any] = {}
        if name: body["name"] = name
        if website: body["website"] = website
        if domain: body["domain"] = domain
        if linkedin_url: body["linkedinUrl"] = linkedin_url
        if country: body["country"] = country
        if city: body["city"] = city
        if anchor_fields: body["anchorFields"] = anchor_fields
        if min_match_score is not None: body["minMatchScore"] = min_match_score
        if not body:
            raise ValueError("enrich_account requires at least one identity field.")
        return self._request("POST", "account/enrich", json=body)

    # ---- entitlement (quels champs cette clé peut voir) ---------------------

    def contact_entitlement(self) -> Dict[str, Any]:
        """Détail de l'entitlement Contact de la clé configurée (quels champs
        sont visibles — email/téléphones/etc.)."""
        return self._request("GET", "entitlement/contactEntitlementSubscription")

    def account_entitlement(self) -> Dict[str, Any]:
        """Détail de l'entitlement Account de la clé configurée."""
        return self._request("GET", "entitlement/accountEntitlementSubscription")

    def verify_key(self) -> Dict[str, Any]:
        """Valide la clé via un appel entitlement. Lève la HTTPError amont
        (401 = clé invalide) si KO."""
        self.contact_entitlement()
        return {"valid": True}

    # ---- filtres à liste dynamique (regions/countries/technologies/…) ------

    def filter_values(
        self,
        kind: str,
        *,
        search: Optional[str] = None,
        index_size: int = 20,
        last_returned_key: Optional[str] = None,
    ) -> Any:
        """Valeurs autorisées pour un champ de filtre à liste DYNAMIQUE
        (PAS les champs à liste fermée déjà validés côté client — cf.
        `enums.py` — ceux-là n'ont pas besoin d'un appel réseau).

        Args:
            kind: un de `technologies`, `managementLevels`, `companySizes`,
                `industries`, `jobFunctions`, `regions`, `countries`,
                `states`, `sic`, `isic`, `naics`, `skills`, `companyTypes`,
                `seniority`.
            search, index_size, last_returned_key: uniquement pour
                `kind="technologies"` (seule liste paginée/cherchable côté
                Cognism — les autres renvoient la liste complète en un appel).
        """
        if kind not in self._FILTER_ENDPOINTS:
            raise ValueError(
                f"Unknown filter kind {kind!r}. Allowed: "
                f"{sorted(self._FILTER_ENDPOINTS)!r}"
            )
        endpoint = self._FILTER_ENDPOINTS[kind]
        params = None
        if kind == "technologies":
            params = {
                "search": search or "",
                "indexSize": index_size,
                "lastReturnedKey": last_returned_key or "",
            }
        return self._request("GET", f"filter/{endpoint}", params=params)
