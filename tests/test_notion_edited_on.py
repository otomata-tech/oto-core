"""Détecter les pages Notion éditées à une date de calendrier donnée.

Signal oto-core#69 (feedback plateforme #468/#469, 16/08) : `POST /search` de
Notion ne FILTRE jamais sur `last_edited_time`, il ne fait que TRIER dessus
(doc API : le seul `filter` du endpoint search porte sur le type d'objet).
Sans levier serveur, un run qui veut « qu'est-ce qui a changé aujourd'hui »
doit rapatrier puis jeter, et « rien n'a changé » devient une inférence.

`search_edited_on` pagine triée décroissant par `last_edited_time` et
s'arrête au premier objet plus vieux que la fenêtre visée — ce qui suit l'est
forcément aussi. Ces tests vérifient : la requête envoyée (tri + filtre de
type), l'arrêt anticipé (pas de pages inutiles), les bornes du jour, et le
refus nommé plutôt qu'une réponse tronquée en silence.
"""
from __future__ import annotations

import pytest

from oto.tools.notion.lib import notion_client as nc


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, body):
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def _obj(obj_id: str, edited: str) -> dict:
    return {"object": "page", "id": obj_id, "last_edited_time": edited}


@pytest.fixture()
def client():
    return nc.NotionClient(token="test-token", cache_enabled=False)


def _capture(monkeypatch, pages):
    """`pages` = liste de (results, has_more, next_cursor) rendus dans l'ordre
    des appels successifs à `requests.request`."""
    calls = []
    state = {"i": 0}

    def fake_request(**kwargs):
        calls.append(kwargs)
        i = state["i"]
        state["i"] += 1
        results, has_more, next_cursor = pages[i]
        body = {"results": results, "has_more": has_more}
        if next_cursor is not None:
            body["next_cursor"] = next_cursor
        return _Resp(body)

    monkeypatch.setattr(nc.requests, "request", fake_request)
    return calls


def test_requete_porte_le_tri_et_le_filtre_de_type(monkeypatch, client):
    calls = _capture(monkeypatch, [([], False, None)])
    client.search_edited_on("2026-08-16", filter_type="page")
    body = calls[0]["json"]
    assert body["sort"] == {"direction": "descending", "timestamp": "last_edited_time"}
    assert body["filter"] == {"value": "page", "property": "object"}


def test_objets_du_jour_vise_sont_retenus(monkeypatch, client):
    page = _obj("p1", "2026-08-16T10:00:00.000Z")
    _capture(monkeypatch, [([page], False, None)])
    matches = client.search_edited_on("2026-08-16")
    assert [m["id"] for m in matches] == ["p1"]


def test_objets_hors_fenetre_sont_ecartes(monkeypatch, client):
    """Édité après le jour visé (à ignorer, on continue de descendre) et
    édité avant (frontière basse, arrête la pagination)."""
    later = _obj("later", "2026-08-17T00:00:01.000Z")
    on_day = _obj("on-day", "2026-08-16T23:59:59.000Z")
    earlier = _obj("earlier", "2026-08-15T23:59:59.000Z")
    calls = _capture(monkeypatch, [([later, on_day, earlier], True, "cursor-2"),
                                    ([_obj("never-fetched", "2026-08-01T00:00:00.000Z")],
                                     False, None)])
    matches = client.search_edited_on("2026-08-16")
    assert [m["id"] for m in matches] == ["on-day"]
    # Une seule page appelée : la borne basse a été franchie dans la 1re page.
    assert len(calls) == 1


def test_arret_anticipe_ne_pagine_pas_au_dela_du_necessaire(monkeypatch, client):
    """`has_more=True` sur la 1re page mais elle contient déjà un objet plus
    vieux que la fenêtre : la 2e page n'est jamais demandée."""
    on_day = _obj("on-day", "2026-08-16T12:00:00.000Z")
    older = _obj("older", "2026-08-10T00:00:00.000Z")
    calls = _capture(monkeypatch, [([on_day, older], True, "cursor-2")])
    client.search_edited_on("2026-08-16")
    assert len(calls) == 1


def test_pagine_tant_que_tout_reste_dans_la_fenetre_du_jour(monkeypatch, client):
    p1 = _obj("p1", "2026-08-16T23:00:00.000Z")
    p2 = _obj("p2", "2026-08-16T01:00:00.000Z")
    calls = _capture(monkeypatch, [([p1], True, "cursor-2"),
                                    ([p2], False, None)])
    matches = client.search_edited_on("2026-08-16")
    assert [m["id"] for m in matches] == ["p1", "p2"]
    assert len(calls) == 2
    assert calls[1]["json"]["start_cursor"] == "cursor-2"


def test_date_invalide_est_nommee(client):
    with pytest.raises(ValueError, match="2026-13-40"):
        client.search_edited_on("2026-13-40")


def test_max_pages_refuse_plutot_que_de_tronquer_en_silence(monkeypatch, client):
    always_recent = _obj("x", "2026-08-20T00:00:00.000Z")
    calls = _capture(monkeypatch, [([always_recent], True, "cursor") for _ in range(3)])
    with pytest.raises(Exception, match="max_pages"):
        client.search_edited_on("2026-08-16", max_pages=3)
    assert len(calls) == 3
