"""Contrat du client TheirStack (v1, Bearer, jobs/companies search).

Mocke `requests.Session.request` : vérifie chemin, méthode, corps passé TEL QUEL
(la DSL de filtres n'est pas re-typée) et le typage des erreurs amont.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.theirstack import client as ts


class _Resp:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"x"
        self.text = str(body)
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"metadata": {"total_results": 0}, "data": []})

    monkeypatch.setattr(ts.requests.Session, "request", fake_request)
    return seen


def _client():
    return ts.TheirStackClient(api_key="ts-test")


def test_auth_header_is_bearer_and_never_in_query():
    c = _client()
    assert c.session.headers["Authorization"] == "Bearer ts-test"


def test_search_jobs_posts_the_dsl_verbatim(capture):
    payload = {"company_name_or": ["PUIG & FILS"], "posted_at_max_age_days": 90,
               "job_country_code_or": ["FR"], "limit": 25, "page": 0}
    _client().search_jobs(payload)

    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.theirstack.com/v1/jobs/search"
    assert capture["kwargs"]["json"] == payload
    assert capture["kwargs"]["timeout"] == ts._HTTP_TIMEOUT
    # Le dict de l'appelant n'est pas muté (copie).
    assert capture["kwargs"]["json"] is not payload


def test_search_companies_posts_the_dsl_verbatim(capture):
    payload = {"company_name_or": ["PUIG & FILS"], "company_country_code_or": ["FR"],
               "limit": 25, "page": 0}
    _client().search_companies(payload)

    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.theirstack.com/v1/companies/search"
    assert capture["kwargs"]["json"] == payload


def test_credit_balance_is_a_get(capture):
    _client().credit_balance()
    assert capture["method"] == "GET"
    assert capture["url"] == "https://api.theirstack.com/v0/billing/credit-balance"
    assert "json" not in capture["kwargs"]


def test_payload_must_be_a_dict():
    with pytest.raises(ValueError):
        _client().search_jobs(["not", "a", "dict"])  # type: ignore[arg-type]


def test_http_error_is_typed(monkeypatch):
    monkeypatch.setattr(ts.requests.Session, "request",
                        lambda self, *a, **k: _Resp(402, {"detail": "Not enough credits"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().search_companies({"company_name_or": ["X"]})
    assert e.value.status_code == 402
    assert e.value.is_client_error
    assert e.value.service == "theirstack"
