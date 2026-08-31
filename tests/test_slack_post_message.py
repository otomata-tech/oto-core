"""`SlackClient.post_message` — échappement des faux emoji + split au-delà de
la limite recommandée par Slack (oto-backend#711, signaux #575 et #613).

#613 (session e836f3f7, 29/08) : un digest de ~4 058 caractères posté en UN
appel `slack_post_message` (calllog vérifié : un seul appel, `arg_keys` =
["channel", "text"], pas de `thread_ts`) a produit deux messages Slack
distincts, et le retour du tool n'exposait qu'un seul `ts` — aucun moyen de
relier le second message ni de répondre au bon endroit dans le fil. Le
correctif fait porter le split par le connecteur lui-même (déterministe,
testable) plutôt que de laisser Slack tronquer en silence au-delà de 40 000
caractères.
"""
from oto.tools.slack.client import SlackClient


def _capture_sequence(client, responses):
    """Remplace `_request` par un espion qui rend une réponse par appel (dans
    l'ordre) et capture chaque payload envoyé."""
    calls = []

    def fake(method, endpoint, as_user=None, **kwargs):
        calls.append({"method": method, "endpoint": endpoint, "as_user": as_user, **kwargs})
        return responses[len(calls) - 1]

    client._request = fake
    return calls


def test_post_message_escapes_clock_time_before_sending():
    """Cas de test « heure dans le texte » (#575) : le texte réellement
    envoyé à Slack ne contient plus le faux shortcode, et les chiffres de
    l'heure sont préservés."""
    c = SlackClient(bot_token="xoxb-1")
    calls = _capture_sequence(c, [{"ok": True, "channel": "C1", "ts": "1.1",
                                   "message": {"text": "…"}}])
    c.post_message("C1", text="20:02 to 20:51: one Partoo voice asked")
    sent_text = calls[0]["json"]["text"]
    assert ":51:" not in sent_text
    assert "20:02" in sent_text
    assert "51" in sent_text


def test_post_message_short_text_is_a_single_call_unchanged():
    """Non-régression : un texte court n'active ni split ni clé `ts_all` —
    la forme de retour reste celle d'avant #711."""
    c = SlackClient(bot_token="xoxb-1")
    _capture_sequence(c, [{"ok": True, "channel": "C1", "ts": "1.1",
                          "message": {"text": "hello"}}])
    result = c.post_message("C1", text="hello")
    assert result == {"ok": True, "channel": "C1", "ts": "1.1", "message": {"text": "hello"}}
    assert "ts_all" not in result


def test_post_message_splits_over_limit_and_returns_every_ts():
    """Cas de test « message multi-parties » (#613) : un texte au-delà de la
    limite recommandée part en plusieurs `chat.postMessage`, et TOUS les `ts`
    produits sont rendus — plus seulement le dernier."""
    c = SlackClient(bot_token="xoxb-1")
    long_text = ("Un paragraphe assez long pour dépasser la limite recommandée. " * 100).strip()
    assert len(long_text) > 4000
    calls = _capture_sequence(c, [
        {"ok": True, "channel": "C1", "ts": "100.001", "message": {"text": "part1"}},
        {"ok": True, "channel": "C1", "ts": "100.002", "message": {"text": "part2"}},
    ])
    result = c.post_message("C1", text=long_text)

    assert len(calls) == 2
    assert result["ts_all"] == ["100.001", "100.002"]
    assert result["split_into"] == 2
    # `ts` reste le PREMIER — l'ancre à réutiliser pour une future réponse en fil.
    assert result["ts"] == "100.001"


def test_post_message_threads_later_parts_under_the_first():
    """Sans `thread_ts` fourni par l'appelant, la 2e partie doit répondre en
    fil à la 1re — pas repartir en message top-level indépendant (c'est ce
    qui rendait le second fragment introuvable en pratique)."""
    c = SlackClient(bot_token="xoxb-1")
    long_text = ("Un paragraphe assez long pour dépasser la limite recommandée. " * 100).strip()
    calls = _capture_sequence(c, [
        {"ok": True, "channel": "C1", "ts": "100.001", "message": {}},
        {"ok": True, "channel": "C1", "ts": "100.002", "message": {}},
    ])
    c.post_message("C1", text=long_text)
    assert "thread_ts" not in calls[0]["json"]
    assert calls[1]["json"]["thread_ts"] == "100.001"


def test_post_message_keeps_caller_thread_ts_on_every_part():
    """Si l'appelant répond DÉJÀ dans un fil existant, chaque partie reste
    rattachée à CE fil (Slack aplatit de toute façon la réponse-à-une-réponse
    sur le parent réel) — pas chaînée sur la partie précédente."""
    c = SlackClient(bot_token="xoxb-1")
    long_text = ("Un paragraphe assez long pour dépasser la limite recommandée. " * 100).strip()
    calls = _capture_sequence(c, [
        {"ok": True, "channel": "C1", "ts": "100.001", "message": {}},
        {"ok": True, "channel": "C1", "ts": "100.002", "message": {}},
    ])
    c.post_message("C1", text=long_text, thread_ts="9.9")
    assert calls[0]["json"]["thread_ts"] == "9.9"
    assert calls[1]["json"]["thread_ts"] == "9.9"


def test_post_message_escapes_every_part_when_split():
    """L'échappement d'emoji doit s'appliquer à CHAQUE morceau, pas seulement
    au texte d'origine avant découpe."""
    c = SlackClient(bot_token="xoxb-1")
    filler = "x " * 3000
    text = filler + "ending at 20:51: right at a chunk boundary"
    calls = _capture_sequence(c, [
        {"ok": True, "channel": "C1", "ts": "1.1", "message": {}},
        {"ok": True, "channel": "C1", "ts": "1.2", "message": {}},
    ])
    c.post_message("C1", text=text)
    sent = "".join(call["json"]["text"] for call in calls)
    assert ":51:" not in sent


def test_post_message_with_blocks_bypasses_split_and_escaping():
    """`blocks` est construit par l'appelant : on ne le retouche pas."""
    c = SlackClient(bot_token="xoxb-1")
    calls = _capture_sequence(c, [{"ok": True, "channel": "C1", "ts": "1.1", "message": {}}])
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "at 20:51: go"}}]
    c.post_message("C1", text="fallback 20:51: text", blocks=blocks)
    assert len(calls) == 1
    assert calls[0]["json"]["blocks"] == blocks
