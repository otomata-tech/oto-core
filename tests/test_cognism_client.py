"""CognismClient — verrouille le contrat HTTP (verbe, URL, query params,
shape du body) pour chaque endpoint, la validation des enums fermés
(seniority/jobFunctions/managementLevel/account.types/funding/hiring
department/sort_fields/accountSearchOptions — un typo doit lever AVANT
l'appel réseau, pas répondre 200 avec une page vide) et les contraintes
XOR (redeem: ids OU redeemIds ; enrich: au moins un champ d'identité).

Corps de requête tirés de la collection Postman officielle Cognism
(developers.cognism.com) pour garantir que le shaping colle exactement à ce
que l'API attend, pas seulement à notre lecture de la doc.

Mocke `requests.request` : contrat HTTP uniquement, sans réseau ni clé réelle.
"""
import json

import pytest

from oto.tools.cognism import client as cognism_client
from oto.tools.cognism.client import CognismClient
from oto.tools.cognism.enums import validate_enum_filters

BASE = "https://app.cognism.com/api/search"


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(response=self)

    def json(self):
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def fake_request(method, url, headers=None, json=None, params=None, timeout=None):
        captured.append({
            "method": method, "url": url, "headers": headers,
            "json": json, "params": params,
        })
        return _Resp({"totalResults": 0, "results": []})

    monkeypatch.setattr(cognism_client.requests, "request", fake_request)
    return captured


@pytest.fixture
def c():
    return CognismClient(api_key="test-key")


# --- auth / headers -----------------------------------------------------------

def test_bearer_auth_header(c, calls):
    c.search_contacts({"firstName": "Stjepan"})
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["headers"]["Content-Type"] == "application/json"


# --- search_contacts : contrat HTTP + shape (fixture Postman) -----------------

def test_search_contacts_endpoint_and_query_params(c, calls):
    c.search_contacts({"firstName": "Stjepan"}, index_size=25, last_returned_key=None)
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/contact/search"
    assert call["params"] == {"indexSize": 25, "lastReturnedKey": ""}


def test_search_contacts_body_matches_postman_fixture(c, calls):
    # Corps exact de l'exemple "Search Contacts" de la collection Postman Cognism.
    filters = {
        "firstName": "Stjepan",
        "lastName": "Buljat",
        "jobTitles": ["Chief Innovation Officer"],
        "excludeJobTitles": ["CEO"],
        "regions": ["EMEA"],
        "mobilePhoneNumbers": {"highPlus": True},
        "emailQuality": {"highPlus": True},
        "account": {
            "names": ["Cognism"],
            "officePhoneNumbers": {"medium": True},
        },
    }
    c.search_contacts(filters)
    assert calls[0]["json"] == filters


def test_search_contacts_pagination_cursor_passthrough(c, calls):
    c.search_contacts({}, last_returned_key="1714687499208_~34d633b7")
    assert calls[0]["params"]["lastReturnedKey"] == "1714687499208_~34d633b7"


# --- search_accounts -----------------------------------------------------------

def test_search_accounts_endpoint_default_index_size(c, calls):
    c.search_accounts({"names": ["Cognism"], "domains": ["cognism.com"]})
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/account/search"
    assert call["params"]["indexSize"] == 100  # défaut Cognism = 100 pour accounts


def test_search_accounts_body_matches_postman_fixture(c, calls):
    filters = {
        "names": ["Cognism"],
        "domains": ["cognism.com"],
        "accountSearchOptions": {
            "match_exact_account_name": True,
            "match_exact_domain": False,
            "filter_domain": "exists",
            "events_operator": "AND",
        },
    }
    c.search_accounts(filters)
    assert calls[0]["json"] == filters


# --- redeem : XOR ids/redeemIds, query param mergePhonesAndLocations -----------

def test_redeem_contacts_by_ids(c, calls):
    c.redeem_contacts(ids=["34d633b7-41ea-3ac7-a280-431d71fd77eb"])
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/contact/redeem"
    assert call["json"] == {"ids": ["34d633b7-41ea-3ac7-a280-431d71fd77eb"]}
    assert call["params"] == {"mergePhonesAndLocations": "false"}


