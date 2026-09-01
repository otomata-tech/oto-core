"""HubSpot CRM API client (v3).

Auth = **private app access token** (Bearer). Créé dans HubSpot :
Settings → Integrations → Private Apps → scopes `crm.objects.*` (read/write).
Passé en clair au constructeur (ou `HUBSPOT_API_KEY` en fallback CLI).

Surface générique sur les objets CRM : `contacts`, `companies`, `deals`,
`tickets` (et tout objet custom) partagent les mêmes verbes
(`list/get/search/create/update/delete` + associations). Les notes/engagements
ont un helper dédié car ils s'attachent à un objet via une association.

Deux propriétés de ce client valent d'être sues avant de l'appeler :

- **Le 429 se rattrape, borné ; rien d'autre ne se rattrape.** Une private app a
  droit à 190 requêtes / 10 s, et une phase de synchro CRM en fait ~4 par lead :
  ~40 leads suffisent à taper le plafond. Un 429 dit que la requête a été REFUSÉE
  (rien n'a été fait), donc la rejouer est sans effet de bord ; un 5xx ne dit pas
  si l'écriture est passée, et le rejouer créerait un doublon dans un CRM. Cf.
  `_request`.
- **`batch_read_objects` est STRICT sur les noms de propriétés**, contrairement au
  GET unitaire : une propriété absente du portail fait un 400
  `PROPERTY_DOESNT_EXIST` sur la tranche ENTIÈRE, elle ne revient pas vide. C'est
  le piège que l'appelant hérite en passant de N lectures unitaires à une lecture
  groupée.

Docs : https://developers.hubspot.com/docs/api/crm/understanding-the-crm

Requires: requests
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

# Type d'association par défaut HubSpot (note → objet). 202 = Note↔Contact,
# mais l'API accepte les "HUBSPOT_DEFINED" via le endpoint /associations/default.
_NOTE_ASSOCIATION_TYPE = {
    "contacts": 202,
    "companies": 190,
    "deals": 214,
    "tickets": 216,
}

logger = logging.getLogger(__name__)

# HubSpot plafonne TOUT endpoint `batch/*` à 100 entrées par requête (« Object API
# batch endpoints are limited to 100 inputs per request »). Une page d'appartenances
# de liste en vaut 250 : découper est la seule façon de la servir.
BATCH_READ_MAX = 100

# Débit d'une private app : 190 requêtes / 10 s. Une phase de synchro CRM fait ~4
# appels par lead, donc ~40 leads suffisent à taper le plafond — et un 429 non
# rattrapé arrête le run AU MILIEU d'un enregistrement à moitié écrit.
# La reprise est BORNÉE EN DURÉE, pas seulement en nombre de tentatives : le
# handler tourne dans le threadpool du serveur MCP, qui borne la CONCURRENCE et pas
# le temps (oto-backend `docs/event-loop-perf.md`). Pire cas écrit ici :
# RATE_LIMIT_ATTEMPTS appels HTTP et RATE_LIMIT_MAX_TOTAL_SLEEP secondes d'attente
# cumulée — relever le nombre de tentatives ne peut donc pas rallonger le pire cas.
RATE_LIMIT_ATTEMPTS = 3            # >= 1 ; 1 = pas de reprise
RATE_LIMIT_MAX_SLEEP = 10.0        # plafond d'UNE attente
RATE_LIMIT_MAX_TOTAL_SLEEP = 20.0  # plafond du CUMUL — la borne de durée

# Un 429 ne dit pas toujours « réessaie ». HubSpot nomme la politique franchie dans
# le corps (`policyName`) : les fenêtres courtes se rattrapent, un quota JOURNALIER
# ou MENSUEL ne se rattrape pas — attendre puis rejouer immobilise un worker pour
# rien, et le refus arrivera de toute façon.
_RATE_LIMIT_POLICY_NOT_RETRYABLE = frozenset({"DAILY", "MONTHLY"})


class HubSpotClient:
    """Client HubSpot CRM v3 — objets CRM génériques + notes + owners."""

    BASE_URL = "https://api.hubapi.com"

    def __init__(self, api_key: Optional[str] = None):
        """Initialise le client.

        Args:
            api_key: private app access token (ou env `HUBSPOT_API_KEY`).
        """
        self.api_key = api_key or require_secret("HUBSPOT_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Un appel HubSpot, avec la seule reprise qui soit sûre : le 429.

        ⚠️ **Le 429 est réessayé, les 5xx non.** Un 429 dit que la requête a été
        REFUSÉE — rien n'a été fait, la rejouer est sans effet de bord, quel que
        soit le verbe. Un 502/504 ne dit pas si l'écriture est passée : rejouer un
        POST dessus créerait un doublon, et un doublon dans un CRM coûte plus cher
        que l'erreur qu'on voulait éviter. La différence est le tout du sujet.

        À bout de tentatives (ou de budget d'attente) on ne rend RIEN de dégradé :
        `raise_for_upstream` lève l'`UpstreamHTTPError(429)` du dernier essai,
        `status_code` compris — un refus nommé, que l'appelant peut router.
        """
        url = f"{self.BASE_URL}{path}"
        budget = RATE_LIMIT_MAX_TOTAL_SLEEP
        attempt = 0
        while True:
            attempt += 1
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code != 429 or attempt >= RATE_LIMIT_ATTEMPTS:
                break
            if not self._rate_limit_is_retryable(resp):
                break              # quota journalier : attendre n'y change rien
            delay = min(self._retry_delay(resp, attempt), budget)
            if delay <= 0:
                break              # budget de durée épuisé : on refuse, on ne dort pas
            logger.info(
                "hubspot 429 sur %s %s — nouvelle tentative dans %.1f s (%d/%d)",
                method, path, delay, attempt, RATE_LIMIT_ATTEMPTS)
            time.sleep(delay)
            budget -= delay
        if resp.status_code == 429:
            logger.warning(
                "hubspot 429 non rattrapé après %d tentative(s) sur %s %s",
                attempt, method, path)
        raise_for_upstream(resp, service="hubspot")
        return resp.json() if resp.content else {}

    @staticmethod
    def _rate_limit_is_retryable(resp: Any) -> bool:
        """Un 429 se rattrape-t-il ?

        HubSpot nomme la politique franchie dans le corps (`policyName` : SECONDLY
        | TEN_SECONDLY_ROLLING | DAILY…). Politique inconnue ou corps illisible ⇒
        on RÉESSAIE : le cas fréquent est la rafale.
        """
        try:
            body = resp.json()
        # noqa: SILENT — corps non-JSON : le défaut « rafale » ci-dessous
        except Exception:
            return True
        policy = body.get("policyName") if isinstance(body, dict) else None
        return str(policy or "").upper() not in _RATE_LIMIT_POLICY_NOT_RETRYABLE

    @staticmethod
    def _retry_delay(resp: Any, attempt: int) -> float:
        """Combien attendre après un 429 : `Retry-After` s'il est là (l'amont sait
        mieux que nous), sinon un palier exponentiel.

        HubSpot ne garantit pas l'en-tête, donc le repli est ÉCRIT plutôt que
        deviné — et il est plafonné, parce qu'une attente non bornée dans un
        handler est un gel sous un autre nom. `Retry-After` peut aussi être une
        date HTTP : non parsable ⇒ palier.
        """
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
        try:
            delay = float(raw) if raw else float(2 ** attempt)
        except (TypeError, ValueError):
            delay = float(2 ** attempt)
        return min(max(delay, 0.0), RATE_LIMIT_MAX_SLEEP)

    # --- Objets CRM (génériques) -------------------------------------------

    def list_objects(
        self,
        object_type: str,
        properties: Optional[List[str]] = None,
        limit: int = 100,
        after: Optional[str] = None,
        associations: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Liste les objets d'un type (paginé). `after` = curseur de page suivante."""
        params: Dict[str, Any] = {"limit": min(limit, 100)}
        if properties:
            params["properties"] = ",".join(properties)
        if associations:
            params["associations"] = ",".join(associations)
        if after:
            params["after"] = after
        return self._request("GET", f"/crm/v3/objects/{object_type}", params=params)

    def get_object(
        self,
        object_type: str,
        object_id: str,
        properties: Optional[List[str]] = None,
        associations: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Récupère un objet par id."""
        params: Dict[str, Any] = {}
        if properties:
            params["properties"] = ",".join(properties)
        if associations:
            params["associations"] = ",".join(associations)
        return self._request(
            "GET", f"/crm/v3/objects/{object_type}/{object_id}",
            params=params or None,
        )

    def search_objects(
        self,
        object_type: str,
        query: Optional[str] = None,
        filters: Optional[List[Dict[str, Any]]] = None,
        properties: Optional[List[str]] = None,
        limit: int = 100,
        after: Optional[str] = None,
        sorts: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Recherche d'objets via l'endpoint /search.

        Args:
            query: recherche plein-texte.
            filters: liste de `{propertyName, operator, value}` combinés en ET
                (operators HubSpot : EQ, NEQ, GT, GTE, LT, LTE, CONTAINS_TOKEN,
                HAS_PROPERTY, IN…). Pour `IN`, passer `values` (liste).
            sorts: ex. `[{"propertyName": "createdate", "direction": "DESCENDING"}]`.
        """
        body: Dict[str, Any] = {"limit": min(limit, 100)}
        if query:
            body["query"] = query
        if filters:
            body["filterGroups"] = [{"filters": filters}]
        if properties:
            body["properties"] = properties
        if sorts:
            body["sorts"] = sorts
        if after:
            body["after"] = after
        return self._request(
            "POST", f"/crm/v3/objects/{object_type}/search", json=body,
        )

    def create_object(
        self,
        object_type: str,
        properties: Dict[str, Any],
        associations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Crée un objet. `associations` = format HubSpot v3 (liste de `to`+`types`)."""
        body: Dict[str, Any] = {"properties": properties}
        if associations:
            body["associations"] = associations
        return self._request("POST", f"/crm/v3/objects/{object_type}", json=body)

    def update_object(
        self, object_type: str, object_id: str, properties: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Met à jour (PATCH) les propriétés d'un objet."""
        return self._request(
            "PATCH", f"/crm/v3/objects/{object_type}/{object_id}",
            json={"properties": properties},
        )

    def delete_object(self, object_type: str, object_id: str) -> Dict[str, Any]:
        """Archive un objet (corbeille HubSpot)."""
        return self._request("DELETE", f"/crm/v3/objects/{object_type}/{object_id}")

    def batch_read_objects(
        self,
        object_type: str,
        ids: Iterable[Any],
        properties: Optional[List[str]] = None,
        id_property: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Lit N objets en un appel par tranche de 100, au lieu de N appels.

        Rend `{"results": [...], "missing_ids": [...]}`.

        ⚠️ `missing_ids` est un DIFF LOCAL (ce qu'on a demandé moins ce qui est
        revenu), pas une lecture du corps d'erreur : un id supprimé, archivé ou
        d'un autre portail est simplement ABSENT des `results` et HubSpot répond
        207 — donc `raise_for_upstream` ne lève pas. Sans ce relevé, une page de
        250 membres reviendrait à 247 lignes sans que personne ne l'apprenne :
        c'est le « succès déguisé » que la maison refuse.

        ⚠️ Une tranche qui échoue LÈVE : jamais une demi-page qui passerait pour la
        liste entière.

        Args:
            object_type: contacts | companies | deals | tickets | objet custom.
            ids: les ids (ou, avec `id_property`, les valeurs de cette propriété).
                Dédupliqués en gardant l'ordre d'entrée ; les vides sont écartés.
            properties: noms INTERNES des propriétés à rendre. ⚠️ `batch/read` est
                STRICT là-dessus, contrairement au GET unitaire : une propriété
                absente du portail fait un 400 `PROPERTY_DOESNT_EXIST` sur la
                tranche entière, elle ne revient pas vide.
            id_property: lire par clé d'unicité (`email`…) au lieu du record id.
                Elle est alors AJOUTÉE aux `properties` demandées si elle n'y est
                pas — sans elle dans la réponse, `missing_ids` serait incalculable
                (les `results` sont keyés sur le record id, pas sur la clé). La
                comparaison est exacte d'abord, puis insensible à la casse :
                HubSpot normalise certaines clés (un email revient en minuscules),
                et signaler ces lignes en `missing_ids` serait un faux.
        """
        wanted: List[str] = list(dict.fromkeys(
            str(i) for i in ids if i is not None and str(i) != ""))
        if not wanted:
            # Aucun id : pas d'appel du tout. HubSpot refuserait un `inputs` vide,
            # et « rien à lire » n'est pas une erreur.
            return {"results": [], "missing_ids": []}

        props = list(properties or [])
        if id_property and id_property not in props:
            props.append(id_property)

        out: List[Dict[str, Any]] = []
        for start in range(0, len(wanted), BATCH_READ_MAX):
            body: Dict[str, Any] = {
                "inputs": [{"id": i} for i in wanted[start:start + BATCH_READ_MAX]],
            }
            if props:
                body["properties"] = props
            if id_property:
                body["idProperty"] = id_property
            page = self._request(
                "POST", f"/crm/v3/objects/{object_type}/batch/read", json=body)
            out.extend((page or {}).get("results") or [])

        seen = set()
        for record in out:
            if id_property:
                value = (record.get("properties") or {}).get(id_property)
            else:
                value = record.get("id")
            if value is not None:
                seen.add(str(value))
        folded = {v.casefold() for v in seen}
        missing = [i for i in wanted if i not in seen and i.casefold() not in folded]
        return {"results": out, "missing_ids": missing}

    # --- Associations -------------------------------------------------------

    def list_associations(
        self, object_type: str, object_id: str, to_object_type: str,
    ) -> Dict[str, Any]:
        """Liste les objets `to_object_type` associés à un objet (ex. deals d'un contact)."""
        return self._request(
            "GET",
            f"/crm/v3/objects/{object_type}/{object_id}/associations/{to_object_type}",
        )

    # --- Notes (engagement attaché à un objet) ------------------------------

    def _note_association_type(self, object_type: str) -> int:
        """Type d'association Note→<objet>.

        Les quatre objets standard sont en table (aucun appel). Pour tout autre
        type (objet custom notamment), on DEMANDE le libellé par défaut à
        `/crm/v4/associations/notes/<type>/labels` plutôt que de retomber
        silencieusement sur 202 (= Note↔Contact) : ce défaut accrochait la note
        au mauvais type d'association sans rien signaler.
        """
        known = _NOTE_ASSOCIATION_TYPE.get(object_type)
        if known is not None:
            return known
        labels = self._request(
            "GET", f"/crm/v4/associations/notes/{object_type}/labels")
        results = labels.get("results") or []
        for entry in results:
            if entry.get("category") == "HUBSPOT_DEFINED":
                return entry["typeId"]
        if results:
            return results[0]["typeId"]
        raise ValueError(
            f"aucun type d'association note→{object_type} : rattache la note "
            "toi-même via create_object('notes', ..., associations=[...])")

    def create_note(
        self, body: str, object_type: str, object_id: str,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attache une note à un objet CRM.

        Args:
            body: contenu de la note (texte/HTML).
            object_type: contacts | companies | deals | tickets.
            object_id: id de l'objet auquel rattacher la note.
            timestamp: ISO 8601 ou epoch ms (défaut = maintenant côté HubSpot).
        """
        props: Dict[str, Any] = {
            "hs_note_body": body,
            "hs_timestamp": timestamp or str(int(time.time() * 1000)),
        }
        assoc_type = self._note_association_type(object_type)
        associations = [{
            "to": {"id": object_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": assoc_type,
            }],
        }]
        return self.create_object("notes", props, associations=associations)

    # --- Owners -------------------------------------------------------------

    def list_owners(self, limit: int = 100, after: Optional[str] = None) -> Dict[str, Any]:
        """Liste les owners (utilisateurs HubSpot) — pour assigner contacts/deals."""
        params: Dict[str, Any] = {"limit": min(limit, 100)}
        if after:
            params["after"] = after
        return self._request("GET", "/crm/v3/owners", params=params)

    # --- Propriétés (schéma) ------------------------------------------------
    # Sans ce référentiel, tout create/update est une devinette : les noms
    # internes ne sont PAS les libellés de l'UI (`dealstage`, pas « Deal stage »)
    # et les listes déroulantes n'acceptent que leurs `options[].value`. C'est
    # aussi ce qui permet d'écrire un `filterBranch` de liste dynamique, qui
    # référence des propriétés par nom interne.

    def list_properties(
        self, object_type: str, archived: bool = False,
        properties: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Liste les propriétés d'un type d'objet (nom interne, type, options)."""
        params: Dict[str, Any] = {"archived": str(archived).lower()}
        if properties:
            params["properties"] = ",".join(properties)
        return self._request(
            "GET", f"/crm/v3/properties/{object_type}", params=params)

    def get_property(
        self, object_type: str, property_name: str, archived: bool = False,
    ) -> Dict[str, Any]:
        """Récupère UNE propriété par son nom interne."""
        return self._request(
            "GET", f"/crm/v3/properties/{object_type}/{property_name}",
            params={"archived": str(archived).lower()})

    def create_property(
        self, object_type: str, definition: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Crée une propriété. `definition` = {name, label, type, fieldType,
        groupName, options?} (cf. doc HubSpot Properties)."""
        return self._request(
            "POST", f"/crm/v3/properties/{object_type}", json=definition)

    def update_property(
        self, object_type: str, property_name: str, definition: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Met à jour (PATCH) une propriété."""
        return self._request(
            "PATCH", f"/crm/v3/properties/{object_type}/{property_name}",
            json=definition)

    def delete_property(self, object_type: str, property_name: str) -> Dict[str, Any]:
        """Archive une propriété."""
        return self._request(
            "DELETE", f"/crm/v3/properties/{object_type}/{property_name}")

    def list_property_groups(self, object_type: str) -> Dict[str, Any]:
        """Liste les groupes de propriétés (onglets de la fiche) d'un type d'objet."""
        return self._request("GET", f"/crm/v3/properties/{object_type}/groups")

    # --- Listes (= les « segments » HubSpot) --------------------------------
    # `/crm/v3/lists` ; l'API v1 (`/contacts/v1/lists`) est sunset depuis le
    # 2026-04-30, ne pas y retomber. Les listes sont keyées sur un
    # `objectTypeId` NUMÉRIQUE (`0-1` contacts, `0-2` companies, `0-3` deals,
    # `0-5` tickets, `2-<n>` objets custom) et non sur le nom d'objet utilisé
    # partout ailleurs dans ce client — la traduction se fait côté appelant.
    #
    # `processingType` :
    #   MANUAL   → membres gérés à la main / par l'API (les endpoints memberships)
    #   DYNAMIC  → membres recalculés par HubSpot depuis `filterBranch` ; les
    #              endpoints memberships d'écriture sont REFUSÉS dessus
    #   SNAPSHOT → filtré une fois puis figé, membres gérés à la main ensuite

    def create_list(
        self,
        name: str,
        object_type_id: str,
        processing_type: str = "MANUAL",
        filter_branch: Optional[Dict[str, Any]] = None,
        custom_properties: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Crée une liste.

        Args:
            name: nom de la liste (unique par type d'objet).
            object_type_id: `0-1` (contacts), `0-2`, `0-3`, `0-5`, `2-<n>`…
            processing_type: MANUAL | DYNAMIC | SNAPSHOT.
            filter_branch: arbre de critères (DYNAMIC/SNAPSHOT). Passé tel quel :
                c'est une structure récursive HubSpot (filterBranchType
                OR/AND/UNIFIED_EVENTS/ASSOCIATION), pas modélisée ici.
        """
        body: Dict[str, Any] = {
            "name": name,
            "objectTypeId": object_type_id,
            "processingType": processing_type,
        }
        if filter_branch is not None:
            body["filterBranch"] = filter_branch
        if custom_properties:
            body["customProperties"] = custom_properties
        return self._request("POST", "/crm/v3/lists", json=body)

    def get_list(self, list_id: str, include_filters: bool = False) -> Dict[str, Any]:
        """Récupère une liste par id. `include_filters` renvoie son `filterBranch`."""
        return self._request(
            "GET", f"/crm/v3/lists/{list_id}",
            params={"includeFilters": str(include_filters).lower()})

    def get_lists(
        self, list_ids: List[str], include_filters: bool = False,
    ) -> Dict[str, Any]:
        """Récupère plusieurs listes en un appel (`listIds` répété)."""
        return self._request(
            "GET", "/crm/v3/lists",
            params={
                "listIds": list_ids,
                "includeFilters": str(include_filters).lower(),
            })

    def get_list_by_name(
        self, object_type_id: str, list_name: str, include_filters: bool = False,
    ) -> Dict[str, Any]:
        """Récupère une liste par son nom (dans un type d'objet donné)."""
        return self._request(
            "GET",
            f"/crm/v3/lists/object-type-id/{object_type_id}/name/{list_name}",
            params={"includeFilters": str(include_filters).lower()})

    def search_lists(
        self,
        query: Optional[str] = None,
        processing_types: Optional[List[str]] = None,
        object_type_id: Optional[str] = None,
        count: Optional[int] = None,
        offset: Optional[int] = None,
        additional_properties: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Cherche des listes par nom / type de traitement / type d'objet."""
        body: Dict[str, Any] = {}
        if query:
            body["query"] = query
        if processing_types:
            body["processingTypes"] = processing_types
        if object_type_id:
            body["objectTypeId"] = object_type_id
        if count is not None:
            body["count"] = count
        if offset is not None:
            body["offset"] = offset
        if additional_properties:
            body["additionalProperties"] = additional_properties
        return self._request("POST", "/crm/v3/lists/search", json=body)

    def update_list_name(
        self, list_id: str, list_name: str, include_filters: bool = False,
    ) -> Dict[str, Any]:
        """Renomme une liste (le nom passe en query param, pas en body)."""
        return self._request(
            "PUT", f"/crm/v3/lists/{list_id}/update-list-name",
            params={
                "listName": list_name,
                "includeFilters": str(include_filters).lower(),
            })

    def update_list_filters(
        self,
        list_id: str,
        filter_branch: Dict[str, Any],
        enroll_objects_in_workflows: bool = False,
    ) -> Dict[str, Any]:
        """Remplace l'arbre de critères d'une liste DYNAMIC/SNAPSHOT."""
        return self._request(
            "PUT", f"/crm/v3/lists/{list_id}/update-list-filters",
            params={
                "enrollObjectsInWorkflows": str(enroll_objects_in_workflows).lower(),
            },
            json={"filterBranch": filter_branch})

    def delete_list(self, list_id: str) -> Dict[str, Any]:
        """Supprime une liste — restaurable pendant 90 jours (`restore_list`)."""
        return self._request("DELETE", f"/crm/v3/lists/{list_id}")

    def restore_list(self, list_id: str) -> Dict[str, Any]:
        """Restaure une liste supprimée (fenêtre de 90 jours)."""
        return self._request("PUT", f"/crm/v3/lists/{list_id}/restore")

    # --- Appartenances (membres d'une liste) --------------------------------

    def get_list_memberships(
        self, list_id: str, limit: int = 100, after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les ids des enregistrements membres d'une liste (paginé)."""
        params: Dict[str, Any] = {"limit": min(limit, 250)}
        if after:
            params["after"] = after
        return self._request(
            "GET", f"/crm/v3/lists/{list_id}/memberships", params=params)

    def add_list_memberships(
        self, list_id: str, record_ids: List[str],
    ) -> Dict[str, Any]:
        """Ajoute des enregistrements à une liste MANUAL/SNAPSHOT.

        ⚠️ Le body est un TABLEAU NU d'ids (`["1","2"]`), pas un objet.
        """
        return self._request(
            "PUT", f"/crm/v3/lists/{list_id}/memberships/add", json=record_ids)

    def remove_list_memberships(
        self, list_id: str, record_ids: List[str],
    ) -> Dict[str, Any]:
        """Retire des enregistrements d'une liste (body = tableau nu d'ids)."""
        return self._request(
            "PUT", f"/crm/v3/lists/{list_id}/memberships/remove", json=record_ids)

    def add_and_remove_list_memberships(
        self,
        list_id: str,
        record_ids_to_add: Optional[List[str]] = None,
        record_ids_to_remove: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Ajoute ET retire en une seule opération (une seule révision de liste)."""
        return self._request(
            "PUT", f"/crm/v3/lists/{list_id}/memberships/add-and-remove",
            json={
                "recordIdsToAdd": record_ids_to_add or [],
                "recordIdsToRemove": record_ids_to_remove or [],
            })

    def delete_all_list_memberships(self, list_id: str) -> Dict[str, Any]:
        """Vide une liste de TOUS ses membres (la liste elle-même survit)."""
        return self._request("DELETE", f"/crm/v3/lists/{list_id}/memberships")

    def add_memberships_from_list(
        self, list_id: str, source_list_id: str,
    ) -> Dict[str, Any]:
        """Copie les membres d'une autre liste (plafond HubSpot : 100 000)."""
        return self._request(
            "PUT",
            f"/crm/v3/lists/{list_id}/memberships/add-from/{source_list_id}")

    def get_record_memberships(
        self, object_type_id: str, record_id: str,
    ) -> Dict[str, Any]:
        """Listes auxquelles UN enregistrement appartient."""
        return self._request(
            "GET",
            f"/crm/v3/lists/records/{object_type_id}/{record_id}/memberships")
