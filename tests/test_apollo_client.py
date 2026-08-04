"""Apollo — le reveal d'une personne coûte un crédit à chaque appel, y compris
quand il ne rend rien. Ces tests figent ce qui a coûté ~12 crédits sur 15 en une
session (feedbacks oto #347-350) : un identifiant trop faible ne doit plus PARTIR.
"""
from unittest.mock import patch

import pytest

from oto.tools.apollo.client import ApolloClient


class _Resp:
    def __init__(self, payload, ok=True, status=200):
        self._payload, self.ok, self.status_code = payload, ok, status
        self.text = ""

    def json(self):
        return self._payload


def _client_and_calls(payload=None):
    """(client, calls) — `calls` collecte les kwargs de chaque requête sortante."""
    calls = []
    client = ApolloClient(api_key="k")
    patcher = patch("oto.tools.apollo.client.requests.request",
                    side_effect=lambda m, u, **kw: (calls.append({"url": u, **kw}),
                                                    _Resp(payload or {"person": {}}))[1])
    patcher.start()
    return client, calls, patcher


@pytest.mark.parametrize("kwargs, why", [
    ({"first_name": "Ninon", "org_name": "Faure", "domain": "faure.fr"},
     "prénom + société : l'appel qui fabriquait des fiches fantômes"),
    ({"name": "Ninon"}, "un seul mot ne fait pas un nom complet"),
    ({"first_name": "Ninon"}, "prénom seul"),
    ({}, "aucun identifiant"),
])
def test_weak_identifier_is_refused_before_spending_a_credit(kwargs, why):
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError) as e:
            client.match_person(**kwargs)
        assert not calls, f"aucune requête ne doit partir ({why})"
        assert "person_id" in str(e.value)   # le message dit quoi passer
    finally:
        patcher.stop()


@pytest.mark.parametrize("kwargs", [
    {"person_id": "abc"},                                  # id de search_people
    {"email": "a@b.co"},
    {"linkedin_url": "https://linkedin.com/in/x"},
    {"first_name": "Jan", "last_name": "Ellsberger"},      # nom COMPLET
    {"name": "Jan Ellsberger"},
])
def test_strong_identifier_goes_through(kwargs):
    client, calls, patcher = _client_and_calls()
    try:
        client.match_person(**kwargs)
        assert len(calls) == 1
    finally:
        patcher.stop()


def test_person_id_maps_to_the_api_field_id():
    """`search_people` rend un `id` ; `people/match` l'accepte sous le nom `id`.
    Sans ce mapping, révéler un résultat de search est impossible (les noms de
    famille y sont obfusqués)."""
    client, calls, patcher = _client_and_calls()
    try:
        client.match_person(person_id="55f0b1e2")
        assert calls[0]["json"] == {"id": "55f0b1e2"}
    finally:
        patcher.stop()


def test_domain_uses_the_name_the_api_knows():
    """`organization_domain` n'existe pas côté Apollo : envoyé, il était ignoré en
    SILENCE — le domaine ne participait pas au match. Le champ est `domain`."""
    client, calls, patcher = _client_and_calls()
    try:
        client.match_person(first_name="Jan", last_name="E", domain="etsi.org")
        assert calls[0]["json"]["domain"] == "etsi.org"
        assert "organization_domain" not in calls[0]["json"]
    finally:
        patcher.stop()


def test_empty_record_is_flagged_as_a_stub():
    """Apollo répond par une fiche NEUVE et vide plutôt que par « pas de match » —
    et facture. L'appelant doit pouvoir distinguer ça d'un enrichissement."""
    ghost = {"person": {"id": "new", "first_name": "Ninon", "last_name": None,
                        "title": None, "email": None, "linkedin_url": None}}
    client, calls, patcher = _client_and_calls(ghost)
    try:
        assert client.match_person(person_id="x")["person"]["_stub"] is True
    finally:
        patcher.stop()


def test_real_record_is_not_flagged():
    real = {"person": {"id": "p1", "first_name": "Jan", "last_name": "Ellsberger",
                       "title": "CTO", "email": "jan@etsi.org",
                       "linkedin_url": "https://linkedin.com/in/jan"}}
    client, calls, patcher = _client_and_calls(real)
    try:
        assert "_stub" not in client.match_person(person_id="x")["person"]
    finally:
        patcher.stop()


def test_bulk_enrich_refuses_more_than_the_api_accepts():
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError) as e:
            client.bulk_enrich_organizations([f"c{i}.com" for i in range(11)])
        assert "10" in str(e.value) and not calls
    finally:
        patcher.stop()