def test_redeem_contacts_by_redeem_ids(c, calls):
    rid = "YmVkOWMxZDItYjgwNi0zN2I2LTk2MzItNzVlZjk5MWE0ODdh"
    c.redeem_contacts(redeem_ids=[rid], merge_phones_and_locations=True)
    call = calls[0]
    assert call["json"] == {"redeemIds": [rid]}
    assert call["params"] == {"mergePhonesAndLocations": "true"}


def test_redeem_contacts_requires_exactly_one_of_ids_or_redeem_ids(c):
    with pytest.raises(ValueError):
        c.redeem_contacts()
    with pytest.raises(ValueError):
        c.redeem_contacts(ids=["a"], redeem_ids=["b"])


def test_redeem_accounts_requires_exactly_one_of_ids_or_redeem_ids(c):
    with pytest.raises(ValueError):
        c.redeem_accounts()
    with pytest.raises(ValueError):
        c.redeem_accounts(ids=["a"], redeem_ids=["b"])


# --- enrich : au moins un champ d'identité --------------------------------------

def test_enrich_contact_body_matches_postman_fixture(c, calls):
    c.enrich_contact(email="stjepan.buljat@cognism.com")
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/contact/enrich"
    assert call["json"] == {"email": "stjepan.buljat@cognism.com"}


def test_enrich_contact_requires_at_least_one_field(c):
    with pytest.raises(ValueError):
        c.enrich_contact()


def test_enrich_account_requires_at_least_one_field(c):
    with pytest.raises(ValueError):
        c.enrich_account()


def test_enrich_account_body_matches_postman_fixture(c, calls):
    # Corps exact de l'exemple "Enrich Account" de la collection Postman Cognism.
    c.enrich_account(website="www.cognism.com")
    assert calls[0]["json"] == {"website": "www.cognism.com"}


def test_enrich_account_maps_all_named_fields(c, calls):
    c.enrich_account(
        name="Cognism", country="United Kingdom", city="London",
        linkedin_url="https://linkedin.com/company/cognism",
        anchor_fields=["domain"], min_match_score=45,
    )
    assert calls[0]["json"] == {
        "name": "Cognism", "country": "United Kingdom", "city": "London",
        "linkedinUrl": "https://linkedin.com/company/cognism",
        "anchorFields": ["domain"], "minMatchScore": 45,
    }


def test_enrich_contact_maps_all_named_fields(c, calls):
    c.enrich_contact(
        first_name="Stjepan", last_name="Buljat", job_title="CIO",
        account_name="Cognism", account_website="cognism.com",
        anchor_fields=["email"], min_match_score=40,
    )
    assert calls[0]["json"] == {
        "firstName": "Stjepan", "lastName": "Buljat", "jobTitle": "CIO",
        "accountName": "Cognism", "accountWebsite": "cognism.com",
        "anchorFields": ["email"], "minMatchScore": 40,
    }


# --- entitlement / verify_key ---------------------------------------------------

def test_contact_entitlement_endpoint(c, calls):
    c.contact_entitlement()
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == f"{BASE}/entitlement/contactEntitlementSubscription"


def test_account_entitlement_endpoint(c, calls):
    c.account_entitlement()
    assert calls[0]["url"] == f"{BASE}/entitlement/accountEntitlementSubscription"


def test_verify_key_raises_on_401(c, monkeypatch):
    def fake_request(method, url, headers=None, json=None, params=None, timeout=None):
        return _Resp(None, status_code=401)
    monkeypatch.setattr(cognism_client.requests, "request", fake_request)
    with pytest.raises(Exception):
        c.verify_key()


# --- filter_values : dispatch + technologies-only pagination params -----------

def test_filter_values_plain_endpoint_no_params(c, calls):
    c.filter_values("seniority")
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == f"{BASE}/filter/seniority"
    assert calls[0]["params"] is None


def test_filter_values_technologies_has_search_pagination(c, calls):
    c.filter_values("technologies", search="salesforce", index_size=20)
    assert calls[0]["url"] == f"{BASE}/filter/technologiesSearch"
    assert calls[0]["params"] == {
        "search": "salesforce", "indexSize": 20, "lastReturnedKey": "",
    }


def test_filter_values_unknown_kind_raises(c):
    with pytest.raises(ValueError):
        c.filter_values("not-a-real-kind")


