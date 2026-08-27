"""FolkClient — verrouille method+endpoint des opérations ajoutées (deals get/
delete/search, reminders get/update/delete, users list/me/get).

Mocke `requests.request` : on vérifie le CONTRAT HTTP (verbe + URL + body) que le
client construit, sans réseau ni clé réelle.
"""
import json

import pytest

from oto.tools.folk import client as folk_client
from oto.tools.folk.client import FolkClient

BASE = "https://api.folk.app/v1"


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {}
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class _Stub:
    """field_filter no-op (évite la lecture de ~/.otomata/config.yaml)."""

    def apply(self, x):
        return x


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def fake_request(method, url, headers=None, **kwargs):
        captured.append({"method": method, "url": url, **kwargs})
        return _Resp({"data": {"id": "x", "items": [], "pagination": {}}})

    monkeypatch.setattr(folk_client.requests, "request", fake_request)
    return captured


@pytest.fixture
def c():
    return FolkClient(api_key="test-key", field_filter=_Stub())


# --- Deals ---------------------------------------------------------------

def test_get_deal(c, calls):
    c.get_deal("grp_1", "obj_9")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/deals/obj_9"


def test_get_deal_custom_object(c, calls):
    c.get_deal("grp_1", "obj_9", object_type="projects")
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/projects/obj_9"


def test_delete_deal(c, calls):
    c.delete_deal("grp_1", "obj_9")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/deals/obj_9"


def test_search_deals_via_list_filters(c, calls):
    c.list_deals("grp_1", name="Acme")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/deals"
    assert calls[-1]["params"]["filter[name][like]"] == "Acme"


# --- Reminders -----------------------------------------------------------

def test_get_reminder(c, calls):
    c.get_reminder("rmd_1")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/reminders/rmd_1"


def test_update_reminder(c, calls):
    c.update_reminder("rmd_1", name="Follow-up")
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/reminders/rmd_1"
    assert calls[-1]["json"] == {"name": "Follow-up"}


def test_delete_reminder(c, calls):
    c.delete_reminder("rmd_1")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/reminders/rmd_1"


# --- Users ---------------------------------------------------------------

def test_list_users(c, calls):
    c.list_users()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/users"


def test_get_current_user(c, calls):
    c.get_current_user()
    assert calls[-1]["url"] == f"{BASE}/users/me"


def test_get_user_me_alias(c, calls):
    c.get_user("me")
    assert calls[-1]["url"] == f"{BASE}/users/me"


def test_get_user_by_id(c, calls):
    c.get_user("usr_42")
    assert calls[-1]["url"] == f"{BASE}/users/usr_42"


# --- Notes / reminders : filtre par entité côté client (oto-backend#224) ----
# L'API Folk ignore filter[entity.id][eq] → on récupère tout et on filtre sur
# entity.id. On patche _paginate pour isoler la logique de filtrage.

_NOTES = [
    {"id": "nte_1", "entity": {"id": "per_A"}},
    {"id": "nte_2", "entity": {"id": "com_B"}},
    {"id": "nte_3", "entity": {"id": "per_A"}},
    {"id": "nte_4"},  # sans entité → jamais retenu par un filtre
]


def test_list_notes_filters_by_entity_client_side(c, monkeypatch):
    monkeypatch.setattr(c, "_paginate", lambda ep, params=None: list(_NOTES))
    out = c.list_notes(entity_id="per_A")
    assert [n["id"] for n in out] == ["nte_1", "nte_3"]


def test_list_notes_no_filter_returns_all(c, monkeypatch):
    monkeypatch.setattr(c, "_paginate", lambda ep, params=None: list(_NOTES))
    assert len(c.list_notes()) == 4


def test_list_reminders_filters_by_entity_client_side(c, monkeypatch):
    monkeypatch.setattr(c, "_paginate", lambda ep, params=None: list(_NOTES))
    out = c.list_reminders(entity_id="com_B")
    assert [r["id"] for r in out] == ["nte_2"]


# --- Traduction des filtres (signal #260 : lister les membres d'un groupe) ---

