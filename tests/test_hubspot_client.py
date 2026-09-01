"""Contrat du client HubSpot : la lecture groupée et la reprise sur 429.

Mocke `requests.Session.request` — verbes, chemins et corps relevés dans la
référence éditeur — et neutralise `time.sleep` : une suite qui dort vingt
secondes apprend à tout le monde à la sauter.

Les deux invariants que ce fichier existe pour verrouiller :

1. **`batch_read_objects` ne rétrécit pas une page en silence.** Un id supprimé,
   archivé ou d'un autre portail est simplement absent des `results` et HubSpot
   répond 207 — donc rien ne lève. Sans `missing_ids`, une page de 250 membres
   reviendrait à 247 lignes sans que personne ne l'apprenne.
2. **La reprise sur 429 est bornée EN DURÉE, et ne s'étend à rien d'autre.** Un
   5xx n'est jamais rejoué (un POST dupliquerait un enregistrement CRM), un quota
   journalier n'est jamais attendu, et le cumul des attentes reste sous plafond
   même si quelqu'un relève le nombre de tentatives.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.hubspot import client as hs


class _Resp:
    def __init__(self, status_code: int = 200, body=None, headers=None):
        self.status_code = status_code
        self._body = {} if body is None else body
        self.content = b"x"
        self.text = str(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture()
def http(monkeypatch):
    """Relève chaque requête sortante et chaque attente ; ne dort jamais.

    `responses` = la file des réponses à servir ; la dernière est resservie une
    fois épuisée (le cas « tout en 429 »).
    """
    seen = {"calls": [], "sleeps": [], "responses": [_Resp(200)]}

    def fake_request(self, method, url, **kwargs):
        seen["calls"].append({"method": method, "url": url, **kwargs})
        queue = seen["responses"]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(hs.requests.Session, "request", fake_request)
    monkeypatch.setattr(hs.time, "sleep", lambda s: seen["sleeps"].append(s))
    return seen


def _client():
    return hs.HubSpotClient(api_key="pat-test")


def _record(rid: str, **props):
    return {"id": rid, "properties": props}


# --- Lecture groupée : le contrat -------------------------------------------

def test_batch_read_hits_the_documented_endpoint(http):
    http["responses"] = [_Resp(200, {"results": [_record("1"), _record("2")]})]
    _client().batch_read_objects("contacts", ["1", "2"], properties=["email"])

    (call,) = http["calls"]
    assert call["method"] == "POST"
    assert call["url"].endswith("/crm/v3/objects/contacts/batch/read")
    assert call["json"] == {
        "inputs": [{"id": "1"}, {"id": "2"}],
        "properties": ["email"],
    }


def test_a_batch_read_is_split_at_a_hundred(http):
    ids = [str(i) for i in range(250)]
    http["responses"] = [
        _Resp(200, {"results": [_record(i) for i in ids[0:100]]}),
        _Resp(200, {"results": [_record(i) for i in ids[100:200]]}),
        _Resp(200, {"results": [_record(i) for i in ids[200:250]]}),
    ]
    out = _client().batch_read_objects("companies", ids)

    assert len(http["calls"]) == 3
    assert [len(c["json"]["inputs"]) for c in http["calls"]] == [100, 100, 50]
    assert [r["id"] for r in out["results"]] == ids   # concaténés dans l'ordre des pages
    assert out["missing_ids"] == []


def test_an_id_that_does_not_exist_comes_back_named(http):
    """Le 207 de HubSpot ne lève pas : sans ce diff, la page rétrécit en silence."""
    http["responses"] = [_Resp(207, {"results": [_record("1"), _record("2")],
                                     "numErrors": 1})]
    out = _client().batch_read_objects("contacts", ["1", "2", "3"])

    assert len(out["results"]) == 2
    assert out["missing_ids"] == ["3"]


def test_an_empty_input_does_not_call_hubspot(http):
    out = _client().batch_read_objects("contacts", [])

    assert http["calls"] == []
    assert out == {"results": [], "missing_ids": []}


def test_ids_are_deduplicated_in_the_order_given(http):
    http["responses"] = [_Resp(200, {"results": [_record("2"), _record("1")]})]
    _client().batch_read_objects("contacts", ["2", "1", "2", None, ""])

    (call,) = http["calls"]
    assert call["json"]["inputs"] == [{"id": "2"}, {"id": "1"}]


def test_reading_by_email_carries_the_id_property_and_can_still_name_the_misses(http):
    http["responses"] = [_Resp(200, {"results": [
        _record("11", email="a@x.fr", firstname="A"),
    ]})]
    out = _client().batch_read_objects(
        "contacts", ["a@x.fr", "b@x.fr"],
        properties=["firstname"], id_property="email")

    (call,) = http["calls"]
    assert call["json"]["idProperty"] == "email"
    # `email` est ajoutée d'office : sans elle en réponse, le diff serait incalculable.
    assert call["json"]["properties"] == ["firstname", "email"]
    assert out["missing_ids"] == ["b@x.fr"]


def test_a_key_hubspot_normalised_is_not_reported_missing(http):
    """HubSpot rend l'email en minuscules ; le signaler absent serait un faux."""
    http["responses"] = [_Resp(200, {"results": [_record("11", email="a@x.fr")]})]
    out = _client().batch_read_objects(
        "contacts", ["A@X.fr"], id_property="email")

    assert out["missing_ids"] == []


