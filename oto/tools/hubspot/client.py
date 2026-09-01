"""HubSpot CRM API client (v3).

Auth = **private app access token** (Bearer). Créé dans HubSpot :
Settings → Integrations → Private Apps → scopes `crm.objects.*` (read/write).
Passé en clair au constructeur (ou `HUBSPOT_API_KEY` en fallback CLI).

Surface générique sur les objets CRM : `contacts`, `companies`, `deals`,
`tickets` (et tout objet custom) partagent les mêmes verbes
(`list/get/search/create/update/delete` + associations). Les notes/engagements
ont un helper dédié car ils s'attachent à un objet via une association.

Docs : https://developers.hubspot.com/docs/api/crm/understanding-the-crm

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

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
        url = f"{self.BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        raise_for_upstream(resp, service="hubspot")
        return resp.json() if resp.content else {}

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
        import time
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
