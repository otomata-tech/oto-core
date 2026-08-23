"""Stripe API client (v1, https://api.stripe.com) — billing, subscriptions,
invoices, payments, catalog.

Bearer token (`Authorization: Bearer <key>`). Stripe documents HTTP Basic with
the key as username and an empty password (`-u sk_live_xxx:`); the docs state
bearer is equivalent server-side, and it is what every other client here uses.

**Requests are FORM-ENCODED** (`application/x-www-form-urlencoded`), not JSON —
this is the single most common way a Stripe integration written from memory is
wrong. Nested values use Stripe's bracket syntax (`created[gte]=…`,
`expand[]=customer`, `line_items[0][price]=…`), produced by `_encode` below and
applied identically to query strings and bodies. Responses are JSON.

**No money-moving method exists in this client, deliberately.** Refunds, payouts
(create/cancel/reverse), dispute close and evidence submission, subscription
cancellation, invoice finalize/pay/void, customer delete and charge capture are
all absent — not merely unexposed at the tool layer. This departs from the
`AhrefsClient` doctrine ("implement the full API, let the tool layer choose"),
and the reason is that Ahrefs' destructive endpoints delete a keyword list while
these move real money irreversibly. A method that exists is a method a future
tool can be pointed at in one line; a method that does not exist forces the
decision back through review. `oto_mcp/tools/stripe.py` therefore cannot refund,
and neither can anything built on this client without a deliberate PR here.

Two consequences of Stripe's own design that callers must know:

- **`limit` defaults to 10 on every list endpoint** and silently truncates. It is
  NOT defaulted here (a client that invents a limit hides the truncation one
  layer deeper); the tool layer sets it explicitly. Max is 100.
- **Only seven resources support `/search`** — charges, customers, invoices,
  payment_intents, subscriptions, prices, products. Search is eventually
  consistent (a just-created object may not appear for ~a minute) and paginates
  by an opaque `next_page` token, NOT by the `starting_after` cursor the list
  endpoints use. The two paginators are not interchangeable.

Every response carries a `Request-Id` header; `_request` attaches it to the
`UpstreamHTTPError` body on failure so a support escalation can quote it.

Verified against docs.stripe.com (api/authentication, api/pagination, api/errors,
search, keys) on 2026-08-22. **Not yet live-tested** — no key was available at
authoring time; this docstring is to be updated with the live-test date and
findings once one is, exactly as `GranolaClient` and `AhrefsClient` record theirs.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import requests

from ...config import require_secret
from ..common import UpstreamHTTPError

_HTTP_TIMEOUT = (10, 60)  # (connect, read) — never an unbounded wait
_BASE_URL = "https://api.stripe.com"

# Les sept ressources qui portent `/v1/<resource>/search` (docs.stripe.com/search).
# Toute autre valeur est une erreur d'appelant, pas un 404 amont à décoder.
SEARCHABLE = ("charges", "customers", "invoices", "payment_intents",
              "subscriptions", "prices", "products")


def _encode(value: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    """Aplatit une valeur Python en paires clé/valeur au format bracket de Stripe.

    `{"created": {"gte": 1}}` → `[("created[gte]", 1)]` ;
    `{"expand": ["customer"]}` → `[("expand[]", "customer")]` ;
    `{"line_items": [{"price": "p"}]}` → `[("line_items[0][price]", "p")]`.

    Les `None` sont ABANDONNÉS (un kwarg omis ne doit pas devenir la chaîne
    "None"), et les booléens rendus "true"/"false" — Python enverrait "True",
    que Stripe lit comme une chaîne non vide, donc comme vrai : `active=False`
    filtrerait alors sur les objets ACTIFS, silencieusement.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        out: List[Tuple[str, Any]] = []
        for k, v in value.items():
            out.extend(_encode(v, f"{prefix}[{k}]" if prefix else str(k)))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for i, v in enumerate(value):
            # Un scalaire va dans `clé[]` (répété), un objet dans `clé[i][champ]`.
            out.extend(_encode(v, f"{prefix}[{i}]" if isinstance(v, (dict, list, tuple))
                               else f"{prefix}[]"))
        return out
    if isinstance(value, bool):
        return [(prefix, "true" if value else "false")]
    return [(prefix, value)]


