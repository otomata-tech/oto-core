"""Contrat du client Monid (API `/v1`, Bearer, portefeuille prépayé).

Faux réseau posé sur `requests.Session.request`, faux temps posé sur `client.time`
(aucun test ne dort). Ce qui est figé : la clé ne voyage qu'en en-tête ; le
lancement d'un run se lit à la FORME du corps et jamais au code HTTP ; il n'est
jamais re-tenté, et une issue inconnue le dit (`may_have_run`) ; les lectures sont
re-tentées dans une borne ; l'attente d'un run s'arrête à son échéance et rend le
dernier état ; le coût se lit, il ne se recalcule pas.
"""
from __future__ import annotations

import inspect as pyinspect
import json
import re
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.monid import client as mo

KEY = "monid_live_s3cr3t-value"
BASE = "https://api.monid.ai"


class _Resp:
    """Réponse minimale : `json()` lève comme requests sur un corps non JSON."""

    def __init__(self, status_code=200, body=None, *, headers=None, raw=None):
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        self.text = raw if raw is not None else ("" if body is None else json.dumps(body))
        self.content = self.text.encode("utf-8")

    def json(self):
        return json.loads(self.text)


class _Net:
    """Rejoue des réponses dans l'ordre (ou `default`) et journalise chaque requête."""

    def __init__(self, clock):
        self.clock = clock
        self.replies = []
        self.calls = []
        self.default = lambda: _Resp(200, {})
        self.latency = 0.0

    def reply(self, *items):
        self.replies.extend(items)

    def __call__(self, session, method, url, **kwargs):
        self.calls.append(SimpleNamespace(method=method, url=url,
                                          headers=dict(session.headers), kwargs=kwargs))
        self.clock.now += self.latency
        item = self.replies.pop(0) if self.replies else self.default
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []
        self.woke_at = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:  # comme `time.sleep` : un délai négatif lève
            raise ValueError("sleep length must be non-negative")
        self.slept.append(seconds)
        self.now += seconds
        self.woke_at.append(self.now)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(mo, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Aucun test n'atteint le vrai transport : un garde local qui régresse sans le
    faux réseau `net` échoue ici, au lieu d'appeler l'API payante."""
    def refuse(self, method, url, **kwargs):
        raise AssertionError(f"requête réelle tentée : {method} {url}")

    monkeypatch.setattr(mo.requests.Session, "request", refuse)


@pytest.fixture()
def net(monkeypatch, clock, _no_network):
    fake = _Net(clock)

    def fake_request(self, method, url, **kwargs):
        return fake(self, method, url, **kwargs)

    monkeypatch.setattr(mo.requests.Session, "request", fake_request)
    return fake


def _client(**kw):
    return mo.MonidClient(api_key=KEY, **kw)


def _run(status="COMPLETED", http=200, **extra):
    body = {"runId": "01JRUN0000000000000000000", "provider": "example.io",
            "endpoint": "/v1/lookup", "status": status,
            "providerResponse": {"httpStatus": http}, "price": {"type": "PER_CALL"}}
    body.update(extra)
    return body


def _error(code, message="nope", **extra):
    return {"code": code, "message": message, **extra}


# --- auth et adresse ----------------------------------------------------------

def test_the_key_travels_as_a_bearer_header_never_in_the_url(net):
    net.default = lambda: _Resp(200, _run())
    c = _client()
    c.whoami()
    c.list_runs(limit=5, cursor="abc", status="running")
    c.discover("work email")
    c.run("example.io", "/v1/lookup", query_params={"q": "x"})
    for call in net.calls:
        assert call.headers["Authorization"] == f"Bearer {KEY}"
        assert call.headers["Accept"] == "application/json"
        assert KEY not in call.url
        assert KEY not in json.dumps(call.kwargs.get("params"))
        assert KEY not in json.dumps(call.kwargs.get("json"))


def test_no_workspace_or_client_header_is_sent(net):
    _client().whoami()
    sent = {k.lower() for k in net.calls[0].headers}
    assert "x-workspace-id" not in sent and "x-monid-client" not in sent


def test_the_key_is_read_from_the_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv("MONID_API_KEY", "monid_live_from_env")
    assert mo.MonidClient().api_key == "monid_live_from_env"


def test_base_url_defaults_to_the_api_host_and_an_override_is_stripped():
    assert _client().base_url == BASE == mo.DEFAULT_BASE_URL
    assert _client(base_url="https://monid.test/").base_url == "https://monid.test"


# --- verbe, chemin, corps -----------------------------------------------------

def test_whoami_and_wallet_are_plain_gets(net):
    c = _client()
    c.whoami()
    c.wallet_balance()
    assert [(x.method, x.url) for x in net.calls] == [
        ("GET", f"{BASE}/v1/auth/whoami"), ("GET", f"{BASE}/v1/wallet/balance")]
    for call in net.calls:
        assert call.kwargs["params"] is None and call.kwargs["json"] is None
        assert call.kwargs["timeout"] == mo._HTTP_TIMEOUT
        assert call.kwargs["allow_redirects"] is False


def test_discover_sends_q_and_camel_case_min_score(net):
    _client().discover("  work email  ", limit=5, category="lead-generation", min_score=0.4)
    call = net.calls[0]
    assert (call.method, call.url) == ("POST", f"{BASE}/v1/discover/endpoints")
    assert call.kwargs["json"] == {"q": "work email", "limit": 5,
                                   "category": "lead-generation", "minScore": 0.4}


def test_discover_drops_what_is_not_given(net):
    _client().discover("x")
    assert net.calls[0].kwargs["json"] == {"q": "x"}


def test_discover_accepts_its_bounds(net):
    _client().discover("x" * 1000, category="a" * 60, min_score=0)
    _client().discover("y", min_score=2)
    assert len(net.calls) == 2


def test_inspect_passes_provider_and_endpoint_verbatim(net):
    """Des slugs à points, un chemin à slash initial ou non : rien n'est réécrit."""
    _client().inspect("api.example.io", "/v1/people/linkedin/work-email")
    _client().inspect("bid.example", "search")
    assert net.calls[0].method == "POST" and net.calls[0].url == f"{BASE}/v1/inspect"
    assert net.calls[0].kwargs["json"] == {"provider": "api.example.io",
                                           "endpoint": "/v1/people/linkedin/work-email"}
    assert net.calls[1].kwargs["json"] == {"provider": "bid.example", "endpoint": "search"}


def test_run_sends_the_three_part_input(net):
    net.reply(_Resp(200, _run()))
    _client().run("example.io", "/v1/lookup", body={"a": 1},
                  query_params={"q": "x"}, path_params={"id": "7"})
    call = net.calls[0]
    assert (call.method, call.url) == ("POST", f"{BASE}/v1/run")
    assert call.kwargs["json"] == {
        "provider": "example.io", "endpoint": "/v1/lookup",
        "input": {"body": {"a": 1}, "queryParams": {"q": "x"}, "pathParams": {"id": "7"}}}


def test_run_carries_only_non_empty_parts_and_omits_an_empty_input(net):
    net.reply(_Resp(200, _run()), _Resp(200, _run()))
    c = _client()
    c.run("example.io", "/v1/lookup", body={}, query_params={"q": "x"}, path_params=None)
    c.run("example.io", "/v1/lookup", body={}, query_params={}, path_params={})
    assert net.calls[0].kwargs["json"]["input"] == {"queryParams": {"q": "x"}}
    assert "input" not in net.calls[1].kwargs["json"]


def test_run_read_budget_is_the_timeout_keyword(net):
    net.reply(_Resp(200, _run()), _Resp(200, _run()))
    _client().run("example.io", "/v1/lookup")
    _client().run("example.io", "/v1/lookup", timeout=25)
    assert net.calls[0].kwargs["timeout"] == (10, mo.RUN_READ_TIMEOUT) == (10, 60)
    assert net.calls[1].kwargs["timeout"] == (10, 25)


def test_runs_routes(net):
    c = _client()
    c.get_run("01JRUN")
    c.list_runs()
    c.list_runs(limit=20, cursor="abc", status="COMPLETED")
    net.reply(_Resp(202, {"runId": "01JRUN", "status": "STOPPING", "message": "ok"}))
    c.stop_run("01JRUN")
    got = [(x.method, x.url, x.kwargs["params"], x.kwargs["json"]) for x in net.calls]
    assert got == [
        ("GET", f"{BASE}/v1/runs/01JRUN", None, None),
        ("GET", f"{BASE}/v1/runs", None, None),
        ("GET", f"{BASE}/v1/runs", {"limit": 20, "cursor": "abc", "status": "COMPLETED"}, None),
        ("POST", f"{BASE}/v1/runs/01JRUN/stop", None, None),
    ]


def test_stop_run_returns_the_accepted_body(net):
    accepted = {"runId": "01JRUN", "status": "STOPPING", "message": "Stop requested"}
    net.reply(_Resp(202, accepted))
    assert _client().stop_run("01JRUN") == accepted


def test_an_empty_2xx_body_is_an_empty_dict(net):
    net.reply(_Resp(202))
    assert _client().stop_run("01JRUN") == {}


@pytest.mark.parametrize("hostile, escaped", [
    ("a/b", "a%2Fb"), ("r?admin=1", "r%3Fadmin%3D1"), ("r#frag", "r%23frag"),
    ("../../wallet/balance", "..%2F..%2Fwallet%2Fbalance"),
])
def test_run_ids_are_escaped_in_the_path(net, hostile, escaped):
    net.default = lambda: _Resp(200, _run())
    c = _client()
    c.get_run(hostile)
    c.stop_run(hostile)
    c.wait_for_run(hostile, max_wait_s=0)
    assert [x.url for x in net.calls] == [
        f"{BASE}/v1/runs/{escaped}", f"{BASE}/v1/runs/{escaped}/stop",
        f"{BASE}/v1/runs/{escaped}"]
    # Le faux réseau voit l'URL AVANT sa préparation : la préparer ne doit rien résoudre.
    for x in net.calls:
        assert requests.Request(x.method, x.url).prepare().url == x.url


@pytest.mark.parametrize("dot", [".", ".."])
def test_a_dot_run_id_would_be_resolved_to_another_route(dot):
    """Pourquoi `.` et `..` sont refusés : `quote` les laisse, la préparation les résout."""
    url = f"{BASE}/v1/runs/{quote(dot, safe='')}"
    assert requests.Request("GET", url).prepare().url != url


# --- refus locaux : rien ne part ----------------------------------------------

_REFUSALS = [
    lambda c: c.discover(""), lambda c: c.discover("   "),
    lambda c: c.discover("x" * 1001), lambda c: c.discover(123),
    lambda c: c.discover("x", limit=0), lambda c: c.discover("x", limit=True),
    lambda c: c.discover("x", limit=2.5), lambda c: c.discover("x", limit="5"),
    lambda c: c.discover("x", category="Lead Gen"), lambda c: c.discover("x", category="lead_gen"),
    lambda c: c.discover("x", category="a" * 61), lambda c: c.discover("x", category="-lead"),
    lambda c: c.discover("x", category="lead\n"),
    lambda c: c.discover("x", category="lead-generation\n"),
    lambda c: c.discover("x", min_score=-0.1), lambda c: c.discover("x", min_score=2.1),
    lambda c: c.discover("x", min_score=True), lambda c: c.discover("x", min_score="0.5"),
    lambda c: c.discover("x", min_score=float("nan")),
    lambda c: c.inspect("", "/x"), lambda c: c.inspect("  ", "/x"),
    lambda c: c.inspect("p", ""), lambda c: c.inspect("p", None),
    lambda c: c.run("", "/x"), lambda c: c.run("p", " "),
    lambda c: c.run("p", "/x", body=[1]), lambda c: c.run("p", "/x", query_params="a=b"),
    lambda c: c.run("p", "/x", path_params=("id",)),
    lambda c: c.run("p", "/x", timeout=0), lambda c: c.run("p", "/x", timeout=-1),
    lambda c: c.run("p", "/x", timeout=True), lambda c: c.run("p", "/x", timeout="60"),
    lambda c: c.get_run(""), lambda c: c.get_run(None), lambda c: c.get_run(42),
    lambda c: c.stop_run(""), lambda c: c.stop_run(42),
    lambda c: c.get_run("."), lambda c: c.get_run(".."),
    lambda c: c.stop_run("."), lambda c: c.stop_run(".."),
    lambda c: c.wait_for_run(".", max_wait_s=1), lambda c: c.wait_for_run("..", max_wait_s=1),
    lambda c: c.list_runs(limit=0), lambda c: c.list_runs(limit=101),
    lambda c: c.list_runs(limit=True), lambda c: c.list_runs(status="DONE"),
    lambda c: c.list_runs(status=""), lambda c: c.list_runs(status=3),
    lambda c: c.list_runs(cursor=5),
    lambda c: c.wait_for_run("", max_wait_s=1),
    lambda c: c.wait_for_run("r", max_wait_s=-1),
    lambda c: c.wait_for_run("r", max_wait_s=301),
    lambda c: c.wait_for_run("r", max_wait_s=True),
    lambda c: c.wait_for_run("r", max_wait_s=5, poll_initial=0),
    lambda c: c.wait_for_run("r", max_wait_s=5, poll_initial=6, poll_max=5),
    lambda c: c.wait_for_run("r", max_wait_s=5, poll_max=float("inf")),
]


@pytest.mark.parametrize("call", _REFUSALS)
def test_a_local_refusal_sends_nothing(net, call):
    with pytest.raises(ValueError):
        call(_client())
    assert net.calls == []


def test_list_runs_uppercases_the_status(net):
    _client().list_runs(status=" timed_out ")
    assert net.calls[0].kwargs["params"] == {"status": "TIMED_OUT"}


def test_an_unknown_status_is_refused_with_the_valid_ones_named(net):
    with pytest.raises(ValueError) as exc:
        _client().list_runs(status="done")
    for status in mo.RUN_STATUSES:
        assert status in str(exc.value)
    assert net.calls == []


def test_refusals_prescribe_no_tool_name(net):
    """Un refus d'oto-core dit le fait, jamais un nom d'outil ou de famille `xxx_*`."""
    pattern = re.compile(r"(?<![\w`])[a-z]+_(?:[a-z_]+|\*)")
    messages = []
    for call in (lambda c: c.discover(""), lambda c: c.discover("x", min_score=9),
                 lambda c: c.discover("x", category="lead\n"),
                 lambda c: c.list_runs(status="x"), lambda c: c.run("p", "/x", body=[1]),
                 lambda c: c.wait_for_run("r", max_wait_s=5, poll_initial=0),
                 lambda c: c.wait_for_run("r", max_wait_s=999), lambda c: c.get_run(""),
                 lambda c: c.get_run("..")):
        with pytest.raises(ValueError) as exc:
            call(_client())
        messages.append(str(exc.value))
    assert net.calls == []
    messages.append(mo._unknown_outcome("HTTP 504 sans corps JSON"))
    net.reply(requests.exceptions.ConnectionError("refused"))
    with pytest.raises(mo.MonidProtocolError) as lost:
        _client().run("example.io", "/v1/lookup")
    messages.append(str(lost.value))
    for message in messages:
        assert not pattern.search(message), message
        assert KEY not in message


# --- le lancement se lit à la forme du corps ----------------------------------

@pytest.mark.parametrize("status, body", [
    (200, _run("COMPLETED", 200)),
    (200, _run("BLOCKED", None, reason="Workspace budget exceeded", controls=[])),
    (202, {"runId": "01JRUN", "provider": "example.io", "endpoint": "/v1/lookup",
           "status": "READY", "price": {}, "createdAt": "2026-09-12T00:00:00Z"}),
    (408, _run("TIMED_OUT", None)),
    (502, _run("COMPLETED", 402, providerResponse={"httpStatus": 402, "error": "quota"})),
    (404, _run("COMPLETED", 404)),
    (504, _run("COMPLETED", 504)),
    (429, _run("COMPLETED", 429)),
])
def test_run_returns_a_run_body_whatever_the_http_status(net, status, body):
    net.reply(_Resp(status, body))
    assert _client().run("example.io", "/v1/lookup") == body


def test_a_monid_402_envelope_is_raised_with_its_request_id(net):
    net.reply(_Resp(402, _error(402, "Insufficient wallet balance"),
                    headers={"x-request-id": "req-402"}))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().run("example.io", "/v1/lookup")
    err = exc.value
    assert isinstance(err, UpstreamHTTPError)
    assert (err.status_code, err.request_id, err.may_have_run) == (402, "req-402", False)
    assert err.upstream_message == "Insufficient wallet balance"
    assert err.body == _error(402, "Insufficient wallet balance")


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_4xx_envelope_on_run_did_not_run(net, status):
    net.reply(_Resp(status, _error(status)))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().run("example.io", "/v1/lookup")
    assert exc.value.status_code == status and exc.value.may_have_run is False


@pytest.mark.parametrize("status", [500, 503])
def test_a_5xx_envelope_on_run_may_have_run(net, status):
    net.reply(_Resp(status, _error(status, "Internal server error"),
                    headers={"x-request-id": "req-5xx"}))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().run("example.io", "/v1/lookup")
    assert exc.value.may_have_run is True and exc.value.request_id == "req-5xx"
    assert mo.MonidHTTPError(500).may_have_run is False  # attribut d'instance, pas de classe


@pytest.mark.parametrize("resp", [
    _Resp(504, raw="<html>Gateway Timeout</html>", headers={"Content-Type": "text/html",
                                                            "x-request-id": "req-cf"}),
    _Resp(200, raw="<html>oops</html>", headers={"Content-Type": "text/html"}),
    _Resp(200),
    _Resp(200, {"ok": True}),
])
def test_an_unreadable_run_response_is_an_unknown_outcome(net, resp):
    net.reply(resp)
    with pytest.raises(mo.MonidProtocolError) as exc:
        _client().run("example.io", "/v1/lookup")
    assert exc.value.may_have_run is True
    assert not isinstance(exc.value, ValueError)
    assert exc.value.request_id == resp.headers.get("x-request-id")


@pytest.mark.parametrize("failure", [
    requests.exceptions.ReadTimeout("read timed out"),
    requests.exceptions.ConnectionError("connection reset"),
    requests.exceptions.ChunkedEncodingError("truncated"),
    requests.exceptions.ContentDecodingError("failed to decode gzip"),
])
def test_a_run_whose_response_was_lost_may_have_run(net, failure):
    net.reply(failure)
    with pytest.raises(mo.MonidProtocolError) as exc:
        _client().run("example.io", "/v1/lookup")
    assert exc.value.may_have_run is True
    assert exc.value.__cause__ is failure
    assert len(net.calls) == 1


def test_a_connect_timeout_on_run_propagates_unchanged(net):
    """Rien n'est parti : ce n'est pas une issue inconnue."""
    failure = requests.exceptions.ConnectTimeout("connect timed out")
    net.reply(failure)
    with pytest.raises(requests.exceptions.ConnectTimeout) as exc:
        _client().run("example.io", "/v1/lookup")
    assert exc.value is failure


def test_a_redirect_on_run_is_refused_and_did_not_run(net):
    net.reply(_Resp(307, headers={"Location": "https://elsewhere.test/v1/run"}))
    with pytest.raises(mo.MonidProtocolError) as exc:
        _client().run("example.io", "/v1/lookup")
    assert exc.value.may_have_run is False
    assert net.calls[0].kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status", [302, 307])
def test_a_mirrored_provider_3xx_carrying_a_run_is_returned_not_refused(net, status):
    """Un run `COMPLETED` dont le fournisseur a répondu 3xx revient avec ce code : il a
    tourné. La redirection n'est pas suivie, mais le corps se lit avant de la refuser."""
    body = _run("COMPLETED", status)
    net.reply(_Resp(status, body, headers={"Location": "https://elsewhere.test/"}))
    assert _client().run("example.io", "/v1/lookup") == body
    assert len(net.calls) == 1 and net.calls[0].kwargs["allow_redirects"] is False


# --- ce qui n'est jamais re-tenté ----------------------------------------------

@pytest.mark.parametrize("resp", [
    _Resp(502, _error(502)), _Resp(503, _error(503), headers={"Retry-After": "1"}),
    _Resp(500, _error(500)), _Resp(429, _error(429), headers={"Retry-After": "1"}),
    _Resp(502, _run("COMPLETED", 402)), _Resp(504, _run("COMPLETED", 504)),
])
def test_run_is_never_retried(net, clock, resp):
    net.default = resp
    try:
        _client().run("example.io", "/v1/lookup")
    except mo.MonidHTTPError:
        pass
    assert len(net.calls) == 1 and clock.slept == []


@pytest.mark.parametrize("status", [502, 503])
def test_stop_run_is_never_retried(net, clock, status):
    net.default = _Resp(status, _error(status), headers={"Retry-After": "1"})
    with pytest.raises(mo.MonidHTTPError):
        _client().stop_run("01JRUN")
    assert len(net.calls) == 1 and clock.slept == []


def test_stop_run_409_is_raised(net):
    net.reply(_Resp(409, _error(409, "Run is not stoppable")))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().stop_run("01JRUN")
    assert exc.value.status_code == 409


def test_a_post_read_is_not_retried_either(net, clock):
    net.default = _Resp(503, _error(503))
    with pytest.raises(mo.MonidHTTPError):
        _client().discover("x")
    assert len(net.calls) == 1 and clock.slept == []


# --- lectures : re-tentées, dans une borne -------------------------------------

def test_a_read_is_retried_on_429_and_503_honouring_retry_after(net, clock):
    net.reply(_Resp(429, _error(429), headers={"Retry-After": "3"}),
              _Resp(503, _error(503), headers={"Retry-After": "1.5"}),
              _Resp(200, {"items": []}))
    assert _client().list_runs() == {"items": []}
    assert len(net.calls) == 3 and clock.slept == [3.0, 1.5]


def test_an_unreadable_retry_after_falls_back_to_exponential_backoff(net, clock):
    net.reply(_Resp(503, _error(503), headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
              _Resp(502, raw="bad gateway"),
              _Resp(200, {"user": {}}))
    assert _client().whoami() == {"user": {}}
    assert clock.slept == [1.0, 2.0]


def test_a_read_gives_up_after_its_attempts(net, clock):
    net.default = _Resp(500, _error(500))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().get_run("01JRUN")
    assert exc.value.status_code == 500
    assert len(net.calls) == mo._MAX_ATTEMPTS and len(clock.slept) == mo._MAX_ATTEMPTS - 1


def test_a_long_retry_after_is_raised_not_slept(net, clock):
    wallet = {"code": 503, "message": "Wallet is provisioning", "walletStatus": "PROVISIONING"}
    net.reply(_Resp(503, wallet, headers={"Retry-After": "120", "x-request-id": "req-w"}))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().wallet_balance()
    assert exc.value.retry_after == 120.0 and exc.value.request_id == "req-w"
    assert exc.value.body["walletStatus"] == "PROVISIONING"
    assert clock.slept == [] and len(net.calls) == 1


def test_a_short_wallet_503_is_absorbed(net, clock):
    balance = {"balance": {"value": -1.5, "currency": "USD"}, "held": {"value": 2, "currency": "USD"}}
    net.reply(_Resp(503, _error(503), headers={"Retry-After": "2"}), _Resp(200, balance))
    assert _client().wallet_balance() == balance
    assert clock.slept == [2.0]


@pytest.mark.parametrize("wallet_status", ["FAILED", None])
def test_a_wallet_503_without_retry_after_is_raised_at_once(net, clock, wallet_status):
    """Contrat : l'état transitoire porte `Retry-After` ; sans lui (portefeuille en échec,
    ou `walletStatus: null` = pas encore créé), re-tenter ne répare rien."""
    net.default = _Resp(503, _error(503, "Wallet unavailable", walletStatus=wallet_status))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().wallet_balance()
    assert exc.value.status_code == 503 and exc.value.retry_after is None
    assert len(net.calls) == 1 and clock.slept == []


@pytest.mark.parametrize("resp", [
    _Resp(503, raw="Service Unavailable", headers={"Content-Type": "text/plain"}),
    _Resp(503, _error(503, "Service Unavailable")),
])
def test_an_infrastructure_503_without_retry_after_is_still_retried(net, clock, resp):
    net.default = resp
    with pytest.raises(mo.MonidHTTPError):
        _client().wallet_balance()
    assert len(net.calls) == 3 and clock.slept == [1.0, 2.0]


def test_a_retry_after_at_the_bound_is_slept(net, clock):
    net.reply(_Resp(503, _error(503), headers={"Retry-After": "15"}), _Resp(200, {"balance": {}}))
    assert _client().wallet_balance() == {"balance": {}}
    assert clock.slept == [15.0]


def test_a_retry_after_just_past_the_bound_is_raised(net, clock):
    net.reply(_Resp(503, _error(503), headers={"Retry-After": "16"}))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().wallet_balance()
    assert exc.value.retry_after == 16.0
    assert clock.slept == [] and len(net.calls) == 1


def test_a_negative_retry_after_is_clamped_to_zero(net, clock):
    """`time.sleep(-3)` lèverait `ValueError`, que l'aval lirait comme un refus d'arguments."""
    net.reply(_Resp(503, _error(503), headers={"Retry-After": "-3"}), _Resp(200, {"balance": {}}))
    assert _client().wallet_balance() == {"balance": {}}
    assert len(net.calls) == 2 and clock.slept == [0.0]


def test_the_bounds_are_the_spec_ones():
    """Figés en littéraux : le budget de temps de la face servie repose sur ces nombres."""
    assert (mo._HTTP_TIMEOUT, mo._MAX_ATTEMPTS, mo._RETRY_AFTER_MAX, mo._MAX_WAIT_S,
            mo.RUN_READ_TIMEOUT) == ((10, 30), 3, 15, 300, 60)


def test_a_client_error_on_a_read_is_not_retried(net, clock):
    net.default = _Resp(404, _error(404, "Run not found"))
    with pytest.raises(mo.MonidHTTPError):
        _client().get_run("01JRUN")
    assert len(net.calls) == 1 and clock.slept == []


def test_a_non_json_2xx_read_is_a_protocol_error_not_a_value_error(net):
    net.reply(_Resp(200, raw="<!doctype html><title>Monid</title>",
                    headers={"Content-Type": "text/html"}))
    with pytest.raises(mo.MonidProtocolError) as exc:
        _client().whoami()
    assert not isinstance(exc.value, ValueError)


def test_a_redirect_is_never_followed(net):
    net.reply(_Resp(302, headers={"Location": "https://login.elsewhere.test/"}))
    with pytest.raises(mo.MonidProtocolError):
        _client().whoami()
    assert len(net.calls) == 1 and net.calls[0].kwargs["allow_redirects"] is False


# --- l'erreur HTTP porte ce qu'il faut ----------------------------------------

def test_http_error_reads_both_envelope_shapes(net):
    net.reply(_Resp(401, _error(401, "Invalid API key format"), headers={"X-Request-Id": "r1"}),
              _Resp(403, {"error": {"message": "No workspace", "code": "FORBIDDEN"}}),
              _Resp(402, _error(402, "Wallet", errorCode="X402_ACCRUING_COST")))
    c = _client()
    errors = []
    for _ in range(3):
        with pytest.raises(mo.MonidHTTPError) as exc:
            c.whoami()
        errors.append(exc.value)
    assert (errors[0].upstream_message, errors[0].request_id, errors[0].error_code) == (
        "Invalid API key format", "r1", None)
    assert errors[1].upstream_message == "No workspace"
    assert errors[2].error_code == "X402_ACCRUING_COST"
    for err in errors:
        assert KEY not in str(err) and str(err).startswith(f"monid HTTP {err.status_code}: ")


def test_an_error_code_outside_the_registry_counts_as_absent(net):
    """Le contrat : une valeur hors du registre `ApiErrorCode` se traite comme absente."""
    net.reply(_Resp(402, _error(402, "Wallet", errorCode="SOMETHING_NEW")))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().whoami()
    assert exc.value.error_code is None
    assert exc.value.body["errorCode"] == "SOMETHING_NEW"


def test_a_non_json_error_keeps_its_text_but_bounds_the_message(net):
    page = "<html>" + "x" * 5000 + "</html>"
    net.reply(_Resp(403, raw=page))
    with pytest.raises(mo.MonidHTTPError) as exc:
        _client().inspect("example.io", "/v1/lookup")
    assert exc.value.body == page and exc.value.upstream_message is None
    assert len(str(exc.value)) < 400


# --- attendre un run : borné, et le dernier état ------------------------------

def test_wait_for_run_returns_as_soon_as_the_run_is_terminal(net, clock):
    net.reply(_Resp(200, _run("READY")), _Resp(200, _run("RUNNING")),
              _Resp(200, _run("STOPPED")))
    out = _client().wait_for_run("01JRUN", max_wait_s=60)
    assert out["status"] == "STOPPED"
    assert len(net.calls) == 3 and clock.slept == [1.0, 1.5]


def test_wait_for_run_returns_the_last_state_at_the_deadline_without_oversleeping(net, clock):
    polls = iter(range(1, 1000))
    net.default = lambda: _Resp(200, _run("RUNNING", poll=next(polls)))
    net.latency = 0.4
    start = clock.now
    out = _client().wait_for_run("01JRUN", max_wait_s=10, poll_initial=1, poll_max=5)
    assert out["status"] == "RUNNING" and out["poll"] == len(net.calls)
    assert sum(clock.slept) <= 10
    assert all(t <= start + 10 + 1e-9 for t in clock.woke_at)
    assert clock.now <= start + 10 + net.latency + 1e-9


def test_wait_for_run_backoff_is_capped(net, clock):
    net.default = lambda: _Resp(200, _run("RUNNING"))
    _client().wait_for_run("01JRUN", max_wait_s=30, poll_initial=1, poll_max=2)
    assert clock.slept[:3] == [1.0, 1.5, 2.0]
    assert max(clock.slept) == 2.0
    # la dernière attente est rognée à l'échéance, pas prise en entier
    assert clock.slept[-1] == pytest.approx(1.5) and sum(clock.slept) == pytest.approx(30)


def test_wait_for_run_with_no_budget_polls_once(net, clock):
    net.default = lambda: _Resp(200, _run("RUNNING"))
    assert _client().wait_for_run("01JRUN", max_wait_s=0)["status"] == "RUNNING"
    assert len(net.calls) == 1 and clock.slept == []


def test_wait_for_run_polls_without_retry_and_with_a_bounded_read(net, clock):
    net.reply(_Resp(200, _run("RUNNING")), _Resp(503, _error(503), headers={"Retry-After": "1"}))
    with pytest.raises(mo.MonidHTTPError):
        _client().wait_for_run("01JRUN", max_wait_s=60, poll_initial=5)
    assert len(net.calls) == 2 and clock.slept == [5.0]
    assert net.calls[0].kwargs["timeout"] == (5, 30.0)


def test_wait_for_run_survives_a_malformed_status_and_returns_the_last_state(net, clock):
    """Un `status` non chaîne n'est pas final et ne lève pas de `TypeError`."""
    net.default = lambda: _Resp(200, _run(["COMPLETED"]))
    out = _client().wait_for_run("01JRUN", max_wait_s=3)
    assert out["status"] == ["COMPLETED"] and len(net.calls) > 1
    assert sum(clock.slept) == pytest.approx(3)


def test_wait_for_run_read_budget_shrinks_to_its_floor_near_the_deadline(net, clock):
    net.default = lambda: _Resp(200, _run("RUNNING"))
    _client().wait_for_run("01JRUN", max_wait_s=12, poll_initial=5, poll_max=5)
    reads = [call.kwargs["timeout"] for call in net.calls]
    assert reads[0] == (5, 12.0) and reads[-1] == (5, 5.0)


# --- helpers purs --------------------------------------------------------------

@pytest.mark.parametrize("run, expected", [
    ({"cost": {"value": 0.15, "currency": "USD"}}, 0.15),
    ({"cost": {"value": 0, "currency": "USD"}}, 0.0),
    ({"cost": {"value": 0.2, "currency": "USD"},
      "billing": {"reportedCost": {"value": 9, "unit": "DOLLAR", "currency": "USD"}}}, 0.2),
    ({"billing": {"reportedCost": {"value": 3000, "unit": "MICRO_DOLLAR", "currency": "USD"}}}, 0.003),
    ({"billing": {"reportedCost": {"value": 25, "unit": "CENT", "currency": "USD"}}}, 0.25),
    ({"billing": {"reportedCost": {"value": 2, "unit": "DOLLAR", "currency": "USD"}}}, 2.0),
    ({"price": {"type": "PER_CALL", "amount": {"value": 0.1}}}, None),
    ({"billing": {"reportedCost": {"value": 5, "unit": "SATOSHI", "currency": "USD"}}}, None),
    ({"cost": {"value": True, "currency": "USD"}}, None),
    ({"cost": {"value": 1, "currency": "EUR"}}, None),
    ({"cost": {"value": 1, "currency": "EUR"},
      "billing": {"reportedCost": {"value": 25, "unit": "CENT", "currency": "USD"}}}, 0.25),
    ({"billing": {"reportedCost": {"value": 25, "unit": "CENT", "currency": "EUR"}}}, None),
    ({}, None),
    (None, None),
])
def test_run_cost_is_read_never_recomputed(run, expected):
    got = mo.run_cost_usd(run)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_is_terminal_is_the_five_final_statuses_case_sensitive():
    assert {s for s in mo.RUN_STATUSES if mo.is_terminal({"status": s})} == set(mo.TERMINAL_STATUSES)
    assert set(mo.RUN_STATUSES) - mo.TERMINAL_STATUSES == {"READY", "RUNNING", "STOPPING"}
    assert not mo.is_terminal({"status": "completed"})
    assert not mo.is_terminal({}) and not mo.is_terminal(None)
    assert not mo.is_terminal({"status": ["COMPLETED"]})
    assert not mo.is_terminal({"status": {"s": "COMPLETED"}})


# --- la surface --------------------------------------------------------------

def test_the_api_surface_is_not_invented():
    """Budgets, plafonds, ressources, clés d'API, recharge : délibérément absents."""
    public = {n for n in dir(mo.MonidClient) if not n.startswith("_")}
    assert public == {"whoami", "wallet_balance", "discover", "inspect", "run", "get_run",
                      "wait_for_run", "list_runs", "stop_run"}


def test_the_public_signatures_are_the_ones_the_backend_calls():
    def shape(fn):
        return [(p.name, p.kind.name, p.default) for p in pyinspect.signature(fn).parameters.values()]

    empty = pyinspect.Parameter.empty
    C = mo.MonidClient
    assert shape(C.__init__) == [("self", "POSITIONAL_OR_KEYWORD", empty),
                                 ("api_key", "POSITIONAL_OR_KEYWORD", None),
                                 ("base_url", "POSITIONAL_OR_KEYWORD", None)]
    assert shape(C.discover)[1:] == [("q", "POSITIONAL_OR_KEYWORD", empty),
                                     ("limit", "KEYWORD_ONLY", None),
                                     ("category", "KEYWORD_ONLY", None),
                                     ("min_score", "KEYWORD_ONLY", None)]
    assert shape(C.run)[1:] == [("provider", "POSITIONAL_OR_KEYWORD", empty),
                                ("endpoint", "POSITIONAL_OR_KEYWORD", empty),
                                ("body", "KEYWORD_ONLY", None),
                                ("query_params", "KEYWORD_ONLY", None),
                                ("path_params", "KEYWORD_ONLY", None),
                                ("timeout", "KEYWORD_ONLY", 60)]
    assert shape(C.wait_for_run)[1:] == [("run_id", "POSITIONAL_OR_KEYWORD", empty),
                                         ("max_wait_s", "KEYWORD_ONLY", empty),
                                         ("poll_initial", "KEYWORD_ONLY", 1.0),
                                         ("poll_max", "KEYWORD_ONLY", 5.0)]
    assert shape(C.list_runs)[1:] == [("limit", "KEYWORD_ONLY", None),
                                      ("cursor", "KEYWORD_ONLY", None),
                                      ("status", "KEYWORD_ONLY", None)]
    for name in ("get_run", "stop_run"):
        assert shape(getattr(C, name))[1:] == [("run_id", "POSITIONAL_OR_KEYWORD", empty)]
    assert shape(C.inspect)[1:] == [("provider", "POSITIONAL_OR_KEYWORD", empty),
                                    ("endpoint", "POSITIONAL_OR_KEYWORD", empty)]


def test_the_package_reexports_the_client_surface():
    from oto.tools import monid
    for name in ("MonidClient", "MonidHTTPError", "MonidProtocolError", "is_terminal",
                 "run_cost_usd", "RUN_STATUSES", "TERMINAL_STATUSES"):
        assert getattr(monid, name) is getattr(mo, name)
