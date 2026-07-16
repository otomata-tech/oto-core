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


def test_fetch_file_prefers_user_token_and_returns_bytes(monkeypatch):
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    c.file_info = lambda fid: {"file": {
        "url_private_download": "https://files.slack.com/x",
        "name": "plan.md", "mimetype": "text/markdown"}}
    seen = {}

    class Resp:
        content = b"# plan"
        def raise_for_status(self):
            return None

    def fake_get(url, headers=None, **kw):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        return Resp()

    monkeypatch.setattr("oto.tools.slack.client.requests.get", fake_get)
    blob = c.fetch_file("F1")
    assert blob == {"data": b"# plan", "filename": "plan.md", "mimetype": "text/markdown"}
    assert seen["url"] == "https://files.slack.com/x"
    assert seen["auth"] == "Bearer xoxp-1"      # user token prime


def test_fetch_file_falls_back_to_bot_token(monkeypatch):
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    c.user_token = None
    c.file_info = lambda fid: {"file": {"url_private": "https://files.slack.com/y", "name": "a.png"}}
    seen = {}

    class Resp:
        content = b"\x89PNG"
        def raise_for_status(self):
            return None

    monkeypatch.setattr("oto.tools.slack.client.requests.get",
                        lambda url, headers=None, **kw: (seen.update(auth=headers["Authorization"]) or Resp()))
    blob = c.fetch_file("F2")
    assert blob["mimetype"] == "application/octet-stream"   # défaut si absent
    assert seen["auth"] == "Bearer xoxb-1"


def test_explicit_as_user_overrides_routing():
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    seen = _capture(c)
    c.history("C123", as_user=True)          # force user malgré le canal
    assert seen["as_user"] is True
    c.list_channels("im", as_user=False)     # force bot malgré im
    assert seen["as_user"] is False
