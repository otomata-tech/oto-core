"""`conversations.replies` — lire les RÉPONSES d'un fil (signaux #567/#576/#584/#592).

`conversations.history` ne rend que le premier niveau : sur un parent il annonce
`reply_count`, `reply_users`, `latest_reply`, mais **jamais un corps de réponse**.
Or une décision ou un désaccord vit presque toujours dans le fil — quatre signaux
en huit jours, trois personnes, même manque.

Le contrat encodé ici a été SONDÉ en direct sur l'API Slack le 2026-08-28 (workspace
Otomata Community, canal privé C089SSRH7LG, fil ts=1787217054.283119, 7 réponses) —
pas déduit de la doc. Ce que la sonde a établi, et que ces tests figent :

- le paramètre s'appelle **`ts`** : `thread_ts=` seul est rejeté (`invalid_arguments`) ;
- Slack **AVALE un paramètre inconnu en rendant ok:true** (`zzz_inconnu=x` → 8 messages,
  soit exactement le résultat nu) : un filtre inventé y ressemble à un filtre qui marche.
  D'où la preuve par DIFFÉRENTIEL — `limit=2` → 3 messages, `limit="absurde"` → 400
  `invalid_arguments` : le paramètre est bien reconnu ;
- `oldest`/`latest` sont des bornes **exclusives**, `inclusive=true` les inclut ;
- le **parent est TOUJOURS renvoyé en `messages[0]`**, et il est répété à CHAQUE page.
"""
import pytest

from oto.tools.slack.client import SlackClient, SlackError


def _capture(client, payload=None):
    seen = {}

    def fake(method, endpoint, as_user=None, **kwargs):
        seen.update(method=method, endpoint=endpoint, as_user=as_user, **kwargs)
        return payload or {"ok": True, "messages": []}

    client._request = fake
    return seen


def test_replies_appelle_conversations_replies_avec_ts():
    # `thread_ts=` seul est refusé par Slack (invalid_arguments, sondé le 28/08) :
    # l'appelant parle de `thread_ts` (c'est le nom que porte le message), le
    # transport doit émettre `ts`.
    c = SlackClient(bot_token="xoxb-1")
    seen = _capture(c)
    c.replies("C123", "1787217054.283119")
    assert seen["endpoint"] == "conversations.replies"
    assert seen["method"] == "GET"
    assert seen["params"]["channel"] == "C123"
    assert seen["params"]["ts"] == "1787217054.283119"
    assert "thread_ts" not in seen["params"]


def test_replies_rend_les_corps_des_reponses():
    """Le manque signalé quatre fois : history prouve qu'une réponse existe
    (`reply_count`) sans en rendre un mot."""
    c = SlackClient(bot_token="xoxb-1")
    _capture(c, {"ok": True, "has_more": False, "messages": [
        {"ts": "1787217054.283119", "text": "parent", "reply_count": 2},
        {"ts": "1787218336.039429", "text": "J'ai testé, c'est cool", "thread_ts": "1787217054.283119"},
        {"ts": "1787218733.026749", "text": "A voir comment se démarquer", "thread_ts": "1787217054.283119"},
    ]})
    out = c.replies("C123", "1787217054.283119")
    assert [m["text"] for m in out["messages"][1:]] == [
        "J'ai testé, c'est cool", "A voir comment se démarquer"]


def test_la_fenetre_et_la_pagination_ne_partent_que_si_elles_sont_posees():
    # Un `oldest=None` envoyé tel quel devient la chaîne "None" côté query string :
    # Slack répond alors `invalid_ts_oldest` (sondé) au lieu de ne pas filtrer.
    c = SlackClient(bot_token="xoxb-1")
    seen = _capture(c)
    c.replies("C123", "1.1")
    assert set(seen["params"]) == {"channel", "ts", "limit"}

    seen = _capture(c)
    c.replies("C123", "1.1", limit=2, cursor="cur", oldest="2.2", latest="3.3", inclusive=True)
    assert seen["params"] == {"channel": "C123", "ts": "1.1", "limit": 2, "cursor": "cur",
                              "oldest": "2.2", "latest": "3.3", "inclusive": "true"}


def test_replies_route_le_token_comme_history():
    """Fil de canal → bot (invité, garde les scopes du user token minimaux) ;
    fil de DM → user (seul lui voit ses conversations)."""
    c = SlackClient(bot_token="xoxb-1", user_token="xoxp-1")
    seen = _capture(c)
    c.replies("C123", "1.1")
    assert seen["as_user"] is False
    seen = _capture(c)
    c.replies("D999", "1.1")
    assert seen["as_user"] is True
    seen = _capture(c)
    c.replies("C123", "1.1", as_user=True)
    assert seen["as_user"] is True


def test_slack_error_porte_le_scope_manquant_annonce_par_slack():
    """Slack NOMME lui-même le droit qui manque (`needed`) et ceux qu'il a vus
    (`provided`) — sondé : replies sur un canal privé avec un token sans
    `groups:history` rend `needed=groups:history`. Les perdre oblige à deviner ;
    or c'est ce qui bloque deux orgs (#510, #532)."""
    e = SlackError("missing_scope", needed="groups:history",
                   provided="identify,im:history,chat:write")
    assert e.error == "missing_scope" and e.status == 403
    assert e.needed == "groups:history"
    assert "identify" in (e.provided or "")
    # Un code sans scope reste construit comme avant (contrat existant).
    assert SlackError("not_in_channel").needed is None


def test_le_transport_remonte_needed_et_provided():
    c = SlackClient(bot_token="xoxb-1")
    import oto.tools.slack.client as mod

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "error": "missing_scope",
                    "needed": "channels:history", "provided": "chat:write"}

    mod.requests.request = lambda *a, **k: Resp()
    with pytest.raises(SlackError) as ei:
        c.replies("C1", "1.1")
    assert ei.value.needed == "channels:history" and ei.value.provided == "chat:write"


def test_join_channel_appelle_conversations_join():
    c = SlackClient(bot_token="xoxb-1")
    seen = _capture(c, {"ok": True, "channel": {"id": "C123"}})
    c.join_channel("C123")
    assert seen["endpoint"] == "conversations.join" and seen["method"] == "POST"
    assert seen["json"] == {"channel": "C123"}
    # Rejoindre est un acte de l'app : toujours le bot quand il existe.
    assert seen["as_user"] is False


def test_channel_info_appelle_conversations_info():
    """`conversations.info` répond sur un canal PUBLIC dont on n'est pas membre
    (sondé : is_private=False, is_member=False) — c'est ce qui permet de savoir
    si un canal est joignable AVANT de tenter quoi que ce soit."""
    c = SlackClient(bot_token="xoxb-1")
    seen = _capture(c, {"ok": True, "channel": {"id": "C1", "is_private": False}})
    c.channel_info("C1")
    assert seen["endpoint"] == "conversations.info"
    assert seen["params"] == {"channel": "C1"}
