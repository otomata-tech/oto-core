"""Lead lifecycle additions to LemlistClient (create/launch/variables).

Mocks `requests.request` (the shared `_request` helper): verifies method/URL,
JSON body vs query params per endpoint, and that a 4xx surfaces as
`UpstreamHTTPError` with the upstream body intact (lemlist's launch-lead 400s
carry a structured error code an agent needs to see).
"""
from __future__ import annotations

import pytest

from oto.tools.lemlist import client as lm
from oto.tools.common.errors import UpstreamHTTPError


class _Resp:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._body


def test_create_lead_posts_body_and_no_flags_by_default(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, headers=headers, **kwargs)
        return _Resp(200, {"_id": "lea_123", "campaignId": "camp_1", "email": "a@acme.fr"})

    monkeypatch.setattr(lm.requests, "request", fake_request)
    c = lm.LemlistClient(api_key="k")
    out = c.create_lead("camp_1", {"email": "a@acme.fr", "firstName": "A"})

    assert out["_id"] == "lea_123"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/campaigns/camp_1/leads/")
    assert captured["json"] == {"email": "a@acme.fr", "firstName": "A"}
    assert captured["params"] == {}


def test_create_lead_flags_become_true_query_params(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(kwargs)
        return _Resp(200, {"_id": "lea_123"})

    monkeypatch.setattr(lm.requests, "request", fake_request)
    c = lm.LemlistClient(api_key="k")
    c.create_lead(
        "camp_1", {"email": "a@acme.fr"},
        deduplicate=True, find_email=True,
    )

    assert captured["params"] == {"deduplicate": "true", "findEmail": "true"}


def test_launch_lead_posts_to_review_path(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url)
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(lm.requests, "request", fake_request)
    out = lm.LemlistClient(api_key="k").launch_lead("lea_8xJSc7sV7ggpiVnXe")

    assert out == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/leads/review/lea_8xJSc7sV7ggpiVnXe")


def test_launch_lead_400_raises_upstream_error_with_code(monkeypatch):
    monkeypatch.setattr(
        lm.requests, "request",
        lambda method, url, headers=None, **kw: _Resp(
            400, {"error": "CAMPAIGN_LEAD_REVIEW_LEAD_ALREADY_LAUNCHED",
                  "message": "Lead already launched"}),
    )
    with pytest.raises(UpstreamHTTPError) as exc:
        lm.LemlistClient(api_key="k").launch_lead("lea_1")

    assert exc.value.status_code == 400
    assert "CAMPAIGN_LEAD_REVIEW_LEAD_ALREADY_LAUNCHED" in str(exc.value)


def test_add_lead_variables_sends_query_params_not_body(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(lm.requests, "request", fake_request)
    out = lm.LemlistClient(api_key="k").add_lead_variables(
        "lea_1", {"customField1": "Lemlist", "customField2": "Will Rule"},
    )

    assert out == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/leads/lea_1/variables")
    assert captured["params"] == {"customField1": "Lemlist", "customField2": "Will Rule"}
    assert "json" not in captured
