"""Contrat du client Stripe (v1, Bearer, corps FORM-ENCODÉ).

Mocke `requests.Session.request` : vérifie méthode/URL/query/corps, le header
Bearer, l'encodage bracket de Stripe (le point où une intégration écrite de
mémoire se trompe), le refus d'une clé publiable, le `Request-Id` attaché à
l'erreur, et l'ABSENCE des méthodes qui déplacent de l'argent.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.stripe import client as st


class _Resp:
    def __init__(self, status_code: int, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.content = b"x"
        self.text = str(body)
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"object": "list", "data": [], "has_more": False})

    monkeypatch.setattr(st.requests.Session, "request", fake_request)
    return seen


def _client(**kw):
    return st.StripeClient(api_key="rk_test_123", **kw)


def _query(capture) -> dict:
    """La query string rendue en dict — les paires sont une LISTE de tuples
    (l'ordre compte pour les listes répétées, pas pour ces assertions)."""
    return dict(capture["kwargs"]["params"] or [])


def _body(capture) -> dict:
    return dict(capture["kwargs"]["data"] or [])


# --- auth ---------------------------------------------------------------------

def test_auth_header_is_bearer():
    assert _client().session.headers["Authorization"] == "Bearer rk_test_123"


def test_publishable_key_is_refused_at_construction():
    """Une `pk_` ne peut lire aucune donnée de compte : la refuser ici change un
    401 incompréhensible au premier appel en un message de configuration."""
    with pytest.raises(ValueError, match="PUBLIABLE"):
        st.StripeClient(api_key="pk_test_123")


def test_secret_and_restricted_keys_are_accepted():
    assert st.StripeClient(api_key="sk_test_1").session.headers["Authorization"]
    assert st.StripeClient(api_key="rk_live_1").session.headers["Authorization"]


def test_api_version_is_not_pinned_by_default():
    """Un pin qui diverge du compte change les formes de réponse en silence —
    le défaut est donc la version du compte."""
    assert "Stripe-Version" not in _client().session.headers
    assert _client(api_version="2026-03-31.basil").session.headers[
        "Stripe-Version"] == "2026-03-31.basil"


def test_stripe_account_header_only_when_asked():
    assert "Stripe-Account" not in _client().session.headers
    assert _client(stripe_account="acct_1").session.headers["Stripe-Account"] == "acct_1"


# --- encodage bracket (le cœur du contrat Stripe) -----------------------------

def test_nested_dict_uses_bracket_syntax(capture):
    _client().list_invoices(created={"gte": 1704067200, "lt": 1706745600})
    q = _query(capture)
    assert q["created[gte]"] == 1704067200
    assert q["created[lt]"] == 1706745600


def test_scalar_list_repeats_the_bracket_key(capture):
    _client().get_customer("cus_1", expand=["subscriptions", "tax"])
    pairs = capture["kwargs"]["params"]
    assert ("expand[]", "subscriptions") in pairs
    assert ("expand[]", "tax") in pairs


def test_list_of_objects_is_indexed(capture):
    _client().create_payment_link([{"price": "price_1", "quantity": 2}])
    b = _body(capture)
    assert b["line_items[0][price]"] == "price_1"
    assert b["line_items[0][quantity]"] == 2


def test_booleans_are_lowercase_strings(capture):
    """Python enverrait "True"/"False" ; Stripe lit toute chaîne non vide comme
    vraie, donc `active=False` filtrerait sur les ACTIFS — silencieusement."""
    _client().list_products(active=False)
    assert _query(capture)["active"] == "false"
    _client().list_products(active=True)
    assert _query(capture)["active"] == "true"


def test_none_values_are_dropped(capture):
    _client().list_charges(customer=None, limit=100)
    q = _query(capture)
    assert "customer" not in q
    assert q == {"limit": 100}


def test_empty_params_send_no_query(capture):
    _client().balance()
    assert capture["kwargs"]["params"] is None
    assert capture["kwargs"]["data"] is None


def test_timeout_is_always_bounded(capture):
    _client().balance()
    assert capture["kwargs"]["timeout"] == st._HTTP_TIMEOUT


# --- routage ------------------------------------------------------------------

def test_balance_probe_is_a_bare_get(capture):
    _client().balance()
    assert capture["method"] == "GET"
    assert capture["url"] == "https://api.stripe.com/v1/balance"


def test_writes_are_form_encoded_posts(capture):
    _client().create_customer(email="a@b.co", metadata={"plan": "pro"})
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.stripe.com/v1/customers"
    b = _body(capture)
    assert b["email"] == "a@b.co"
    assert b["metadata[plan]"] == "pro"
    # form-encodé, JAMAIS json= : Stripe rejette un corps JSON.
    assert "json" not in capture["kwargs"]


def test_idempotency_key_is_sent_only_when_given(capture):
    _client()._post("/v1/customers", {"email": "a@b.co"})
    assert capture["kwargs"]["headers"] is None
    _client()._post("/v1/customers", {"email": "a@b.co"}, idempotency_key="tool-call-42")
    assert capture["kwargs"]["headers"] == {"Idempotency-Key": "tool-call-42"}


def test_subscription_items_are_scoped_to_their_subscription(capture):
    _client().list_subscription_items("sub_1", limit=50)
    assert capture["url"] == "https://api.stripe.com/v1/subscription_items"
    assert _query(capture) == {"subscription": "sub_1", "limit": 50}


# --- coupons & promotion codes -------------------------------------------------

def test_create_coupon_posts_to_v1_coupons(capture):
    _client().create_coupon(percent_off=20, duration="once", name="LAUNCH20")
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.stripe.com/v1/coupons"
    b = _body(capture)
    assert b["percent_off"] == 20
    assert b["duration"] == "once"
    assert b["name"] == "LAUNCH20"


def test_update_coupon_posts_to_the_coupon_id(capture):
    _client().update_coupon("cp_1", name="renamed")
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.stripe.com/v1/coupons/cp_1"
    assert _body(capture)["name"] == "renamed"


def test_create_promotion_code_links_a_coupon_to_a_typed_code(capture):
    _client().create_promotion_code(coupon="cp_1", code="LAUNCH20",
                                     restrictions={"first_time_transaction": True})
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.stripe.com/v1/promotion_codes"
    b = _body(capture)
    assert b["coupon"] == "cp_1"
    assert b["code"] == "LAUNCH20"
    # syntaxe bracket, comme tout dict imbriqué (cf. _encode)
    assert b["restrictions[first_time_transaction]"] == "true"


def test_update_promotion_code_can_deactivate_without_deleting(capture):
    """Aucune suppression n'existe dans ce client (cf. le docstring de tête du
    module) — révoquer un code passe par `active=False`."""
    _client().update_promotion_code("promo_1", active=False)
    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.stripe.com/v1/promotion_codes/promo_1"
    assert _body(capture)["active"] == "false"


# --- search : sept ressources, pas une de plus ---------------------------------

def test_search_builds_the_resource_path(capture):
    _client().search("customers", 'email~"acme.com"', limit=100)
    assert capture["url"] == "https://api.stripe.com/v1/customers/search"
    q = _query(capture)
    assert q["query"] == 'email~"acme.com"'
    assert q["limit"] == 100


def test_search_refuses_a_resource_stripe_cannot_search(capture):
    """`/v1/refunds/search` n'existe pas : le dire ici évite un 404 amont
    qu'on prendrait pour un id introuvable."""
    with pytest.raises(ValueError, match="ne sait chercher que dans"):
        _client().search("refunds", "amount>100")


def test_the_seven_searchable_resources_are_exactly_those_documented():
    assert set(st.SEARCHABLE) == {
        "charges", "customers", "invoices", "payment_intents",
        "subscriptions", "prices", "products"}


# --- erreurs ------------------------------------------------------------------

def test_upstream_error_is_typed_and_carries_the_request_id(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return _Resp(402, {"error": {"type": "card_error", "code": "card_declined"}},
                     headers={"Request-Id": "req_abc123"})

    monkeypatch.setattr(st.requests.Session, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as e:
        _client().get_charge("ch_1")
    assert e.value.status_code == 402
    assert e.value.service == "stripe"
    # Le Request-Id est ce que le support Stripe réclame en premier.
    assert e.value.body["request_id"] == "req_abc123"
    assert e.value.body["error"]["code"] == "card_declined"


def test_error_without_request_id_still_raises(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return _Resp(401, {"error": {"type": "invalid_request_error"}})

    monkeypatch.setattr(st.requests.Session, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as e:
        _client().balance()
    assert e.value.status_code == 401
    assert "request_id" not in e.value.body


# --- la propriété de sûreté : rien ne déplace d'argent ------------------------

def test_no_money_moving_method_exists_on_the_client():
    """Ce n'est PAS seulement « non exposé au niveau tool » : la méthode n'existe
    pas, donc aucun tool futur ne peut y être branché en une ligne sans repasser
    par une PR ici. Écart assumé avec la doctrine `AhrefsClient` (couverture
    complète côté client) — un `delete` Ahrefs perd une liste de mots-clés, un
    `refund` Stripe perd de l'argent, sans retour."""
    for absent in (
        "create_refund", "refund", "refund_charge",
        "create_payout", "cancel_payout", "reverse_payout",
        "close_dispute", "submit_dispute_evidence", "update_dispute",
        "cancel_subscription", "delete_subscription", "create_subscription",
        "update_subscription", "resume_subscription",
        "finalize_invoice", "pay_invoice", "void_invoice", "delete_invoice",
        "delete_customer",
        "create_charge", "capture_charge",
        "create_payment_intent", "confirm_payment_intent",
        "capture_payment_intent", "cancel_payment_intent",
        "create_credit_note", "void_credit_note",
        "create_checkout_session", "expire_checkout_session",
        "create_webhook_endpoint", "update_webhook_endpoint",
        "delete_webhook_endpoint",
        "create_tax_transaction", "create_meter_event",
    ):
        assert not hasattr(st.StripeClient, absent), (
            f"StripeClient.{absent} existe — une méthode qui déplace de l'argent "
            "ou casse une intégration ne s'ajoute pas sans décision explicite.")


def test_the_read_surface_that_answers_the_flagship_questions_exists():
    for present in (
        # « combien avons-nous facturé / encaissé »
        "list_invoices", "get_invoice", "get_invoice_lines",
        "balance", "list_balance_transactions", "list_payouts",
        # « qui est ce client »
        "list_customers", "get_customer", "list_customer_payment_methods",
        # « qui résilie »
        "list_subscriptions", "get_subscription", "list_subscription_items",
        # « pourquoi ce paiement a échoué »
        "get_payment_intent", "get_charge", "list_charges",
        "list_refunds", "list_disputes", "get_dispute",
        # catalogue + activité
        "list_products", "list_prices", "list_events",
        "list_checkout_sessions", "list_payment_links",
        "search",
    ):
        assert callable(getattr(st.StripeClient, present, None)), f"{present} manquant"
