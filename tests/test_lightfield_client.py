"""Contrat du client Lightfield (v1, Bearer + en-tête de version, CRM par workspace).

Mocke `requests.Session.request` : verbes et chemins relevés dans la référence
éditeur, en-têtes, bornes de pagination, idempotence, re-tentatives, et la lecture
des scopes — dont l'inversion « liste vide = accès complet », qui est le piège que
ce fichier existe surtout pour verrouiller.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.lightfield import client as lf


class _Resp:
    def __init__(self, status_code: int, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"data": []}
        self.content = b"x"
        self.text = str(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    """Enregistre le dernier appel et compte les tentatives."""
    seen = {"calls": []}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        seen["calls"].append((method, url))
        return _Resp(200)

    monkeypatch.setattr(lf.requests.Session, "request", fake_request)
    monkeypatch.setattr(lf.time, "sleep", lambda _s: None)
    return seen


def _client():
    return lf.LightfieldClient(api_key="sk_lf_test")


# --- en-têtes ---------------------------------------------------------------

def test_auth_is_a_bearer_header_and_the_key_never_reaches_the_query():
    c = _client()
    assert c.session.headers["Authorization"] == "Bearer sk_lf_test"


def test_the_version_header_is_sent_on_every_request_and_is_pinned_once():
    c = _client()
    assert c.session.headers["Lightfield-Version"] == lf.DEFAULT_API_VERSION
    assert lf.LightfieldClient(api_key="k", api_version="2027-01-01") \
        .session.headers["Lightfield-Version"] == "2027-01-01"


# --- la carte des endpoints -------------------------------------------------
# Verbes et chemins relevés page par page dans la référence éditeur le 18/08/2026.
# Ce tableau EST le relevé : s'il diverge, c'est le client qui a bougé.

_ENDPOINTS = [
    ("validate", (), "GET", "/v1/auth/validate"),
    ("list_accounts", (), "GET", "/v1/accounts"),
    ("get_account", ("a1",), "GET", "/v1/accounts/a1"),
    ("create_account", ({"fields": {}},), "POST", "/v1/accounts"),
    ("update_account", ("a1", {"fields": {}}), "POST", "/v1/accounts/a1"),
    ("account_definitions", (), "GET", "/v1/accounts/definitions"),
    ("list_contacts", (), "GET", "/v1/contacts"),
    ("get_contact", ("c1",), "GET", "/v1/contacts/c1"),
    ("create_contact", ({"fields": {}},), "POST", "/v1/contacts"),
    ("update_contact", ("c1", {"fields": {}}), "POST", "/v1/contacts/c1"),
    ("contact_definitions", (), "GET", "/v1/contacts/definitions"),
    ("list_opportunities", (), "GET", "/v1/opportunities"),
    ("get_opportunity", ("o1",), "GET", "/v1/opportunities/o1"),
    ("create_opportunity", ({"fields": {}},), "POST", "/v1/opportunities"),
    ("update_opportunity", ("o1", {"fields": {}}), "POST", "/v1/opportunities/o1"),
    ("opportunity_definitions", (), "GET", "/v1/opportunities/definitions"),
    ("create_note", ({"fields": {}},), "POST", "/v1/notes"),
    ("note_definitions", (), "GET", "/v1/notes/definitions"),
    ("create_task", ({"fields": {}},), "POST", "/v1/tasks"),
    ("update_task", ("t1", {"fields": {}}), "POST", "/v1/tasks/t1"),
    ("task_definitions", (), "GET", "/v1/tasks/definitions"),
    ("list_lists", (), "GET", "/v1/lists"),
    ("get_list", ("l1",), "GET", "/v1/lists/l1"),
    ("create_list", ({"name": "x"},), "POST", "/v1/lists"),
    ("update_list", ("l1", {"name": "x"}), "POST", "/v1/lists/l1"),
    ("list_accounts_of_list", ("l1",), "GET", "/v1/lists/l1/accounts"),
    ("list_contacts_of_list", ("l1",), "GET", "/v1/lists/l1/contacts"),
    ("list_opportunities_of_list", ("l1",), "GET", "/v1/lists/l1/opportunities"),
    ("list_meetings", (), "GET", "/v1/meetings"),
    ("get_meeting", ("m1",), "GET", "/v1/meetings/m1"),
    ("meeting_definitions", (), "GET", "/v1/meetings/definitions"),
    ("list_emails", (), "GET", "/v1/emails"),
    ("get_email", ("e1",), "GET", "/v1/emails/e1"),
    ("list_object_types", (), "GET", "/v1/objects"),
    ("object_definitions", ("deal",), "GET", "/v1/objects/deal/definitions"),
]


@pytest.mark.parametrize("name,args,verb,path", _ENDPOINTS)
def test_each_method_hits_its_documented_verb_and_path(capture, name, args, verb, path):
    getattr(_client(), name)(*args)
    assert capture["method"] == verb
    assert capture["url"] == f"https://api.lightfield.app{path}"
    assert capture["kwargs"]["timeout"] == lf._HTTP_TIMEOUT


def test_updates_are_POST_because_the_api_has_no_put_or_patch(capture):
    _client().update_account("a1", {"fields": {"name": "Acme"}})
    assert capture["method"] == "POST"


def test_the_client_never_exposes_a_send_email_path():
    """L'envoi est hors périmètre par DÉCISION : qu'aucune méthode ne le rouvre."""
    assert not [m for m in dir(lf.LightfieldClient) if "send" in m.lower()]


# --- pagination -------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, 26, 100, -1])
def test_a_limit_outside_the_api_bounds_is_refused_locally(bad):
    """L'API plafonne à 25 : le dire ici NOMME la borne au lieu de laisser un 400."""
    with pytest.raises(ValueError, match="25"):
        _client().list_accounts(limit=bad)


