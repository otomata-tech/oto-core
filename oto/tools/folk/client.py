"""Folk CRM API Client — https://developer.folk.app/api-reference"""

import time
from datetime import datetime, timezone
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


# Filtres de `GET /v1/tasks` — champ → opérateurs LÉGAUX (doc
# developer.folk.app/api-reference/filtering §Filterable fields for tasks,
# 2026-08-27). Deux raisons de ne PAS réutiliser `filter_params` ici :
#
# 1. l'opérateur par défaut de `filter_params` est `like`, qui n'existe sur
#    AUCUN champ de tâche — `{"dueAt": "2026-08-27"}` partirait en
#    `filter[dueAt][like]` (422, ou pire : silencieusement ignoré) ;
# 2. `entity` est un champ de RELATION mais s'écrit À PLAT
#    (`filter[entity][in]=per_…`), là où `groups`/`companies` sur les
#    personnes veulent `filter[groups][in][id]=grp_…`. L'ajouter à
#    RELATION_FIELDS produirait donc le mauvais encodage.
#
# Allow-list codée en dur, comme `_CREATE_FIELDS` côté backend : un champ ou
# un opérateur inconnu doit lever en NOMMANT ce qui existe, jamais partir tel
# quel vers Folk.
TASK_FILTER_OPS: Dict[str, frozenset] = {
    "dueAt": frozenset({"eq", "not_eq", "gt", "lt"}),
    "createdAt": frozenset({"gt", "lt"}),
    "assigneeUserId": frozenset({"in", "not_in"}),
    "entity": frozenset({"in", "not_in"}),
    "completedAt": frozenset({"empty", "not_empty", "gt", "lt"}),
}

# Opérateur implicite quand l'appelant passe une valeur nue plutôt qu'un
# `{op: valeur}`. Défini seulement là où il n'y a pas d'ambiguïté : une date
# `createdAt`/`completedAt` nue ne veut rien dire (avant ? après ?), on exige
# l'opérateur plutôt que d'en inventer un.
TASK_FILTER_DEFAULT_OP = {"dueAt": "eq", "assigneeUserId": "in", "entity": "in"}

# `empty`/`not_empty` sont des prédicats sans opérande : Folk les veut avec une
# valeur VIDE (`filter[completedAt][empty]=`). Quoi que passe l'appelant (True,
# None, "yes"…), on normalise — sinon `filter[completedAt][empty]=True` teste
# une égalité qui n'a pas de sens.
_TASK_VALUELESS_OPS = frozenset({"empty", "not_empty"})


