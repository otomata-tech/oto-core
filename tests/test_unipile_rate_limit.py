"""429 Unipile → `UnipileRateLimited` (quota amont, cf. incident 2026-07-21)."""
import pytest

from oto.tools.unipile.client import (
    UnipileClient, UnipileError, UnipileRateLimited, _parse_retry_after,
)


def test_parse_retry_after():
    assert _parse_retry_after("We only allow 100 requests. Retry in 12 hours.") == 12 * 3600
    assert _parse_retry_after("Retry in 30 minutes") == 30 * 60
    assert _parse_retry_after("Retry in 5 sec") == 5
    assert _parse_retry_after("aucun indice") is None
    assert _parse_retry_after("") is None


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = "raw"
        self.reason = "R"

    def json(self):
        return self._payload


def _client_with(resp):
    c = UnipileClient.__new__(UnipileClient)
    c._account_id = "acc_x"
    c.base_url = "https://api.unipile.com/v2"

    class _S:
        def request(self, *a, **k):
            return resp

    c.session = _S()
    return c


def test_429_raises_rate_limited_with_delay():
    resp = _Resp(429, {"object": "Error", "status": 429, "type": "api/too_many_requests",
                       "title": "We only allow 100 requests. Retry in 12 hours."})
    with pytest.raises(UnipileRateLimited) as ei:
        _client_with(resp)._request("GET", "/x")
    assert ei.value.status_code == 429
    assert ei.value.retry_after == 12 * 3600
    assert isinstance(ei.value, UnipileError)  # routable comme un UnipileError


def test_other_4xx_stays_plain_error():
    with pytest.raises(UnipileError) as ei:
        _client_with(_Resp(404, {"message": "nope"}))._request("GET", "/x")
    assert not isinstance(ei.value, UnipileRateLimited)
    assert ei.value.status_code == 404
