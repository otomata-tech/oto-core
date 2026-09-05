"""Le refresh Zoho dit à l'appelant qu'il a réussi — otomata-tech/oto#25 lot b3.

Symétrique de l'`on_refresh` du client Salesforce, et pour la même raison : c'est
le seul instant où l'appelant apprend que ce credential authentifie RÉELLEMENT, à
l'instant présent. Sans ce rappel, le démarquage d'une ligne de coffre rejetée
excluait Zoho — et le disait, faute de pouvoir faire autrement.

⚠️ Le point qui décide de la justesse : le rappel ne doit PAS partir sur un succès
de cache. Un jeton encore valide prouve qu'un refresh a marché il y a un moment,
pas que le credential marche maintenant. S'en servir comme preuve de vie
démarquerait une ligne sur une information périmée — exactement ce qu'un
démarquage ne doit jamais faire.
"""
from __future__ import annotations

import time

import pytest

from oto.tools.zoho import auth


class _Reponse:
    def __init__(self, corps, code=200):
        self.status_code, self._corps = code, corps

    def json(self):
        return self._corps


@pytest.fixture(autouse=True)
def cache_vierge():
    auth._TOKEN_CACHE.clear()
    yield
    auth._TOKEN_CACHE.clear()


def _poste(monkeypatch, corps, code=200):
    appels = {"n": 0}

    def _post(url, **kw):
        appels["n"] += 1
        return _Reponse(corps, code)

    monkeypatch.setattr(auth.requests, "post", _post)
    return appels


def test_un_refresh_reussi_appelle_le_rappel_avec_la_reponse(monkeypatch):
    _poste(monkeypatch, {"access_token": "tok", "expires_in": 3600})
    vus = []
    jeton = auth.get_access_token("https://acc", "cid", "sec", "rt",
                                  on_refresh=vus.append)
    assert jeton == "tok"
    assert vus == [{"access_token": "tok", "expires_in": 3600}]


def test_un_succes_de_CACHE_n_appelle_PAS_le_rappel(monkeypatch):
    """LE point du lot. Sinon on démarquerait une ligne sur la foi d'un jeton
    obtenu il y a cinquante minutes — une preuve périmée présentée comme fraîche."""
    appels = _poste(monkeypatch, {"access_token": "tok", "expires_in": 3600})
    vus = []
    for _ in range(3):
        auth.get_access_token("https://acc", "cid", "sec", "rt", on_refresh=vus.append)
    assert appels["n"] == 1, "le cache doit avoir servi les deux appels suivants"
    assert len(vus) == 1, (
        f"{len(vus)} rappels pour un seul refresh : le cache déclenche le rappel, "
        "donc la preuve de vie serait périmée")


def test_un_refresh_EN_ECHEC_n_appelle_pas_le_rappel(monkeypatch):
    """Un credential refusé ne doit surtout pas se faire démarquer comme sain."""
    _poste(monkeypatch, {"error": "invalid_client"})
    vus = []
    with pytest.raises(auth.ZohoAuthError):
        auth.get_access_token("https://acc", "cid", "sec", "rt", on_refresh=vus.append)
    assert vus == []


def test_une_panne_du_rappel_ne_casse_pas_l_appel(monkeypatch):
    """Le jeton est valide : la persistance est un effet de bord de l'appelant, sa
    panne ne doit pas lui coûter son appel."""
    _poste(monkeypatch, {"access_token": "tok", "expires_in": 3600})

    def _casse(_):
        raise RuntimeError("écriture impossible")

    assert auth.get_access_token("https://acc", "cid", "sec", "rt",
                                 on_refresh=_casse) == "tok"


def test_sans_rappel_le_comportement_est_inchange(monkeypatch):
    """Non-régression : l'argument est optionnel, tous les appelants d'avant
    continuent de marcher à l'identique."""
    _poste(monkeypatch, {"access_token": "tok", "expires_in": 3600})
    assert auth.get_access_token("https://acc", "cid", "sec", "rt") == "tok"


def test_le_client_propage_le_rappel(monkeypatch):
    """Le chemin réel : personne n'appelle `get_access_token` à la main côté
    serveur, tout passe par le client."""
    from oto.tools.zoho.client import ZohoClient

    _poste(monkeypatch, {"access_token": "tok", "expires_in": 3600})
    vus = []
    cli = ZohoClient(client_id="cid", client_secret="sec", refresh_token="rt",
                     accounts_url="https://acc", on_refresh=vus.append)
    assert cli._get_access_token() == "tok"
    assert len(vus) == 1


def test_un_jeton_PERIME_en_cache_redeclenche_le_rappel(monkeypatch):
    """L'autre sens du cache : quand il expire, le refresh a bien lieu et la preuve
    de vie est de nouveau fraîche."""
    _poste(monkeypatch, {"access_token": "tok", "expires_in": 3600})
    vus = []
    auth.get_access_token("https://acc", "cid", "sec", "rt", on_refresh=vus.append)
    # On périme l'entrée sans toucher au reste : le cache est keyé par credential.
    k = auth.cred_key("https://acc", "cid", "rt")
    auth._TOKEN_CACHE[k] = ("tok", time.time() - 1)
    auth.get_access_token("https://acc", "cid", "sec", "rt", on_refresh=vus.append)
    assert len(vus) == 2
