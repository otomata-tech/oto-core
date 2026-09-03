"""Contrat du client Leexi (v1, Basic KEY_ID:KEY_SECRET, intelligence conversationnelle).

Mocke `requests.Session.request` : verbes et chemins relevés dans la référence
éditeur, en-tête d'auth, bornes de pagination, énumérations, re-tentatives — et
surtout **l'encodage `nom[]=` des filtres multi-valués**, qui est le piège que ce
fichier existe d'abord pour verrouiller : sans crochets, Rails ne retient que la
dernière valeur, l'amont répond 200, et le filtre ment sans que rien n'échoue.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.leexi import client as lx


class _Resp:
    def __init__(self, status_code: int = 200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"data": []}
        self.content = b"x"
        self.text = str(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    """Enregistre le dernier appel et compte les tentatives."""
    seen = {"calls": []}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        seen["calls"].append((method, url))
        return _Resp(200)

    monkeypatch.setattr(lx.requests.Session, "request", fake_request)
    monkeypatch.setattr(lx.time, "sleep", lambda _s: None)
    return seen


@pytest.fixture()
def cli():
    return lx.LeexiClient(key_id="KEY_ID", key_secret="KEY_SECRET")


# --- authentification ------------------------------------------------------

def test_signature_basic_est_celle_de_la_doc():
    """L'exemple travaillé de la doc éditeur, à la lettre : `KEY_ID:KEY_SECRET`
    encodé en base64 vaut `S0VZX0lEOktFWV9TRUNSRVQ=`."""
    assert lx.basic_signature("KEY_ID", "KEY_SECRET") == "S0VZX0lEOktFWV9TRUNSRVQ="


def test_la_cle_part_en_header_jamais_en_query(cli, capture):
    cli.list_calls()
    assert cli.session.headers["Authorization"] == "Basic S0VZX0lEOktFWV9TRUNSRVQ="
    # Le secret ne doit apparaître ni dans l'URL ni dans les params : il finirait
    # dans le message de toute exception requests, les access logs et Sentry.
    params = capture["kwargs"].get("params") or []
    assert "KEY_SECRET" not in capture["url"]
    assert all("KEY_SECRET" not in str(v) for _k, v in params)


# --- LE piège : encodage des filtres multi-valués --------------------------

def test_filtres_multivalues_partent_avec_des_crochets(cli, capture):
    """`owner_uuid[]=a&owner_uuid[]=b` — sans les crochets, Rails ne lit que `b`."""
    cli.list_calls(owner_uuid=["aaa-bbb", "ccc-ddd"])
    params = capture["kwargs"]["params"]
    assert ("owner_uuid[]", "aaa-bbb") in params
    assert ("owner_uuid[]", "ccc-ddd") in params
    # et surtout : jamais la forme nue, qui serait réduite à une seule valeur
    assert not any(k == "owner_uuid" for k, _v in params)


@pytest.mark.parametrize("nom", sorted(lx.ARRAY_PARAMS))
def test_chaque_param_multivalue_declare_prend_les_crochets(cli, capture, nom):
    """Le contrat porte sur la LISTE déclarée, pas sur un cas d'espèce : chacun
    des paramètres d'`ARRAY_PARAMS` doit sortir suffixé."""
    encoded = cli._encode_params({nom: ["x", "y"]})
    assert encoded == [(f"{nom}[]", "x"), (f"{nom}[]", "y")]


def test_une_valeur_scalaire_sur_un_param_multivalue_est_aussi_suffixee(cli):
    """Passer une seule valeur ne doit pas changer la forme envoyée : l'amont
    attend `[]` dans les deux cas."""
    assert cli._encode_params({"owner_uuid": "seul"}) == [("owner_uuid[]", "seul")]


def test_une_liste_sur_un_param_scalaire_est_refusee(cli):
    """Refus net plutôt qu'un écrasement silencieux côté serveur."""
    with pytest.raises(ValueError, match="n'accepte pas plusieurs valeurs"):
        cli._encode_params({"source": ["a", "b"]})


def test_none_est_retire_et_les_booleens_sont_serialises(cli):
    got = dict(cli._encode_params({"a": None, "b": True, "c": False, "d": 3}))
    assert "a" not in got
    assert got["b"] == "true" and got["c"] == "false" and got["d"] == 3


# --- pagination et énumérations --------------------------------------------

@pytest.mark.parametrize("bad", [0, 101, 1000, -1])
def test_items_hors_bornes_est_refuse_localement(cli, bad):
    with pytest.raises(ValueError, match="entre 1 et 100"):
        cli.list_calls(items=bad)


@pytest.mark.parametrize("ok", [1, 10, 100])
def test_items_dans_les_bornes_passe(cli, capture, ok):
    cli.list_calls(items=ok)
    assert ("items", ok) in capture["kwargs"]["params"]


