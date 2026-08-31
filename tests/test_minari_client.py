"""Contrat du client Minari (API publique v1, Bearer, clé d'entreprise).

Mocke `requests.Session.request` / `.get` : vérifie que l'enveloppe est rendue
ENTIÈRE (la pagination vit à côté de `data`, la déballer la perdrait), que les
refus locaux nomment le contact fautif plutôt que le lot, que le 429 emporte son
délai de réarmement, que la garde SSRF tient sur `next_url`, et que la sonde
d'enregistrement ne tire jamais le corps audio.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.minari import client as mn


class _Resp:
    def __init__(self, status_code: int, body=None, headers=None, content=b"x",
                 *, stream_bytes: bytes | None = None, raw_text: str | None = None):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.text = raw_text if raw_text is not None else str(body)
        self.headers = headers or {}
        # Ce que la sonde d'enregistrement lira en flux, s'il y a lieu.
        self._stream = stream_bytes
        self.read_bytes = 0

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, chunk_size):
        """Compte ce qui est réellement lu — c'est l'objet du test de la sonde."""
        data = self._stream or b""
        for i in range(0, max(len(data), 1), chunk_size):
            chunk = data[i:i + chunk_size]
            self.read_bytes += len(chunk)
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def capture(monkeypatch):
    """Intercepte la requête et rend une enveloppe de liste plausible."""
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Resp(200, {"data": [], "has_more": False, "next_url": None})

    monkeypatch.setattr(mn.requests.Session, "request", fake_request)
    return seen


def _client(**kw):
    return mn.MinariClient(api_key="mk_test", **kw)


# --- auth et adresse ----------------------------------------------------------

def test_key_is_sent_as_bearer():
    assert _client().session.headers["Authorization"] == "Bearer mk_test"


def test_base_url_defaults_to_the_documented_host():
    assert _client().base_url == "https://api.minari.ai/v1"


def test_base_url_can_be_overridden_without_trailing_slash():
    c = _client(base_url="https://example.test/v1/")
    assert c.base_url == "https://example.test/v1"


# --- l'enveloppe : `data` n'est pas le tout -----------------------------------

def test_list_calls_returns_the_whole_envelope_not_just_data(capture):
    """`has_more`/`next_url` sont des FRÈRES de `data`. Un client qui déballerait
    `data` perdrait la pagination en silence — le pire mode d'échec possible sur
    un journal d'appels, où l'agent conclurait « c'est tout » à la 50e ligne."""
    out = _client().list_calls()
    assert set(out) == {"data", "has_more", "next_url"}


def test_none_params_are_dropped_not_sent_as_null(capture):
    _client().list_calls(direction="outgoing")
    assert capture["kwargs"]["params"] == {"direction": "outgoing"}


def test_repeatable_filters_are_sent_as_lists(capture):
    """`user_id`, `status` et `list_id` sont des tableaux côté Minari : requests
    répète la clé. Les passer autrement ne filtrerait que sur la première valeur."""
    _client().list_calls(user_id=[1, 2], status=["connected", "voicemail"], list_id=["a"])
    params = capture["kwargs"]["params"]
    assert params["user_id"] == [1, 2]
    assert params["status"] == ["connected", "voicemail"]
    assert params["list_id"] == ["a"]


# --- refus locaux : nommer le coupable ----------------------------------------

def test_empty_contact_batch_is_refused():
    with pytest.raises(ValueError, match="vide"):
        _client().create_list(name="x", assigned_to=1, contacts=[])


def test_oversized_contact_batch_is_refused_before_the_network():
    contacts = [{"email": f"a{i}@x.test"} for i in range(mn.MAX_CONTACTS_PER_REQUEST + 1)]
    with pytest.raises(ValueError, match=r"1501 contacts pour un plafond de 1500"):
        _client().create_list(name="x", assigned_to=1, contacts=contacts)


def test_contact_without_any_identifying_field_names_its_index():
    """Un lot de 1500 rejeté d'un bloc n'apprend pas QUEL contact est en faute ;
    le rang est la seule information avec laquelle on corrige un import."""
    contacts = [{"email": "ok@x.test"}, {"company": "Acme"}]
    with pytest.raises(ValueError, match=r"contact #1"):
        _client().add_contacts("L1", contacts)


def test_whitespace_only_identifier_does_not_count():
    with pytest.raises(ValueError, match=r"contact #0"):
        _client().add_contacts("L1", [{"firstName": "   "}])


def test_removing_no_contact_is_refused():
    with pytest.raises(ValueError, match="vide"):
        _client().remove_contacts("L1", [])


# --- analytics : les paramètres qui changent la DÉFINITION ---------------------

def test_analytics_lists_refuses_an_unknown_period():
    with pytest.raises(ValueError, match="`period` doit valoir"):
        _client().analytics_lists(period="fortnight", call_limit=3)


@pytest.mark.parametrize("bad", [0, 11, -1])
def test_analytics_lists_refuses_a_call_limit_out_of_range(bad):
    with pytest.raises(ValueError, match="`call_limit` doit être entre 1 et 10"):
        _client().analytics_lists(period="week", call_limit=bad)