def test_relation_field_uses_in_id_not_like():
    """`groups` est une RELATION : Folk y refuse `like` (422 unrecognized_keys) et
    attend un objet portant l'id — vérifié live le 2026-08-03."""
    assert folk_client.filter_params({"groups": "grp_42"}) == {
        "filter[groups][in][id]": "grp_42"}
    assert folk_client.filter_params({"companies": "cpy_7"}) == {
        "filter[companies][in][id]": "cpy_7"}


def test_text_field_keeps_the_historical_like():
    assert folk_client.filter_params({"fullName": "Dupont"}) == {
        "filter[fullName][like]": "Dupont"}


def test_explicit_operator_is_honoured():
    assert folk_client.filter_params({"fullName": {"eq": "Dupont"}}) == {
        "filter[fullName][eq]": "Dupont"}
    assert folk_client.filter_params({"emails": {"not_empty": "true"}}) == {
        "filter[emails][not_empty]": "true"}


def test_explicit_relation_operator_still_nests_the_id():
    assert folk_client.filter_params({"groups": {"not_in": "grp_42"}}) == {
        "filter[groups][not_in][id]": "grp_42"}


def test_no_filters_no_params():
    assert folk_client.filter_params({}) == {} and folk_client.filter_params(None) == {}


def test_list_people_by_group_hits_the_right_query(monkeypatch):
    seen = {}

    def fake_request(method, url, headers=None, **kwargs):
        seen["params"] = kwargs.get("params")
        return _Resp({"data": {"items": [{"id": "p1"}], "pagination": {}}})

    monkeypatch.setattr(folk_client.requests, "request", fake_request)
    c = FolkClient(api_key="k", field_filter=_Stub())
    assert c.list_people(groups="grp_42") == [{"id": "p1"}]
    assert seen["params"]["filter[groups][in][id]"] == "grp_42"


# --- Webhooks --------------------------------------------------------------

def test_list_webhooks(c, calls):
    c.list_webhooks()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/webhooks"


def test_get_webhook(c, calls):
    c.get_webhook("wbk_1")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/webhooks/wbk_1"


def test_create_webhook(c, calls):
    events = [{"eventType": "person.created", "filter": {"groupId": "grp_1"}}]
    c.create_webhook("My app", "https://example.com/hook", events)
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/webhooks"
    assert calls[-1]["json"] == {
        "name": "My app",
        "targetUrl": "https://example.com/hook",
        "subscribedEvents": events,
    }


def test_update_webhook(c, calls):
    c.update_webhook("wbk_1", status="inactive")
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/webhooks/wbk_1"
    assert calls[-1]["json"] == {"status": "inactive"}


def test_delete_webhook(c, calls):
    c.delete_webhook("wbk_1")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/webhooks/wbk_1"


def test_webhook_event_types_are_the_documented_set():
    assert folk_client.WEBHOOK_EVENT_TYPES >= {
        "person.created", "company.updated", "object.deleted", "reminder.triggered",
    }


# --- Groups ------------------------------------------------------------------

def test_list_groups(c, calls):
    c.list_groups()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/groups"


def test_create_group(c, calls):
    c.create_group("Leads", "private")
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/groups"
    assert calls[-1]["json"] == {"name": "Leads", "visibility": "private"}


def test_update_group(c, calls):
    c.update_group("grp_1", visibility="public")
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1"
    assert calls[-1]["json"] == {"visibility": "public"}


def test_get_group_custom_fields_paginates(c, monkeypatch):
    """Le endpoint est paginé cursor côté API (jusqu'à 100/page) : un `_request`
    simple tronquait silencieusement au-delà de la première page."""
    pages = [
        {"data": {"items": [{"name": "Status"}],
                  "pagination": {"nextLink": f"{BASE}/groups/grp_1/custom-fields/person?cursor=abc"}}},
        {"data": {"items": [{"name": "Source"}], "pagination": {}}},
    ]

    def fake_request(method, url, headers=None, **kwargs):
        return _Resp(pages.pop(0))

    monkeypatch.setattr(folk_client.requests, "request", fake_request)
    result = c.get_group_custom_fields("grp_1")
    assert result == [{"name": "Status"}, {"name": "Source"}]