@pytest.mark.parametrize("ok", [1, 25])
def test_a_limit_inside_the_bounds_reaches_the_query(capture, ok):
    _client().list_accounts(limit=ok, offset=50)
    assert capture["kwargs"]["params"] == {"limit": ok, "offset": 50}


def test_none_params_are_dropped_and_booleans_are_serialised_for_the_server(capture):
    _client().list_accounts(limit=None, offset=None, archived=True, starred=False)
    assert capture["kwargs"]["params"] == {"archived": "true", "starred": "false"}


def test_field_filters_pass_through_as_query_params(capture):
    _client().list_contacts(**{"primary-email": "a@b.test"})
    assert capture["kwargs"]["params"]["primary-email"] == "a@b.test"


# --- idempotence ------------------------------------------------------------

def test_writes_carry_an_idempotency_key_even_when_the_caller_gives_none(capture):
    _client().create_account({"fields": {}})
    assert capture["kwargs"]["headers"]["Idempotency-Key"].startswith("oto-")


def test_the_caller_key_wins_so_a_replayed_turn_can_deduplicate(capture):
    _client().create_account({"fields": {}}, idempotency_key="turn-42")
    assert capture["kwargs"]["headers"]["Idempotency-Key"] == "turn-42"


def test_an_over_long_idempotency_key_is_refused():
    with pytest.raises(ValueError, match="255"):
        _client().create_account({"fields": {}}, idempotency_key="x" * 256)


def test_reads_send_no_idempotency_header(capture):
    _client().list_accounts()
    assert capture["kwargs"]["headers"] is None


def test_the_caller_payload_is_copied_not_mutated(capture):
    payload = {"fields": {"name": "Acme"}}
    _client().create_account(payload)
    assert capture["kwargs"]["json"] == payload
    assert capture["kwargs"]["json"] is not payload


def test_a_non_dict_payload_is_refused_before_any_call(capture):
    with pytest.raises(ValueError):
        _client().create_account(["not", "a", "dict"])
    assert capture["calls"] == []


# --- re-tentatives ----------------------------------------------------------

