"""Contrat du client PostHog (API privée REST + /query/, Bearer clé personnelle).

Mocke `requests.Session.request` : vérifie le refus des mauvais types de clé,
le host régional, la résolution de `project_id`, la forme des requêtes HogQL et
typées, la garde SSRF sur `next`, et l'ABSENCE des écritures qui changent le
produit ou détruisent de la donnée.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.posthog import client as ph


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
        return _Resp(200, {"results": []})

    monkeypatch.setattr(ph.requests.Session, "request", fake_request)
    return seen


def _client(**kw):
    kw.setdefault("project_id", 571144)
    return ph.PostHogClient(api_key="phx_test", **kw)


# --- types de clé : le piège n°1 ----------------------------------------------

def test_project_api_key_is_refused():
    """`phc_` est la clé que PostHog met le plus en avant, et l'API de lecture la
    rejette par un 401 indistinguable d'une clé morte — d'où le refus ici."""
    with pytest.raises(ValueError, match="Clé de PROJET"):
        ph.PostHogClient(api_key="phc_abc")


def test_project_secret_key_is_refused():
    with pytest.raises(ValueError, match="Clé secrète de projet"):
        ph.PostHogClient(api_key="phs_abc")


def test_personal_key_is_accepted_and_sent_as_bearer():
    assert _client().session.headers["Authorization"] == "Bearer phx_test"


# --- région ------------------------------------------------------------------

def test_default_host_is_us_cloud():
    assert _client().host == "https://us.posthog.com"


def test_host_is_configurable_and_trailing_slash_is_dropped():
    assert _client(host="https://eu.posthog.com/").host == "https://eu.posthog.com"
    assert _client(host="https://ph.interne.acme.fr").host == "https://ph.interne.acme.fr"


def test_requests_go_to_the_configured_host(capture):
    _client(host="https://eu.posthog.com").list_events(limit=2)
    assert capture["url"].startswith("https://eu.posthog.com/api/projects/571144/events/")


# --- résolution du projet ------------------------------------------------------

def test_configured_project_id_wins_without_a_lookup(capture):
    _client(project_id=42).list_cohorts()
    assert "/api/projects/42/cohorts/" in capture["url"]


def test_project_id_is_discovered_from_the_key(monkeypatch):
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append(url)
        if url.endswith("/api/users/@me/"):
            return _Resp(200, {"organization": {"teams": [{"id": 999, "name": "P"}]}})
        return _Resp(200, {"results": []})

    monkeypatch.setattr(ph.requests.Session, "request", fake_request)
    c = ph.PostHogClient(api_key="phx_test")
    assert c.resolve_project_id() == "999"
    # mémorisé : un second appel ne re-sonde pas l'identité
    c.list_cohorts()
    assert sum(1 for u in calls if u.endswith("/api/users/@me/")) == 1


def test_a_key_that_sees_no_project_says_so(monkeypatch):
    monkeypatch.setattr(ph.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(200, {"organization": {"teams": []}}))
    with pytest.raises(ValueError, match="ne voit aucun projet"):
        ph.PostHogClient(api_key="phx_test").resolve_project_id()


# --- HogQL et requêtes typées --------------------------------------------------

def test_query_wraps_hogql_in_the_expected_envelope(capture):
    _client().query("SELECT count() FROM events")
    assert capture["method"] == "POST"
    assert capture["url"].endswith("/api/projects/571144/query/")
    assert capture["kwargs"]["json"] == {
        "query": {"kind": "HogQLQuery", "query": "SELECT count() FROM events"}}


def test_run_query_passes_a_named_kind_through(capture):
    q = {"kind": "FunnelsQuery", "series": [{"kind": "EventsNode", "event": "signup"}]}
    _client().run_query(q)
    assert capture["kwargs"]["json"] == {"query": q}


def test_run_query_refuses_a_query_without_a_kind():
    """Sans `kind`, PostHog rend une 400 obscure ; le dire ici est plus utile."""
    with pytest.raises(ValueError, match="kind"):
        _client().run_query({"query": "SELECT 1"})


def test_run_insight_replays_the_saved_query_with_an_overridden_window(monkeypatch):
    """« Notre entonnoir, mais sur la semaine dernière » : la DÉFINITION vient de
    l'équipe et le calcul de PostHog — jamais une reconstitution en HogQL, qui
    rendrait un nombre plausible en désaccord avec le tableau de bord."""
    seen = {}

    def fake_request(self, method, url, **kwargs):
        if method == "GET":
            return _Resp(200, {"id": 7, "query": {
                "kind": "InsightVizNode",
                "source": {"kind": "FunnelsQuery",
                           "dateRange": {"date_from": "-30d", "date_to": None}}}})
        seen.update(json=kwargs.get("json"))
        return _Resp(200, {"results": []})

    monkeypatch.setattr(ph.requests.Session, "request", fake_request)
    _client().run_insight(7, date_from="-7d")
    source = seen["json"]["query"]["source"]
    assert source["kind"] == "FunnelsQuery"
    assert source["dateRange"]["date_from"] == "-7d"
    # date_to non fourni ⇒ la valeur enregistrée est conservée
    assert source["dateRange"]["date_to"] is None


def test_run_insight_on_a_legacy_insight_explains_instead_of_failing_opaquely(monkeypatch):
    monkeypatch.setattr(ph.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(200, {"id": 7, "filters": {}}))
    with pytest.raises(ValueError, match="format hérité"):
        _client().run_insight(7)


def test_database_schema_uses_the_dedicated_kind(capture):
    _client().database_schema()
    assert capture["kwargs"]["json"] == {"query": {"kind": "DatabaseSchemaQuery"}}


# --- pagination + garde SSRF ---------------------------------------------------

def test_next_page_follows_a_url_on_the_configured_host(monkeypatch):
    seen = {}
    monkeypatch.setattr(ph.requests.Session, "get",
                        lambda self, url, **k: seen.update(url=url) or _Resp(200, {"results": []}))
    _client().next_page("https://us.posthog.com/api/projects/571144/insights/?offset=1")
    assert seen["url"].endswith("offset=1")


def test_next_page_refuses_a_url_off_host():
    """`next` vient de l'amont : le suivre sans le valider enverrait notre
    en-tête Authorization vers un hôte arbitraire (SSRF)."""
    with pytest.raises(ValueError, match="hors du host configuré"):
        _client().next_page("https://evil.example.com/api/projects/1/insights/")


def test_none_params_are_dropped(capture):
    _client().list_events(event=None, limit=5)
    assert capture["kwargs"]["params"] == {"limit": 5}


def test_timeout_is_always_bounded(capture):
    _client().list_events()
    assert capture["kwargs"]["timeout"] == ph._HTTP_TIMEOUT


# --- erreurs -------------------------------------------------------------------

def test_upstream_error_is_typed(monkeypatch):
    monkeypatch.setattr(ph.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(400, {
                            "type": "validation_error", "code": "hogql_query_error",
                            "detail": "Unable to resolve field: nope"}))
    with pytest.raises(UpstreamHTTPError) as e:
        _client().query("SELECT nope FROM events")
    assert e.value.status_code == 400
    assert e.value.service == "posthog"
    assert e.value.body["code"] == "hogql_query_error"


# --- groupes -------------------------------------------------------------------

def test_groups_require_the_type_index(capture):
    _client().list_groups(0, search="acme")
    assert capture["kwargs"]["params"] == {"group_type_index": 0, "search": "acme"}


# --- la propriété de sûreté ----------------------------------------------------

def test_no_product_changing_or_destructive_write_exists():
    """Basculer un feature flag change le produit pour de vrais utilisateurs, et
    supprimer une personne est irréversible (et réglementaire). Ces méthodes
    n'existent pas — pas seulement « non exposées » : les ajouter demande une PR
    ici. Même doctrine que StripeClient."""
    for absent in (
        "create_feature_flag", "update_feature_flag", "toggle_feature_flag",
        "delete_feature_flag", "create_experiment", "update_experiment",
        "delete_person", "split_person", "update_person_property",
        "create_insight", "update_insight", "delete_insight",
        "create_cohort", "update_cohort", "delete_cohort",
        "create_dashboard", "update_dashboard", "delete_dashboard",
        "delete_session_recording", "create_survey", "update_survey",
        "capture", "create_event", "batch_capture",
    ):
        assert not hasattr(ph.PostHogClient, absent), (
            f"PostHogClient.{absent} existe — écriture non prévue par ce connecteur.")


def test_the_only_write_is_the_annotation(capture):
    _client().create_annotation("déploiement v2.3", date_marker="2026-08-22T12:00:00Z")
    assert capture["method"] == "POST"
    assert capture["url"].endswith("/api/projects/571144/annotations/")
    assert capture["kwargs"]["json"] == {
        "content": "déploiement v2.3", "date_marker": "2026-08-22T12:00:00Z"}


def test_the_read_surface_that_answers_the_flagship_questions_exists():
    for present in (
        "query", "run_query", "run_insight", "database_schema",
        "list_event_definitions", "list_property_definitions",
        "list_events", "list_persons", "get_person",
        "list_insights", "get_insight", "list_dashboards",
        "list_feature_flags", "list_experiments",
        "list_cohorts", "list_cohort_persons",
        "list_session_recordings", "list_annotations",
        "list_group_types", "list_groups",
    ):
        assert callable(getattr(ph.PostHogClient, present, None)), f"{present} manquant"