def test_get_group_custom_fields_quotes_entity_type(c, calls):
    c.get_group_custom_fields("grp_1", entity_type="deal type")
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/custom-fields/deal%20type"


def test_get_group_custom_field(c, calls):
    c.get_group_custom_field("grp_1", "person", "Deal Status")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/custom-fields/person/Deal%20Status"


def test_create_group_custom_field(c, calls):
    c.create_group_custom_field("grp_1", "person", type="textField", name="Status")
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/custom-fields/person"
    assert calls[-1]["json"] == {"type": "textField", "name": "Status"}


def test_update_group_custom_field(c, calls):
    c.update_group_custom_field(
        "grp_1", "person", "Status",
        addOptions=[{"label": "New", "color": "#5738ff"}],
    )
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/custom-fields/person/Status"
    assert calls[-1]["json"] == {"addOptions": [{"label": "New", "color": "#5738ff"}]}


def test_update_group_custom_field_unwraps_data_item(c, monkeypatch):
    """Seul endpoint Folk du client dont l'exemple de réponse officiel nest le
    champ sous `data.item` (+ `data.nextLink`) plutôt qu'à plat sous `data` —
    tous ses siblings (get/create custom field) rendent le champ à plat."""
    payload = {"data": {
        "item": {"name": "Status", "type": "singleSelect",
                  "options": [{"id": "gcfo_1", "label": "Active", "color": "#ffffff"}]},
        "nextLink": f"{BASE}/groups/grp_1/custom-fields/person/Status",
    }}

    def fake_request(method, url, headers=None, **kwargs):
        return _Resp(payload)

    monkeypatch.setattr(folk_client.requests, "request", fake_request)
    result = c.update_group_custom_field("grp_1", "person", "Status", name="Status")
    assert result == {"name": "Status", "type": "singleSelect",
                       "options": [{"id": "gcfo_1", "label": "Active", "color": "#ffffff"}]}


# --- Group members -------------------------------------------------------

def test_list_group_members(c, calls):
    c.list_group_members("grp_1")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/members"


def test_list_group_members_paginates(c, monkeypatch):
    pages = [
        {"data": {"items": [{"id": "usr_1"}],
                  "pagination": {"nextLink": f"{BASE}/groups/grp_1/members?cursor=abc"}}},
        {"data": {"items": [{"id": "usr_2"}], "pagination": {}}},
    ]

    def fake_request(method, url, headers=None, **kwargs):
        return _Resp(pages.pop(0))

    monkeypatch.setattr(folk_client.requests, "request", fake_request)
    assert c.list_group_members("grp_1") == [{"id": "usr_1"}, {"id": "usr_2"}]


def test_add_group_member(c, calls):
    c.add_group_member("grp_1", "usr_1", "admin")
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/members"
    assert calls[-1]["json"] == {"id": "usr_1", "role": "admin"}


def test_remove_group_member(c, calls):
    c.remove_group_member("grp_1", "usr_1")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/members/usr_1"


def test_update_group_member(c, calls):
    c.update_group_member("grp_1", "usr_1", "reader")
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/groups/grp_1/members/usr_1"
    assert calls[-1]["json"] == {"role": "reader"}
    assert "deal.created" not in folk_client.WEBHOOK_EVENT_TYPES  # deals = object.*


# --- Interactions : lecture (open beta chez Folk) --------------------------
# Le connecteur n'a longtemps exposé que la CRÉATION d'interaction, d'où la
# croyance qu'on ne pouvait pas relire ce qui s'était dit. Folk expose bien
# past/upcoming/get/patch/delete — ces tests verrouillent leur contrat HTTP.


def test_list_past_interactions(c, calls):
    c.list_past_interactions("per_A")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/interactions/past"
    assert calls[-1]["params"] == {"entity.id": "per_A"}


