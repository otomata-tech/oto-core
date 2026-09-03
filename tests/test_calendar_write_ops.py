"""`calendar_event` savait créer, pas défaire — signal #686.

« Un outil qui écrit sans pouvoir défaire pousse à laisser des traces derrière soi » :
un événement créé en double n'a pas pu être retiré, et c'est l'utilisateur qui l'a vu.
Le connecteur voisin (`tasks_task`) porte pourtant `upsert`/`rm` depuis toujours.

Ces bancs tiennent les trois décisions de conception, celles qu'un remaniement
reperdrait en silence.
"""
import pytest

pytest.importorskip("googleapiclient")
from oto.tools.google.calendar.lib.calendar_client import CalendarClient  # noqa: E402


class _Faux:
    """Double du service Google : enregistre l'appel au lieu de le passer."""
    def __init__(self, rendu=None):
        self.vus = []
        self._rendu = rendu or {"id": "evt1", "summary": "Titre", "status": "confirmed",
                                "start": {"dateTime": "2026-09-04T10:00:00Z"},
                                "end": {"dateTime": "2026-09-04T11:00:00Z"}}

    def events(self):
        return self

    def patch(self, **kw):
        self.vus.append(("patch", kw)); return self

    def update(self, **kw):
        # Présent EXPRÈS : sans lui, remplacer patch par update ferait tomber les
        # bancs sur un AttributeError — un rouge qui ne prouve rien.
        self.vus.append(("update", kw)); return self

    def delete(self, **kw):
        self.vus.append(("delete", kw)); return self

    def get(self, **kw):
        self.vus.append(("get", kw)); return self

    def execute(self):
        return self._rendu


def _client(rendu=None):
    c = CalendarClient.__new__(CalendarClient)
    c.service = _Faux(rendu)
    return c


def test_une_correction_PATCHE_et_ne_remplace_pas():
    """`events.update` REMPLACE l'événement : corriger un titre effacerait les
    invités, la récurrence, les rappels et la visioconférence — la réunion serait
    annulée par une faute de frappe. Seul `patch` touche ce qu'on lui donne."""
    c = _client()
    c.update_event("evt1", summary="Titre corrigé")
    verbe, kw = c.service.vus[0]
    assert verbe == "patch"
    assert kw["body"] == {"summary": "Titre corrigé"}   # et RIEN d'autre


def test_les_invites_ne_sont_PAS_prevenus_par_defaut():
    """Le paramètre est passé EXPLICITEMENT plutôt que laissé au défaut de l'API :
    un défaut qu'on ne contrôle pas peut changer sous nos pieds, et corriger une
    coquille ne doit pas écrire à douze personnes."""
    c = _client()
    c.update_event("evt1", summary="x")
    assert c.service.vus[0][1]["sendUpdates"] == "none"
    c2 = _client()
    c2.delete_event("evt1")
    assert [v for v, _ in c2.service.vus] == ["get", "delete"]
    assert c2.service.vus[1][1]["sendUpdates"] == "none"
    # et il reste possible de prévenir, en le demandant
    c3 = _client()
    c3.update_event("evt1", summary="x", send_updates="all")
    assert c3.service.vus[0][1]["sendUpdates"] == "all"


def test_une_suppression_rend_CE_QU_ELLE_a_supprime():
    """`events.delete` répond 204 sans corps : il n'y a rien à renvoyer de l'API.
    Sans lecture préalable, supprimer le mauvais identifiant donne exactement la même
    réponse que supprimer le bon — et l'appelant ne peut pas s'en apercevoir."""
    c = _client()
    out = c.delete_event("evt1")
    assert out["deleted"] is True and out["id"] == "evt1"
    assert out["summary"] == "Titre"      # relu AVANT la suppression
    assert out["start"] == "2026-09-04T10:00:00Z"
    assert out["notified"] is False


def test_un_patch_VIDE_est_refuse_plutot_que_joue():
    """Sans garde, `update_event(id)` sans champ dépenserait une écriture et rendrait
    un succès sans avoir rien changé — le pire des retours : celui qui confirme."""
    c = _client()
    with pytest.raises(ValueError, match="nothing to change"):
        c.update_event("evt1")
    assert c.service.vus == []