def _flaky(monkeypatch, statuses, headers=None):
    """Rend `statuses` dans l'ordre, puis 200. Compte les tentatives."""
    calls = {"n": 0}
    seq = list(statuses)

    def fake_request(self, method, url, **kwargs):
        calls["n"] += 1
        if seq:
            return _Resp(seq.pop(0), headers=headers)
        return _Resp(200)

    monkeypatch.setattr(lf.requests.Session, "request", fake_request)
    return calls


def test_a_429_is_retried_and_then_succeeds(monkeypatch):
    calls = _flaky(monkeypatch, [429])
    monkeypatch.setattr(lf.time, "sleep", lambda _s: None)
    assert _client().list_accounts() == {"data": []}
    assert calls["n"] == 2


def test_retry_after_is_honoured_rather_than_our_own_backoff(monkeypatch):
    _flaky(monkeypatch, [429], headers={"Retry-After": "7"})
    slept = []
    monkeypatch.setattr(lf.time, "sleep", lambda s: slept.append(s))
    _client().list_accounts()
    assert slept == [7.0]


def test_a_validation_4xx_is_never_retried(monkeypatch):
    calls = _flaky(monkeypatch, [400, 400, 400])
    monkeypatch.setattr(lf.time, "sleep", lambda _s: None)
    with pytest.raises(UpstreamHTTPError):
        _client().list_accounts()
    assert calls["n"] == 1


def test_a_post_without_an_idempotency_key_is_NOT_retried(monkeypatch):
    """Le chemin de rattrapage lui-même : sans clé, une réponse perdue en vol ferait
    créer deux fois l'enregistrement — on préfère l'erreur au doublon."""
    calls = _flaky(monkeypatch, [503])
    monkeypatch.setattr(lf.time, "sleep", lambda _s: None)
    with pytest.raises(UpstreamHTTPError):
        _client()._request("POST", "/v1/accounts", json={})
    assert calls["n"] == 1


def test_retries_are_bounded(monkeypatch):
    calls = _flaky(monkeypatch, [503] * 10)
    monkeypatch.setattr(lf.time, "sleep", lambda _s: None)
    with pytest.raises(UpstreamHTTPError):
        _client().list_accounts()
    assert calls["n"] == lf._MAX_ATTEMPTS


# --- erreurs ----------------------------------------------------------------

def test_an_upstream_error_keeps_its_status_and_body(monkeypatch):
    body = {"type": "bad_request", "code": "unknown_field", "param": "nom_du_champ"}

    def fake_request(self, method, url, **kwargs):
        return _Resp(400, body)

    monkeypatch.setattr(lf.requests.Session, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as e:
        _client().create_account({"fields": {"nom_du_champ": "x"}})
    assert e.value.status_code == 400
    assert e.value.body["code"] == "unknown_field"
    assert e.value.service == "lightfield"


# --- scopes : l'inversion ---------------------------------------------------

def test_an_EMPTY_scope_list_means_FULL_access_not_none():
    """Le piège central. Lue naïvement (`scope in resp["scopes"]`), la clé la PLUS
    puissante passerait pour une clé sans droits, et on refuserait le connecteur au
    client qui a le mieux configuré sa clé."""
    full = {"active": True, "scopes": [], "subjectType": "workspace"}
    assert lf.scope_granted(full, "accounts:read") is True
    assert lf.scope_granted(full, "opportunities:update") is True


def test_a_named_scope_list_grants_only_what_it_names():
    limited = {"active": True, "scopes": ["accounts:read", "contacts:read"]}
    assert lf.scope_granted(limited, "accounts:read") is True
    assert lf.scope_granted(limited, "accounts:update") is False


def test_a_missing_or_malformed_validate_response_grants_nothing_by_name():
    assert lf.scope_granted({"active": True}, "accounts:read") is True   # absent = full
    assert lf.scope_granted(None, "accounts:read") is False
    assert lf.scope_granted("nope", "accounts:read") is False
