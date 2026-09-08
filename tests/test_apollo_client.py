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


def test_people_search_sends_the_location_filters_apollo_knows():
    """Isoler la filiale FRANÇAISE d'un domaine mondial (signaux #354/#356).

    Un domaine est partagé par tout le groupe — verifone.com rend 3282 profils tous
    pays confondus — et rien d'autre ne permet d'en garder les Français : chaque
    reveal à l'aveugle coûte un crédit. Apollo accepte `person_locations` et
    `organization_locations` sur `mixed_people/api_search` ; ils manquaient au
    wrapper. On vérifie le PAYLOAD, pas la réponse : une API permissive ignore un
    champ inconnu en silence, donc un mauvais nom ne lèverait jamais d'erreur."""
    client, calls, patcher = _client_and_calls({"people": []})
    try:
        client.search_people(domains=["verifone.com"], person_locations=["France"],
                             organization_locations=["Paris, France"])
        sent = calls[0]["json"]
        assert sent["person_locations"] == ["France"]
        assert sent["organization_locations"] == ["Paris, France"]
        assert sent["q_organization_domains_list"] == ["verifone.com"]
        assert "mixed_people/api_search" in calls[0]["url"]
    finally:
        patcher.stop()


def test_people_search_omits_location_filters_when_unused():
    """Un filtre non demandé ne part pas : un tableau vide n'est pas « partout »."""
    client, calls, patcher = _client_and_calls({"people": []})
    try:
        client.search_people(domains=["acme.com"])
        assert "person_locations" not in calls[0]["json"]
        assert "organization_locations" not in calls[0]["json"]
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


# ----------------------------------------------------------------------
# Sequences / enrollment — l'appel le plus à risque du client : il démarre
# une campagne automatisée vers des personnes réelles. Ces tests figent le
# verrou local (pas de boîte connectée explicite ⟹ aucun appel ne part),
# même logique que Lightfield `_check_from` (oto-core 97c53ce).
# ----------------------------------------------------------------------

def test_add_contacts_refuses_without_a_connected_mailbox():
    """Sans `send_email_from_email_account_id`, l'appel ne doit JAMAIS partir —
    c'est le seul rempart local contre un envoi depuis une boîte non voulue."""
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError) as e:
            client.add_contacts_to_sequence(
                sequence_id="seq1", send_email_from_email_account_id=None,
                contact_ids=["c1"])
        assert not calls
        assert "send_email_from_email_account_id" in str(e.value)
    finally:
        patcher.stop()


def test_add_contacts_refuses_without_contacts_or_labels():
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError):
            client.add_contacts_to_sequence(
                sequence_id="seq1", send_email_from_email_account_id="acc1")
        assert not calls
    finally:
        patcher.stop()


def test_add_contacts_sends_query_params_not_json_body():
    """`add_contact_ids` est documenté en paramètres de QUERY : un champ envoyé
    en `json=` serait ignoré en silence par Apollo (même défaut vécu que
    `organization_domain` sur `match_person`)."""
    client, calls, patcher = _client_and_calls({"contacts": []})
    try:
        client.add_contacts_to_sequence(
            sequence_id="seq1", send_email_from_email_account_id="acc1",
            contact_ids=["c1", "c2"])
        assert calls[0].get("json") is None
        assert calls[0]["params"]["contact_ids[]"] == ["c1", "c2"]
        assert calls[0]["params"]["send_email_from_email_account_id"] == "acc1"
        assert "emailer_campaigns/seq1/add_contact_ids" in calls[0]["url"]
    finally:
        patcher.stop()


def test_update_sequence_contact_status_validates_mode():
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError):
            client.update_sequence_contact_status(
                emailer_campaign_ids=["s1"], contact_ids=["c1"], mode="delete")
        assert not calls
    finally:
        patcher.stop()


def test_update_sequence_contact_status_sends_query_params():
    client, calls, patcher = _client_and_calls({"entity_progress_job": {}})
    try:
        client.update_sequence_contact_status(
            emailer_campaign_ids=["s1"], contact_ids=["c1"], mode="remove")
        assert calls[0].get("json") is None
        assert calls[0]["params"]["mode"] == "remove"
        assert calls[0]["params"]["emailer_campaign_ids[]"] == ["s1"]
    finally:
        patcher.stop()


def test_create_sequence_requires_a_send_schedule():
    """L'API refuse la création sans planning d'envoi — le verrou local évite
    un aller-retour pour rien."""
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError) as e:
            client.create_sequence(name="Q3 outbound", emailer_schedule_id="")
        assert not calls
        assert "emailer_schedule_id" in str(e.value)
    finally:
        patcher.stop()


