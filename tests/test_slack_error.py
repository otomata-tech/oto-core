"""SlackError : un `ok:false` Slack devient une erreur amont TYPÉE portant `.status`.

Contrat consommé en aval (oto-backend : calllog + tri Sentry) — un rejet client
(channel introuvable, droits, scope…) = 4xx amont, pas un bug backend ; un incident
Slack (internal_error…) = 5xx → reporté.
"""
import pytest

from oto.tools.slack.client import SlackError


def test_client_rejections_map_to_4xx():
    assert SlackError("channel_not_found").status == 404
    assert SlackError("not_in_channel").status == 403
    assert SlackError("invalid_auth").status == 401
    assert SlackError("ratelimited").status == 429


def test_slack_incidents_map_to_5xx():
    assert SlackError("internal_error").status == 502
    assert SlackError("service_unavailable").status == 503


def test_unknown_error_defaults_to_400_client():
    # Un code inconnu = rejet amont par défaut (4xx), pas un faux bug backend.
    assert SlackError("some_new_code").status == 400
    assert SlackError(None).status == 400


def test_carries_code_and_message():
    e = SlackError("channel_not_found")
    assert e.error == "channel_not_found"
    assert "channel_not_found" in str(e)
    assert isinstance(e, RuntimeError)
