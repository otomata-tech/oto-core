"""`failed_payments` : enrichissement mémoïsé et parallèle.

3 requêtes séquentielles par ligne (mandat, client, motif) + une pause : sur 200
échecs, 600 allers-retours en file — 186 s mesurés en prod. Les échecs se
concentrent sur peu de débiteurs, donc mandat/client se mémoïsent ; le motif, lui,
est par paiement. On verrouille les deux moitiés du contrat : moins d'appels, et
exactement les mêmes lignes qu'avant.
"""
from oto.tools.gocardless.client import GoCardlessClient


def _client(payments, mandates, customers, fails):
    c = GoCardlessClient(api_key="k", rate_limit_delay=0)
    c.calls = {"mandate": [], "customer": [], "fail": []}

    def list_payments(status=None, limit=None, created_gt=None):
        return payments

    def get_mandate(mid):
        c.calls["mandate"].append(mid)
        return mandates.get(mid, {})

    def get_customer(cid):
        c.calls["customer"].append(cid)
        return customers.get(cid, {})

    def failure_reason(pid):
        c.calls["fail"].append(pid)
        return fails.get(pid, {})

    c.list_payments = list_payments
    c.get_mandate = get_mandate
    c.get_customer = get_customer
    c.failure_reason = failure_reason
    return c


PAYMENTS = [
    {"id": "PM1", "amount": 12000, "currency": "EUR", "charge_date": "2026-07-01",
     "links": {"mandate": "MD1"}},
    {"id": "PM2", "amount": 4500, "currency": "EUR", "charge_date": "2026-07-02",
     "links": {"mandate": "MD1"}},          # même mandat : le débiteur rate 2 fois
    {"id": "PM3", "amount": 900, "currency": "EUR", "charge_date": "2026-07-03",
     "links": {"mandate": "MD2"}},
]
MANDATES = {"MD1": {"status": "active", "links": {"customer": "CU1"}},
            "MD2": {"status": "cancelled", "links": {"customer": "CU1"}}}
CUSTOMERS = {"CU1": {"company_name": "ACME", "email": "pay@acme.fr"}}
FAILS = {"PM1": {"created_at": "2026-07-01T10:00:00Z", "cause": "insufficient_funds",
                 "reason_code": "AM04", "will_attempt_retry": True},
         "PM2": {"created_at": "2026-07-02T10:00:00Z", "cause": "mandate_cancelled",
                 "reason_code": "MD01", "will_attempt_retry": False},
         "PM3": {"created_at": "2026-07-03T10:00:00Z", "cause": "refer_to_payer",
                 "reason_code": "MS02", "will_attempt_retry": False}}


def test_repeated_mandate_and_customer_are_fetched_once():
    c = _client(PAYMENTS, MANDATES, CUSTOMERS, FAILS)
    c.failed_payments()
    assert sorted(c.calls["mandate"]) == ["MD1", "MD2"]   # 3 lignes, 2 mandats
    assert c.calls["customer"] == ["CU1"]                 # …et un seul client
    assert sorted(c.calls["fail"]) == ["PM1", "PM2", "PM3"]  # le motif reste par paiement


def test_rows_keep_their_shape_and_ordering():
    c = _client(PAYMENTS, MANDATES, CUSTOMERS, FAILS)
    rows = c.failed_payments()
    assert [r["payment_id"] for r in rows] == ["PM3", "PM2", "PM1"]  # failed_at desc
    first = rows[-1]
    assert first["name"] == "ACME" and first["email"] == "pay@acme.fr"
    assert first["amount"] == 120.0 and first["mandate_status"] == "active"
    assert first["cause"] == "insufficient_funds" and first["will_attempt_retry"] is True


def test_a_payment_without_mandate_still_yields_a_row():
    c = _client([{"id": "PM9", "amount": 100, "links": {}}], {}, {}, {})
    rows = c.failed_payments()
    assert len(rows) == 1 and rows[0]["payment_id"] == "PM9"
    assert rows[0]["name"] == "" and rows[0]["mandate_id"] is None
    assert c.calls["mandate"] == [] and c.calls["customer"] == []


def test_upstream_error_is_passed_through_untouched():
    c = _client({"error": "401"}, {}, {}, {})
    assert c.failed_payments() == {"error": "401"}


def test_name_falls_back_to_given_and_family_name():
    c = _client([{"id": "PM1", "amount": 100, "links": {"mandate": "MD1"}}],
                {"MD1": {"links": {"customer": "CU2"}}},
                {"CU2": {"given_name": "Jean", "family_name": "Dupont"}}, {})
    assert c.failed_payments()[0]["name"] == "Jean Dupont"
