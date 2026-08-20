"""Contrat du client Ahrefs (v3, Bearer, select/where/order_by en passthrough).

Mocke `requests.Session.request` : vérifie méthode/URL/query/body, le header
Bearer, le typage des erreurs amont, et le garde-fou `output='json'` uniquement.
"""
from __future__ import annotations

import pytest

from oto.tools.ahrefs import client as ah
from oto.tools.common.errors import UpstreamHTTPError


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
        return _Resp(200, {"metrics": []})

    monkeypatch.setattr(ah.requests.Session, "request", fake_request)
    return seen


def _client():
    return ah.AhrefsClient(api_key="ah-test")


def test_auth_header_is_bearer():
    c = _client()
    assert c.session.headers["Authorization"] == "Bearer ah-test"


def test_get_endpoint_builds_url_and_query(capture):
    _client().domain_rating(target="example.com", date="2026-01-01")

    assert capture["method"] == "GET"
    assert capture["url"] == "https://api.ahrefs.com/v3/site-explorer/domain-rating"
    assert capture["kwargs"]["params"] == {"target": "example.com", "date": "2026-01-01"}
    assert capture["kwargs"]["timeout"] == ah._HTTP_TIMEOUT


def test_none_params_are_dropped_from_query(capture):
    _client().backlinks_stats(target="example.com", date="2026-01-01", protocol=None)
    assert "protocol" not in capture["kwargs"]["params"]


def test_extra_params_pass_through_verbatim(capture):
    _client().organic_keywords(
        target="example.com", select="keyword,volume", date="2026-01-01",
        where="volume>100", order_by="volume:desc", limit=50)

    params = capture["kwargs"]["params"]
    assert params["select"] == "keyword,volume"
    assert params["where"] == "volume>100"
    assert params["order_by"] == "volume:desc"
    assert params["limit"] == 50


def test_post_endpoint_sends_json_body(capture):
    targets = [{"url": "https://example.com", "mode": "domain", "protocol": "both"}]
    _client().batch_analysis(targets=targets, select=["domain_rating"])

    assert capture["method"] == "POST"
    assert capture["url"] == "https://api.ahrefs.com/v3/batch-analysis/batch-analysis"
    assert capture["kwargs"]["json"] == {"targets": targets, "select": ["domain_rating"]}


def test_delete_endpoint_uses_query_params(capture):
    _client().delete_projects(project_ids="1,2,3")
    assert capture["method"] == "DELETE"
    assert capture["kwargs"]["params"] == {"project_ids": "1,2,3"}


def test_output_other_than_json_is_rejected_client_side(capture):
    with pytest.raises(ValueError, match="output"):
        _client().domain_rating(target="example.com", date="2026-01-01", output="csv")
    # Never even reached the network — nothing was captured.
    assert capture == {}


def test_output_json_is_allowed(capture):
    _client().domain_rating(target="example.com", date="2026-01-01", output="json")
    assert capture["kwargs"]["params"]["output"] == "json"


def test_web_analytics_rejects_unknown_report():
    with pytest.raises(ValueError, match="Unknown Web Analytics report"):
        _client().web_analytics("not-a-real-report", project_id=1)


def test_web_analytics_known_report_builds_path(capture):
    _client().web_analytics("source-channels-chart", project_id=1, granularity="daily")
    assert capture["url"] == "https://api.ahrefs.com/v3/web-analytics/source-channels-chart"
    assert capture["kwargs"]["params"]["granularity"] == "daily"


def test_gsc_rejects_unknown_report():
    with pytest.raises(ValueError, match="Unknown GSC report"):
        _client().gsc_report("not-a-real-report", date_from="2026-01-01")


# --- corps vérifiés contre le spec OpenAPI réel (docs.ahrefs.com/openapi.json,
# 2026-08-20) — trois formes fausses en doc-résumé, corrigées et verrouillées ici.

def test_add_project_keywords_sends_two_parallel_arrays(capture):
    kw = [{"keyword": "seo tools"}]
    loc = [{"country": "fr"}]
    _client().add_project_keywords(1, kw, loc)
    assert capture["kwargs"]["json"] == {"keywords": kw, "locations": loc}
    assert capture["kwargs"]["params"] == {"project_id": 1}


def test_tag_project_keywords_puts_project_id_in_body_not_query(capture):
    _client().tag_project_keywords(1, [{"keyword": "x"}], ["a"])
    assert capture["kwargs"]["json"]["project_id"] == 1
    assert capture["kwargs"]["params"] in (None, {})


def test_create_brand_radar_report_has_no_top_level_data_source(capture):
    pf = [{"data_source": "chatgpt", "frequency": "daily"}]
    _client().create_brand_radar_report(pf, name="My report")
    body = capture["kwargs"]["json"]
    assert body == {"prompts_frequency": pf, "name": "My report"}
    assert "data_source" not in body and "frequency" not in body


def test_http_error_is_typed(monkeypatch):
    monkeypatch.setattr(
        ah.requests.Session, "request",
        lambda self, *a, **k: _Resp(401, {"error": "invalid_token"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().domain_rating(target="example.com", date="2026-01-01")
    assert e.value.status_code == 401
    assert e.value.is_client_error
    assert e.value.service == "ahrefs"
