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