def test_create_sequence_defaults_to_inactive():
    client, calls, patcher = _client_and_calls({"emailer_campaign": {}})
    try:
        client.create_sequence(name="Q3 outbound", emailer_schedule_id="sched1")
        assert calls[0]["json"]["active"] is False
        assert "sequences" in calls[0]["url"] and "emailer_campaigns" not in calls[0]["url"]
    finally:
        patcher.stop()


# ----------------------------------------------------------------------
# One-off emails — brouillon et envoi sur deux appels distincts
# ----------------------------------------------------------------------

def test_create_email_draft_requires_a_recipient_or_a_thread():
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError):
            client.create_email_draft(subject="hi")
        assert not calls
    finally:
        patcher.stop()


def test_create_email_draft_allows_a_thread_reply_without_contact_id():
    client, calls, patcher = _client_and_calls({"emailer_message": {}})
    try:
        client.create_email_draft(
            in_response_to_emailer_message_id="msg1", body_html="<p>hi</p>")
        assert calls[0]["json"]["in_response_to_emailer_message_id"] == "msg1"
        assert "contact_id" not in calls[0]["json"]
    finally:
        patcher.stop()


def test_send_email_now_hits_the_right_message_id():
    client, calls, patcher = _client_and_calls({"emailer_message": {}})
    try:
        client.send_email_now("msg1")
        assert calls[0]["url"].endswith("emailer_messages/msg1/send_now")
    finally:
        patcher.stop()


def test_get_email_content_caps_ids_at_ten():
    client, calls, patcher = _client_and_calls({"emailer_messages": []})
    try:
        client.get_email_content([f"m{i}" for i in range(15)])
        assert len(calls[0]["json"]["ids"]) == 10
    finally:
        patcher.stop()


# ----------------------------------------------------------------------
# Conversations
# ----------------------------------------------------------------------

def test_export_conversations_requires_all_three_fields():
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError):
            client.export_conversations(start_time="", end_time="2024-01-01", email="a@b.co")
        assert not calls
    finally:
        patcher.stop()


def test_get_conversation_hits_the_right_id():
    client, calls, patcher = _client_and_calls({"id": "conv1"})
    try:
        client.get_conversation("conv1")
        assert calls[0]["url"].endswith("conversations/conv1")
    finally:
        patcher.stop()


# ----------------------------------------------------------------------
# Read-only prerequisites (email accounts / schedules)
# ----------------------------------------------------------------------

def test_list_email_accounts_and_schedules_are_plain_gets():
    client, calls, patcher = _client_and_calls({"email_accounts": []})
    try:
        client.list_email_accounts()
        assert calls[-1]["url"].endswith("email_accounts")
    finally:
        patcher.stop()

    client, calls, patcher = _client_and_calls({"emailer_schedules": []})
    try:
        client.list_email_schedules()
        assert calls[-1]["url"].endswith("emailer_schedules")
    finally:
        patcher.stop()


# ----------------------------------------------------------------------
# Reveal téléphone / emails personnels — le contrat d'Apollo est en QUERY
# STRING, et une API permissive ignore en silence ce qu'elle ne reconnaît
# pas : un mauvais nom ou un mauvais type ne lèverait JAMAIS d'erreur, il
# rendrait juste 200 sans reveal. D'où des tests sur la forme envoyée.
# ----------------------------------------------------------------------

def test_reveal_flags_travel_in_the_query_string_never_in_the_body():
    client, calls, patcher = _client_and_calls()
    try:
        client.match_person(person_id="p1", reveal_phone_number=True,
                            webhook_url="https://hooks.acme.test/apollo")
        params = calls[0]["params"]
        assert params["reveal_phone_number"] == "true"
        assert params["webhook_url"] == "https://hooks.acme.test/apollo"
        # Le corps ne porte QUE l'identité : c'est la découpe que ce test fige.
        assert "reveal_phone_number" not in calls[0]["json"]
        assert "webhook_url" not in calls[0]["json"]
        assert calls[0]["json"]["id"] == "p1"
    finally:
        patcher.stop()


def test_reveal_booleans_are_serialised_lowercase_not_python():
    """`requests` écrirait `reveal_personal_emails=False` — Apollo attend `false`."""
    client, calls, patcher = _client_and_calls()
    try:
        client.match_person(person_id="p1", reveal_personal_emails=False)
        assert calls[0]["params"]["reveal_personal_emails"] == "false"
    finally:
        patcher.stop()