def test_items_booleen_est_refuse(cli):
    """`True` est un `int` en Python — sans garde, `items=True` partirait en 1."""
    with pytest.raises(ValueError, match="doit être un entier"):
        cli.list_calls(items=True)


def test_ordre_inconnu_refuse_en_nommant_les_valides(cli):
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.list_calls(order="created_at sideways")


def test_origine_de_reunion_inconnue_refusee(cli):
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.list_meeting_events(origin="telepathie")


@pytest.mark.parametrize("order", lx.CALL_ORDERS)
def test_tous_les_ordres_documentes_passent(cli, capture, order):
    cli.list_calls(order=order)
    assert ("order", order) in capture["kwargs"]["params"]


# --- `from`/`to` : le mot réservé -------------------------------------------

def test_date_from_et_date_to_partent_sous_from_et_to(cli, capture):
    """Renommés côté Python (`from` est réservé), inchangés sur le fil."""
    cli.list_calls(date_from="2026-01-01", date_to="2026-02-01")
    params = dict(capture["kwargs"]["params"])
    assert params["from"] == "2026-01-01" and params["to"] == "2026-02-01"
    assert "date_from" not in params and "date_to" not in params


# --- verbes et chemins ------------------------------------------------------

@pytest.mark.parametrize("appel,attendu", [
    (lambda c: c.list_users(), ("GET", "/users")),
    (lambda c: c.get_user("u1"), ("GET", "/users/u1")),
    (lambda c: c.create_user({"email": "a@b.c"}), ("POST", "/users")),
    (lambda c: c.update_user("u1", {"name": "N"}), ("PATCH", "/users/u1")),
    (lambda c: c.deactivate_user("u1"), ("DELETE", "/users/u1")),
    (lambda c: c.list_teams(), ("GET", "/teams")),
    (lambda c: c.get_team("t1"), ("GET", "/teams/t1")),
    (lambda c: c.create_team({"name": "T"}), ("POST", "/teams")),
    (lambda c: c.update_team("t1", {"name": "T"}), ("PATCH", "/teams/t1")),
    (lambda c: c.delete_team("t1"), ("DELETE", "/teams/t1")),
    (lambda c: c.list_calls(), ("GET", "/calls")),
    (lambda c: c.get_call("c1"), ("GET", "/calls/c1")),
    (lambda c: c.create_call({"external_id": "x"}), ("POST", "/calls")),
    (lambda c: c.presign_recording_url("mp3"),
     ("POST", "/calls/presign_recording_url")),
    (lambda c: c.list_call_notes("c1"), ("GET", "/call_notes")),
    (lambda c: c.get_call_note("n1"), ("GET", "/call_notes/n1")),
    (lambda c: c.update_call_note("n1", "fr", "t"), ("PATCH", "/call_notes/n1")),
    (lambda c: c.delete_call_note("n1"), ("DELETE", "/call_notes/n1")),
    (lambda c: c.list_meeting_events(), ("GET", "/meeting_events")),
    (lambda c: c.get_meeting_event("m1"), ("GET", "/meeting_events/m1")),
    (lambda c: c.create_meeting_event({"owned": True}), ("POST", "/meeting_events")),
    (lambda c: c.delete_meeting_event("m1"), ("DELETE", "/meeting_events/m1")),
    (lambda c: c.launch_meeting_assistant("m1"),
     ("POST", "/meeting_events/m1/launch_bot")),
])
def test_verbe_et_chemin_de_chaque_methode(cli, capture, appel, attendu):
    appel(cli)
    verbe, chemin = attendu
    assert capture["method"] == verbe
    assert capture["url"] == f"{cli.BASE_URL}{chemin}"


def test_desactivation_utilise_delete_mais_se_nomme_deactivate(cli, capture):
    """Le verbe HTTP dit « delete », l'effet amont est une désactivation (les
    appels restent, la licence se libère) — le nom de la méthode dit l'effet."""
    cli.deactivate_user("u1")
    assert capture["method"] == "DELETE"
    assert not hasattr(cli, "delete_user")


# --- notes : les exigences de l'amont, dites localement --------------------

def test_lister_des_notes_sans_call_uuid_est_refuse(cli):
    with pytest.raises(ValueError, match="call_uuid"):
        cli.list_call_notes("")


def test_note_mise_a_jour_exige_locale_et_texte(cli):
    with pytest.raises(ValueError, match="requis"):
        cli.update_call_note("n1", "fr", "")


def test_call_uuid_part_bien_en_parametre(cli, capture):
    cli.list_call_notes("c1", prompt_uuid="p1")
    params = dict(capture["kwargs"]["params"])
    assert params["call_uuid"] == "c1" and params["prompt_uuid"] == "p1"


# --- assistant : un endpoint, deux sens ------------------------------------

