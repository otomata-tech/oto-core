"""Folk CRM API Client — https://developer.folk.app/api-reference"""

import time
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs, quote

import requests

from ...config import require_secret
from ..common import FieldFilter, raise_for_upstream


# Champs de RELATION : Folk n'y accepte pas les opérateurs texte (`like` → 422
# unrecognized_keys), mais `in`/`not_in`, et leur valeur est un OBJET qui porte
# l'id : `filter[groups][in][id]=grp_…` (vérifié live le 2026-08-03, doc
# developer.folk.app §list-people). Tant que le client enveloppait TOUT filtre en
# `[like]`, lister les membres d'un groupe était impossible (signal #260).
RELATION_FIELDS = frozenset({"groups", "companies"})
RELATION_OPS = frozenset({"in", "not_in"})

# Valeurs valides de `subscribedEvents[].eventType` pour les webhooks (doc
# developer.folk.app/api-reference/webhooks/create-a-webhook, 2026-08-04).
# `object.*` couvre les deals ET tout autre object_type custom (pas de variante
# par object_type — le scoping se fait via `filter.objectType`).
WEBHOOK_EVENT_TYPES = frozenset({
    "person.created", "person.updated", "person.deleted",
    "person.groups_updated", "person.workspace_interaction_metadata_updated",
    "company.created", "company.updated", "company.deleted",
    "company.groups_updated",
    "object.created", "object.updated", "object.deleted",
    "note.created", "note.updated", "note.deleted",
    "reminder.created", "reminder.updated", "reminder.deleted",
    "reminder.triggered",
})


