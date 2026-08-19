"""Lightfield API client — CRM agent-native (comptes, contacts, opportunités).

API v1 (`https://api.lightfield.app/v1`, doc https://docs.lightfield.app), auth
**Bearer** `sk_lf_…` + en-tête de version **obligatoire** `Lightfield-Version`. Une
méthode = un endpoint ; corps et réponses passent tels quels, le client n'invente
aucune sémantique. Chemins, verbes et scopes relevés page par page dans la référence
éditeur le 18/08/2026.

⚠️ **L'API est en BETA** (annoncé sur le quickstart) : « methods, parameters, and
response schemas may change ». D'où deux partis pris : les corps ne sont pas re-typés
(l'appelant compose le dict, la doc éditeur fait foi) et les réponses ressortent
brutes — un champ ajouté en amont arrive jusqu'à l'appelant au lieu d'être filtré ici.

Conventions à connaître (elles conditionnent l'appelant) :

- **Modèle de champs PAR WORKSPACE.** Un enregistrement porte
  `{id, createdAt, fields: {slug: {value, valueType}}, relationships: {...}, httpLink}`.
  Les slugs ne sont PAS universels : ils se découvrent aux endpoints `…/definitions`.
  Un slug inconnu → 400 `code: "unknown_field"` (idem `unknown_relationship`). Ne
  jamais coder un slug en dur : le workspace d'un client n'a pas ceux d'un autre.

- **Pagination `limit`/`offset`, et `limit` PLAFONNE À 25** (minimum 1). C'est bas :
  toute collecte réelle boucle. `_check_limit` refuse localement hors bornes plutôt
  que de laisser partir un 400.

- ⚠️ **Les listes lisent un INDEX DE RECHERCHE qui peut être en retard.** Doc, mot
  pour mot : « Information fetched from list methods is served out of a search index
  that may not have recent changes. If getting the latest version of a record is a
  requirement, you will need to use the individual Retrieve methods. » Donc `list_*`
  et `get_*` ne sont PAS interchangeables : après une écriture, relire par `get_*`.

- **Erreurs** : `{type, code?, param?}` — `code` sur certains 400/422
  (`unknown_field`, `unknown_relationship`, `relationship_write_limit_exceeded`…),
  remontées telles quelles dans `UpstreamHTTPError.body`. 429 porte `Retry-After`.

- **Idempotence** : en-tête `Idempotency-Key` (≤255 c.) sur les POST. Clé valable
  24 h, scopée à l'organisation ET au type d'opération ; un rejeu renvoie la réponse
  mise en cache. ⚠️ Si l'appel d'origine a ÉCHOUÉ, rejouer la clé re-tente l'opération
  au lieu de resservir l'erreur — une clé ne « fige » donc que les succès.

- ⚠️ **`scopes: []` sur `/auth/validate` veut dire ACCÈS COMPLET, pas « aucun droit »**
  (doc : « Empty when the key has full access »). L'inversion est un piège de sécurité
  à l'envers : lue naïvement, une clé toute-puissante passerait pour une clé morte, et
  on refuserait le connecteur au client qui a le mieux configuré sa clé. C'est pourquoi
  la lecture passe par `scope_granted()` ci-dessous, jamais par un `in` à la main.

Écritures : `POST` partout, y compris les mises à jour (`POST /v1/accounts/{id}` —
il n'y a ni PUT ni PATCH). Ce client N'EST PAS en lecture seule.

**Délibérément absent** (hors périmètre du connecteur oto, à ne pas « compléter » sans
décision) : `POST /v1/emails/send` et le brouillon d'email (credential capable
d'envoyer — question de politique non tranchée), toutes les suppressions, les fusions,
les sessions d'upload de fichiers, messages et canaux, l'état des exécutions de
workflow, l'historique de champs, et le CRUD des types d'objets personnalisés.

Requires: requests
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

# (connexion, lecture) — aucune attente illimitée.
_HTTP_TIMEOUT = (10, 60)

# Version d'API envoyée à CHAQUE requête. Épinglée ici, jamais recopiée sur un site
# d'appel : une API en beta bougera, et il doit y avoir UN endroit à changer.
DEFAULT_API_VERSION = "2026-03-01"

# Bornes de pagination imposées par l'API (doc « List methods »).
MIN_LIMIT, MAX_LIMIT = 1, 25

# Statuts que l'on retente : rate limit et indisponibilités passagères. Un 4xx de
# validation n'est jamais retenté (il serait rejeté à l'identique).
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


def scope_granted(validate_response: Dict[str, Any], scope: str) -> bool:
    """La clé décrite par `/auth/validate` porte-t-elle `scope` ?

    ⚠️ **Une liste `scopes` VIDE signifie accès complet**, pas « aucun droit » (doc
    éditeur : « Empty when the key has full access »). Cette fonction existe pour que
    cette inversion soit écrite UNE fois : un `if scope in resp["scopes"]` naïf
    déclarerait inutilisable la clé la plus puissante.
    """
    if not isinstance(validate_response, dict):
        return False
    scopes = validate_response.get("scopes")
    if not scopes:                      # [] ou absent = full access
        return True
    return scope in scopes


class LightfieldClient:
    """Client Lightfield v1 (https://api.lightfield.app), auth Bearer `sk_lf_…`."""

    BASE_URL = "https://api.lightfield.app"

    def __init__(self, api_key: Optional[str] = None,
                 api_version: Optional[str] = None):
        """
        Args:
            api_key: clé Lightfield (ou variable d'env `LIGHTFIELD_API_KEY`).
            api_version: valeur de l'en-tête `Lightfield-Version`
                (défaut `DEFAULT_API_VERSION`).
        """
        self.api_key = api_key or require_secret("LIGHTFIELD_API_KEY")
        self.api_version = api_version or DEFAULT_API_VERSION
        self.session = requests.Session()
        # Clé en HEADER uniquement (jamais en query string : elle finirait dans l'URL,
        # donc dans le message de toute exception, les logs et Sentry).
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Lightfield-Version": self.api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # --- transport ----------------------------------------------------------

    @staticmethod
    def _check_limit(limit: Optional[int]) -> None:
        """`limit` hors [1, 25] est refusé ICI. L'API rendrait un 400 ; le dire
        localement nomme la borne réelle, que personne ne devine."""
        if limit is None:
            return
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("`limit` doit être un entier.")
        if not (MIN_LIMIT <= limit <= MAX_LIMIT):
            raise ValueError(
                f"`limit` doit être entre {MIN_LIMIT} et {MAX_LIMIT} "
                f"(plafond de l'API Lightfield) ; reçu {limit}. "
                "Au-delà, paginer avec `offset`.")

    @staticmethod
    def _retry_after(resp: Any, attempt: int) -> float:
        """Délai avant re-tentative : `Retry-After` s'il est là (l'amont sait mieux
        que nous), sinon backoff exponentiel."""
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        return float(2 ** attempt)

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 json: Any = None,
                 idempotency_key: Optional[str] = None) -> Any:
        # Params à None retirés ; booléens en `true`/`false` (requests écrirait
        # `True`, que le serveur ne lit pas comme un booléen).
        clean: Dict[str, Any] = {}
        for k, v in (params or {}).items():
            if v is None:
                continue
            clean[k] = ("true" if v else "false") if isinstance(v, bool) else v

        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None

        # Retente 429/5xx. Un POST n'est retenté QUE s'il porte une clé d'idempotence :
        # sans elle, une réponse perdue en vol ferait créer deux fois l'enregistrement.
        retryable = method.upper() == "GET" or idempotency_key is not None
        last = None
        for attempt in range(_MAX_ATTEMPTS):
            last = self.session.request(
                method, f"{self.BASE_URL}{path}", params=clean or None, json=json,
                headers=headers, timeout=_HTTP_TIMEOUT)
            if (last.status_code not in _RETRY_STATUSES
                    or not retryable or attempt == _MAX_ATTEMPTS - 1):
                break
            time.sleep(self._retry_after(last, attempt))
        raise_for_upstream(last, service="lightfield")
        return last.json() if last.content else {}

    def _write(self, path: str, payload: Dict[str, Any],
               idempotency_key: Optional[str] = None) -> Any:
        """POST d'écriture. Sans clé fournie, on en génère une : elle rend SÛRE la
        boucle de re-tentative interne (le rejeu resservira la réponse du premier
        essai). Pour dédupliquer entre deux appels distincts — un agent qui rejoue
        son tour —, c'est à l'appelant de passer SA clé, stable d'un appel à l'autre."""
        if not isinstance(payload, dict):
            raise ValueError("le corps doit être un dict.")
        key = idempotency_key or f"oto-{uuid.uuid4()}"
        if len(key) > 255:
            raise ValueError("`idempotency_key` : 255 caractères maximum.")
        return self._request("POST", path, json=dict(payload), idempotency_key=key)

    def _list(self, path: str, limit: Optional[int], offset: Optional[int],
              extra: Optional[Dict[str, Any]] = None) -> Any:
        self._check_limit(limit)
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        params.update(extra or {})
        return self._request("GET", path, params=params)

    # --- auth ---------------------------------------------------------------

    def validate(self) -> Dict[str, Any]:
        """GET /v1/auth/validate — métadonnées de la clé courante. AUCUN scope requis,
        donc c'est la sonde d'authentification : elle répond même à une clé très
        restreinte.

        Rend `{active, scopes, subjectType: "user"|"workspace", tokenType: "api_key"}`.
        ⚠️ Lire `scopes` avec `scope_granted()` : une liste VIDE = accès complet.
        """
        return self._request("GET", "/v1/auth/validate")

    # --- comptes ------------------------------------------------------------

    def list_accounts(self, limit: Optional[int] = None, offset: Optional[int] = None,
                      **filters: Any) -> Dict[str, Any]:
        """GET /v1/accounts — `{data: [compte…]}`. Scope `accounts:read`.

        ⚠️ Sert l'index de recherche, potentiellement en retard : pour l'état à jour
        d'un enregistrement précis, `get_account`. `filters` = filtres par champ ou
        relation (clé = slug de définition), cf. doc « List methods »."""
        return self._list("/v1/accounts", limit, offset, filters)

    def get_account(self, account_id: str) -> Dict[str, Any]:
        """GET /v1/accounts/{id} — lecture DIRECTE (pas l'index) : la version à jour.
        Scope `accounts:read`."""
        return self._request("GET", f"/v1/accounts/{account_id}")

    def create_account(self, payload: Dict[str, Any],
                       idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/accounts — `payload` = `{fields: {slug: value…}, relationships?}`,
        slugs issus de `account_definitions()`. Scope `accounts:create`."""
        return self._write("/v1/accounts", payload, idempotency_key)

    def update_account(self, account_id: str, payload: Dict[str, Any],
                       idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/accounts/{id} — mise à jour (l'API n'a ni PUT ni PATCH).
        Scope `accounts:update`."""
        return self._write(f"/v1/accounts/{account_id}", payload, idempotency_key)

    def account_definitions(self) -> Dict[str, Any]:
        """GET /v1/accounts/definitions — champs et relations du workspace : LA source
        des slugs. Scope `accounts:read`."""
        return self._request("GET", "/v1/accounts/definitions")

    # --- contacts -----------------------------------------------------------

    def list_contacts(self, limit: Optional[int] = None, offset: Optional[int] = None,
                      **filters: Any) -> Dict[str, Any]:
        """GET /v1/contacts — index de recherche. Scope `contacts:read`."""
        return self._list("/v1/contacts", limit, offset, filters)

    def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """GET /v1/contacts/{id} — lecture directe. Scope `contacts:read`."""
        return self._request("GET", f"/v1/contacts/{contact_id}")

    def create_contact(self, payload: Dict[str, Any],
                       idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/contacts. Scope `contacts:create`."""
        return self._write("/v1/contacts", payload, idempotency_key)

    def update_contact(self, contact_id: str, payload: Dict[str, Any],
                       idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/contacts/{id}. Scope `contacts:update`."""
        return self._write(f"/v1/contacts/{contact_id}", payload, idempotency_key)

    def contact_definitions(self) -> Dict[str, Any]:
        """GET /v1/contacts/definitions. Scope `contacts:read`."""
        return self._request("GET", "/v1/contacts/definitions")

    # --- opportunités -------------------------------------------------------

    def list_opportunities(self, limit: Optional[int] = None,
                           offset: Optional[int] = None,
                           **filters: Any) -> Dict[str, Any]:
        """GET /v1/opportunities — index de recherche. Scope `opportunities:read`."""
        return self._list("/v1/opportunities", limit, offset, filters)

    def get_opportunity(self, opportunity_id: str) -> Dict[str, Any]:
        """GET /v1/opportunities/{id} — lecture directe. Scope `opportunities:read`."""
        return self._request("GET", f"/v1/opportunities/{opportunity_id}")

    def create_opportunity(self, payload: Dict[str, Any],
                           idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/opportunities. Scope `opportunities:create`."""
        return self._write("/v1/opportunities", payload, idempotency_key)

    def update_opportunity(self, opportunity_id: str, payload: Dict[str, Any],
                           idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/opportunities/{id}. Scope `opportunities:update`."""
        return self._write(f"/v1/opportunities/{opportunity_id}", payload,
                           idempotency_key)

    def opportunity_definitions(self) -> Dict[str, Any]:
        """GET /v1/opportunities/definitions. Scope `opportunities:read`."""
        return self._request("GET", "/v1/opportunities/definitions")

    # --- notes & tâches -----------------------------------------------------

    def create_note(self, payload: Dict[str, Any],
                    idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/notes — note rattachée à un compte/contact/opportunité par
        relation. Scope `notes:create`."""
        return self._write("/v1/notes", payload, idempotency_key)

    def note_definitions(self) -> Dict[str, Any]:
        """GET /v1/notes/definitions. Scope `notes:read`."""
        return self._request("GET", "/v1/notes/definitions")

    def create_task(self, payload: Dict[str, Any],
                    idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/tasks. Scope `tasks:create`."""
        return self._write("/v1/tasks", payload, idempotency_key)

    def update_task(self, task_id: str, payload: Dict[str, Any],
                    idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/tasks/{id}. Scope `tasks:update`."""
        return self._write(f"/v1/tasks/{task_id}", payload, idempotency_key)

    def task_definitions(self) -> Dict[str, Any]:
        """GET /v1/tasks/definitions. Scope `tasks:read`."""
        return self._request("GET", "/v1/tasks/definitions")

    # --- listes -------------------------------------------------------------

    def list_lists(self, limit: Optional[int] = None, offset: Optional[int] = None,
                   **filters: Any) -> Dict[str, Any]:
        """GET /v1/lists. Scope `lists:read`."""
        return self._list("/v1/lists", limit, offset, filters)

    def get_list(self, list_id: str) -> Dict[str, Any]:
        """GET /v1/lists/{id}. Scope `lists:read`."""
        return self._request("GET", f"/v1/lists/{list_id}")

    def create_list(self, payload: Dict[str, Any],
                    idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/lists. Scope `lists:create`."""
        return self._write("/v1/lists", payload, idempotency_key)

    def update_list(self, list_id: str, payload: Dict[str, Any],
                    idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1/lists/{id}. Scope `lists:update`."""
        return self._write(f"/v1/lists/{list_id}", payload, idempotency_key)

    def list_accounts_of_list(self, list_id: str, limit: Optional[int] = None,
                              offset: Optional[int] = None) -> Dict[str, Any]:
        """GET /v1/lists/{listId}/accounts. Scopes `lists:read` ET `accounts:read` —
        une clé qui n'a que `lists:read` échoue ici, pas sur `get_list`."""
        return self._list(f"/v1/lists/{list_id}/accounts", limit, offset)

    def list_contacts_of_list(self, list_id: str, limit: Optional[int] = None,
                              offset: Optional[int] = None) -> Dict[str, Any]:
        """GET /v1/lists/{listId}/contacts. Scopes `lists:read` ET `contacts:read`."""
        return self._list(f"/v1/lists/{list_id}/contacts", limit, offset)

    def list_opportunities_of_list(self, list_id: str, limit: Optional[int] = None,
                                   offset: Optional[int] = None) -> Dict[str, Any]:
        """GET /v1/lists/{listId}/opportunities. Scopes `lists:read` ET
        `opportunities:read`."""
        return self._list(f"/v1/lists/{list_id}/opportunities", limit, offset)

    # --- réunions & emails (lecture seule) ----------------------------------

    def list_meetings(self, limit: Optional[int] = None, offset: Optional[int] = None,
                      **filters: Any) -> Dict[str, Any]:
        """GET /v1/meetings. Scope `meetings:read`."""
        return self._list("/v1/meetings", limit, offset, filters)

    def get_meeting(self, meeting_id: str) -> Dict[str, Any]:
        """GET /v1/meetings/{id}. Scope `meetings:read`."""
        return self._request("GET", f"/v1/meetings/{meeting_id}")

    def meeting_definitions(self) -> Dict[str, Any]:
        """GET /v1/meetings/definitions. Scope `meetings:read`."""
        return self._request("GET", "/v1/meetings/definitions")

    def list_emails(self, limit: Optional[int] = None, offset: Optional[int] = None,
                    **filters: Any) -> Dict[str, Any]:
        """GET /v1/emails — LECTURE seule ; l'envoi (`POST /v1/emails/send`) n'est
        volontairement pas exposé. Scope `emails:read`."""
        return self._list("/v1/emails", limit, offset, filters)

    def get_email(self, email_id: str) -> Dict[str, Any]:
        """GET /v1/emails/{id}. Scope `emails:read`."""
        return self._request("GET", f"/v1/emails/{email_id}")

    # --- types d'objets -----------------------------------------------------

    def list_object_types(self) -> Dict[str, Any]:
        """GET /v1/objects — types d'objets (standards et personnalisés) visibles par
        la clé : `{data: [{label, objectType}]}`, `objectType` = le slug à passer à
        `object_definitions`."""
        return self._request("GET", "/v1/objects")

    def object_definitions(self, entity_slug: str) -> Dict[str, Any]:
        """GET /v1/objects/{entitySlug}/definitions — champs et relations d'un type
        d'objet, y compris personnalisé."""
        return self._request("GET", f"/v1/objects/{entity_slug}/definitions")
