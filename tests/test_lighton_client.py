"""LightOnClient (API v3, api.lighton.ai) — verrouille le contrat HTTP
(verbe, URL, body, multipart) des endpoints couverts : search/ask (retrieval),
parse/extract (traitement one-shot), files (upload workspace-requis, filtres),
workspaces ; le surfaçage d'erreur ({detail}/{error} LightOn remonté dans le
RuntimeError) et le défaut/override de `base_url` (SaaS vs instance privée).

Mocke `requests.request` : contrat HTTP uniquement, sans réseau ni clé réelle.
"""
import json

import pytest

from oto.tools.lighton import client as lighton_client
from oto.tools.lighton.client import LightOnClient

BASE = "https://api.lighton.ai/api/v3"


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
        return _Resp({"results": []})

    monkeypatch.setattr(lighton_client.requests, "request", fake_request)
    return captured


@pytest.fixture
def c():
    return LightOnClient(api_key="test-key")


# --- auth / base_url ----------------------------------------------------------

def test_bearer_header(calls, c):
    c.list_workspaces()
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"


def test_default_base_url_is_v3(calls, c):
    c.list_workspaces()
    assert calls[0]["url"] == f"{BASE}/workspaces"


def test_private_instance_base_url(calls):
    c = LightOnClient(api_key="test-key", base_url="https://lighton.acme.fr/")
    c.list_workspaces()
    # trailing slash normalisé, /api/v3 ajouté par le client
    assert calls[0]["url"] == "https://lighton.acme.fr/api/v3/workspaces"


# --- retrieval ----------------------------------------------------------------

def test_search_body_shape(calls, c):
    c.search("code secret", workspace_ids=[7046], max_results=3)
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/search"
    assert call["json"] == {"query": "code secret", "workspace_id": [7046],
                            "max_results": 3}


def test_search_omits_unset_options(calls, c):
    c.search("x")
    assert calls[0]["json"] == {"query": "x"}


def test_search_vision_mode_and_flags(calls, c):
    c.search("plan", mode="vision", include_image=True, include_bboxes=True)
    body = calls[0]["json"]
    assert body["mode"] == "vision"
    assert body["include_image"] is True
    assert body["include_bboxes"] is True


def test_ask_body_shape(calls, c):
    c.ask("quel code ?", workspace_ids=[7046], model="mistral-large-latest")
    call = calls[0]
    assert call["url"] == f"{BASE}/ask"
    assert call["json"] == {"query": "quel code ?", "stream": False,
                            "workspace_id": [7046],
                            "model": "mistral-large-latest"}


# --- parse / extract ----------------------------------------------------------

def test_parse_bytes_multipart(calls, c):
    c.parse_bytes(b"abc", "doc.pdf")
    call = calls[0]
    assert call["url"] == f"{BASE}/parse"
    assert call["files"] == {"file": ("doc.pdf", b"abc")}
    assert call["data"] is None  # sync : pas d'options


def test_parse_bytes_async_option(calls, c):
    c.parse_bytes(b"abc", "doc.pdf", async_=True)
    assert calls[0]["data"] == {"options": '{"async": true}'}


def test_parse_url_json(calls, c):
    c.parse_url("https://example.com/r.pdf")
    assert calls[0]["json"] == {"document": "https://example.com/r.pdf"}


def test_parse_job_path(calls, c):
    c.parse_job("parse_abc")
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == f"{BASE}/parse/parse_abc"


def test_extract_bytes_schema_json_encoded(calls, c):
    schema = {"type": "object", "properties": {"n": {"type": "string"}}}
    c.extract_bytes(b"abc", "f.pdf", schema)
    call = calls[0]
    assert call["url"] == f"{BASE}/extract"
    assert call["files"] == {"file": ("f.pdf", b"abc")}
    assert json.loads(call["data"]["schema"]) == schema


def test_extract_url_json(calls, c):
    schema = {"type": "object"}
    c.extract_url("https://example.com/f.pdf", schema)
    assert calls[0]["json"] == {"document": "https://example.com/f.pdf",
                                "schema": schema}


# --- files --------------------------------------------------------------------

def test_upload_requires_workspace_in_form(calls, c):
    c.upload_file_bytes(b"abc", "note.txt", 7046, title="Note")
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/files"
    assert call["files"] == {"file": ("note.txt", b"abc")}
    assert call["data"] == {"workspace_id": "7046", "title": "Note"}
    # le multipart ne doit PAS forcer un Content-Type json
    assert "Content-Type" not in call["headers"]


def test_list_files_filters(calls, c):
    c.list_files(workspace_ids=[7046, 8], search="contrat", status="embedded")
    call = calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE}/files"
    assert call["params"] == {"workspace_id": "7046,8", "search": "contrat",
                              "status": "embedded"}


def test_delete_file_path(calls, c):
    c.delete_file(42)
    call = calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == f"{BASE}/files/42"


# --- erreurs ------------------------------------------------------------------

def test_error_surfaces_lighton_detail(monkeypatch, c):
    monkeypatch.setattr(
        lighton_client.requests, "request",
        lambda *a, **k: _Resp({"code": 404, "error": "not_found",
                               "detail": "No API endpoint at /x."}, 404))
    with pytest.raises(RuntimeError) as exc:
        c.list_workspaces()
    assert "404" in str(exc.value)
    assert "No API endpoint" in str(exc.value)


def test_error_falls_back_to_error_field(monkeypatch, c):
    monkeypatch.setattr(
        lighton_client.requests, "request",
        lambda *a, **k: _Resp({"error": "Permission denied", "code": 403}, 403))
    with pytest.raises(RuntimeError) as exc:
        c.list_workspaces()
    assert "403" in str(exc.value)
    assert "Permission denied" in str(exc.value)


def test_validation_error_dict_surfaced(monkeypatch, c):
    # DRF renvoie parfois {"champ": ["message"]} sans detail/error.
    monkeypatch.setattr(
        lighton_client.requests, "request",
        lambda *a, **k: _Resp({"workspace_id": ["This field is required."]}, 400))
    with pytest.raises(RuntimeError) as exc:
        c.list_workspaces()
    assert "workspace_id" in str(exc.value)