def filter_params(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Traduit `{champ: valeur}` en query params `filter[...]` de Folk.

    - valeur simple sur un champ de relation → `filter[champ][in][id]`
      (« appartient à ce groupe / cette société ») ;
    - valeur simple sur un champ texte → `filter[champ][like]` (le défaut
      historique : contient) ;
    - valeur `{opérateur: valeur}` → l'opérateur demandé, tel quel
      (`eq`, `not_eq`, `not_like`, `empty`, `not_empty`, `gt`, `in`, `not_in`)
      — un appelant qui veut l'égalité stricte n'a plus à contourner le client.
    """
    params: Dict[str, Any] = {}
    for key, val in (filters or {}).items():
        if isinstance(val, dict):
            for op, v in val.items():
                if key in RELATION_FIELDS and op in RELATION_OPS:
                    params[f"filter[{key}][{op}][id]"] = v
                else:
                    params[f"filter[{key}][{op}]"] = v
        elif key in RELATION_FIELDS:
            params[f"filter[{key}][in][id]"] = val
        else:
            params[f"filter[{key}][like]"] = val
    return params


class FolkClient:
    BASE_URL = "https://api.folk.app/v1"

    def __init__(self, api_key: str = None, field_filter: Optional[FieldFilter] = None):
        self.api_key = api_key or require_secret("FOLK_API_KEY")
        # Redacts sensitive fields (emails, names…) from every response.
        # Defaults to the `field_filters.folk` policy in ~/.otomata/config.yaml.
        self.field_filter = field_filter or FieldFilter.from_config("folk")

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}/{endpoint}" if not endpoint.startswith("http") else endpoint
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if method.upper() != "DELETE":
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            resp = requests.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
            raise_for_upstream(resp, service="folk")
            return self.field_filter.apply(resp.json()) if resp.content else {}
        raise Exception("Rate limit exceeded after retries")

    def _paginate(self, endpoint: str, params: Dict = None) -> List[Dict]:
        params = params or {}
        params.setdefault("limit", 100)
        all_items = []
        while True:
            data = self._request("GET", endpoint, params=params)
            items = data.get("data", {}).get("items", [])
            all_items.extend(items)
            next_link = data.get("data", {}).get("pagination", {}).get("nextLink")
            if not next_link:
                break
            # Extract cursor from nextLink
            parsed = parse_qs(urlparse(next_link).query)
            cursor = parsed.get("cursor", [None])[0]
            if not cursor:
                break
            params["cursor"] = cursor
        return all_items

    # --- Groups ---

    def list_groups(self) -> List[Dict]:
        return self._paginate("groups")

    def create_group(self, name: str, visibility: str) -> Dict:
        return self._request("POST", "groups", json={
            "name": name, "visibility": visibility,
        }).get("data", {})

    def update_group(self, group_id: str, **fields) -> Dict:
        return self._request("PATCH", f"groups/{group_id}", json=fields).get("data", {})

    # entity_type/custom_field_name sont des noms libres (espaces, accents…), pas
    # des ids opaques — quote() sinon un nom comme "Deal Status" casse le chemin.

    def get_group_custom_fields(self, group_id: str, entity_type: str = "person") -> List[Dict]:
        # Paginé côté API (cursor, jusqu'à 100/page) — un groupe avec >20 champs
        # custom (limite par défaut) était silencieusement tronqué par un `_request`
        # simple.
        return self._paginate(f"groups/{group_id}/custom-fields/{quote(entity_type, safe='')}")

    def get_group_custom_field(self, group_id: str, entity_type: str,
                               custom_field_name: str) -> Dict:
        return self._request(
            "GET",
            f"groups/{group_id}/custom-fields/{quote(entity_type, safe='')}/"
            f"{quote(custom_field_name, safe='')}",
        ).get("data", {})

    def create_group_custom_field(self, group_id: str, entity_type: str, **field) -> Dict:
        return self._request(
            "POST", f"groups/{group_id}/custom-fields/{quote(entity_type, safe='')}",
            json=field,
        ).get("data", {})

    def update_group_custom_field(self, group_id: str, entity_type: str,
                                  custom_field_name: str, **fields) -> Dict:
        # Seul endpoint Folk de ce client dont la doc montre le champ nested
        # sous `data.item` (+ `data.nextLink`) plutôt qu'à plat sous `data` —
        # confirmé sur l'exemple JSON brut de la doc (get/create custom field,
        # eux, rendent le champ à plat). `.get("item", data)` couvre les deux
        # formes si Folk aligne un jour ce endpoint sur ses siblings.
        #
        # Renommage vérifié EN LIVE (2026-08-17, workspace de test) : la
        # réponse du PATCH porte bien le NOUVEAU nom (`item.name`), et un
        # `get_custom_field` sur l'ancien nom juste après renvoie 404 propre —
        # pas de fenêtre où `custom_field_name` deviendrait incohérent entre
        # la réponse et un re-fetch.
        data = self._request(
            "PATCH",
            f"groups/{group_id}/custom-fields/{quote(entity_type, safe='')}/"
            f"{quote(custom_field_name, safe='')}",
            json=fields,
        ).get("data", {})
        return data.get("item", data)

    # --- Group members ---
    # user_id est un id opaque (usr_…), pas de quote() nécessaire (≠ entity_type/
    # custom_field_name qui sont des noms libres).

    def list_group_members(self, group_id: str) -> List[Dict]:
        # Paginé côté API (cursor, jusqu'à 100/page) — même bug que
        # get_group_custom_fields évité d'entrée : _paginate, pas _request.
        return self._paginate(f"groups/{group_id}/members")

    def add_group_member(self, group_id: str, user_id: str, role: str) -> Dict:
        return self._request("POST", f"groups/{group_id}/members", json={
            "id": user_id, "role": role,
        }).get("data", {})

    def remove_group_member(self, group_id: str, user_id: str) -> Dict:
        return self._request("DELETE", f"groups/{group_id}/members/{user_id}")

    def update_group_member(self, group_id: str, user_id: str, role: str) -> Dict:
        return self._request(
            "PATCH", f"groups/{group_id}/members/{user_id}", json={"role": role},
        ).get("data", {})

    # --- People ---

    def list_people(self, **filters) -> List[Dict]:
        return self._paginate("people", filter_params(filters))

    def get_person(self, person_id: str) -> Dict:
        return self._request("GET", f"people/{person_id}").get("data", {})

    def create_person(self, first_name: str, last_name: str = None,
                      emails: List[str] = None, phones: List[str] = None,
                      job_title: str = None, company_name: str = None,
                      company_id: str = None, group_ids: List[str] = None,
                      urls: List[str] = None, description: str = None,
                      **kwargs) -> Dict:
        body: Dict[str, Any] = {"firstName": first_name}
        if last_name:
            body["lastName"] = last_name
        if emails:
            body["emails"] = emails
        if phones:
            body["phones"] = phones
        if urls:
            body["urls"] = urls
        if description:
            body["description"] = description
        if job_title:
            body["jobTitle"] = job_title
        companies = []
        if company_id:
            companies.append({"id": company_id})
        elif company_name:
            companies.append({"name": company_name})
        if companies:
            body["companies"] = companies
        if group_ids:
            body["groups"] = [{"id": gid} for gid in group_ids]
        body.update(kwargs)
        return self._request("POST", "people", json=body).get("data", {})

    def update_person(self, person_id: str, **fields) -> Dict:
        return self._request("PATCH", f"people/{person_id}", json=fields).get("data", {})

    def delete_person(self, person_id: str) -> Dict:
        return self._request("DELETE", f"people/{person_id}")

    # --- Companies ---

    def list_companies(self, **filters) -> List[Dict]:
        return self._paginate("companies", filter_params(filters))

    def get_company(self, company_id: str) -> Dict:
        return self._request("GET", f"companies/{company_id}").get("data", {})

    def create_company(self, name: str, emails: List[str] = None,
                       industry: str = None, **kwargs) -> Dict:
        body: Dict[str, Any] = {"name": name}
        if emails:
            body["emails"] = emails
        if industry:
            body["industry"] = industry
        body.update(kwargs)
        return self._request("POST", "companies", json=body).get("data", {})

    def update_company(self, company_id: str, **fields) -> Dict:
        return self._request("PATCH", f"companies/{company_id}", json=fields).get("data", {})

    def delete_company(self, company_id: str) -> Dict:
        return self._request("DELETE", f"companies/{company_id}")

    # --- Deals (objects in groups) ---

    def list_deals(self, group_id: str, object_type: str = "deals", **filters) -> List[Dict]:
        return self._paginate(f"groups/{group_id}/{object_type}", filter_params(filters))

    def get_deal(self, group_id: str, deal_id: str, object_type: str = "deals") -> Dict:
        return self._request(
            "GET", f"groups/{group_id}/{object_type}/{deal_id}"
        ).get("data", {})

    def create_deal(self, group_id: str, name: str, object_type: str = "deals",
                    people_ids: List[str] = None, company_ids: List[str] = None,
                    custom_fields: Dict = None) -> Dict:
        body: Dict[str, Any] = {"name": name}
        if people_ids:
            body["people"] = [{"id": pid} for pid in people_ids]
        if company_ids:
            body["companies"] = [{"id": cid} for cid in company_ids]
        if custom_fields:
            body["customFieldValues"] = custom_fields
        return self._request("POST", f"groups/{group_id}/{object_type}", json=body).get("data", {})

    def update_deal(self, group_id: str, deal_id: str, object_type: str = "deals",
                    **fields) -> Dict:
        return self._request("PATCH", f"groups/{group_id}/{object_type}/{deal_id}", json=fields).get("data", {})

    def delete_deal(self, group_id: str, deal_id: str, object_type: str = "deals") -> Dict:
        return self._request("DELETE", f"groups/{group_id}/{object_type}/{deal_id}")

    # --- Notes ---

    def list_notes(self, entity_id: str = None) -> List[Dict]:
        # L'API Folk IGNORE `filter[entity.id][eq]` sur /notes (vérifié
        # empiriquement : le param est accepté sans erreur mais renvoie tout le
        # workspace). On filtre donc côté client sur l'entité rattachée
        # (chaque note porte `entity.id`). oto-backend#224.
        notes = self._paginate("notes", {})
        if entity_id:
            notes = [n for n in notes if (n.get("entity") or {}).get("id") == entity_id]
        return notes

    def create_note(self, entity_id: str, content: str, visibility: str = "public") -> Dict:
        return self._request("POST", "notes", json={
            "entity": {"id": entity_id},
            "content": content,
            "visibility": visibility,
        }).get("data", {})

    def update_note(self, note_id: str, **fields) -> Dict:
        return self._request("PATCH", f"notes/{note_id}", json=fields).get("data", {})

    def delete_note(self, note_id: str) -> Dict:
        return self._request("DELETE", f"notes/{note_id}")

    # --- Interactions ---

    def create_interaction(self, entity_id: str, type: str, title: str,
                           content: str = None, date_time: str = None) -> Dict:
        body: Dict[str, Any] = {
            "entity": {"id": entity_id},
            "type": type,
            "title": title,
        }
        if content:
            body["content"] = content
        if date_time:
            body["dateTime"] = date_time
        return self._request("POST", "interactions", json=body).get("data", {})

    # --- Reminders ---

    def list_reminders(self, entity_id: str = None) -> List[Dict]:
        # Même bug que list_notes : le filtre serveur par entité est ignoré →
        # on filtre côté client sur `entity.id`. oto-backend#224.
        reminders = self._paginate("reminders", {})
        if entity_id:
            reminders = [r for r in reminders if (r.get("entity") or {}).get("id") == entity_id]
        return reminders

    def create_reminder(self, entity_id: str, name: str,
                        recurrence_rule: str, visibility: str = "public") -> Dict:
        return self._request("POST", "reminders", json={
            "entity": {"id": entity_id},
            "name": name,
            "recurrenceRule": recurrence_rule,
            "visibility": visibility,
        }).get("data", {})

    def get_reminder(self, reminder_id: str) -> Dict:
        return self._request("GET", f"reminders/{reminder_id}").get("data", {})

    def update_reminder(self, reminder_id: str, **fields) -> Dict:
        return self._request("PATCH", f"reminders/{reminder_id}", json=fields).get("data", {})

    def delete_reminder(self, reminder_id: str) -> Dict:
        return self._request("DELETE", f"reminders/{reminder_id}")

    # --- Users (workspace members, read-only) ---

    def list_users(self) -> List[Dict]:
        return self._paginate("users")

    def get_current_user(self) -> Dict:
        return self._request("GET", "users/me").get("data", {})

    def get_user(self, user_id: str) -> Dict:
        """Fetch a workspace user by ID. `user_id="me"` returns the current user."""
        if user_id == "me":
            return self.get_current_user()
        return self._request("GET", f"users/{user_id}").get("data", {})

    # --- Webhooks ---

    def list_webhooks(self) -> List[Dict]:
        return self._paginate("webhooks")

    def get_webhook(self, webhook_id: str) -> Dict:
        return self._request("GET", f"webhooks/{webhook_id}").get("data", {})

    def create_webhook(self, name: str, target_url: str,
                       subscribed_events: List[Dict]) -> Dict:
        return self._request("POST", "webhooks", json={
            "name": name,
            "targetUrl": target_url,
            "subscribedEvents": subscribed_events,
        }).get("data", {})

    def update_webhook(self, webhook_id: str, **fields) -> Dict:
        return self._request("PATCH", f"webhooks/{webhook_id}", json=fields).get("data", {})

    def delete_webhook(self, webhook_id: str) -> Dict:
        return self._request("DELETE", f"webhooks/{webhook_id}")