def test_list_upcoming_interactions(c, calls):
    c.list_upcoming_interactions("per_A")
    assert calls[-1]["url"] == f"{BASE}/interactions/upcoming"
    assert calls[-1]["params"] == {"entity.id": "per_A"}


def test_interaction_listing_sends_no_limit(c, calls):
    """`/interactions/past|upcoming` ne déclarent QUE `cursor` et `entity.id`.
    Live 2026-08-27 : un `limit` y est silencieusement IGNORÉ (page fixe de
    30), pas rejeté — on ne l'envoie donc pas plutôt que de promettre une
    taille de page qu'on ne contrôle pas."""
    c.list_past_interactions("per_A")
    assert "limit" not in calls[-1]["params"]


def test_other_listings_still_send_limit(c, calls):
    """…et l'opt-out ci-dessus ne doit pas déborder sur les autres endpoints."""
    c.list_people()
    assert calls[-1]["params"]["limit"] == 100


def test_get_interaction_requires_entity_in_query(c, calls):
    c.get_interaction("lit_1", "per_A")
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/interactions/lit_1"
    assert calls[-1]["params"] == {"entity.id": "per_A"}


def test_get_interaction_quotes_imported_id(c, calls):
    """Une interaction importée porte l'id synthétique de sa source (jusqu'à
    512 car.), pas un id Folk opaque de 40 — il doit être échappé."""
    c.get_interaction("gmail/thread+1 2", "per_A")
    assert calls[-1]["url"] == f"{BASE}/interactions/gmail%2Fthread%2B1%202"


def test_update_interaction_always_carries_its_entity(c, calls):
    """La spec OpenAPI liste `entity` dans le corps du PATCH sans le marquer
    requis — ça se lit « optionnel, omets-le pour garder l'entité actuelle ».
    Live 2026-08-27 : sans lui, Folk répond 422 `path: ['entity'], Required`.
    Le PATCH est scopé comme le get et le delete, par le corps au lieu de la
    query."""
    c.update_interaction("lit_1", "per_A", title="Café", activityType="coffee")
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/interactions/lit_1"
    assert calls[-1]["json"] == {"entity": {"id": "per_A"}, "title": "Café",
                                 "activityType": "coffee"}
    assert "params" not in calls[-1]  # scopé par le corps, pas par la query


def test_delete_interaction_requires_entity_in_query(c, calls):
    c.delete_interaction("lit_1", "per_A")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/interactions/lit_1"
    assert calls[-1]["params"] == {"entity.id": "per_A"}


# --- Tasks (successeur des rappels) ----------------------------------------


def test_list_tasks(c, calls):
    c.list_tasks({"entity": "per_A"})
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"] == f"{BASE}/tasks"
    assert calls[-1]["params"]["filter[entity][in]"] == "per_A"
    assert calls[-1]["params"]["limit"] == 100  # /tasks, lui, déclare `limit`


def test_list_tasks_filters_is_a_dict_not_a_splat(c, calls):
    """Un filtre nommé comme un paramètre de la méthode doit être VALIDÉ, pas
    avalé : `**filters` aurait posé `combinator=or` en silence."""
    with pytest.raises(ValueError, match="filtre de tâche inconnu"):
        c.list_tasks({"combinator": "or"})


def test_list_tasks_only_assigned_to_me_is_a_string_enum(c, calls):
    """Folk déclare `onlyAssignedToMe` en enum "true"/"false" : un bool Python
    partirait en "True" (majuscule), hors enum."""
    c.list_tasks(only_assigned_to_me=True)
    assert calls[-1]["params"]["onlyAssignedToMe"] == "true"
    c.list_tasks(only_assigned_to_me=False)
    assert calls[-1]["params"]["onlyAssignedToMe"] == "false"


def test_get_task(c, calls):
    c.get_task("tsk_1")
    assert calls[-1]["url"] == f"{BASE}/tasks/tsk_1"


