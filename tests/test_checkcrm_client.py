"""Contrat du client CheckCrm (X-API-Key, send_contacts/add_subsidiary/list_subsidiaries).

Mocke `requests.request` : vérifie payload/headers/params émis pour chaque méthode,
en particulier `list_subsidiaries`'s optional `slug`/`name` query params.
"""
from __future__ import annotations

import pytest

from oto.tools.checkcrm import client as cc
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.content = b"x"
        self.text = str(body)
        self.headers = {}

    def json(self):
        return self._body


def test_send_contacts_payload(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, headers=headers, kwargs=kwargs)
        return _Resp(200, {"checkId": "c1", "contactCount": 1, "skippedCount": 0})

    monkeypatch.setattr(cc.requests, "request", fake_request)
    out = cc.CheckCrmClient(api_key="k").send_contacts(
        "acc1", [{"id": "1", "linkedinUrl": "https://www.linkedin.com/in/jane"}],
        account_linkedin_url="https://www.linkedin.com/company/acme-corp",
    )

    assert out["checkId"] == "c1"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/contacts")
    assert captured["headers"]["X-API-Key"] == "k"
    body = captured["kwargs"]["json"]
    assert body["accountId"] == "acc1"
    assert body["accountLinkedinUrl"] == "https://www.linkedin.com/company/acme-corp"


def test_add_subsidiary_payload(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return _Resp(201, {"subsidiary": {"id": "s1"}, "duplicate": False})

    monkeypatch.setattr(cc.requests, "request", fake_request)
    cc.CheckCrmClient(api_key="k").add_subsidiary(
        "https://www.linkedin.com/company/acme-corp",
        "https://www.linkedin.com/company/acme-labs",
        subsidiary_name="Acme Labs",
    )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/companies/subsidiaries")
    body = captured["kwargs"]["json"]
    assert body["companyLinkedinUrl"] == "https://www.linkedin.com/company/acme-corp"
    assert body["subsidiaryLinkedinUrl"] == "https://www.linkedin.com/company/acme-labs"
    assert body["subsidiaryName"] == "Acme Labs"


def test_list_subsidiaries_no_filter(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"companies": []})

    monkeypatch.setattr(cc.requests, "request", fake_request)
    cc.CheckCrmClient(api_key="k").list_subsidiaries()

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/companies/subsidiaries")
    assert captured["kwargs"]["params"] == {}


def test_list_subsidiaries_slug_only(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(kwargs=kwargs)
        return _Resp(200, {"slug": "acme-labs", "companies": []})

    monkeypatch.setattr(cc.requests, "request", fake_request)
    cc.CheckCrmClient(api_key="k").list_subsidiaries(slug="acme-labs")

    assert captured["kwargs"]["params"] == {"slug": "acme-labs"}


def test_list_subsidiaries_name_only(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(kwargs=kwargs)
        return _Resp(200, {"name": "labs", "companies": []})

    monkeypatch.setattr(cc.requests, "request", fake_request)
    cc.CheckCrmClient(api_key="k").list_subsidiaries(name="labs")

    assert captured["kwargs"]["params"] == {"name": "labs"}


def test_list_subsidiaries_slug_and_name_both_sent(monkeypatch):
    """The enrichment API ORs these two filters together — the client's job is just
    to forward both params, not to pick one."""
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(kwargs=kwargs)
        return _Resp(200, {"slug": "acme-corp", "name": "globex", "companies": []})

    monkeypatch.setattr(cc.requests, "request", fake_request)
    cc.CheckCrmClient(api_key="k").list_subsidiaries(slug="acme-corp", name="globex")

    assert captured["kwargs"]["params"] == {"slug": "acme-corp", "name": "globex"}


def test_list_subsidiaries_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        cc.requests, "request",
        lambda method, url, headers=None, **kwargs: _Resp(401, {"error": "Unauthorized"}),
    )
    with pytest.raises(UpstreamHTTPError) as exc:
        cc.CheckCrmClient(api_key="bad").list_subsidiaries(slug="acme-corp")
    assert exc.value.status_code == 401