# --- validate_enum_filters : chaque champ fermé, valeur bonne + mauvaise ------

@pytest.mark.parametrize("filters", [
    {"seniority": ["Manager"]},
    {"jobFunctions": ["Sales", "Marketing"]},
    {"managementLevel": ["CxO"]},
    {"account": {"types": ["Public Company"]}},
    {"account": {"fundingEvent": {"fundingType": ["seed"]}}},
    {"account": {"fundingEvent": {"series": ["B"]}}},
    {"account": {"hiringEvent": {"department": ["R&D"]}}},
    {"searchOptions": {"sort_fields": ["ProfileScoreDESC"]}},
    {"account": {"accountSearchOptions": {"filter_email": "exists"}}},
    {"account": {"accountSearchOptions": {"location_type": "HQ"}}},
    {"account": {"accountSearchOptions": {"events_operator": "AND"}}},
])
def test_validate_enum_filters_accepts_valid_values(filters):
    validate_enum_filters(filters)  # ne doit pas lever


@pytest.mark.parametrize("filters,bad_field", [
    ({"seniority": ["Founder"]}, "seniority"),          # pas dans la liste Cognism
    ({"jobFunctions": ["Engineering"]}, "jobFunctions"),  # ressemble à Technology mais faux
    ({"managementLevel": ["C-Level"]}, "managementLevel"),
    ({"account": {"types": ["Startup"]}}, "account.types"),
    ({"account": {"fundingEvent": {"fundingType": ["ipo"]}}}, "fundingType"),
    ({"account": {"fundingEvent": {"series": ["Z"]}}}, "series"),
    ({"account": {"hiringEvent": {"department": ["engineering"]}}}, "department"),
    ({"searchOptions": {"sort_fields": ["RelevanceDESC"]}}, "sort_fields"),
    ({"account": {"accountSearchOptions": {"filter_email": "yes"}}}, "filter_email"),
    ({"account": {"accountSearchOptions": {"location_type": "OFFICE"}}}, "location_type"),
    ({"account": {"accountSearchOptions": {"events_operator": "XOR"}}}, "events_operator"),
])
def test_validate_enum_filters_rejects_invalid_values(filters, bad_field):
    with pytest.raises(ValueError, match=bad_field.split(".")[-1]):
        validate_enum_filters(filters)


def test_validate_enum_filters_ignores_absent_fields():
    validate_enum_filters({"firstName": "Stjepan"})  # aucun champ fermé présent
    validate_enum_filters(None)
    validate_enum_filters({})


# --- scope contact vs account : mêmes noms de champ, profondeur différente ----
# (account.types sous search_contacts, types à la racine sous search_accounts)

def test_scope_contact_validates_account_fields_under_account_prefix():
    validate_enum_filters({"account": {"types": ["Public Company"]}}, scope="contact")
    with pytest.raises(ValueError):
        validate_enum_filters({"account": {"types": ["Startup"]}}, scope="contact")


def test_scope_account_validates_same_fields_at_root_not_under_prefix():
    validate_enum_filters({"types": ["Public Company"]}, scope="account")
    with pytest.raises(ValueError):
        validate_enum_filters({"types": ["Startup"]}, scope="account")


def test_scope_account_ignores_account_prefixed_field_it_wont_receive():
    # Un body search_accounts n'a jamais de clé "account" à sa racine — si on
    # lui en passe une par erreur, le validator scope="account" ne doit PAS la
    # valider comme si c'était le format search_contacts (chemin différent).
    validate_enum_filters({"account": {"types": ["Startup"]}}, scope="account")


def test_scope_contact_ignores_root_level_types_it_wont_receive():
    # Symétriquement, un `types` à la racine n'existe pas dans le format
    # search_contacts (il est toujours sous account.*) — scope="contact" ne
    # doit pas le voir.
    validate_enum_filters({"types": ["Startup"]}, scope="contact")


def test_search_contacts_rejects_bad_enum_before_network_call(c, calls):
    with pytest.raises(ValueError):
        c.search_contacts({"seniority": ["Founder"]})
    assert calls == []  # jamais parti sur le réseau


def test_search_accounts_rejects_bad_enum_before_network_call(c, calls):
    with pytest.raises(ValueError):
        c.search_accounts({"types": ["Startup"]})
    assert calls == []