def test_a_failing_chunk_raises_rather_than_serving_half_a_page(http):
    http["responses"] = [
        _Resp(200, {"results": [_record(str(i)) for i in range(100)]}),
        _Resp(400, {"category": "VALIDATION_ERROR",
                    "message": "Property \"tropo\" does not exist",
                    "errorType": "PROPERTY_DOESNT_EXIST"}),
    ]
    with pytest.raises(UpstreamHTTPError) as ei:
        _client().batch_read_objects(
            "contacts", [str(i) for i in range(150)], properties=["tropo"])

    assert ei.value.status_code == 400
    assert "tropo" in str(ei.value.body)


# --- 429 : la reprise --------------------------------------------------------

def test_a_429_then_a_200_is_served_transparently(http):
    http["responses"] = [_Resp(429, {"policyName": "SECONDLY"}),
                         _Resp(200, {"id": "1"})]
    out = _client().get_object("contacts", "1")

    assert len(http["calls"]) == 2
    assert len(http["sleeps"]) == 1
    assert out == {"id": "1"}


def test_retry_after_is_honoured(http):
    http["responses"] = [_Resp(429, {}, {"Retry-After": "3"}), _Resp(200)]
    _client().get_object("contacts", "1")

    assert http["sleeps"] == [3.0]


def test_an_unparsable_retry_after_falls_back_to_the_written_step(http):
    http["responses"] = [
        _Resp(429, {}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _Resp(200),
    ]
    _client().get_object("contacts", "1")

    assert http["sleeps"] == [float(2 ** 1)]


def test_a_single_wait_is_capped(http):
    http["responses"] = [_Resp(429, {}, {"Retry-After": "600"}), _Resp(200)]
    _client().get_object("contacts", "1")

    assert http["sleeps"] == [hs.RATE_LIMIT_MAX_SLEEP]


def test_the_total_wait_is_bounded_not_just_the_attempt_count(http):
    """Relever RATE_LIMIT_ATTEMPTS ne doit pas pouvoir rallonger un handler."""
    http["responses"] = [_Resp(429, {}, {"Retry-After": "10"})]
    with pytest.raises(UpstreamHTTPError):
        _client().get_object("contacts", "1")

    assert sum(http["sleeps"]) <= hs.RATE_LIMIT_MAX_TOTAL_SLEEP


def test_an_exhausted_retry_is_a_named_refusal_not_a_none(http):
    http["responses"] = [_Resp(429, {"policyName": "TEN_SECONDLY_ROLLING"})]
    with pytest.raises(UpstreamHTTPError) as ei:
        _client().get_object("contacts", "1")

    assert len(http["calls"]) == hs.RATE_LIMIT_ATTEMPTS
    assert ei.value.status_code == 429
    assert ei.value.service == "hubspot"


def test_a_daily_quota_is_not_hammered(http):
    """Attendre dix secondes contre un quota journalier immobilise un worker pour rien."""
    http["responses"] = [_Resp(429, {"policyName": "DAILY"},
                               {"Retry-After": "10"})]
    with pytest.raises(UpstreamHTTPError) as ei:
        _client().get_object("contacts", "1")

    assert len(http["calls"]) == 1
    assert http["sleeps"] == []
    assert ei.value.status_code == 429


def test_a_429_without_a_policy_is_still_retried(http):
    http["responses"] = [_Resp(429, {"message": "rafale"}), _Resp(200)]
    _client().get_object("contacts", "1")

    assert len(http["calls"]) == 2


# --- Aucun dégât collatéral sur les appels existants -------------------------

def test_a_500_is_never_retried(http):
    """Rejouer un POST sur un 502 dupliquerait un enregistrement CRM : on ne sait
    pas si l'écriture est passée. Seul le 429 dit « rien n'a été fait »."""
    http["responses"] = [_Resp(500, {"message": "boom"})]
    with pytest.raises(UpstreamHTTPError) as ei:
        _client().create_object("contacts", {"email": "a@x.fr"})

    assert len(http["calls"]) == 1
    assert http["sleeps"] == []
    assert ei.value.status_code == 500


def test_the_nominal_path_still_makes_exactly_one_call_and_never_sleeps(http):
    http["responses"] = [_Resp(200, {"id": "1"})]
    assert _client().get_object("contacts", "1") == {"id": "1"}

    assert len(http["calls"]) == 1
    assert http["sleeps"] == []


def test_shipped_membership_names_are_the_ones_this_client_exposes():
    """Les noms LIVRÉS (oto-core#72). Une branche antérieure disait
    `get_list_members` : la remettre par un merge distrait casserait le backend
    au bump du pin, pas ici."""
    for shipped in ("get_list_memberships", "add_list_memberships",
                    "remove_list_memberships"):
        assert hasattr(hs.HubSpotClient, shipped), shipped
    for stale in ("get_list_members", "add_list_members", "remove_list_members"):
        assert not hasattr(hs.HubSpotClient, stale), stale