def test_create_task(c, calls):
    c.create_task(entity_id="per_A", title="Relancer", due_at="2026-09-01",
                  due_time="09:00", description="**Avant** d'appeler",
                  recurrence_frequency="weekly", is_public=False)
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/tasks"
    assert calls[-1]["json"] == {
        "entity": {"id": "per_A"}, "title": "Relancer", "dueAt": "2026-09-01",
        "dueTime": "09:00", "description": "**Avant** d'appeler",
        "recurrenceFrequency": "weekly", "isPublic": False,
    }


def test_create_task_is_public_false_is_not_dropped(c, calls):
    """`isPublic=False` (tâche privée) est une valeur SIGNIFIANTE : un test de
    vérité l'aurait avalée comme un champ absent."""
    c.create_task(entity_id="per_A", title="T", due_at="2026-09-01",
                  is_public=False)
    assert calls[-1]["json"]["isPublic"] is False


def test_create_task_assigned_users_ids_and_emails(c, calls):
    c.create_task(entity_id="per_A", title="T", due_at="2026-09-01",
                  assigned_users=["usr_1", {"id": "usr_2"}])
    assert calls[-1]["json"]["assignedUsers"] == [{"id": "usr_1"}, {"id": "usr_2"}]
    c.create_task(entity_id="per_A", title="T", due_at="2026-09-01",
                  assigned_users=["a@b.co"])
    assert calls[-1]["json"]["assignedUsers"] == [{"email": "a@b.co"}]


def test_create_task_rejects_mixed_ids_and_emails(c, calls):
    with pytest.raises(ValueError, match="ids OU des emails"):
        c.create_task(entity_id="per_A", title="T", due_at="2026-09-01",
                      assigned_users=["usr_1", "a@b.co"])


def test_update_task(c, calls):
    c.update_task("tsk_1", title="Relancer (bis)", dueAt="2026-09-08")
    assert calls[-1]["method"] == "PATCH"
    assert calls[-1]["url"] == f"{BASE}/tasks/tsk_1"
    assert calls[-1]["json"] == {"title": "Relancer (bis)", "dueAt": "2026-09-08"}


def test_update_task_normalises_assigned_users(c, calls):
    c.update_task("tsk_1", assignedUsers=["a@b.co"])
    assert calls[-1]["json"]["assignedUsers"] == [{"email": "a@b.co"}]


def test_delete_task(c, calls):
    c.delete_task("tsk_1")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"] == f"{BASE}/tasks/tsk_1"


def test_mark_task_done_path_and_default_timestamp(c, calls):
    """Chemin pris sur l'OpenAPI (`mark-as-done`), PAS sur l'exemple de la page
    de migration qui écrit `mark-done`. `completedAt` est REQUIS."""
    c.mark_task_done("tsk_1")
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/tasks/tsk_1/mark-as-done"
    stamp = calls[-1]["json"]["completedAt"]
    assert stamp.endswith("Z") and "+00:00" not in stamp
    assert len(stamp) == len("2026-08-27T07:46:40.610Z")


def test_mark_task_done_explicit_timestamp(c, calls):
    c.mark_task_done("tsk_1", completed_at="2026-08-26T10:00:00.000Z")
    assert calls[-1]["json"] == {"completedAt": "2026-08-26T10:00:00.000Z"}


def test_mark_task_todo_has_no_body(c, calls):
    c.mark_task_todo("tsk_1")
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["url"] == f"{BASE}/tasks/tsk_1/mark-as-todo"
    assert "json" not in calls[-1]


# --- Filtres de tâches : allow-list, pas `filter_params` -------------------


def test_task_entity_filter_is_flat_not_nested_id():
    """Piège : `entity` est une relation mais s'écrit À PLAT sur /tasks, là où
    `groups` sur /people veut `[in][id]`. Réutiliser `filter_params` aurait
    produit `filter[entity][in][id]`."""
    assert folk_client.task_filter_params({"entity": "per_A"}) == {
        "filter[entity][in]": "per_A"}
    assert folk_client.filter_params({"groups": "grp_42"}) == {
        "filter[groups][in][id]": "grp_42"}


def test_task_valueless_operators_send_empty_string():
    assert folk_client.task_filter_params({"completedAt": {"empty": True}}) == {
        "filter[completedAt][empty]": ""}


