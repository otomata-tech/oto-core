"""Contrat du client Dropcontact (submit/fetch async, header X-Access-Token).

Mocke `requests.post`/`requests.get` : vérifie payload + headers émis, le parse
du statut pending/done, et les guards client-side (plafond batch, taille item).
"""
from __future__ import annotations

import pytest

from oto.tools.dropcontact import client as dc
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_submit_payload_and_headers(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Resp(200, {"error": False, "request_id": "req_1", "success": True,
                            "credits_left": 42, "data": [{"email": "a@acme.fr"}]})

    monkeypatch.setattr(dc.requests, "post", fake_post)
    c = dc.DropcontactClient(api_key="k")
    out = c.submit(
        [{"email": "a@acme.fr", "first_name": "A", "last_name": "B", "company": "Acme"}],
        siren=True, language="en",
    )

    assert out["request_id"] == "req_1"
    assert out["credits_left"] == 42
    assert captured["url"].endswith("/enrich/all")
    assert captured["headers"]["X-Access-Token"] == "k"
    assert captured["timeout"] == 30
    assert captured["json"]["siren"] is True
    assert captured["json"]["language"] == "en"
    assert len(captured["json"]["data"]) == 1


def test_submit_guards():
    c = dc.DropcontactClient(api_key="k")
    with pytest.raises(ValueError, match="aucun contact"):
        c.submit([])
    with pytest.raises(ValueError, match="plafond"):
        c.submit([{"email": "a@acme.fr"}] * (dc.MAX_CONTACTS_PER_BATCH + 1))
    with pytest.raises(ValueError, match="octets"):
        c.submit([{"custom_fields": {"blob": "x" * (dc.MAX_CONTACT_BYTES + 1)}}])


def test_fetch_pending(monkeypatch):
    monkeypatch.setattr(
        dc.requests, "get",
        lambda url, headers=None, params=None, timeout=None: _Resp(
            200, {"error": False, "success": False,
                  "reason": "Request not ready yet, try again in 30 seconds"}),
    )
    out = dc.DropcontactClient(api_key="k").fetch("req_1")
    assert out == {"done": False, "reason": "Request not ready yet, try again in 30 seconds"}


def test_fetch_done(monkeypatch):
    body = {
        "error": False, "success": True, "credits_left": 41,
        "data": [{"email": [{"email": "nominative@pro", "qualification": "nominative@pro"}],
                  "first_name": "A", "last_name": "B"}],
    }
    monkeypatch.setattr(
        dc.requests, "get",
        lambda url, headers=None, params=None, timeout=None: _Resp(200, body),
    )
    out = dc.DropcontactClient(api_key="k").fetch("req_1", force_results=True)
    assert out["done"] is True
    assert out["credits_left"] == 41
    assert out["data"][0]["first_name"] == "A"


def test_fetch_force_results_query_param(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured.update(params=params)
        return _Resp(200, {"error": False, "success": False, "reason": "wait"})

    monkeypatch.setattr(dc.requests, "get", fake_get)
    dc.DropcontactClient(api_key="k").fetch("req_1", force_results=True)
    assert captured["params"] == {"forceResults": "true"}

    dc.DropcontactClient(api_key="k").fetch("req_1")
    assert captured["params"] is None


def test_submit_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        dc.requests, "post",
        lambda url, headers=None, json=None, timeout=None: _Resp(
            401, {"error": True, "reason": "Wrong access token"}),
    )
    with pytest.raises(UpstreamHTTPError) as exc:
        dc.DropcontactClient(api_key="bad").submit([{"email": "a@acme.fr"}])
    assert exc.value.status_code == 401


def test_check_credits_uses_empty_contact(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json=json)
        return _Resp(200, {"error": False, "success": True, "credits_left": 100, "data": [{}]})

    monkeypatch.setattr(dc.requests, "post", fake_post)
    out = dc.DropcontactClient(api_key="k").check_credits()
    assert out["credits_left"] == 100
    assert captured["json"]["data"] == [{}]