class StripeClient:
    """Stripe API v1 client (https://api.stripe.com), Bearer auth, form-encoded."""

    BASE_URL = _BASE_URL

    def __init__(self, api_key: Optional[str] = None, *,
                 api_version: Optional[str] = None,
                 stripe_account: Optional[str] = None):
        """
        Args:
            api_key: Stripe API key (or env var `STRIPE_API_KEY`). A
                **restricted key** (`rk_test_…` / `rk_live_…`) is the right
                credential for this connector — it grants per-resource read
                permissions, so an operator can hand over reads without handing
                over the ability to move money. A secret key (`sk_…`) also
                works. A **publishable key (`pk_…`) is refused here**: it is
                client-side only and cannot read a single customer or invoice,
                so accepting it would defer a config mistake into a confusing
                401 at the first real call.
            api_version: optional `Stripe-Version` override. Omitted = the
                account's own default version, which is what the operator sees
                in their dashboard. Pinning is deliberately NOT the default:
                a pin that disagrees with the account silently changes response
                shapes (Stripe moved usage-based billing twice this way).
            stripe_account: optional `Stripe-Account` header (Connect) — act on
                a connected account rather than the key's own account.
        """
        self.api_key = api_key or require_secret("STRIPE_API_KEY")
        if self.api_key.startswith("pk_"):
            raise ValueError(
                "Clé Stripe PUBLIABLE (`pk_…`) : elle est destinée au navigateur et ne "
                "peut lire aucune donnée de compte. Utilise une clé restreinte "
                "(`rk_…`, recommandée) ou secrète (`sk_…`) — Stripe Dashboard → "
                "Developers → API keys.")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"
        if api_version:
            self.session.headers["Stripe-Version"] = api_version
        if stripe_account:
            self.session.headers["Stripe-Account"] = stripe_account

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None,
                 idempotency_key: Optional[str] = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}",
            params=_encode(params or {}) or None,
            data=_encode(body or {}) or None,
            headers=headers, timeout=_HTTP_TIMEOUT)
        if resp.status_code >= 400:
            # Le `Request-Id` est ce que le support Stripe demande en premier ;
            # sans lui l'utilisateur doit retrouver l'appel à la main dans les logs.
            request_id = resp.headers.get("Request-Id")
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            if request_id and isinstance(payload, dict):
                payload = {**payload, "request_id": request_id}
            raise UpstreamHTTPError(resp.status_code, payload, service="stripe")
        return resp.json() if resp.content else {}

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Dict[str, Any], *,
              idempotency_key: Optional[str] = None) -> Any:
        return self._request("POST", path, body=body, idempotency_key=idempotency_key)

    # ================================================================
    # Search — les SEPT ressources qui la portent
    # ================================================================

    def search(self, resource: str, query: str, **params: Any) -> Any:
        """GET /v1/{resource}/search — Stripe Query Language sur l'une des sept
        ressources cherchables.

        Args:
            resource: l'une de `SEARCHABLE` — charges, customers, invoices,
                payment_intents, subscriptions, prices, products.
            query: la requête SQL-like de Stripe, ex.
                `status:"active" AND created>1704067200`,
                `email~"acme.com"`, `metadata["order_id"]:"6735"`.
                Opérateurs : `:` (égal), `~` (contient / commence par selon le
                champ), `>` `<` `>=` `<=` sur les numériques et dates, `AND`/`OR`,
                `-` (négation). Les champs cherchables diffèrent PAR ressource.
            **params: `limit` (1-100, défaut 10), `page` (jeton opaque
                `next_page` rendu par l'appel précédent — PAS `starting_after`),
                `expand`.

        Note: la recherche est en cohérence à terme — un objet créé à l'instant
        peut n'apparaître qu'au bout d'une minute. Pour lire un objet qu'on vient
        d'écrire, passer par son id.
        """
        if resource not in SEARCHABLE:
            raise ValueError(
                f"Stripe ne sait chercher que dans {', '.join(SEARCHABLE)} "
                f"(reçu {resource!r}). Les autres ressources se listent avec des "
                "filtres, pas avec une requête.")
        return self._get(f"/v1/{resource}/search", query=query, **params)

    # ================================================================
    # Solde, écritures de solde, virements — « combien avons-nous »
    # ================================================================

    def balance(self) -> Any:
        """GET /v1/balance — solde `available` et `pending` par devise. Sans
        paramètre, une seule petite réponse : c'est aussi la sonde d'auth."""
        return self._get("/v1/balance")

    def list_balance_transactions(self, **params: Any) -> Any:
        """GET /v1/balance_transactions — chaque mouvement DU solde, avec son
        `fee` et son `net`. C'est la bonne source pour « combien avons-nous
        réellement encaissé » (le facturé, lui, vit sur les factures).

        Args:
            **params: `created` (dict `gte`/`gt`/`lte`/`lt`, timestamps Unix),
                `currency`, `payout` (le contenu d'un virement précis), `source`,
                `type`, `limit`, `starting_after`, `ending_before`, `expand`.
        """
        return self._get("/v1/balance_transactions", **params)

    def get_balance_transaction(self, txn_id: str) -> Any:
        """GET /v1/balance_transactions/{id}."""
        return self._get(f"/v1/balance_transactions/{txn_id}")

    def list_payouts(self, **params: Any) -> Any:
        """GET /v1/payouts — les virements vers le compte bancaire : « quand
        arrive le prochain dépôt, et de combien ».

        Args:
            **params: `status`, `arrival_date` (dict d'opérateurs), `created`,
                `destination`, `limit`, `starting_after`, `ending_before`.
        """
        return self._get("/v1/payouts", **params)

    def get_payout(self, payout_id: str) -> Any:
        """GET /v1/payouts/{id}. Le DÉTAIL d'un virement se lit avec
        `list_balance_transactions(payout=payout_id)`."""
        return self._get(f"/v1/payouts/{payout_id}")

    # ================================================================
    # Clients
    # ================================================================

    def list_customers(self, **params: Any) -> Any:
        """GET /v1/customers — filtres `email` (égalité EXACTE), `created`,
        `limit`, `starting_after`, `ending_before`, `expand`. Pour une
        correspondance partielle, `search("customers", 'email~"acme.com"')`."""
        return self._get("/v1/customers", **params)

    def get_customer(self, customer_id: str, **params: Any) -> Any:
        """GET /v1/customers/{id}. `expand` accepte notamment
        `subscriptions`, `default_source`, `tax`."""
        return self._get(f"/v1/customers/{customer_id}", **params)

    def create_customer(self, **body: Any) -> Any:
        """POST /v1/customers — `email`, `name`, `phone`, `description`,
        `address`, `metadata`, `preferred_locales`. Aucun champ n'est requis
        par Stripe, mais un client sans `email` est introuvable ensuite."""
        return self._post("/v1/customers", body)

    def update_customer(self, customer_id: str, **body: Any) -> Any:
        """POST /v1/customers/{id} — mêmes champs que la création. Seuls les
        champs fournis changent."""
        return self._post(f"/v1/customers/{customer_id}", body)

    def list_customer_payment_methods(self, customer_id: str, **params: Any) -> Any:
        """GET /v1/customers/{id}/payment_methods — les moyens de paiement
        enregistrés. `card.exp_month`/`exp_year` expliquent la plupart des
        impayés involontaires (carte expirée) ; une liste VIDE explique le reste.

        Args:
            **params: `type` (card, sepa_debit…), `limit`.
        """
        return self._get(f"/v1/customers/{customer_id}/payment_methods", **params)

    # ================================================================
    # Abonnements
    # ================================================================

    def list_subscriptions(self, **params: Any) -> Any:
        """GET /v1/subscriptions.

        Args:
            **params: `customer`, `price`, `status` (incomplete,
                incomplete_expired, trialing, active, past_due, canceled,
                unpaid, paused, ended, ou `all` — SANS `status`, Stripe ne rend
                que les abonnements actifs et en essai, ce qui fait
                silencieusement disparaître les résiliés), `collection_method`,
                `created`, `current_period_end`, `current_period_start`,
                `limit`, `starting_after`, `ending_before`, `expand`.
        """
        return self._get("/v1/subscriptions", **params)

    def get_subscription(self, subscription_id: str, **params: Any) -> Any:
        """GET /v1/subscriptions/{id}."""
        return self._get(f"/v1/subscriptions/{subscription_id}", **params)

    def list_subscription_items(self, subscription: str, **params: Any) -> Any:
        """GET /v1/subscription_items?subscription=… — les lignes d'un
        abonnement, où vivent réellement le prix et la quantité (sièges)."""
        return self._get("/v1/subscription_items", subscription=subscription, **params)

    # ================================================================
    # Factures
    # ================================================================

    def list_invoices(self, **params: Any) -> Any:
        """GET /v1/invoices.

        Args:
            **params: `customer`, `subscription`, `status` (draft, open, paid,
                uncollectible, void), `collection_method`, `created`, `due_date`,
                `limit`, `starting_after`, `ending_before`, `expand`.
        """
        return self._get("/v1/invoices", **params)

    def get_invoice(self, invoice_id: str, **params: Any) -> Any:
        """GET /v1/invoices/{id}."""
        return self._get(f"/v1/invoices/{invoice_id}", **params)

    def get_invoice_lines(self, invoice_id: str, **params: Any) -> Any:
        """GET /v1/invoices/{id}/lines — le détail ligne à ligne."""
        return self._get(f"/v1/invoices/{invoice_id}/lines", **params)

    def create_invoice(self, **body: Any) -> Any:
        """POST /v1/invoices — crée une facture au BROUILLON. Elle n'est ni
        finalisée ni envoyée ni encaissée par cet appel : rien ne part au client
        et rien n'est débité (`finalize`/`pay` ne sont pas implémentés ici).

        Args:
            **body: `customer` (requis en pratique), `auto_advance` (laisser
                False pour garder la main), `collection_method`, `description`,
                `days_until_due`, `metadata`, `currency`.
        """
        return self._post("/v1/invoices", body)

    def update_invoice(self, invoice_id: str, **body: Any) -> Any:
        """POST /v1/invoices/{id} — modifie une facture. Une fois FINALISÉE,
        Stripe n'accepte plus que `metadata`, `description` et quelques champs
        annexes ; le reste est refusé côté API."""
        return self._post(f"/v1/invoices/{invoice_id}", body)

    def list_invoice_items(self, **params: Any) -> Any:
        """GET /v1/invoiceitems — filtres `customer`, `invoice`, `pending`,
        `created`."""
        return self._get("/v1/invoiceitems", **params)

    def create_invoice_item(self, **body: Any) -> Any:
        """POST /v1/invoiceitems — pose un montant ponctuel sur la PROCHAINE
        facture d'un client (ou sur une facture brouillon nommée). Un `amount`
        négatif est un geste commercial (avoir).

        Args:
            **body: `customer` (requis), `amount` (en plus petite unité —
                centimes) + `currency`, ou `price`/`quantity` ; `invoice` pour
                viser un brouillon précis, `description`, `metadata`.
        """
        return self._post("/v1/invoiceitems", body)

    # ================================================================
    # Paiements — intentions, encaissements, remboursements, litiges
    # ================================================================

    def list_payment_intents(self, **params: Any) -> Any:
        """GET /v1/payment_intents — filtres `customer`, `created`, `limit`,
        `starting_after`, `ending_before`, `expand`."""
        return self._get("/v1/payment_intents", **params)

    def get_payment_intent(self, payment_intent_id: str, **params: Any) -> Any:
        """GET /v1/payment_intents/{id}. `last_payment_error` porte la raison
        d'échec faisant autorité (`code`, `decline_code`, `message`)."""
        return self._get(f"/v1/payment_intents/{payment_intent_id}", **params)

    def list_charges(self, **params: Any) -> Any:
        """GET /v1/charges — filtres `customer`, `created`, `payment_intent`,
        `limit`, `starting_after`, `ending_before`, `expand`."""
        return self._get("/v1/charges", **params)

    def get_charge(self, charge_id: str, **params: Any) -> Any:
        """GET /v1/charges/{id}. En échec, `failure_code`/`failure_message` et
        surtout `outcome.seller_message`, rédigé pour le marchand."""
        return self._get(f"/v1/charges/{charge_id}", **params)

    def list_refunds(self, **params: Any) -> Any:
        """GET /v1/refunds — LECTURE des remboursements déjà émis (filtres
        `charge`, `payment_intent`, `created`). En émettre un n'est pas
        possible depuis ce client, par construction."""
        return self._get("/v1/refunds", **params)

    def get_refund(self, refund_id: str, **params: Any) -> Any:
        """GET /v1/refunds/{id}."""
        return self._get(f"/v1/refunds/{refund_id}", **params)

    def list_disputes(self, **params: Any) -> Any:
        """GET /v1/disputes — filtres `charge`, `payment_intent`, `created`.
        `evidence_details.due_by` est une échéance dure : au-delà, le litige est
        perdu par défaut, et l'argent est déjà retiré du solde pendant ce temps."""
        return self._get("/v1/disputes", **params)

    def get_dispute(self, dispute_id: str, **params: Any) -> Any:
        """GET /v1/disputes/{id}."""
        return self._get(f"/v1/disputes/{dispute_id}", **params)

    # ================================================================
    # Catalogue — produits, prix, liens de paiement, réductions
    # ================================================================

    def list_products(self, **params: Any) -> Any:
        """GET /v1/products — filtres `active`, `ids`, `shippable`, `url`,
        `created`, `limit`, `starting_after`, `ending_before`."""
        return self._get("/v1/products", **params)

    def get_product(self, product_id: str, **params: Any) -> Any:
        """GET /v1/products/{id}."""
        return self._get(f"/v1/products/{product_id}", **params)

    def create_product(self, **body: Any) -> Any:
        """POST /v1/products — `name` (requis), `description`, `active`,
        `metadata`, `images`, `url`, `default_price_data`."""
        return self._post("/v1/products", body)

    def update_product(self, product_id: str, **body: Any) -> Any:
        """POST /v1/products/{id} — mêmes champs. `active=False` retire le
        produit de la vente sans rien supprimer."""
        return self._post(f"/v1/products/{product_id}", body)

    def list_prices(self, **params: Any) -> Any:
        """GET /v1/prices — filtres `product`, `active`, `currency`, `type`
        (one_time | recurring), `lookup_keys`, `recurring` (dict, ex.
        `{"interval": "month"}`), `created`, `limit`, `expand`."""
        return self._get("/v1/prices", **params)

    def get_price(self, price_id: str, **params: Any) -> Any:
        """GET /v1/prices/{id}."""
        return self._get(f"/v1/prices/{price_id}", **params)

    def create_price(self, **body: Any) -> Any:
        """POST /v1/prices — `currency` + `product` requis, plus `unit_amount`
        (centimes) ou `custom_unit_amount`. `recurring` (dict `interval`,
        `interval_count`) en fait un prix d'abonnement.

        ⚠️ Un prix Stripe est **immuable** sur son montant : « changer le prix »
        se fait en créant un nouveau prix et en désactivant l'ancien
        (`update_price(active=False)`), jamais en modifiant celui-ci.
        """
        return self._post("/v1/prices", body)

    def update_price(self, price_id: str, **body: Any) -> Any:
        """POST /v1/prices/{id} — seuls `active`, `metadata`, `nickname`,
        `lookup_key` et les options de tarification sont modifiables ; le
        montant ne l'est pas (cf. `create_price`)."""
        return self._post(f"/v1/prices/{price_id}", body)

    def list_payment_links(self, **params: Any) -> Any:
        """GET /v1/payment_links — filtre `active`, `limit`, `starting_after`."""
        return self._get("/v1/payment_links", **params)

    def get_payment_link(self, payment_link_id: str, **params: Any) -> Any:
        """GET /v1/payment_links/{id}."""
        return self._get(f"/v1/payment_links/{payment_link_id}", **params)

    def get_payment_link_line_items(self, payment_link_id: str, **params: Any) -> Any:
        """GET /v1/payment_links/{id}/line_items — ce que le lien fait payer."""
        return self._get(f"/v1/payment_links/{payment_link_id}/line_items", **params)

    def create_payment_link(self, line_items: List[Dict[str, Any]], **body: Any) -> Any:
        """POST /v1/payment_links — une URL réutilisable qui ouvre une page de
        paiement HÉBERGÉE PAR STRIPE. C'est la façon la plus sûre de faire
        encaisser quelque chose : aucun numéro de carte ne traverse oto, et le
        lien ne débite personne tant qu'un humain ne l'ouvre pas.

        Args:
            line_items: liste de `{"price": "price_…", "quantity": n}` (requis).
            **body: `after_completion`, `allow_promotion_codes`,
                `currency`, `metadata`, `customer_creation`.
        """
        return self._post("/v1/payment_links", {"line_items": line_items, **body})

    def update_payment_link(self, payment_link_id: str, **body: Any) -> Any:
        """POST /v1/payment_links/{id} — notamment `active=False` pour
        désactiver un lien sans le supprimer."""
        return self._post(f"/v1/payment_links/{payment_link_id}", body)

    def list_coupons(self, **params: Any) -> Any:
        """GET /v1/coupons — la RÈGLE de remise (percent_off/amount_off,
        duration). Le code que tape un client est une `promotion_code`."""
        return self._get("/v1/coupons", **params)

    def get_coupon(self, coupon_id: str, **params: Any) -> Any:
        """GET /v1/coupons/{id}."""
        return self._get(f"/v1/coupons/{coupon_id}", **params)

    def create_coupon(self, **body: Any) -> Any:
        """POST /v1/coupons — `duration` (once | repeating | forever) requis,
        plus `percent_off` OU `amount_off`+`currency` (Stripe refuse les deux
        à la fois). `duration_in_months` requis si `duration="repeating"`.
        `id` fixe l'identifiant (sinon Stripe en génère un) ; `name` est ce
        qu'un client voit sur sa facture. `max_redemptions`/`redeem_by`
        bornent l'usage dans le temps/en volume."""
        return self._post("/v1/coupons", body)

    def update_coupon(self, coupon_id: str, **body: Any) -> Any:
        """POST /v1/coupons/{id} — un Coupon Stripe n'a QUE `name` et
        `metadata` de modifiables après création (montant/durée/
        redemptions sont figés, comme le montant d'un `Price`) ; ni
        suppression ni désactivation n'existent dans ce client (cf. le
        docstring de tête du module) — un coupon dont on ne veut plus se
        retire en révoquant ses `promotion_code`s (`update_promotion_code`,
        `active=False`), pas en le touchant lui."""
        return self._post(f"/v1/coupons/{coupon_id}", body)

    def list_promotion_codes(self, **params: Any) -> Any:
        """GET /v1/promotion_codes — filtres `code`, `coupon`, `active`,
        `customer`, `created`."""
        return self._get("/v1/promotion_codes", **params)

    def get_promotion_code(self, promotion_code_id: str, **params: Any) -> Any:
        """GET /v1/promotion_codes/{id}."""
        return self._get(f"/v1/promotion_codes/{promotion_code_id}", **params)

    def create_promotion_code(self, **body: Any) -> Any:
        """POST /v1/promotion_codes — `coupon` (requis) est la règle de
        remise ; ce que ce endpoint ajoute est le CODE qu'un client tape
        réellement au paiement. `code` fixe le texte (majuscules/chiffres —
        Stripe le génère sinon), `customer` restreint le code à un seul
        client, `max_redemptions`/`expires_at` bornent l'usage,
        `restrictions` (dict, ex. `{"minimum_amount": 5000,
        "minimum_amount_currency": "eur"}` ou
        `{"first_time_transaction": True}`) borne QUAND il s'applique."""
        return self._post("/v1/promotion_codes", body)

    def update_promotion_code(self, promotion_code_id: str, **body: Any) -> Any:
        """POST /v1/promotion_codes/{id} — seuls `active` et `metadata`
        sont modifiables après création (le code, le coupon lié et les
        restrictions sont figés). `active=False` est la façon de RÉVOQUER
        un code sans le supprimer (aucune suppression n'existe dans ce
        client) — les redemptions déjà faites ne sont pas affectées."""
        return self._post(f"/v1/promotion_codes/{promotion_code_id}", body)

    # ================================================================
    # Checkout — sessions hébergées
    # ================================================================

    def list_checkout_sessions(self, **params: Any) -> Any:
        """GET /v1/checkout/sessions — filtres `customer`, `payment_intent`,
        `subscription`, `status` (open | complete | expired), `created`.
        `status=open` = paniers abandonnés encore ouverts."""
        return self._get("/v1/checkout/sessions", **params)

    def get_checkout_session(self, session_id: str, **params: Any) -> Any:
        """GET /v1/checkout/sessions/{id}."""
        return self._get(f"/v1/checkout/sessions/{session_id}", **params)

    def get_checkout_session_line_items(self, session_id: str, **params: Any) -> Any:
        """GET /v1/checkout/sessions/{id}/line_items."""
        return self._get(f"/v1/checkout/sessions/{session_id}/line_items", **params)

    # ================================================================
    # Événements — le fil d'activité du compte
    # ================================================================

    def list_events(self, **params: Any) -> Any:
        """GET /v1/events — tout changement d'état du compte, le plus récent
        d'abord, l'objet concerné inclus dans `data.object`. Rétention Stripe :
        30 jours.

        Args:
            **params: `type` (accepte le joker, ex. `invoice.*`), `types`
                (liste), `created`, `limit`, `starting_after`, `ending_before`.
        """
        return self._get("/v1/events", **params)

    def get_event(self, event_id: str, **params: Any) -> Any:
        """GET /v1/events/{id}."""
        return self._get(f"/v1/events/{event_id}", **params)

    # ================================================================
    # Webhooks — LECTURE seule (diagnostic d'intégration)
    # ================================================================

    def list_webhook_endpoints(self, **params: Any) -> Any:
        """GET /v1/webhook_endpoints — quelles intégrations écoutent ce compte.
        En créer, modifier ou supprimer n'est pas implémenté : c'est de la
        configuration d'infrastructure, et une suppression casse en silence
        l'automatisation qui en dépendait (relances, provisioning)."""
        return self._get("/v1/webhook_endpoints", **params)
