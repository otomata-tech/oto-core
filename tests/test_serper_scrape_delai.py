"""Le scrape attend 15 s, pas 60 — et l'appelant peut serrer encore (backend#662).

Mesuré sur une campagne de 193 passages d'agents (01/09/2026) : p95 de
`scrape_page` à **61,6 s**, **58 expirations à 60 s**, 31 % d'échec — et la
moitié des échecs portait sur des URL fabriquées par l'agent à partir d'une
raison sociale, donc des adresses qui ne pouvaient pas répondre.

⚠️ **Ce qui coûte n'est pas l'attente, c'est ce qu'elle emporte.** Pendant une
minute bloquée, le cache de contexte de l'agent expire : le passage médian ayant
scrapé a coûté 72 077 jetons contre 35 798 sans — le double — alors que la
corrélation avec le volume rapporté reste faible (r = +0,177). Un appel qui
bloque une minute peut donc coûter des dizaines de milliers de jetons **sans
rien rapporter**.

⚠️ Le pré-contrôle DNS demandé par le signal a été ÉCARTÉ, et c'est délibéré :
notre résolution n'est pas celle de Serper, un refus fondé dessus ne peut pas
être garanti juste, et il ajouterait une attente bloquante sur le chemin chaud.
Le plafond court donne déjà 4× sans rien risquer.
"""
from __future__ import annotations

import pytest

from oto.tools.serper.client import SerperClient, _HTTP_TIMEOUT, _SCRAPE_TIMEOUT


class _Reponse:
    status_code = 200

    @staticmethod
    def json():
        return {"text": "ok"}


@pytest.fixture()
def client(monkeypatch):
    c = SerperClient(api_key="k-test")
    monkeypatch.setattr(c, "_rate_limit", lambda: None)
    return c


def _delai_observe(client, monkeypatch, **kw):
    vu = {}

    def _post(url, json=None, timeout=None, **_):
        vu["timeout"] = timeout
        return _Reponse()

    monkeypatch.setattr(client.session, "post", _post)
    client.scrape_page("https://example.test/page", **kw)
    return vu["timeout"]


def test_le_scrape_attend_15_s_par_defaut(client, monkeypatch):
    assert _delai_observe(client, monkeypatch) == _SCRAPE_TIMEOUT
    assert _SCRAPE_TIMEOUT[1] == 15


def test_le_scrape_attend_MOINS_que_le_reste_du_client(client, monkeypatch):
    """La borne du scrape n'a de sens que si elle est plus basse que le défaut :
    les deux dans le même fichier, un futur ajustement de l'un se verrait ici."""
    assert _SCRAPE_TIMEOUT[1] < _HTTP_TIMEOUT[1]


def test_l_appelant_peut_serrer_le_delai(client, monkeypatch):
    """Un agent sous contrainte de budget doit pouvoir décider lui-même."""
    assert _delai_observe(client, monkeypatch, timeout_s=3) == (3, 3)


def test_un_delai_absurde_est_BORNE_au_lieu_d_etre_refuse(client, monkeypatch):
    """Ni 0 ni 600 : un refus ferait échouer un appel qui n'a rien de fautif,
    alors que la borne rend exactement le service demandé."""
    assert _delai_observe(client, monkeypatch, timeout_s=0) == (1, 1)
    assert _delai_observe(client, monkeypatch, timeout_s=600) == (5, 60)


def test_les_AUTRES_appels_gardent_leur_delai(client, monkeypatch):
    """Une recherche n'a pas le même profil qu'une extraction de page : serrer
    tout le client aurait fait payer aux uns un défaut mesuré chez l'autre."""
    vu = {}

    def _post(url, json=None, timeout=None, **_):
        vu["timeout"] = timeout
        return _Reponse()

    monkeypatch.setattr(client.session, "post", _post)
    client.search("acme")
    assert vu["timeout"] is None or vu["timeout"] == _HTTP_TIMEOUT
