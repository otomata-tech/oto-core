"""Pipedrive CRM API client.

Auth = **API token personnel** (Settings → Personal preferences → API), passé en
header `x-api-token`. ⚠️ JAMAIS en query string `?api_token=` (le token entrerait
dans l'URL, donc dans les messages d'exception et les access logs — cf. CLAUDE.md).

Deux versions d'API cohabitent chez Pipedrive et on garde ce fait visible :
- **v2** (`/api/v2`) = le CRM courant : deals, persons, organizations, activities,
  products, pipelines, stages + les endpoints de recherche. Pagination **curseur**
  (`cursor`/`limit`), custom fields regroupés sous `custom_fields`.
- **v1** = ce qui n'a pas (encore) été porté : notes, users, leads (CRUD).
  Pagination **offset** (`start`/`limit`).

Base URL : `https://api.pipedrive.com` par défaut (serveur déclaré par la spec
OpenAPI officielle). Passer `company_domain` (le sous-domaine du compte, ex.
`acme` pour `acme.pipedrive.com`) route la requête vers le bon data center —
recommandé par Pipedrive pour la latence, non requis pour l'auth.

Docs : https://developers.pipedrive.com/docs/api/v1

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import FieldFilter, raise_for_upstream

# Entités servies par l'API v2 avec le CRUD générique (chemin = le nom lui-même).
V2_ENTITIES = ("deals", "persons", "organizations", "activities", "products",
               "pipelines", "stages")

# Entités qui ont un endpoint `/{entity}/search` en v2. `leads` n'a QUE la
# recherche en v2 (son CRUD est resté en v1).
SEARCHABLE = ("deals", "persons", "organizations", "products", "leads")

# Endpoint « champs » (schéma, custom fields inclus) par entité.
_FIELDS_ENDPOINT = {
    "deals": "dealFields",
    "persons": "personFields",
    "organizations": "organizationFields",
    "products": "productFields",
    "activities": "activityFields",
}


class PipedriveClient:
    """Client Pipedrive — CRUD générique v2 + notes/users/leads restés en v1."""

    def __init__(
        self,
        api_token: Optional[str] = None,
        company_domain: Optional[str] = None,
        field_filter: Optional[FieldFilter] = None,
    ):
        """Initialise le client.

        Args:
            api_token: token API personnel (ou env `PIPEDRIVE_API_TOKEN`).
            company_domain: sous-domaine du compte (`acme` pour acme.pipedrive.com).
                Optionnel — route vers le bon data center.
            field_filter: redaction de champs (défaut = politique `pipedrive`).
        """
        self.api_token = api_token or require_secret("PIPEDRIVE_API_TOKEN")
        self.company_domain = (company_domain or "").strip().strip(".") or None
        self.field_filter = field_filter or FieldFilter.from_config("pipedrive")
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-token": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # --- transport ----------------------------------------------------------

    def _url(self, version: str, path: str) -> str:
        """URL absolue. Le préfixe de v1 diffère selon l'hôte (`/v1` sur
        api.pipedrive.com, `/api/v1` sur le domaine du compte)."""
        if self.company_domain:
            return f"https://{self.company_domain}.pipedrive.com/api/{version}{path}"
        prefix = "/api/v2" if version == "v2" else "/v1"
        return f"https://api.pipedrive.com{prefix}{path}"

    def _request(self, method: str, version: str, path: str, **kwargs) -> Any:
        resp = self.session.request(
            method, self._url(version, path), timeout=30, **kwargs)
        raise_for_upstream(resp, service="pipedrive")
        if not resp.content:
            return {}
        return self.field_filter.apply(resp.json())

    @staticmethod
    def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
        """Retire les paramètres non renseignés (l'API rejette un `null` explicite)."""
        return {k: v for k, v in params.items() if v is not None}

    @staticmethod
    def _collection(payload: Any) -> Dict[str, Any]:
        """Normalise l'enveloppe de liste : `{data, next_cursor}`.

        Le `success: true` est du bruit pour l'agent (l'échec lève déjà) ; le
        curseur, lui, est enterré dans `additional_data` — on le remonte.
        """
        if not isinstance(payload, dict):
            return {"data": payload, "next_cursor": None}
        extra = payload.get("additional_data") or {}
        cursor = extra.get("next_cursor")
        if cursor is None:  # v1 : pagination offset
            pagination = extra.get("pagination") or {}
            more = pagination.get("more_items_in_collection")
            cursor = pagination.get("next_start") if more else None
        return {"data": payload.get("data"), "next_cursor": cursor}

    @staticmethod
    def _check_entity(entity: str, allowed: tuple) -> str:
        if entity not in allowed:
            raise ValueError(
                f"entité Pipedrive inconnue : {entity!r} — attendu {', '.join(allowed)}")
        return entity

    # --- CRUD générique (API v2) --------------------------------------------

    def list_records(
        self,
        entity: str,
        limit: int = 100,
        cursor: Optional[str] = None,
        **filters,
    ) -> Dict[str, Any]:
        """Liste les enregistrements d'une entité (paginé par curseur).

        Args:
            entity: deals | persons | organizations | activities | products |
                pipelines | stages.
            limit: 1..500 (défaut API : 100).
            cursor: `next_cursor` renvoyé par l'appel précédent.
            **filters: filtres de l'endpoint (owner_id, org_id, person_id,
                pipeline_id, stage_id, status, filter_id, updated_since,
                sort_by, sort_direction, include_fields, custom_fields…).
        """
        self._check_entity(entity, V2_ENTITIES)
        params = self._clean({"limit": min(limit, 500), "cursor": cursor, **filters})
        return self._collection(self._request("GET", "v2", f"/{entity}", params=params))

    def get_record(
        self,
        entity: str,
        record_id: int,
        include_fields: Optional[str] = None,
        custom_fields: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Récupère un enregistrement par id."""
        self._check_entity(entity, V2_ENTITIES)
        params = self._clean(
            {"include_fields": include_fields, "custom_fields": custom_fields})
        return self._request("GET", "v2", f"/{entity}/{record_id}", params=params)

    def create_record(self, entity: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Crée un enregistrement (`data` = corps JSON de l'API v2)."""
        self._check_entity(entity, V2_ENTITIES)
        return self._request("POST", "v2", f"/{entity}", json=data)

    def update_record(
        self, entity: str, record_id: int, data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Met à jour un enregistrement (PATCH partiel — v2 n'utilise plus PUT)."""
        self._check_entity(entity, V2_ENTITIES)
        return self._request("PATCH", "v2", f"/{entity}/{record_id}", json=data)

    def delete_record(self, entity: str, record_id: int) -> Dict[str, Any]:
        """Supprime un enregistrement."""
        self._check_entity(entity, V2_ENTITIES)
        return self._request("DELETE", "v2", f"/{entity}/{record_id}")

    # --- Recherche ----------------------------------------------------------

    def search(
        self,
        entity: str,
        term: str,
        fields: Optional[str] = None,
        exact_match: bool = False,
        limit: int = 100,
        cursor: Optional[str] = None,
        **filters,
    ) -> Dict[str, Any]:
        """Recherche plein texte dans UNE entité.

        Args:
            entity: deals | persons | organizations | products | leads.
            term: ≥2 caractères (1 seul si `exact_match`).
            fields: champs interrogés, séparés par des virgules (défaut = tous les
                champs cherchables ; ex. `name,email` sur persons).
            exact_match: correspondance exacte (insensible à la casse).
            **filters: person_id / organization_id / status selon l'entité.
        """
        self._check_entity(entity, SEARCHABLE)
        params = self._clean({
            "term": term, "fields": fields, "limit": min(limit, 500),
            "cursor": cursor, **filters,
        })
        if exact_match:
            params["exact_match"] = "true"
        return self._collection(
            self._request("GET", "v2", f"/{entity}/search", params=params))

    def search_all(
        self,
        term: str,
        item_types: Optional[str] = None,
        fields: Optional[str] = None,
        exact_match: bool = False,
        search_for_related_items: bool = False,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Recherche transverse (`/itemSearch`) sur plusieurs types d'objets.

        Args:
            item_types: types cherchés, séparés par des virgules — deal, person,
                organization, product, lead, file, mail_attachment, project.
            search_for_related_items: joint aussi les objets liés aux résultats.
        """
        params = self._clean({
            "term": term, "item_types": item_types, "fields": fields,
            "limit": min(limit, 500), "cursor": cursor,
        })
        if exact_match:
            params["exact_match"] = "true"
        if search_for_related_items:
            params["search_for_related_items"] = "true"
        return self._collection(self._request("GET", "v2", "/itemSearch", params=params))

    # --- Schéma (champs, custom fields) -------------------------------------

    def list_fields(
        self, entity: str, limit: int = 100, cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les champs d'une entité — **clé des custom fields** : ils sont
        keyés par un hash de 40 caractères, que cet appel donne (avec leur
        libellé, type et options).

        Args:
            entity: deals | persons | organizations | products | activities.
        """
        endpoint = _FIELDS_ENDPOINT.get(entity)
        if not endpoint:
            raise ValueError(
                f"pas d'endpoint champs pour {entity!r} — attendu "
                f"{', '.join(_FIELDS_ENDPOINT)}")
        params = self._clean({"limit": min(limit, 500), "cursor": cursor})
        return self._collection(
            self._request("GET", "v2", f"/{endpoint}", params=params))

    # --- Notes (API v1) -----------------------------------------------------

    def list_notes(
        self,
        deal_id: Optional[int] = None,
        person_id: Optional[int] = None,
        org_id: Optional[int] = None,
        lead_id: Optional[str] = None,
        user_id: Optional[int] = None,
        limit: int = 100,
        start: int = 0,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les notes, filtrées par objet lié (pagination offset `start`)."""
        params = self._clean({
            "deal_id": deal_id, "person_id": person_id, "org_id": org_id,
            "lead_id": lead_id, "user_id": user_id, "limit": limit,
            "start": start, "sort": sort,
        })
        return self._collection(self._request("GET", "v1", "/notes", params=params))

    def create_note(
        self,
        content: str,
        deal_id: Optional[int] = None,
        person_id: Optional[int] = None,
        org_id: Optional[int] = None,
        lead_id: Optional[str] = None,
        **extra,
    ) -> Dict[str, Any]:
        """Attache une note à un deal / une personne / une organisation / un lead.

        Args:
            content: corps de la note (HTML accepté).
            **extra: champs v1 additionnels (pinned_to_deal_flag, add_time…).
        """
        if not any([deal_id, person_id, org_id, lead_id]):
            raise ValueError(
                "une note doit cibler un objet : deal_id, person_id, org_id ou lead_id")
        body = self._clean({
            "content": content, "deal_id": deal_id, "person_id": person_id,
            "org_id": org_id, "lead_id": lead_id, **extra,
        })
        return self._request("POST", "v1", "/notes", json=body)

    def update_note(self, note_id: int, content: str, **extra) -> Dict[str, Any]:
        """Met à jour le contenu d'une note."""
        return self._request(
            "PUT", "v1", f"/notes/{note_id}", json={"content": content, **extra})

    def delete_note(self, note_id: int) -> Dict[str, Any]:
        """Supprime une note."""
        return self._request("DELETE", "v1", f"/notes/{note_id}")

    # --- Leads (CRUD resté en v1 ; la recherche est en v2 via `search`) ------

    def list_leads(
        self,
        owner_id: Optional[int] = None,
        person_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        filter_id: Optional[int] = None,
        limit: int = 100,
        start: int = 0,
        archived_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les leads (boîte de réception Pipedrive).

        Args:
            archived_status: archived | not_archived | all (défaut API : all).
        """
        params = self._clean({
            "owner_id": owner_id, "person_id": person_id,
            "organization_id": organization_id, "filter_id": filter_id,
            "limit": limit, "start": start, "archived_status": archived_status,
        })
        return self._collection(self._request("GET", "v1", "/leads", params=params))

    def create_lead(
        self,
        title: str,
        person_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        owner_id: Optional[int] = None,
        value: Optional[Dict[str, Any]] = None,
        expected_close_date: Optional[str] = None,
        **extra,
    ) -> Dict[str, Any]:
        """Crée un lead. Il doit être lié à une personne OU une organisation.

        Args:
            value: `{"amount": 1000, "currency": "EUR"}`.
        """
        if not (person_id or organization_id):
            raise ValueError("un lead exige person_id ou organization_id")
        body = self._clean({
            "title": title, "person_id": person_id,
            "organization_id": organization_id, "owner_id": owner_id,
            "value": value, "expected_close_date": expected_close_date, **extra,
        })
        return self._request("POST", "v1", "/leads", json=body)

    # --- Utilisateurs (API v1) ----------------------------------------------

    def list_users(self) -> Dict[str, Any]:
        """Liste les utilisateurs du compte — pour assigner un `owner_id`."""
        return self._collection(self._request("GET", "v1", "/users"))

    def get_current_user(self) -> Dict[str, Any]:
        """Utilisateur porteur du token (compte, société, devise) — sert de sonde."""
        return self._request("GET", "v1", "/users/me")

    # --- Pipelines / stages (raccourcis lisibles sur `list_records`) --------

    def list_pipelines(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Pipelines du compte (id → nom), pour situer un deal."""
        return self.list_records("pipelines", limit=limit).get("data") or []

    def list_stages(
        self, pipeline_id: Optional[int] = None, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Étapes, optionnellement d'un seul pipeline."""
        params = {"pipeline_id": pipeline_id} if pipeline_id else {}
        return self.list_records("stages", limit=limit, **params).get("data") or []
