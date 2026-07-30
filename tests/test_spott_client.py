"""Contrat du client Spott (header x-api-key, curseur vs page, garde-fous).

Mocke `requests.Session.request` : vérifie l'URL/les params émis (dont l'omission
des paramètres vides — `include` déclaré required mais à défaut `[]`), la forme
des corps `_search` et `create_application`, et les garde-fous client-side
(pipeline inconnu, entityType de note inconnu).
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.spott.client import SpottClient


class _Resp:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = {} if body is None else body
        self.content = b"{}"
        self.text = str(self._body)

    def json(self):
        return self._body


def _client(monkeypatch, resp: _Resp | None = None) -> tuple[SpottClient, dict]:
    captured: dict = {}
    c = SpottClient(api_key="k")

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return resp or _Resp(200, {"data": []})

    monkeypatch.setattr(c.session, "request", fake_request)
    return c, captured


def test_auth_header_is_x_api_key():
    c = SpottClient(api_key="secret")
    assert c.session.headers["x-api-key"] == "secret"


def test_list_candidates_drops_empty_params(monkeypatch):
    c, cap = _client(monkeypatch)
    c.list_candidates(limit=200)

    assert cap["url"] == "https://api.gospott.com/candidates"
    # limit plafonné à 50 (borne API) ; cursor/include/listIds omis, pas envoyés nuls.
    assert cap["params"] == {"limit": 50}


def test_list_candidates_passes_include_and_cursor(monkeypatch):
    c, cap = _client(monkeypatch)
    c.list_candidates(cursor="c1", include=["skills"], list_ids=["l1"])

    assert cap["params"]["cursor"] == "c1"
    assert cap["params"]["include"] == ["skills"]
    assert cap["params"]["listIds"] == ["l1"]


def test_list_jobs_hits_vacancies(monkeypatch):
    c, cap = _client(monkeypatch)
    c.list_jobs(company_ids=["co1"], candidate_emails=["a@acme.fr"])

    assert cap["url"].endswith("/vacancies")
    assert cap["params"]["companyIds"] == ["co1"]
    assert cap["params"]["candidateEmailAddresses"] == ["a@acme.fr"]


def test_search_jobs_body_is_filters_plus_page(monkeypatch):
    c, cap = _client(monkeypatch)
    flt = [{"type": "boolean", "operator": "equals",
            "path": "vacancy.stage.isOpen", "value": True}]
    c.search_jobs(filters=flt, page=2, page_size=10)

    assert cap["method"] == "POST"
    assert cap["url"].endswith("/vacancies/_search")
    assert cap["json"] == {"filters": flt, "page": 2, "pageSize": 10}


def test_search_candidates_defaults_to_empty_filters(monkeypatch):
    c, cap = _client(monkeypatch)
    c.search_candidates()

    assert cap["json"] == {"filters": []}


def test_list_applications_maps_job_ids_to_vacancy_ids(monkeypatch):
    c, cap = _client(monkeypatch)
    c.list_applications(job_ids=["v1"], is_inbound=True)

    assert cap["params"]["vacancyIds"] == ["v1"]
    assert cap["params"]["isInbound"] is True


def test_create_application_keeps_null_vacancy_for_speculative(monkeypatch):
    c, cap = _client(monkeypatch)
    c.create_application(candidate_id="cand1", stage_id="st1", client_id="cl1")

    # vacancyId/statusId sont `required` mais nullable : une candidature
    # spontanée les envoie explicitement à null (les omettre = 400).
    assert cap["json"]["vacancyId"] is None
    assert cap["json"]["statusId"] is None
    assert cap["json"]["clientId"] == "cl1"
    assert cap["json"]["candidateId"] == "cand1"


def test_move_application_omits_absent_status(monkeypatch):
    c, cap = _client(monkeypatch)
    c.move_application("app1", stage_id="st2")

    assert cap["method"] == "PUT"
    assert cap["url"].endswith("/applications/app1/move")
    assert cap["json"] == {"stageId": "st2"}


def test_pipeline_stages_rejects_unknown_entity(monkeypatch):
    c, _ = _client(monkeypatch)
    with pytest.raises(ValueError, match="pipeline Spott inconnu"):
        c.pipeline_stages("candidates")


def test_create_note_rejects_unknown_entity_type(monkeypatch):
    c, _ = _client(monkeypatch)
    with pytest.raises(ValueError, match="entityType Spott inconnu"):
        c.create_note("hello", links=[{"entityType": "job", "entityId": "v1"}])


def test_create_note_body(monkeypatch):
    c, cap = _client(monkeypatch)
    c.create_note("appel ok", title="Call", source="phoneOutbound",
                  links=[{"entityType": "candidate", "entityId": "cand1"}])

    assert cap["json"]["content"] == "appel ok"
    assert cap["json"]["title"] == "Call"
    assert cap["json"]["source"] == "phoneOutbound"
    assert cap["json"]["links"][0]["entityType"] == "candidate"


def test_placements_use_page_pagination(monkeypatch):
    c, cap = _client(monkeypatch)
    c.list_placements(page=1, page_size=500)

    assert cap["params"]["page"] == 1
    assert cap["params"]["pageSize"] == 100  # plafond API


def test_upstream_error_is_typed(monkeypatch):
    c, _ = _client(monkeypatch, _Resp(401, {"message": "invalid api key"}))
    with pytest.raises(UpstreamHTTPError) as exc:
        c.list_users()

    assert exc.value.status_code == 401
    assert exc.value.service == "spott"