def test_stop_task_absent_nenvoie_pas_de_corps(cli, capture):
    cli.launch_meeting_assistant("m1")
    assert capture["kwargs"]["json"] is None


def test_stop_task_false_est_transmis(cli, capture):
    """`False` est une DEMANDE explicite (lancer), pas une absence : un `if
    stop_task:` naïf l'aurait avalé."""
    cli.launch_meeting_assistant("m1", stop_task=False)
    assert capture["kwargs"]["json"] == {"stop_task": False}


# --- re-tentatives ----------------------------------------------------------

def test_le_429_est_retente_en_lecture(cli, monkeypatch):
    tentatives = {"n": 0}

    def flaky(self, method, url, **kwargs):
        tentatives["n"] += 1
        if tentatives["n"] < 3:
            return _Resp(429, headers={"Retry-After": "0"})
        return _Resp(200)

    monkeypatch.setattr(lx.requests.Session, "request", flaky)
    monkeypatch.setattr(lx.time, "sleep", lambda _s: None)
    cli.list_calls()
    assert tentatives["n"] == 3


def test_une_ecriture_nest_jamais_retentee(cli, monkeypatch):
    """Aucune clé d'idempotence côté Leexi : rejouer un POST créerait un doublon
    — un appel de plus, ou un utilisateur FACTURÉ de plus."""
    tentatives = {"n": 0}

    def always_429(self, method, url, **kwargs):
        tentatives["n"] += 1
        return _Resp(429, headers={"Retry-After": "0"})

    monkeypatch.setattr(lx.requests.Session, "request", always_429)
    monkeypatch.setattr(lx.time, "sleep", lambda _s: None)
    with pytest.raises(UpstreamHTTPError) as exc:
        cli.create_user({"email": "a@b.c"})
    assert exc.value.status_code == 429
    assert tentatives["n"] == 1


def test_retry_after_est_respecte(cli, monkeypatch):
    dormi = []
    etat = {"n": 0}

    def flaky(self, method, url, **kwargs):
        etat["n"] += 1
        return _Resp(429, headers={"Retry-After": "7"}) if etat["n"] == 1 else _Resp(200)

    monkeypatch.setattr(lx.requests.Session, "request", flaky)
    monkeypatch.setattr(lx.time, "sleep", lambda s: dormi.append(s))
    cli.list_calls()
    assert dormi == [7.0]


def test_un_400_nest_pas_retente_et_remonte(cli, monkeypatch):
    tentatives = {"n": 0}

    def bad(self, method, url, **kwargs):
        tentatives["n"] += 1
        return _Resp(400, body={"error": "nope"})

    monkeypatch.setattr(lx.requests.Session, "request", bad)
    with pytest.raises(UpstreamHTTPError) as exc:
        cli.list_calls()
    assert exc.value.status_code == 400 and tentatives["n"] == 1
    assert exc.value.service == "leexi"


# --- sonde ------------------------------------------------------------------

def test_la_sonde_interroge_les_appels_pas_les_utilisateurs(cli, capture):
    """`read_calls` est le SEUL scope d'une clé neuve : sonder `/users` ferait
    passer une clé saine pour une clé morte (403 au lieu de 401)."""
    cli.probe()
    assert capture["url"] == f"{cli.BASE_URL}/calls"
    assert ("items", 1) in capture["kwargs"]["params"]


# --- téléversement hors session --------------------------------------------

def test_upload_rejoue_les_entetes_signes_hors_session(cli, monkeypatch):
    vu = {}

    def fake_put(url, data=None, headers=None, timeout=None):
        vu.update(url=url, data=data, headers=headers)
        return _Resp(200)

    monkeypatch.setattr(lx.requests, "put", fake_put)
    # `requests.put` est importé dans le mixin : le patch doit porter là aussi.
    from oto.tools.leexi._api import calls as calls_mod
    monkeypatch.setattr(calls_mod.requests, "put", fake_put)

    presigned = {"url": "https://s3.example/x", "headers": {"x-amz-meta": "v"}}
    assert cli.upload_recording(presigned, b"audio") == 200
    assert vu["url"] == "https://s3.example/x"
    assert vu["headers"] == {"x-amz-meta": "v"}
    # L'Authorization Leexi n'a rien à faire chez le stockage : elle casserait
    # la signature de l'URL pré-signée.
    assert "Authorization" not in vu["headers"]


def test_upload_sans_url_est_refuse_en_nommant_les_cles_recues(cli):
    with pytest.raises(ValueError, match="clés reçues"):
        cli.upload_recording({"headers": {}}, b"audio")


def test_upload_refuse_autre_chose_quun_objet(cli):
    with pytest.raises(ValueError, match="presign_recording_url"):
        cli.upload_recording("https://s3.example/x", b"audio")