def test_task_filter_rejects_unknown_field():
    with pytest.raises(ValueError, match="filtre de tâche inconnu"):
        folk_client.task_filter_params({"title": "Relancer"})


def test_task_filter_rejects_illegal_operator():
    """`like` n'existe sur AUCUN champ de tâche — c'est pourtant le défaut de
    `filter_params`, d'où le refus de le réutiliser ici."""
    with pytest.raises(ValueError, match="non supporté"):
        folk_client.task_filter_params({"dueAt": {"like": "2026"}})


def test_task_filter_demands_an_operator_when_ambiguous():
    with pytest.raises(ValueError, match="préciser l'opérateur"):
        folk_client.task_filter_params({"createdAt": "2026-01-01"})


def test_create_interaction_always_sends_a_datetime(c, calls):
    """`dateTime` est REQUIS par Folk (422 `path: ['dateTime'], Required`) alors
    que ce client le donnait pour facultatif : tout appel qui l'omettait
    échouait en 422 opaque. Vérifié en live le 2026-08-27. Défaut = maintenant,
    ce qui ne peut casser aucun appel qui marchait — ceux-là passaient déjà une
    date."""
    c.create_interaction(entity_id="per_A", type="coffee", title="Café")
    stamp = calls[-1]["json"]["dateTime"]
    assert stamp.endswith("Z") and len(stamp) == len("2026-08-27T09:15:07.746Z")
    c.create_interaction(entity_id="per_A", type="coffee", title="Café",
                         date_time="2026-08-20T09:00:00.000Z")
    assert calls[-1]["json"]["dateTime"] == "2026-08-20T09:00:00.000Z"


def test_paginate_stops_at_max_items(c, monkeypatch):
    """Sans borne, `/interactions/past` sur un contact actif tire des centaines
    d'interactions par pages de 30 (mesuré en live : >360 sans être au bout).
    Tout tirer pour n'en montrer que dix n'est pas une troncature, c'est une
    attente — `max_items` coupe les allers-retours, pas seulement la liste."""
    pages = []

    def fake_request(method, endpoint, params=None):
        pages.append(params.get("cursor"))
        return {"data": {"items": [{"id": f"i{len(pages)}-{k}"} for k in range(30)],
                         "pagination": {"nextLink": "https://x?cursor=c%d" % len(pages)}}}

    monkeypatch.setattr(c, "_request", fake_request)
    out = c._paginate("interactions/past", {}, limit=None, max_items=10)
    assert len(out) == 10
    assert len(pages) == 1  # UNE page suffisait, on n'a pas déroulé la suite


def test_paginate_without_max_items_still_drains(c, monkeypatch):
    calls = {"n": 0}

    def fake_request(method, endpoint, params=None):
        calls["n"] += 1
        last = calls["n"] == 3
        return {"data": {"items": [{"id": calls["n"]}],
                         "pagination": {} if last else {"nextLink": "https://x?cursor=c"}}}

    monkeypatch.setattr(c, "_request", fake_request)
    assert len(c._paginate("people", {})) == 3


def test_assigned_users_entry_with_both_id_and_email_is_refused():
    """La garde anti-mixte ne jouait qu'ENTRE entrées : un dict portant les
    DEUX clés partait tel quel vers Folk — le 422 opaque que le helper existe
    pour prévenir (relevé en revue de #61)."""
    with pytest.raises(ValueError, match="un seul"):
        folk_client._assigned_users_payload([{"id": "usr_1", "email": "a@b.co"}])


def test_request_sends_connect_read_timeout(c, monkeypatch):
    """Le transport folk n'avait AUCUN timeout : un host injoignable tenait la
    mono-loop du backend indéfiniment. Convention repo = (connexion, lecture)."""
    seen = {}

    def fake(method, url, headers=None, timeout=None, **kw):
        seen["timeout"] = timeout

        class R:
            status_code, content = 200, b""
            headers = {}
        return R()

    monkeypatch.setattr(folk_client.requests, "request", fake)
    c._request("GET", "people")
    assert seen["timeout"] == (10, 60)
