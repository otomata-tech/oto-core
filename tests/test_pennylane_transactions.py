"""Filtres métier de `PennylaneClient.get_transactions` (oto-backend#76).

Sans levier l'endpoint renvoie tout l'historique (vécu : 307 transactions
≈ 247k chars). `period_start`/`period_end` → filtre serveur (param `filter`
API v2) ; `only_outstanding` → filtre client sur `outstanding_balance`.
"""

import json

from oto.tools.pennylane.client import PennylaneClient, _is_outstanding


def _client(pages):
    """Client stubé : `pages` = payloads successifs renvoyés par `fetch`."""
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    calls = []

    def fake_fetch(endpoint, params=None, retries=3):
        calls.append({"endpoint": endpoint, "params": dict(params or {})})
        return pages.pop(0)

    c.fetch = fake_fetch
    c._calls = calls
    return c


def test_period_filters_sent_server_side():
    c = _client([{"items": [], "has_more": False}])
    c.get_transactions(period_start="2026-01-01", period_end="2026-03-31")
    sent = json.loads(c._calls[0]["params"]["filter"])
    assert {"field": "date", "operator": "gteq", "value": "2026-01-01"} in sent
    assert {"field": "date", "operator": "lteq", "value": "2026-03-31"} in sent


def test_no_filter_param_by_default():
    c = _client([{"items": [{"id": 1}], "has_more": False}])
    out = c.get_transactions()
    assert "filter" not in c._calls[0]["params"]
    assert out == [{"id": 1}]


def test_only_outstanding_filters_client_side():
    items = [
        {"id": 1, "outstanding_balance": "0.0"},
        {"id": 2, "outstanding_balance": "150.5"},
        {"id": 3, "outstanding_balance": 0},
        {"id": 4},                                   # champ absent → conservé
        {"id": 5, "outstanding_balance": "n/a"},     # illisible → conservé
    ]
    c = _client([{"items": items, "has_more": False}])
    out = c.get_transactions(only_outstanding=True)
    assert [t["id"] for t in out] == [2, 4, 5]


def test_per_page_forwarded_as_limit():
    c = _client([{"items": [], "has_more": False}])
    c.get_transactions(per_page=20)
    assert c._calls[0]["params"]["limit"] == 20


def test_is_outstanding_parsing():
    assert _is_outstanding({"outstanding_balance": "12.5"})
    assert not _is_outstanding({"outstanding_balance": "0.00"})
    assert not _is_outstanding({"outstanding_balance": 0})
    assert _is_outstanding({"outstanding_balance": None})
    assert _is_outstanding({})
    assert _is_outstanding("pas un dict")
