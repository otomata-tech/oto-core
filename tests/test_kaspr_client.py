"""Kaspr : le timeout par défaut DOIT partir avec chaque requête (signal #252 —
sans read-timeout, un blip amont suspend l'appel pour toujours)."""
from __future__ import annotations

from oto.tools.kaspr import client as km


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"profile": {}}


def test_request_carries_default_timeout(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Resp()

    monkeypatch.setattr(km.requests, "request", fake_request)
    c = km.KasprClient(api_key="k")
    c.enrich_linkedin("https://www.linkedin.com/in/alexislaporte/")

    assert captured["timeout"] == km.KasprClient.TIMEOUT
    assert captured["json"]["id"] == "alexislaporte"  # URL → slug nu


def test_explicit_timeout_not_overridden(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(**kwargs)
        return _Resp()

    monkeypatch.setattr(km.requests, "request", fake_request)
    km.KasprClient(api_key="k")._request("POST", "profile/linkedin", json={}, timeout=5)
    assert captured["timeout"] == 5