def test_analytics_lists_sends_both_required_definitions(capture):
    _client().analytics_lists(period="week", call_limit=3)
    assert capture["kwargs"]["params"] == {"period": "week", "call_limit": 3}


def test_conversation_threshold_is_constrained_to_the_documented_set():
    with pytest.raises(ValueError, match="conversation_threshold"):
        _client().analytics_overview(conversation_threshold=45)


@pytest.mark.parametrize("ok", mn.CONVERSATION_THRESHOLDS)
def test_every_documented_threshold_is_accepted(capture, ok):
    _client().analytics_overview(conversation_threshold=ok)
    assert capture["kwargs"]["params"]["conversation_threshold"] == ok


# --- pagination : suivre une URL amont est un SSRF ----------------------------

def test_next_page_refuses_a_url_off_the_configured_host():
    """`next_url` est rendue par l'amont. La suivre sans la valider enverrait
    notre en-tête `Authorization` à l'hôte de son choix."""
    with pytest.raises(ValueError, match="hors de l'hôte configuré"):
        _client().next_page("https://evil.test/v1/calls?cursor=x")


def test_next_page_follows_a_url_on_the_configured_host(monkeypatch):
    seen = {}

    def fake_get(self, url, **kwargs):
        seen["url"] = url
        return _Resp(200, {"data": [], "has_more": False, "next_url": None})

    monkeypatch.setattr(mn.requests.Session, "get", fake_get)
    out = _client().next_page("https://api.minari.ai/v1/calls?cursor=abc")
    assert seen["url"] == "https://api.minari.ai/v1/calls?cursor=abc"
    assert out["has_more"] is False


# --- 429 : un refus sans délai est inutilisable -------------------------------