def task_filter_params(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Traduit `{champ: valeur}` / `{champ: {op: valeur}}` en `filter[...]` de
    `GET /v1/tasks`, en refusant tout champ ou opérateur hors doc.

    Les listes (`in`/`not_in`) sont laissées telles quelles : `requests` les
    sérialise en param RÉPÉTÉ (`filter[entity][in]=a&filter[entity][in]=b`).
    Vérifié en live le 2026-08-27 sur deux entités : la clé répétée est bien
    comprise comme une union (2 ids → 4 tâches, 1 id → 2).
    """
    params: Dict[str, Any] = {}
    for key, val in (filters or {}).items():
        allowed = TASK_FILTER_OPS.get(key)
        if allowed is None:
            raise ValueError(
                f"filtre de tâche inconnu : {key!r}. Champs filtrables : "
                f"{sorted(TASK_FILTER_OPS)}.")
        if isinstance(val, dict):
            pairs = list(val.items())
        else:
            op = TASK_FILTER_DEFAULT_OP.get(key)
            if op is None:
                raise ValueError(
                    f"filtre {key!r} : préciser l'opérateur, p.ex. "
                    f"{{{key!r}: {{'gt': '2026-01-01'}}}} — opérateurs "
                    f"acceptés : {sorted(allowed)}.")
            pairs = [(op, val)]
        for op, v in pairs:
            if op not in allowed:
                raise ValueError(
                    f"opérateur {op!r} non supporté sur le filtre {key!r} — "
                    f"acceptés : {sorted(allowed)}.")
            params[f"filter[{key}][{op}]"] = "" if op in _TASK_VALUELESS_OPS else v
    return params


def _assigned_users_payload(assigned_users: List[Any]) -> List[Dict[str, str]]:
    """Normalise `assigned_users` en `[{"id": …}]` OU `[{"email": …}]`.

    Folk accepte les deux formes mais **pas les deux mélangées** dans le même
    appel (doc create/update a task) : un lot mixte part en 422 opaque. On le
    refuse ici, en nommant les deux moitiés — l'appelant sait alors quoi
    couper, ce qu'un 422 de Folk ne lui dit pas.
    """
    ids, emails, out = [], [], []
    for u in assigned_users:
        if isinstance(u, dict):
            entry = {k: v for k, v in u.items() if k in ("id", "email")}
            if not entry:
                raise ValueError(
                    f"assigned_users : {u!r} n'a ni 'id' ni 'email'.")
        elif isinstance(u, str):
            entry = {"email": u} if "@" in u else {"id": u}
        else:
            raise ValueError(
                f"assigned_users : {u!r} doit être un id, un email, ou un "
                "dict {'id'|'email'}.")
        (emails if "email" in entry else ids).append(next(iter(entry.values())))
        out.append(entry)
    if ids and emails:
        raise ValueError(
            "assigned_users : Folk accepte des ids OU des emails, pas les "
            f"deux dans le même appel — ids={ids}, emails={emails}.")
    return out


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

    def _paginate(self, endpoint: str, params: Dict = None,
                  limit: Optional[int] = 100,
                  max_items: Optional[int] = None) -> List[Dict]:
        """`limit=None` : ne PAS envoyer de `limit` du tout.

        Les deux endpoints d'interactions (`/interactions/past`,
        `/interactions/upcoming`) ne déclarent QUE `cursor` et `entity.id`.
        Vérifié en live le 2026-08-27 : un `limit` y est **silencieusement
        ignoré** (page fixe de 30), pas rejeté — donc l'envoyer ne casse rien,
        mais le promettre serait un mensonge. On ne l'envoie pas, et la
        pagination par curseur fait le travail (elle, elle marche partout).

        `max_items` : ARRÊTER dès qu'on en a assez, au lieu de vider la
        collection. Indispensable là où la page est petite ET le volume non
        borné : mesuré en live le 2026-08-27, `/interactions/past` sur un
        contact actif rendait plus de 360 interactions sans être au bout, par
        pages de 30 — soit des dizaines d'allers-retours et plusieurs minutes
        pour répondre à « qu'est-ce qu'on s'est dit ». Tout tirer pour n'en
        afficher que dix n'est pas une troncature, c'est une attente.
        """
        params = dict(params or {})
        if limit is not None:
            params.setdefault("limit", limit)
        all_items = []
        while True:
            data = self._request("GET", endpoint, params=params)
            items = data.get("data", {}).get("items", [])
            all_items.extend(items)
            if max_items is not None and len(all_items) >= max_items:
                return all_items[:max_items]
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
        # `dateTime` est REQUIS par Folk (422 `path: ['dateTime'], Required`),
        # alors que ce client — et la doc du tool — le donnaient pour
        # facultatif : tout appel qui l'omettait échouait en 422 opaque.
        # Vérifié en live le 2026-08-27. Défaut = maintenant : « logue cet
        # appel sur ce contact » sans date veut dire à l'instant, et ce défaut
        # ne peut casser aucun appel qui marchait (ceux-là passaient déjà une
        # date).
        body["dateTime"] = date_time or (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))
        return self._request("POST", "interactions", json=body).get("data", {})

    # Les trois endpoints ci-dessous sont en **open beta** chez Folk (la doc
    # prévient que la surface peut bouger). Ils existaient déjà quand ce
    # client ne portait que `create_interaction` : le connecteur affirmait
    # alors qu'on ne pouvait pas RELIRE une interaction, ce qui était vrai de
    # lui, pas de Folk.
    #
    # `entity.id` est OBLIGATOIRE en query sur past/upcoming/get/delete : une
    # interaction n'est adressable que via la personne ou la société à
    # laquelle elle est rattachée (il n'y a pas de « lister tout le
    # workspace »). Seul le PATCH s'en passe.

    def list_past_interactions(self, entity_id: str,
                               max_items: Optional[int] = None) -> List[Dict]:
        return self._paginate("interactions/past", {"entity.id": entity_id},
                              limit=None, max_items=max_items)

    def list_upcoming_interactions(self, entity_id: str,
                                   max_items: Optional[int] = None) -> List[Dict]:
        return self._paginate("interactions/upcoming", {"entity.id": entity_id},
                              limit=None, max_items=max_items)

    # quote() sur l'id : contrairement aux autres ids Folk (opaques, 40 car.),
    # `get` déclare un id de 1 à 512 caractères — les interactions IMPORTÉES
    # portent l'id synthétique de leur source. Vu en live : des ids Gmail de
    # 60+ caractères contenant `+` et `_`. Folk les accepte échappés OU bruts
    # (testé les deux) ; on échappe quand même, parce que rien ne garantit
    # qu'un id de source future ne portera pas un `/` ou un `?`, qui eux
    # casseraient le chemin. Les ids d'update/delete font 40 (interactions
    # loggées seulement) : quote() y est neutre.

    def get_interaction(self, interaction_id: str, entity_id: str) -> Dict:
        return self._request(
            "GET", f"interactions/{quote(interaction_id, safe='')}",
            params={"entity.id": entity_id},
        ).get("data", {})

    def update_interaction(self, interaction_id: str, entity_id: str,
                           **fields) -> Dict:
        """⚠️ `entity_id` est OBLIGATOIRE — vérifié en live le 2026-08-27.

        La spec OpenAPI liste `entity` parmi les propriétés du corps du PATCH
        sans le marquer requis, ce qui se lit comme « optionnel, omets-le pour
        garder l'entité actuelle » (c'est d'ailleurs ce que dit la description
        du champ sur `PATCH /tasks`). Faux ici : sans lui, Folk répond 422
        `path: ['entity'], message: 'Required'`. Le PATCH est donc scopé comme
        le get et le delete, juste par le corps au lieu de la query.

        Seules les interactions LOGGÉES sont modifiables — Folk refuse les
        importées (email/calendrier/WhatsApp), qui appartiennent à leur source.
        """
        body: Dict[str, Any] = {"entity": {"id": entity_id}}
        body.update(fields)
        return self._request(
            "PATCH", f"interactions/{quote(interaction_id, safe='')}",
            json=body,
        ).get("data", {})

    def delete_interaction(self, interaction_id: str, entity_id: str) -> Dict:
        return self._request(
            "DELETE", f"interactions/{quote(interaction_id, safe='')}",
            params={"entity.id": entity_id},
        )

    # --- Tasks ---
    # Le successeur officiel des rappels (voir la section Reminders ci-dessous).

    def list_tasks(self, filters: Dict[str, Any] = None,
                   only_assigned_to_me: Optional[bool] = None,
                   combinator: Optional[str] = None) -> List[Dict]:
        """`filters` est un DICT, pas un `**splat` — contrairement à
        `list_people`. Les clés viennent de l'appelant (un agent) : `**filters`
        laisserait un filtre nommé `combinator` ou `only_assigned_to_me` se
        faire manger par le paramètre homonyme, appliqué en SILENCE et jamais
        soumis à `task_filter_params`. Même famille de collision que
        `_create_one` côté backend (signal #353) : un champ métier avalé par un
        paramètre du même nom."""
        params = task_filter_params(filters)
        if only_assigned_to_me is not None:
            # Folk déclare cette query en ENUM de chaînes ("true"/"false"),
            # pas en booléen : `requests` sérialiserait un bool Python en
            # "True"/"False" (majuscule), hors enum.
            params["onlyAssignedToMe"] = "true" if only_assigned_to_me else "false"
        if combinator:
            params["combinator"] = combinator
        return self._paginate("tasks", params)

    def get_task(self, task_id: str) -> Dict:
        return self._request("GET", f"tasks/{task_id}").get("data", {})

    def create_task(self, entity_id: str, title: str, due_at: str,
                    due_time: str = None, description: str = None,
                    recurrence_frequency: str = None,
                    assigned_users: List[Any] = None,
                    is_public: bool = None) -> Dict:
        body: Dict[str, Any] = {
            "entity": {"id": entity_id},
            "title": title,
            "dueAt": due_at,
        }
        if due_time:
            body["dueTime"] = due_time
        if description:
            body["description"] = description
        if recurrence_frequency:
            body["recurrenceFrequency"] = recurrence_frequency
        if assigned_users:
            body["assignedUsers"] = _assigned_users_payload(assigned_users)
        if is_public is not None:
            body["isPublic"] = is_public
        return self._request("POST", "tasks", json=body).get("data", {})

    def update_task(self, task_id: str, **fields) -> Dict:
        if "assignedUsers" in fields:
            fields = dict(fields)
            fields["assignedUsers"] = _assigned_users_payload(
                fields["assignedUsers"])
        return self._request("PATCH", f"tasks/{task_id}", json=fields).get("data", {})

    def delete_task(self, task_id: str) -> Dict:
        return self._request("DELETE", f"tasks/{task_id}")

    # Chemins pris sur l'OpenAPI (`/mark-as-done`, `/mark-as-todo`), PAS sur la
    # page de migration reminders→tasks, dont l'exemple écrit `/mark-done` —
    # la spec fait foi, l'exemple est une coquille.
    #
    # Une tâche ne se termine JAMAIS toute seule chez Folk : `completedAt` ne
    # bouge que sur un appel explicite. C'est la différence de fond avec un
    # rappel, qui se marque « déclenché » sur son propre calendrier.

    def mark_task_done(self, task_id: str, completed_at: str = None) -> Dict:
        # `completedAt` est REQUIS par l'endpoint (et n'est PAS acceptable dans
        # un PATCH). Par défaut : maintenant, en ISO 8601 UTC millisecondes —
        # la forme des exemples Folk.
        if not completed_at:
            completed_at = (datetime.now(timezone.utc)
                            .isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z"))
        return self._request(
            "POST", f"tasks/{task_id}/mark-as-done",
            json={"completedAt": completed_at},
        ).get("data", {})

    def mark_task_todo(self, task_id: str) -> Dict:
        # Pas de corps : rouvrir une tâche remet `completedAt` à null.
        return self._request("POST", f"tasks/{task_id}/mark-as-todo").get("data", {})

    # --- Reminders (DEPRECATED chez Folk depuis le 2026-08-13) ---
    # Retrait annoncé pour février 2027 ; le successeur est `tasks` ci-dessus
    # (mapping des champs : name→title, recurrenceRule→dueAt/dueTime +
    # recurrenceFrequency, visibility→isPublic). Ces méthodes restent tant que
    # les endpoints répondent, mais rien de nouveau ne devrait s'y brancher.
    # ⚠️ Folk ne dit NULLE PART si les rappels déjà posés remontent aussi dans
    # `list_tasks` (deux vues d'un même stock) ou s'ils vivent à côté. Non
    # vérifié en live faute de clé — à trancher avant toute migration de
    # données ; voir la note du connecteur.

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
