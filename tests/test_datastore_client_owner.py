"""Le client peut créer un tableau d'organisation — otomata-tech/oto#45.

`create_namespace` n'envoyait que le nom : un intégrateur qui passait par cette
lib ne POUVAIT PAS créer un tableau partagé, alors que la route l'accepte depuis
toujours. Le tableau naissait donc personnel — visible de lui seul — et tout
continuait de fonctionner pour lui, ce qui est le pire cas : l'erreur se découvre
au collègue qui ne trouve rien.
"""
from __future__ import annotations


class _Faux:
    """Note ce qui part sur le fil, rend ce que la route rend."""

    def __init__(self):
        self.envois = []

    def __call__(self, method, path, **kw):
        self.envois.append((method, path, kw.get("json")))
        return {"namespace": "t", "id": 1, "url": "u",
                "owner_type": "user", "owner_id": "u-1", "is_personal": True}


def _client(faux):
    from oto.tools.datastore.client import DatastoreClient

    c = DatastoreClient.__new__(DatastoreClient)
    c._req = faux
    return c


def test_sans_owner_le_corps_ne_porte_que_le_nom():
    """Non-régression : le contrat d'avant, à l'octet."""
    faux = _Faux()
    _client(faux).create_namespace("vivier")
    assert faux.envois == [("POST", "/api/datastore/namespaces", {"namespace": "vivier"})]


def test_avec_owner_le_corps_le_transmet():
    faux = _Faux()
    _client(faux).create_namespace("vivier", owner={"type": "org", "id": 7})
    assert faux.envois[0][2] == {"namespace": "vivier",
                                 "owner": {"type": "org", "id": 7}}


def test_un_owner_vide_ne_part_pas():
    """`{}` ou `None` = rien à dire : ne pas envoyer une clé que la route lirait
    comme une intention."""
    for vide in (None, {}):
        faux = _Faux()
        _client(faux).create_namespace("vivier", owner=vide)
        assert "owner" not in faux.envois[0][2]


def test_la_reponse_porte_le_proprietaire():
    """C'est ce qui permet à l'appelant de VOIR qu'il vient de créer un tableau
    personnel — la seule information qui décide de qui le verra."""
    out = _client(_Faux()).create_namespace("vivier")
    assert out["owner_type"] == "user" and out["is_personal"] is True
