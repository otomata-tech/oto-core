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


# --- enrichissement ----------------------------------------------------------


def _capture(monkeypatch, status=200, body=None):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Resp(status, {} if body is None else body)

    monkeypatch.setattr(lm.requests, "request", fake_request)
    return captured


def test_enrich_sends_actions_and_identity_as_query_params(monkeypatch):
    captured = _capture(monkeypatch, body={"id": "enr_1"})
    c = lm.LemlistClient(api_key="k")

    out = c.enrich(
        first_name="John", last_name="Lempire", company_domain="lempire.com",
        find_email=True, find_phone=True,
    )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/enrich")
    # Query params, pas de corps JSON — c'est le contrat de cet endpoint.
    assert "json" not in captured
    params = captured["params"]
    # Les booléens partent en "true" : `True` se sérialiserait "True".
    assert params["findEmail"] == "true" and params["findPhone"] == "true"
    assert "verifyEmail" not in params and "linkedinEnrichment" not in params
    assert params["firstName"] == "John"
    assert params["companyDomain"] == "lempire.com"
    assert out == {"id": "enr_1"}


def test_enrich_without_any_action_raises_before_the_call(monkeypatch):
    captured = _capture(monkeypatch)
    c = lm.LemlistClient(api_key="k")

    with pytest.raises(ValueError):
        c.enrich(email="a@acme.fr")

    assert captured == {}  # rien n'est parti : pas de crédit dépensé


def test_get_enrichment_returns_not_found_body_instead_of_raising(monkeypatch):
    # 404 porte ici une charge utile légitime (`enrichmentStatus: not-found`),
    # pas une erreur : la lever masquerait un état terminal derrière une exception.
    _capture(monkeypatch, status=404, body={
        "enrichmentId": "enr_x", "enrichmentStatus": "not-found",
        "error": "Enrichment not found", "data": {},
    })
    c = lm.LemlistClient(api_key="k")

    out = c.get_enrichment("enr_x")

    assert out["enrichmentStatus"] == "not-found"


def test_get_enrichment_in_progress_202_is_a_normal_body(monkeypatch):
    _capture(monkeypatch, status=202, body={
        "enrichmentId": "enr_1", "enrichmentStatus": "in-progress", "data": {},
    })
    c = lm.LemlistClient(api_key="k")

    assert c.get_enrichment("enr_1")["enrichmentStatus"] == "in-progress"


def test_get_enrichment_still_raises_on_a_real_error(monkeypatch):
    _capture(monkeypatch, status=401, body={"error": "unauthorized"})
    c = lm.LemlistClient(api_key="k")

    with pytest.raises(UpstreamHTTPError) as e:
        c.get_enrichment("enr_1")
    assert e.value.status_code == 401


def test_enrich_lead_posts_to_the_lead_path(monkeypatch):
    captured = _capture(monkeypatch, body={"id": "enr_2"})
    c = lm.LemlistClient(api_key="k")

    c.enrich_lead("lea_1", linkedin_enrichment=True)

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/leads/lea_1/enrich")
    assert captured["params"] == {"linkedinEnrichment": "true"}


def test_enrich_lead_without_any_action_raises_before_the_call(monkeypatch):
    captured = _capture(monkeypatch)
    c = lm.LemlistClient(api_key="k")

    with pytest.raises(ValueError):
        c.enrich_lead("lea_1")

    assert captured == {}


def test_bulk_enrich_posts_a_json_array(monkeypatch):
    captured = _capture(monkeypatch, body=[{"id": "enr_1"}])
    c = lm.LemlistClient(api_key="k")

    items = [{"input": {"email": "a@acme.fr"}, "enrichmentRequests": ["verify"]}]
    out = c.bulk_enrich(items)

    assert captured["url"].endswith("/v2/enrichments/bulk")
    assert captured["json"] == items
    assert out == [{"id": "enr_1"}]


def test_bulk_action_vocabulary_is_not_a_snake_case_of_the_v1_flags():
    # v2 dit `verify`, v1 dit `verifyEmail` : une conversion mécanique enverrait
    # une action que lemlist ne connaît pas.
    assert lm.LemlistClient.ENRICH_BULK_ACTIONS["verify_email"] == "verify"
    assert lm.LemlistClient.ENRICH_FLAGS["verify_email"] == "verifyEmail"


def test_requests_carry_a_timeout(monkeypatch):
    captured = _capture(monkeypatch, body={"id": "enr_1"})
    c = lm.LemlistClient(api_key="k")

    c.enrich(email="a@acme.fr", find_phone=True)

    assert captured["timeout"] == lm._HTTP_TIMEOUT
