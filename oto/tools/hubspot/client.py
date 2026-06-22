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
        assoc_type = _NOTE_ASSOCIATION_TYPE.get(object_type, 202)
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
