"""Brevo — CRM natif : deals, companies, tasks, notes, pipelines.

Surface **générique** (`entity` en paramètre) plutôt que 4×4 méthodes : les
quatre objets partagent list/get/create/update. Trois asymétries de l'API sont
absorbées ici, elles ne remontent pas à l'appelant :

- le chemin : `companies` vit à `/companies`, les trois autres sous `/crm/…` ;
- la pagination : `companies` pagine par `page` (1-based), les autres par `offset` ;
- le préfixe de filtre : `filters[…]` pour deals/companies, `filter[…]` pour tasks,
  paramètres plats pour notes.

Les corps de création diffèrent trop pour être uniformisés : `payload` est passé
brut à l'API (clés camelCase Brevo), avec les champs requis documentés ci-dessous.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._base import _BrevoBase

# entity → (chemin de collection, préfixe de filtre, pagine par page ?)
_ENTITIES: Dict[str, tuple] = {
    "deals": ("/crm/deals", "filters", False),
    "companies": ("/companies", "filters", True),
    "tasks": ("/crm/tasks", "filter", False),
    "notes": ("/crm/notes", None, False),
}

# entity → champs requis à la création (pour un message d'erreur utile côté agent)
REQUIRED_FIELDS: Dict[str, tuple] = {
    "deals": ("name",),
    "companies": ("name",),
    "tasks": ("name", "taskTypeId", "date"),
    "notes": ("text",),
}


class CrmMixin(_BrevoBase):

    @staticmethod
    def _entity(entity: str) -> tuple:
        try:
            return _ENTITIES[entity]
        except KeyError:
            raise ValueError(
                f"entity inconnue {entity!r} — attendu : {', '.join(_ENTITIES)}")

    def crm_list(
        self,
        entity: str,
        limit: int = 50,
        offset: int = 0,
        filters: Optional[Dict[str, Any]] = None,
        sort: Optional[str] = None,
        sort_by: Optional[str] = None,
        modified_since: Optional[str] = None,
        created_since: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste des objets CRM.

        Args:
            entity: `deals` | `companies` | `tasks` | `notes`.
            filters: clés de filtre BRUTES de l'entité, sans le préfixe. Ex.
                deals → `{"attributes.deal_name": "Acme", "linkedContactsIds": "12"}` ;
                companies → `{"attributes.name": "Acme"}` ;
                tasks → `{"type": "call", "status": "done", "contacts": "12"}` ;
                notes → `{"entity": "deals", "entityIds": "abc123"}` (params plats).
            offset: converti en `page` pour `companies` (l'API y pagine par page).
            sort_by: champ de tri (`deals`/`companies`/`tasks`).
        """
        path, prefix, by_page = self._entity(entity)
        params: Dict[str, Any] = {"limit": limit, "sort": sort, "sortBy": sort_by,
                                  "modifiedSince": modified_since,
                                  "createdSince": created_since}
        if by_page:
            params["page"] = (offset // limit) + 1 if limit else 1
        else:
            params["offset"] = offset
        for key, value in (filters or {}).items():
            params[f"{prefix}[{key}]" if prefix else key] = value
        return self._request("GET", path, params=self._clean(params))

    def crm_get(self, entity: str, object_id: str) -> Dict[str, Any]:
        """Récupère un objet CRM par id."""
        path, _, _ = self._entity(entity)
        return self._request("GET", f"{path}/{object_id}")

    def crm_create(self, entity: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Crée un objet CRM. Renvoie `{"id": …}`.

        `payload` en camelCase Brevo. Champs requis :
        - **deals** : `name` (+ `attributes`, `linkedContactsIds`, `linkedCompaniesIds`)
        - **companies** : `name` (+ `attributes`, `countryCode`, `linkedContactsIds`)
        - **tasks** : `name`, `taskTypeId` (cf. `task_types`), `date` (ISO 8601)
          (+ `contactsIds`, `dealsIds`, `companiesIds`, `assignToId`, `notes`, `done`)
        - **notes** : `text` (+ `contactIds`, `dealIds`, `companyIds`)
        """
        path, _, _ = self._entity(entity)
        missing = [f for f in REQUIRED_FIELDS[entity] if not payload.get(f)]
        if missing:
            raise ValueError(
                f"champs requis manquants pour {entity} : {', '.join(missing)}")
        return self._request("POST", path, json=payload)

    def crm_update(self, entity: str, object_id: str,
                   payload: Dict[str, Any]) -> Dict[str, Any]:
        """Met à jour (PATCH) un objet CRM — champs fournis seulement.

        Pour rattacher/détacher des objets liés, utiliser `crm_link` (endpoint dédié
        sur deals et companies).
        """
        path, _, _ = self._entity(entity)
        return self._request("PATCH", f"{path}/{object_id}", json=payload)

    def crm_link(
        self,
        entity: str,
        object_id: str,
        link_contact_ids: Optional[List[int]] = None,
        unlink_contact_ids: Optional[List[int]] = None,
        link_ids: Optional[List[str]] = None,
        unlink_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Rattache/détache des objets liés — `deals` et `companies` uniquement.

        `link_ids`/`unlink_ids` visent l'objet complémentaire : les **companies**
        d'un deal, les **deals** d'une company.
        """
        if entity not in ("deals", "companies"):
            raise ValueError("crm_link ne s'applique qu'à deals et companies.")
        path, _, _ = self._entity(entity)
        if entity == "deals":
            body = self._clean({
                "linkContactIds": link_contact_ids,
                "unlinkContactIds": unlink_contact_ids,
                "linkCompanyIds": link_ids, "unlinkCompanyIds": unlink_ids})
        else:
            body = self._clean({
                "linkContactIds": link_contact_ids,
                "unlinkContactIds": unlink_contact_ids,
                "linkDealsIds": link_ids, "unlinkDealsIds": unlink_ids})
        return self._request("PATCH", f"{path}/link-unlink/{object_id}", json=body)

    # --- Métadonnées ----------------------------------------------------------

    def pipelines(self) -> Dict[str, Any]:
        """Tous les pipelines de deals et leurs étapes (`id` d'étape pour `deal_stage`)."""
        return self._request("GET", "/crm/pipeline/details/all")

    def task_types(self) -> Dict[str, Any]:
        """Types de tâche du compte (leur `id` est requis pour créer une tâche)."""
        return self._request("GET", "/crm/tasktypes")

    def crm_attributes(self, entity: str) -> Dict[str, Any]:
        """Attributs personnalisés déclarés sur `deals` ou `companies`."""
        if entity not in ("deals", "companies"):
            raise ValueError("crm_attributes ne s'applique qu'à deals et companies.")
        return self._request("GET", f"/crm/attributes/{entity}")
