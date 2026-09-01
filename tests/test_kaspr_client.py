"""Kaspr : le timeout par défaut DOIT partir avec chaque requête (signal #252 —
sans read-timeout, un blip amont suspend l'appel pour toujours), et un nom de
champ `dataToGet` inconnu est refusé ICI plutôt que rendu en 500 opaque."""
from __future__ import annotations

import pytest

from oto.tools.kaspr import client as km


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"profile": {}}


def test_request_carries_default_timeout(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Resp()

    monkeypatch.setattr(km.requests, "request", fake_request)
    c = km.KasprClient(api_key="k")
    c.enrich_linkedin("https://www.linkedin.com/in/alexislaporte/")

    assert captured["timeout"] == km.KasprClient.TIMEOUT
    assert captured["json"]["id"] == "alexislaporte"  # URL → slug nu


def test_explicit_timeout_not_overridden(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(**kwargs)
        return _Resp()

    monkeypatch.setattr(km.requests, "request", fake_request)
    km.KasprClient(api_key="k")._request("POST", "profile/linkedin", json={}, timeout=5)
    assert captured["timeout"] == 5


# --- `dataToGet` : les trois seuls noms que Kaspr accepte ---------------------
#
# Reproduit le 2026-09-01 sur un profil sentinelle (sans crédit consommé) :
#   ["emails", "phones", "company"] → HTTP 500, `TypeError: Cannot read
#       properties of undefined (reading 'push')` — le parser amont plante sur un
#       champ inconnu, donc l'appelant reçoit une PANNE là où il a une faute de
#       frappe ;
#   ["workEmail", "phone"]          → 402 (crédits) — la requête est comprise ;
#   []                              → 200.
#
# Cette forme fautive n'était pas une invention d'agent : la docstring du tool MCP
# `kaspr_enrich_linkedin` la donnait en EXEMPLE depuis la création du tool
# (2026-05-22). Le refus vit ici et pas dans le backend parce que c'est la logique
# canonique du client — la CLI en profite aussi.


def _sans_reseau(monkeypatch):
    """Toute requête partie est une erreur : le refus doit précéder l'appel."""

    def interdit(*a, **k):
        raise AssertionError("une requête est partie malgré un champ inconnu")

    monkeypatch.setattr(km.requests, "request", interdit)


def test_champ_inconnu_refuse_avant_tout_appel(monkeypatch):
    _sans_reseau(monkeypatch)
    with pytest.raises(ValueError) as e:
        km.KasprClient(api_key="k").enrich_linkedin(
            "jane-doe", data_to_get=["emails", "phones", "company"])
    msg = str(e.value)
    # le refus NOMME les noms acceptés — sinon il ne vaut pas mieux que le 500
    for accepte in km.DATA_TO_GET:
        assert accepte in msg, msg
    # …et NOMME ce qu'il a refusé, pour que la correction soit mécanique
    for refuse in ("emails", "phones", "company"):
        assert refuse in msg, msg


def test_un_seul_champ_inconnu_suffit_a_refuser(monkeypatch):
    _sans_reseau(monkeypatch)
    with pytest.raises(ValueError):
        km.KasprClient(api_key="k").enrich_linkedin(
            "jane-doe", data_to_get=["workEmail", "company"])


def test_les_noms_acceptes_passent(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(**kwargs)
        return _Resp()

    monkeypatch.setattr(km.requests, "request", fake_request)
    km.KasprClient(api_key="k").enrich_linkedin(
        "jane-doe", data_to_get=list(km.DATA_TO_GET))
    assert captured["json"]["dataToGet"] == list(km.DATA_TO_GET)


def test_data_to_get_absent_ne_declenche_aucun_refus(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(**kwargs)
        return _Resp()

    monkeypatch.setattr(km.requests, "request", fake_request)
    km.KasprClient(api_key="k").enrich_linkedin("jane-doe")
    # le défaut RÉEL du client — celui que la docstring du tool doit annoncer,
    # et qui n'est PAS « tous les champs »
    assert captured["json"]["dataToGet"] == ["workEmail", "phone"]


def test_verify_key_reste_credit_free(monkeypatch):
    """La sonde de clé passe par `_request` en direct : elle ne doit pas devenir
    tributaire de la validation (son `dataToGet` vide est justement ce qui la
    rend gratuite)."""
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured.update(**kwargs)
        return _Resp()

    monkeypatch.setattr(km.requests, "request", fake_request)
    assert km.KasprClient(api_key="k").verify_key() == {"valid": True}
    assert captured["json"]["dataToGet"] == []
