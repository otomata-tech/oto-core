"""Une erreur amont ne doit jamais être avalée en liste vide (oto-backend#223).

Le bug : `fetch_all_pages` faisait `break` sur un dict d'erreur → renvoyait `[]`
indistinguable d'un « 0 résultat ». Sur clé 401,
`find_invoice_by_external_reference` (anti-doublon d'avoir) concluait alors
« aucune facture d'origine » et pouvait recréer des avoirs en double.
"""

import pytest

from oto.tools.common import UpstreamHTTPError
from oto.tools.pennylane.client import PennylaneClient


def _client_returning(err):
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    c.fetch = lambda endpoint, params=None, retries=3: err
    return c


def test_fetch_all_pages_raises_on_http_error():
    c = _client_returning({"error": "401", "details": "invalid key", "status_code": 401})
    with pytest.raises(UpstreamHTTPError) as ei:
        c.fetch_all_pages("customer_invoices")
    assert ei.value.status_code == 401
    assert ei.value.is_client_error


def test_fetch_all_pages_raises_on_network_error():
    # Erreur sans status HTTP (réseau / max retries) → remonte quand même.
    c = _client_returning({"error": "Max retries exceeded"})
    with pytest.raises(RuntimeError):
        c.fetch_all_pages("customer_invoices")


def test_find_invoice_raises_instead_of_false_negative():
    # Le cœur du risque : sur 401, l'anti-doublon LÈVE au lieu de renvoyer None
    # (ce qui aurait fait recréer un avoir en double).
    c = _client_returning({"error": "401", "status_code": 401})
    with pytest.raises(UpstreamHTTPError):
        c.find_invoice_by_external_reference("gocardless-payment-xyz")


# --- Anti-doublon : filtre serveur, plus de scan borné (signal #268) ---------

class _Spy:
    """Capture le dernier (endpoint, params) et rend une réponse fixée."""
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, endpoint, params=None, retries=3):
        self.calls.append((endpoint, params))
        return self.payload


def _client_with(spy):
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    c.fetch = spy
    return c


def test_find_invoice_uses_the_native_server_filter():
    import json
    spy = _Spy({"items": [{"id": 42, "external_reference": "AUT-70943"}]})
    c = _client_with(spy)
    assert c.find_invoice_by_external_reference("AUT-70943")["id"] == 42
    endpoint, params = spy.calls[-1]
    assert endpoint == "customer_invoices"
    assert json.loads(params["filter"]) == [
        {"field": "external_reference", "operator": "eq", "value": "AUT-70943"}]
    assert len(spy.calls) == 1          # un seul appel : plus de scan paginé


def test_find_invoice_returns_none_when_the_filter_matches_nothing():
    c = _client_with(_Spy({"items": []}))
    assert c.find_invoice_by_external_reference("ZZZ") is None


def test_find_customer_shares_the_same_guard():
    """Le chemin customers court-circuitait la règle #223 : sur 401 il rendait None,
    donc « ce client n'existe pas » — puis un create en 422."""
    c = _client_with(_Spy({"error": "401", "status_code": 401}))
    with pytest.raises(UpstreamHTTPError):
        c.find_customer_by_external_reference("mm-companyId-7")