def test_rate_limit_error_carries_the_reset_delay(monkeypatch):
    """60 req/min PAR ENTREPRISE : sans les secondes de réarmement, l'appelant
    ne peut que deviner combien attendre."""
    def fake_request(self, method, url, **kwargs):
        return _Resp(429, {"error": {"code": "RATE_LIMITED"}},
                     headers={"RateLimit-Reset": "42"})

    monkeypatch.setattr(mn.requests.Session, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as e:
        _client().list_calls()
    assert e.value.status_code == 429
    assert "42" in str(e.value)


def test_rate_limit_without_reset_header_still_explains_itself(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return _Resp(429, {"error": {}}, headers={})

    monkeypatch.setattr(mn.requests.Session, "request", fake_request)
    with pytest.raises(UpstreamHTTPError, match="60 requêtes/minute"):
        _client().list_calls()


def test_other_upstream_errors_keep_their_status(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return _Resp(404, {"error": {"code": "NOT_FOUND"}})

    monkeypatch.setattr(mn.requests.Session, "request", fake_request)
    with pytest.raises(UpstreamHTTPError) as e:
        _client().get_call("missing")
    assert e.value.status_code == 404


# --- enregistrement : sonder sans tirer l'audio -------------------------------

def test_recording_status_reports_availability_from_headers_only(monkeypatch):
    """L'endpoint STREAME un MP3. Lire le corps pour savoir s'il existe coûterait
    le poids d'un appel entier à chaque sonde."""
    def fake_get(self, url, **kwargs):
        assert kwargs.get("stream") is True
        return _Resp(200, None,
                     headers={"Content-Type": "audio/mpeg", "Content-Length": "204800"})

    monkeypatch.setattr(mn.requests.Session, "get", fake_get)
    out = _client().call_recording_status("C1")
    assert out["available"] is True
    assert out["content_type"] == "audio/mpeg"
    assert out["size_bytes"] == 204800
    assert out["url"].endswith("/calls/C1/recording")


def test_recording_status_reads_a_json_answer_as_unavailable(monkeypatch):
    """Pas d'enregistrement ⟹ l'endpoint rend du JSON, pas un 404 : le traiter
    comme une erreur ferait passer « appel non abouti » pour une panne."""
    def fake_get(self, url, **kwargs):
        return _Resp(200, {"data": {"recording_url": None}},
                     headers={"Content-Type": "application/json"},
                     stream_bytes=b'{"data": {"recording_url": null}}')

    monkeypatch.setattr(mn.requests.Session, "get", fake_get)
    out = _client().call_recording_status("C1")
    assert out["available"] is False
    assert out["url"] is None


# --- forme des écritures ------------------------------------------------------

def test_create_list_maps_python_names_onto_the_api_camel_case(capture):
    _client().create_list(name="Q3", assigned_to=7,
                          contacts=[{"email": "a@x.test"}],
                          update_existing_contacts=True)
    body = capture["kwargs"]["json"]
    assert body["name"] == "Q3"
    assert body["assignedTo"] == 7
    assert body["updateExistingContacts"] is True
    assert body["contacts"] == [{"email": "a@x.test"}]


def test_remove_contacts_sends_a_body_on_delete(capture):
    """La forme historique de Minari : un DELETE qui porte un corps JSON."""
    _client().remove_contacts("L1", [1, 2])
    assert capture["method"] == "DELETE"
    assert capture["kwargs"]["json"] == {"contactIds": [1, 2]}


def test_delete_custom_field_targets_the_collection_not_a_path_id(capture):
    """`DELETE /custom-fields` prend l'id dans le CORPS — pas dans le chemin."""
    _client().delete_custom_field("industry")
    assert capture["url"].endswith("/custom-fields")
    assert capture["kwargs"]["json"] == {"id": "industry"}


# --- ce que Minari n'expose pas ------------------------------------------------

@pytest.mark.parametrize("absent", [
    "place_call", "start_call", "dial",        # Minari ne déclenche pas d'appel
    "update_contact", "create_user",           # ni ne modifie contacts/utilisateurs
])
def test_the_api_surface_is_not_invented(absent):
    """Ce qui manque ici manque à l'API : ces gestes n'existent pas côté Minari,
    et une méthode qui prétendrait les rendre mentirait à l'agent."""
    assert not hasattr(mn.MinariClient, absent)


# --- garde SSRF : la validation pré-vol ne suffit pas -------------------------

def test_next_page_does_not_follow_redirects(monkeypatch):
    """La garde d'hôte est PRÉ-VOL : sans `allow_redirects=False`, un `next_url`
    conforme qui répond `302 → ailleurs` emporterait notre `Authorization` vers
    une cible que personne n'a validée."""
    seen = {}

    def fake_get(self, url, **kwargs):
        seen.update(kwargs)
        return _Resp(302, None, headers={"Location": "https://evil.test/steal"})

    monkeypatch.setattr(mn.requests.Session, "get", fake_get)
    with pytest.raises(ValueError, match="redirige"):
        _client().next_page("https://api.minari.ai/v1/calls?cursor=abc")
    assert seen.get("allow_redirects") is False


# --- la sonde d'enregistrement ne tire jamais l'audio -------------------------

def test_recording_probe_reads_a_bounded_prefix_not_the_whole_body(monkeypatch):
    """Un MP3 servi sous un type inattendu ne doit pas être tiré en mémoire pour
    répondre « existe-t-il ? ». On lit un préfixe borné, pas le corps."""
    gros = b"\xff\xfb" + b"\x00" * (5 * 1024 * 1024)  # ~5 Mo d'« audio »
    resp = _Resp(200, None, headers={"Content-Type": "application/octet-stream",
                                     "Content-Length": str(len(gros))},
                 stream_bytes=gros)
    monkeypatch.setattr(mn.requests.Session, "get", lambda self, url, **kw: resp)
    out = _client().call_recording_status("C1")
    assert resp.read_bytes <= mn._PROBE_PREFIX_BYTES, "la sonde a lu tout le corps"
    assert out["available"] is True, "un enregistrement existant nié"


def test_recording_probe_asks_for_audio_first(monkeypatch):
    """L'en-tête de session annonce du JSON ; cet appel-ci attend de l'audio."""
    seen = {}

    def fake_get(self, url, **kwargs):
        seen.update(kwargs)
        return _Resp(200, None, headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(mn.requests.Session, "get", fake_get)
    _client().call_recording_status("C1")
    assert "audio/mpeg" in seen["headers"]["Accept"]


# --- un identifiant vient d'un agent : il s'échappe ---------------------------

@pytest.mark.parametrize("hostile, interdit", [
    ("C1?admin=true", "?"),
    ("C1#frag", "#"),
    ("../../users", "/"),
])
def test_path_ids_are_escaped(capture, hostile, interdit):
    """Non échappé, un id qui porte `?` ou `#` fait répondre le serveur à une
    AUTRE question que celle posée, et `/` lui fait changer d'endpoint."""
    _client().get_call(hostile)
    chemin = capture["url"].split("/v1", 1)[1]
    assert interdit not in chemin.replace("/calls/", "", 1)


# --- un corps illisible est une faute AMONT, pas un mauvais argument ----------

def test_unreadable_json_becomes_an_upstream_error_not_a_value_error(monkeypatch):
    """`resp.json()` lève `json.JSONDecodeError`, sous-classe de `ValueError` :
    laissé tel quel il se confond avec les refus de validation de ce module, et
    l'appelant accuse alors les arguments de l'utilisateur d'une panne amont."""
    def fake_request(self, method, url, **kwargs):
        return _Resp(200, None, content=b"<html>oops</html>", raw_text="<html>oops</html>")

    monkeypatch.setattr(mn.requests.Session, "request", fake_request)
    with pytest.raises(UpstreamHTTPError, match="illisible"):
        _client().list_calls()


# --- l'asymétrie du filtre `status` ------------------------------------------

def test_the_status_filter_accepts_meeting_booked_but_a_row_never_returns_it():
    """Le FILTRE compte neuf valeurs, la RÉPONSE huit. Recopier l'énumération de
    la réponse dans le filtre coûterait la seule façon de demander « les appels
    qui ont donné un rendez-vous » sans balayer tout le journal."""
    assert "meeting-booked" in mn.CALL_STATUSES
    assert "meeting-booked" not in mn.CALL_STATUSES_RETURNED
    assert len(mn.CALL_STATUSES) == 9 and len(mn.CALL_STATUSES_RETURNED) == 8