def test_no_reveal_asked_sends_no_query_string_at_all():
    client, calls, patcher = _client_and_calls()
    try:
        client.match_person(person_id="p1")
        assert calls[0].get("params") is None
    finally:
        patcher.stop()


@pytest.mark.parametrize("kwargs, attendu", [
    ({"reveal_phone_number": True}, "webhook_url"),
    ({"webhook_url": "https://hooks.acme.test/apollo"}, "reveal_phone_number"),
])
def test_the_phone_reveal_pair_is_refused_by_halves(kwargs, attendu):
    """Les deux moitiés se refusent AVANT la requête : sans webhook Apollo refuse
    de toute façon, et avec un webhook sans le drapeau il accepte puis n'envoie
    jamais rien — l'appelant attendrait un POST qui ne partira pas."""
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError) as e:
            client.match_person(person_id="p1", **kwargs)
        assert not calls, "aucune requête ne doit partir"
        assert attendu in str(e.value)
    finally:
        patcher.stop()


# ----------------------------------------------------------------------
# poll_webhook_result — un 404 qui veut dire « pas encore prêt »
# ----------------------------------------------------------------------

def test_pending_result_is_not_an_error():
    client, calls, patcher = _client_and_calls()
    patcher.stop()
    patcher = patch("oto.tools.apollo.client.requests.request",
                    side_effect=lambda m, u, **kw: (calls.append({"url": u, **kw}),
                                                    _Resp({"error_code": "result_pending",
                                                           "retry_after_seconds": 10},
                                                          ok=False, status=404))[1])
    patcher.start()
    try:
        out = client.poll_webhook_result("718432950164203900")
        assert out == {"done": False, "retry_after_seconds": 10}
    finally:
        patcher.stop()


def test_ready_result_comes_back_done():
    payload = {"request_id": 718432950164203900,
               "webhook_result": {"people": [{"phone_numbers": [{"sanitized_number": "+33..."}]}]}}
    client, calls, patcher = _client_and_calls(payload)
    try:
        out = client.poll_webhook_result("718432950164203900")
        assert out["done"] is True
        assert out["result"] == payload
        assert calls[0]["url"].endswith("webhook_result/718432950164203900")
    finally:
        patcher.stop()


def test_a_negative_request_id_survives_unchanged():
    """Entier SIGNÉ 64 bits : le signe fait partie de l'identifiant."""
    client, calls, patcher = _client_and_calls({"webhook_result": {}})
    try:
        client.poll_webhook_result("-718432950164203900")
        assert calls[0]["url"].endswith("webhook_result/-718432950164203900")
    finally:
        patcher.stop()


def test_a_request_id_that_is_not_an_integer_never_leaves():
    client, calls, patcher = _client_and_calls()
    try:
        with pytest.raises(ValueError):
            client.poll_webhook_result("pas-un-entier")
        assert not calls
    finally:
        patcher.stop()


def test_a_success_with_an_unreadable_body_is_a_failure_not_an_empty_result():
    """`requests.exceptions.JSONDecodeError` hérite de `ValueError` : un
    `except ValueError` nu rendrait un 200 en HTML (proxy, maintenance) comme un
    corps VIDE, que l'appelant lit « prêt, aucun numéro ». Le succès illisible
    doit LEVER — la divergence muette est le mode de panne cher."""
    from oto.tools.apollo.client import ApolloError

    class _Html:
        ok, status_code, text = True, 200, "<html>maintenance</html>"

        def json(self):
            raise ValueError("no json")

    client = ApolloClient(api_key="k")
    patcher = patch("oto.tools.apollo.client.requests.request",
                    side_effect=lambda m, u, **kw: _Html())
    patcher.start()
    try:
        with pytest.raises(ApolloError) as e:
            client.poll_webhook_result("718432950164203900")
        assert "non-JSON" in str(e.value)
    finally:
        patcher.stop()


def test_a_tolerated_404_without_a_body_is_a_named_refusal_not_a_pending():
    """Un 404 toléré mais sans `result_pending` ne doit pas passer pour « pas
    encore prêt » — sinon l'appelant sonde à vie un id qui n'existe pas."""
    from oto.tools.apollo.client import ApolloError

    class _Vide:
        ok, status_code, text = False, 404, ""

        def json(self):
            raise ValueError("no json")

    client = ApolloClient(api_key="k")
    patcher = patch("oto.tools.apollo.client.requests.request",
                    side_effect=lambda m, u, **kw: _Vide())
    patcher.start()
    try:
        with pytest.raises(ApolloError):
            client.poll_webhook_result("718432950164203900")
    finally:
        patcher.stop()
