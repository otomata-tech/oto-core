"""Sellsy client — CRM + gestion commerciale FR (API v2, api.sellsy.com/v2).

Sellsy tient dans un même compte le CRM (sociétés, particuliers, contacts,
opportunités) et la chaîne de vente (devis → commande → facture → avoir,
paiements, catalogue). L'API v2 est **uniforme** : chaque ressource expose
`GET /x` (liste), `POST /x/search` (liste filtrée), `GET|PUT|DELETE /x/{id}`,
`POST /x` (création) — d'où un client bâti sur des verbes génériques
(`list_records`, `search_records`, …) plutôt qu'une méthode par endpoint.

**Auth = OAuth2 client_credentials** (Réglages → Portail développeur → API V2 :
un accès *personal* donne un client_id + client_secret). Le jeton est mis en
cache **en mémoire du processus**, keyé par un hash du couple id/secret : un
backend multi-tenant ne peut donc pas servir le jeton d'un compte à un autre, et
rien n'est écrit sur disque.

**Pagination « seek »** (celle recommandée par Sellsy) : la réponse porte
`pagination.offset` = curseur opaque de la page suivante, à repasser tel quel en
`offset`. `list_all` déroule ce curseur.

Quotas : Sellsy compte par seconde/minute/jour/mois et renvoie 429 ; le reste
est lisible dans les en-têtes `X-Quota-Remaining-By-*`, exposés en
`last_quota` après chaque appel.

Docs: https://docs.sellsy.com/api/v2/

Requires: requests
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

# Jetons partagés par processus : {hash(id+secret): (token, expires_at)}. Le hash
# est la clé — sans le secret en clair, on ne peut pas piocher le jeton d'un autre
# compte (le backend sert plusieurs orgs dans le même processus).
_TOKEN_CACHE: Dict[str, tuple] = {}

# Un segment de chemin de ressource, tel que Sellsy les nomme (`credit-notes`,
# `calendar-events`). Garde-fou de forme : jamais d'échappée hors du chemin.
_RESOURCE_RE = re.compile(r"^[a-z][a-z0-9-]*(/[a-z][a-z0-9-]*)*$")


class SellsyClient:
    """Client Sellsy API v2 — auth OAuth2 client_credentials."""

    AUTH_URL = "https://login.sellsy.com/oauth2/access-tokens"
    BASE_URL = "https://api.sellsy.com/v2"

    def __init__(self, client_id: Optional[str] = None,
                 client_secret: Optional[str] = None, timeout: int = 30):
        """
        Args:
            client_id: Client ID de l'accès API v2 (ou env `SELLSY_CLIENT_ID`).
            client_secret: Client Secret associé (ou env `SELLSY_CLIENT_SECRET`).
            timeout: timeout HTTP par appel, en secondes.
        """
        self.client_id = client_id or require_secret("SELLSY_CLIENT_ID")
        self.client_secret = client_secret or require_secret("SELLSY_CLIENT_SECRET")
        self.timeout = timeout
        self.session = requests.Session()
        # Quotas restants du DERNIER appel (X-Quota-Remaining-By-*), utiles pour
        # décider d'un batch : Sellsy décompte même les requêtes en erreur.
        self.last_quota: Dict[str, int] = {}

    # --- auth ---------------------------------------------------------------

    @property
    def _cache_key(self) -> str:
        digest = hashlib.sha256(
            f"{self.client_id}:{self.client_secret}".encode()).hexdigest()
        return digest

    def access_token(self) -> str:
        """Jeton Bearer valide — repris du cache mémoire, sinon frappé à neuf."""
        now = time.time()
        cached = _TOKEN_CACHE.get(self._cache_key)
        if cached and cached[1] > now + 60:
            return cached[0]

        resp = self.session.post(
            self.AUTH_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        raise_for_upstream(resp, service="sellsy")
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise ValueError(f"Sellsy n'a pas rendu d'access_token: {data}")
        _TOKEN_CACHE[self._cache_key] = (
            token, now + int(data.get("expires_in", 3600)))
        return token

    def invalidate_token(self) -> None:
        """Oublie le jeton en cache (401 : il a pu être révoqué avant terme)."""
        _TOKEN_CACHE.pop(self._cache_key, None)

    # --- transport ----------------------------------------------------------

    def request(self, method: str, path: str, *, params: Optional[dict] = None,
                json: Optional[dict] = None, retry_auth: bool = True) -> Any:
        """Appel brut à l'API v2. Rend le JSON parsé (ou `{}` sur 204).

        Un 401 sur un jeton en cache le fait invalider et retenter UNE fois : le
        jeton peut être révoqué côté Sellsy avant son expiration annoncée.
        """
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        resp = self.session.request(
            method, url,
            params=self._encode_params(params),
            json=json,
            headers={"Authorization": f"Bearer {self.access_token()}",
                     "Accept": "application/json"},
            timeout=self.timeout,
        )
        self._read_quota(resp)
        if resp.status_code == 401 and retry_auth:
            self.invalidate_token()
            return self.request(method, path, params=params, json=json,
                                retry_auth=False)
        raise_for_upstream(resp, service="sellsy")
        return resp.json() if resp.content else {}

    def _read_quota(self, resp) -> None:
        self.last_quota = {
            unit: int(resp.headers[header])
            for unit, header in (("second", "X-Quota-Remaining-By-Second"),
                                 ("minute", "X-Quota-Remaining-By-Minute"),
                                 ("day", "X-Quota-Remaining-By-Day"),
                                 ("month", "X-Quota-Remaining-By-Month"))
            if str(resp.headers.get(header, "")).lstrip("-").isdigit()
        }

    @staticmethod
    def _encode_params(params: Optional[dict]) -> Optional[dict]:
        """Retire les `None` et passe les listes en style PHP (`embed[]=company`).

        Sellsy attend `field[]=id&field[]=name`, pas `field=id,name` : une liste
        Python encodée sans crochets serait ignorée EN SILENCE (les champs
        reviendraient tous, ou l'embed manquerait) — d'où la réécriture ici.
        """
        if not params:
            return None
        out: Dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                if not value:
                    continue
                out[f"{key}[]"] = list(value)
            elif isinstance(value, bool):
                out[key] = "true" if value else "false"
            else:
                out[key] = value
        return out

    @staticmethod
    def _resource(resource: str) -> str:
        if not _RESOURCE_RE.match(resource or ""):
            raise ValueError(f"nom de ressource Sellsy invalide: {resource!r}")
        return resource

    def _listing_params(self, *, limit=None, offset=None, order=None,
                        direction=None, fields=None, embed=None,
                        extra: Optional[dict] = None) -> dict:
        # `field` (au singulier) est bien le nom du paramètre de projection côté
        # Sellsy — `fields` serait ignoré sans erreur.
        params = {"limit": limit, "offset": offset, "order": order,
                  "direction": direction, "field": fields, "embed": embed}
        params.update(extra or {})
        return params

    # --- CRUD générique -----------------------------------------------------

    def list_records(self, resource: str, *, limit: Optional[int] = None,
                     offset: Any = None, order: Optional[str] = None,
                     direction: Optional[str] = None,
                     fields: Optional[List[str]] = None,
                     embed: Optional[List[str]] = None,
                     extra_params: Optional[dict] = None) -> dict:
        """`GET /{resource}` — liste paginée.

        Args:
            resource: ressource Sellsy (`companies`, `invoices`, `credit-notes`…).
            limit: taille de page (max 100 côté API, défaut 25).
            offset: curseur `pagination.offset` de la page précédente (méthode
                « seek », recommandée) ou un entier de saut.
            order / direction: champ de tri et sens (`asc` | `desc`).
            fields: projection (`["id", "name"]`) — réduit fortement la réponse.
            embed: objets liés à inclure (`["company", "smart_tags"]`).

        Returns: `{pagination: {limit, count, total, offset}, data: [...]}`.
        """
        return self.request("GET", self._resource(resource),
                            params=self._listing_params(
                                limit=limit, offset=offset, order=order,
                                direction=direction, fields=fields, embed=embed,
                                extra=extra_params))

    def search_records(self, resource: str, filters: Optional[dict] = None, *,
                       limit: Optional[int] = None, offset: Any = None,
                       order: Optional[str] = None,
                       direction: Optional[str] = None,
                       fields: Optional[List[str]] = None,
                       embed: Optional[List[str]] = None) -> dict:
        """`POST /{resource}/search` — liste filtrée, même forme de réponse.

        Args:
            filters: filtres de la ressource, passés tels quels dans
                `{"filters": {...}}` (ex. `{"name": "acme"}`, `{"status":
                ["draft"]}`, `{"created": {"start": "...", "end": "..."}}`).
        """
        return self.request("POST", f"{self._resource(resource)}/search",
                            params=self._listing_params(
                                limit=limit, offset=offset, order=order,
                                direction=direction, fields=fields, embed=embed),
                            json={"filters": filters or {}})

    def list_all(self, resource: str, *, filters: Optional[dict] = None,
                 limit: int = 100, max_pages: int = 10,
                 fields: Optional[List[str]] = None,
                 embed: Optional[List[str]] = None,
                 extra_params: Optional[dict] = None) -> dict:
        """Déroule la pagination « seek » et concatène les pages.

        Args:
            filters: si fourni, passe par `POST /search` ; sinon `GET`.
            max_pages: plafond de pages (borne le coût : chaque page = 1 requête
                décomptée du quota).

        Returns: `{data: [...], pages, truncated}` — `truncated=True` quand le
            plafond a coupé avant la fin.
        """
        rows: List[Any] = []
        offset: Any = None
        pages = 0
        truncated = False
        while pages < max_pages:
            if filters is not None:
                page = self.search_records(resource, filters, limit=limit,
                                           offset=offset, fields=fields,
                                           embed=embed)
            else:
                page = self.list_records(resource, limit=limit, offset=offset,
                                         fields=fields, embed=embed,
                                         extra_params=extra_params)
            chunk = page.get("data") or []
            rows.extend(chunk)
            pages += 1
            pagination = page.get("pagination") or {}
            offset = pagination.get("offset")
            if not chunk or offset is None or len(rows) >= (pagination.get("total") or 0):
                break
        else:
            truncated = True
        return {"data": rows, "pages": pages, "truncated": truncated}

    def get_record(self, resource: str, record_id: Any, *,
                   fields: Optional[List[str]] = None,
                   embed: Optional[List[str]] = None) -> dict:
        """`GET /{resource}/{id}` — la fiche complète d'un objet."""
        return self.request("GET", f"{self._resource(resource)}/{record_id}",
                            params={"field": fields, "embed": embed})

    def create_record(self, resource: str, payload: dict, *,
                      embed: Optional[List[str]] = None,
                      verify: Optional[bool] = None) -> dict:
        """`POST /{resource}` — création.

        Args:
            verify: `True` = validation seule, rien n'est persisté (essai à blanc
                d'un payload avant de vraiment créer).
        """
        return self.request("POST", self._resource(resource), json=payload,
                            params={"embed": embed, "verify": verify})

    def update_record(self, resource: str, record_id: Any, payload: dict, *,
                      embed: Optional[List[str]] = None) -> dict:
        """`PUT /{resource}/{id}` — mise à jour (champs fournis seulement)."""
        return self.request("PUT", f"{self._resource(resource)}/{record_id}",
                            json=payload, params={"embed": embed})

    def patch_record(self, resource: str, record_id: Any, payload: dict) -> dict:
        """`PATCH /{resource}/{id}` — mise à jour partielle (opportunités)."""
        return self.request("PATCH", f"{self._resource(resource)}/{record_id}",
                            json=payload)

    def delete_record(self, resource: str, record_id: Any) -> dict:
        """`DELETE /{resource}/{id}` — suppression."""
        return self.request("DELETE", f"{self._resource(resource)}/{record_id}")

    # --- sous-ressources & actions -----------------------------------------

    def list_sub(self, resource: str, record_id: Any, sub: str, *,
                 limit: Optional[int] = None, offset: Any = None,
                 embed: Optional[List[str]] = None) -> dict:
        """`GET /{resource}/{id}/{sub}` — objets rattachés.

        Ex. contacts d'une société, paiements d'une facture, avoirs d'une facture.
        """
        return self.request(
            "GET", f"{self._resource(resource)}/{record_id}/{self._resource(sub)}",
            params={"limit": limit, "offset": offset, "embed": embed})

    def act(self, resource: str, record_id: Any, action: str, *,
            payload: Optional[dict] = None, method: str = "POST") -> dict:
        """`POST|PUT|PATCH /{resource}/{id}/{action}` — verbe métier.

        Couvre `validate` (facture/avoir), `status` (devis), `convert`
        (prospect → client), `step-rank` (opportunité), `payments`
        (encaissement sur un tiers).
        """
        return self.request(
            method,
            f"{self._resource(resource)}/{record_id}/{self._resource(action)}",
            json=payload)

    def get_custom_fields(self, resource: str, record_id: Any) -> dict:
        """`GET /{resource}/{id}/custom-fields` — champs personnalisés de la fiche."""
        return self.request(
            "GET", f"{self._resource(resource)}/{record_id}/custom-fields")

    def set_custom_fields(self, resource: str, record_id: Any,
                          values: List[dict]) -> dict:
        """`PUT /{resource}/{id}/custom-fields` — écrit des champs personnalisés.

        Args:
            values: `[{"id": <id du champ>, "value": <valeur>}, …]` — l'id se lit
                dans `GET /custom-fields` (référentiel du compte).
        """
        return self.request(
            "PUT", f"{self._resource(resource)}/{record_id}/custom-fields",
            json={"custom_fields": values})

    def link_contact_to_company(self, company_id: Any, contact_id: Any, *,
                                payload: Optional[dict] = None) -> dict:
        """`POST /companies/{id}/contacts/{contactId}` — rattache un contact."""
        return self.request("POST", f"companies/{company_id}/contacts/{contact_id}",
                            json=payload or {})

    def unlink_contact_from_company(self, company_id: Any,
                                    contact_id: Any) -> dict:
        """`DELETE /companies/{id}/contacts/{contactId}` — détache un contact."""
        return self.request(
            "DELETE", f"companies/{company_id}/contacts/{contact_id}")

    # --- recherche transverse & référentiels --------------------------------

    def global_search(self, q: str, *, types: Optional[List[str]] = None,
                      limit: Optional[int] = None,
                      archived: Optional[bool] = None) -> dict:
        """`GET /search` — recherche plein-texte tous objets confondus.

        Args:
            types: restreint aux types voulus (`company`, `company.client`,
                `individual`, `contact`, `opportunity`, `item`…).
        """
        return self.request("GET", "search",
                            params={"q": q, "type": types, "limit": limit,
                                    "archived": archived})

    def smart_tags_autocomplete(self, linked_type: str, *,
                                autocomplete: Optional[str] = None) -> dict:
        """`GET /smart-tags/{linkedtype}/autocomplete` — étiquettes existantes.

        Args:
            linked_type: type d'objet portant l'étiquette (`company`,
                `individual`, `contact`, `opportunity`, `invoice`…).
        """
        return self.request("GET",
                            f"smart-tags/{self._resource(linked_type)}/autocomplete",
                            params={"autocomplete": autocomplete})
