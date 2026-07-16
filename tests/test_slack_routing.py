"""Routage de token en lecture (deux tokens configurés).

Le connecteur garde bot ET user token et choisit le bon PAR OPÉRATION :
- lecture de canal (list channels, history d'un `C…`/`G…`) → **bot** (invité,
  garde les scopes du user token minimaux),
- lecture de DM (`open_dm`, history d'un `D…`), recherche → **user** (seul lui
  voit ses conversations ; `search:read` est user-token-only).
Avec un seul token configuré, la lecture retombe dessus.
"""
import pytest

from oto.tools.slack.client import SlackClient


def _capture(client):
    """Remplace _request par un espion qui capture le as_user résolu."""
    seen = {}

    def fake(method, endpoint, as_user=None, **kwargs):
        seen["as_user"] = as_user
        seen["endpoint"] = endpoint
        return {"ok": True, "messages": [], "channels": [], "channel": {"id": "D1"}}

    client._request = fake
    return seen


def test_both_tokens_route_channels_to_bot_dms_to_user():
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    seen = _capture(c)

    c.history("C123")
    assert seen["as_user"] is False          # canal → bot
    c.history("G123")
    assert seen["as_user"] is False          # canal privé → bot
    c.history("D999")
    assert seen["as_user"] is True           # DM → user

    c.list_channels("public_channel")
    assert seen["as_user"] is False
    c.list_channels("private_channel")
    assert seen["as_user"] is False
    c.list_channels("im")
    assert seen["as_user"] is True           # DM-only → user
    c.list_channels("im,mpim")
    assert seen["as_user"] is True
    c.list_channels("public_channel,im")
    assert seen["as_user"] is False          # dès qu'un canal est demandé → bot

    c.open_dm("U1")
    assert seen["as_user"] is True           # ton DM → user
    c.find_user_by_email("a@b.c")
    assert seen["as_user"] is False          # lookup → bot
    c.search_messages("hello")
    assert seen["as_user"] is True           # search:read → user only


def test_bot_only_falls_through_to_bot():
    # Construit avec les deux puis nullifie le user token (évite de capter un
    # SLACK_USER_TOKEN ambiant du vault via la résolution par secret).
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    c.user_token = None
    seen = _capture(c)
    c.history("D999")
    assert seen["as_user"] is False          # pas de user token → bot
    c.open_dm("U1")
    assert seen["as_user"] is False


def test_user_only_falls_through_to_user():
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    c.bot_token = None
    seen = _capture(c)
    c.history("C123")
    assert seen["as_user"] is True           # pas de bot token → user
    c.list_channels("public_channel")
    assert seen["as_user"] is True


def test_explicit_as_user_overrides_routing():
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    seen = _capture(c)
    c.history("C123", as_user=True)          # force user malgré le canal
    assert seen["as_user"] is True
    c.list_channels("im", as_user=False)     # force bot malgré im
    assert seen["as_user"] is False
