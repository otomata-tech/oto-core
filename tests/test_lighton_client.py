"""LightOnClient — verrouille le contrat HTTP (verbe, URL, body, multipart)
de chaque endpoint Paradigm v2 couvert, la surfaçage d'erreur ({detail}/{error}
du body Paradigm remonté dans le RuntimeError) et le défaut/override de
`base_url` (instance SaaS publique vs instance privée).

Mocke `requests.request` : contrat HTTP uniquement, sans réseau ni clé réelle.
"""
import json

import pytest

from oto.tools.lighton import client as lighton_client
from oto.tools.lighton.client import LightOnClient

BASE = "https://paradigm.lighton.ai/api/v2"


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def fake_request(method, url, headers=None, json=None, params=None,
                     files=None, data=None, timeout=None):
        captured.append({
            "method": method, "url": url, "headers": headers, "json": json,
            "params": params, "files": files, "data": data, "timeout": timeout,
        })
        return _Resp({"object": "list", "data": []})

    monkeypatch.setattr(lighton_client.requests, "request", fake_request)
    return captured


@pytest.fixture
def c():
    return LightOnClient(api_key="test-key")


# --- auth / base_url ----------------------------------------------------------

def test_bearer_header(calls, c):
    c.list_models()
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"


def test_default_base_url(calls, c):
    c.list_models()
    assert calls[0]["url"] == f"{BASE}/models"


def test_private_instance_base_url(calls):
    c = LightOnClient(api_key="test-key",
                      base_url="https://paradigm.acme.fr/api/v2/")
    c.list_models()
    # trailing slash normalisé
    assert calls[0]["url"] == "https://paradigm.acme.fr/api/v2/models"


# --- chat ---------------------------------------------------------------------

def test_chat_body_shape(calls, c):
    msgs = [{"role": "user", "content": "salut"}]
    c.chat(msgs, "alfred-ft5", max_tokens=10, temperature=0.2)
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/chat/completions"
    assert call["json"] == {"model": "alfred-ft5", "messages": msgs,
                            "max_tokens": 10, "temperature": 0.2}


def test_chat_omits_unset_sampling_params(calls, c):
    c.chat([{"role": "user", "content": "x"}], "alfred-ft5")
    assert set(calls[0]["json"]) == {"model", "messages"}


# --- base documentaire --------------------------------------------------------

def test_query_body_shape(calls, c):
    c.query("code secret", collection="base_collection", n=3)
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/query"
    assert call["json"] == {"query": "code secret", "n": 3,
                            "collection": "base_collection"}


def test_query_default_collection_omitted(calls, c):
    c.query("x")
    assert "collection" not in calls[0]["json"]


def test_list_files_scopes(calls, c):
    c.list_files(private_scope=True, company_scope=False, page=2)
    call = calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE}/files"
    assert call["params"] == {"private_scope": True, "company_scope": False,
                              "page": 2}


def test_upload_multipart_shape(calls, c):
    c.upload_file_bytes(b"abc", "note.txt", collection_type="private",
                        title="Note")
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/files"
    assert call["files"] == {"file": ("note.txt", b"abc")}
    assert call["data"] == {"collection_type": "private", "title": "Note"}
    # le multipart ne doit PAS forcer un Content-Type json
    assert "Content-Type" not in call["headers"]


def test_ask_document_path_and_body(calls, c):
    c.ask_document(42, "quel est le code ?")
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/files/42/ask-question"
    assert call["json"] == {"question": "quel est le code ?"}


def test_delete_file_path(calls, c):
    c.delete_file(42)
    call = calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == f"{BASE}/files/42"


# --- erreurs ------------------------------------------------------------------

def test_error_surfaces_paradigm_detail(monkeypatch, c):
    monkeypatch.setattr(
        lighton_client.requests, "request",
        lambda *a, **k: _Resp({"code": 404, "error": "not_found",
                               "detail": "No API endpoint at /x."}, 404))
    with pytest.raises(RuntimeError) as exc:
        c.list_models()
    assert "404" in str(exc.value)
    assert "No API endpoint" in str(exc.value)


def test_error_falls_back_to_error_field(monkeypatch, c):
    monkeypatch.setattr(
        lighton_client.requests, "request",
        lambda *a, **k: _Resp({"error": "Permission denied", "code": 403}, 403))
    with pytest.raises(RuntimeError) as exc:
        c.list_models()
    assert "403" in str(exc.value)
    assert "Permission denied" in str(exc.value)
