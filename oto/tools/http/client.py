"""Client du connecteur `http` générique — appel HTTP multi-méthode, multi-auth.

Un simple « nœud HTTP » (comme le nœud HTTP Request de n8n / une action Zapier) :
injecte le mode d'auth configuré (bearer / clé en header ou query / basic /
oauth2 client-credentials) et forwarde la méthode voulue (GET par défaut ; POST /
PUT / PATCH / DELETE avec un corps JSON) vers l'API cible.

La protection SSRF ne vit PAS ici : comme dans les produits du marché (Zapier,
Make, n8n, GPT Actions…), le trafic sortant initié par un tenant est filtré au
niveau **réseau/egress** de la plateforme, pas par du code par-connecteur.

Pur (`requests` seul) — la résolution du credential et la traduction en erreurs
MCP vivent dans l'adaptateur `oto_mcp/tools/http.py`.
"""
from __future__ import annotations

import threading
import time
from abc import ABC
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPBasicAuth

AUTH_MODES = ("bearer", "header", "query", "basic", "oauth2", "none")


# --- modes d'auth --------------------------------------------------------------

class UpstreamAuth(ABC):
    """Interface d'injection d'auth dans les requêtes sortantes."""

    def configure(self, session: requests.Session) -> None:  # noqa: B027
        """Applique l'auth à une session fraîche. No-op par défaut."""

    def query_params(self) -> dict:
        return {}

    def refresh(self, session: requests.Session) -> None:  # noqa: B027
        """Ré-authentifie après un 401. No-op par défaut (credential statique)."""


class NoAuth(UpstreamAuth):
    """Pas d'authentification (API publique)."""


class StaticBearer(UpstreamAuth):
    def __init__(self, token: str):
        if not token:
            raise ValueError("StaticBearer: token vide")
        self._token = token

    def configure(self, session: requests.Session) -> None:
        session.headers["Authorization"] = f"Bearer {self._token}"


class ApiKeyHeader(UpstreamAuth):
    def __init__(self, name: str, value: str):
        if not name or not value:
            raise ValueError("ApiKeyHeader: name/value requis")
        self._name, self._value = name, value

    def configure(self, session: requests.Session) -> None:
        session.headers[self._name] = self._value


class ApiKeyQuery(UpstreamAuth):
    def __init__(self, param: str, value: str):
        if not param or not value:
            raise ValueError("ApiKeyQuery: param/value requis")
        self._param, self._value = param, value

    def query_params(self) -> dict:
        return {self._param: self._value}


class BasicAuth(UpstreamAuth):
    def __init__(self, username: str, password: str):
        if not username:
            raise ValueError("BasicAuth: username requis")
        self._auth = HTTPBasicAuth(username, password)

    def configure(self, session: requests.Session) -> None:
        session.auth = self._auth


class OAuth2ClientCredentials(UpstreamAuth):
    """OAuth2 client-credentials : fetch d'un access_token + cache TTL."""

    def __init__(self, token_url: str, client_id: str, client_secret: str,
                 scope: str | None = None, timeout: int = 30, leeway: int = 30):
        if not token_url or not client_id or not client_secret:
            raise ValueError("OAuth2ClientCredentials: token_url/client_id/client_secret requis")
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._timeout = timeout
        self._leeway = leeway
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expiry = 0.0

    def _fetch(self) -> str:
        data = {"grant_type": "client_credentials",
                "client_id": self._client_id, "client_secret": self._client_secret}
        if self._scope:
            data["scope"] = self._scope
        r = requests.post(self._token_url, data=data, timeout=self._timeout)
        r.raise_for_status()
        payload = r.json()
        token = payload.get("access_token")
        if not token:
            raise ValueError("OAuth2ClientCredentials: réponse sans access_token")
        self._token = token
        self._expiry = time.monotonic() + int(payload.get("expires_in", 3600)) - self._leeway
        return token

    def _current(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() >= self._expiry:
                return self._fetch()
            return self._token

    def configure(self, session: requests.Session) -> None:
        session.headers["Authorization"] = f"Bearer {self._current()}"

    def refresh(self, session: requests.Session) -> None:
        with self._lock:
            self._fetch()
        session.headers["Authorization"] = f"Bearer {self._token}"


def build_auth(mode: str, fields: dict) -> UpstreamAuth:
    """Construit l'`UpstreamAuth` d'un `auth_mode` + les champs de credential.

    Lève `ValueError` (message actionnable) si le mode est inconnu ou s'il manque
    un champ requis."""
    mode = (mode or "").strip().lower()

    def val(key: str) -> str:
        return (fields.get(key) or "").strip()

    def need(*keys: str) -> None:
        missing = [k for k in keys if not val(k)]
        if missing:
            raise ValueError(f"auth_mode={mode!r} exige : {', '.join(missing)}")

    if mode == "bearer":
        need("token"); return StaticBearer(val("token"))
    if mode == "header":
        need("header_name", "token"); return ApiKeyHeader(val("header_name"), val("token"))
    if mode == "query":
        need("query_param", "token"); return ApiKeyQuery(val("query_param"), val("token"))
    if mode == "basic":
        need("username", "password"); return BasicAuth(val("username"), fields.get("password") or "")
    if mode == "oauth2":
        need("token_url", "client_id", "client_secret")
        return OAuth2ClientCredentials(val("token_url"), val("client_id"),
                                       val("client_secret"), scope=val("scope") or None)
    if mode == "none":
        return NoAuth()
    raise ValueError(f"auth_mode inconnu: {mode!r} (attendu: {'|'.join(AUTH_MODES)})")


# --- forward -------------------------------------------------------------------

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class HttpConnectorClient:
    """Nœud HTTP : (base_url, auth_mode, fields) → `.request(method, path, …)`.

    Injecte l'auth, forwarde la méthode (GET/POST/PUT/PATCH/DELETE), retry unique
    après ré-auth sur 401. `.get()`/`.post()` = raccourcis. Lève `ValueError` sur
    config invalide (schéma non http(s), mode/champ/méthode invalide).

    Comme le nœud HTTP de n8n/Make : les méthodes d'écriture sont transportées ;
    la responsabilité de ce qu'elles font est celle de l'API cible (et, pour un
    bridge en aval, de SA propre allowlist)."""

    def __init__(self, base_url: str, auth_mode: str, fields: dict, *, timeout: int = 45):
        base_url = (base_url or "").strip().rstrip("/")
        if urlsplit(base_url).scheme not in ("http", "https"):
            raise ValueError("base_url doit être en http(s)")
        self._base_url = base_url
        self._auth = build_auth(auth_mode, fields)
        self._timeout = timeout
        self._session: requests.Session | None = None

    def _ready(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            self._auth.configure(s)
            self._session = s
        return self._session

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | list | None = None,
    ) -> dict:
        method = (method or "").strip().upper()
        if method not in METHODS:
            raise ValueError(f"méthode invalide: {method!r} (attendu: {'|'.join(METHODS)})")
        if not path.startswith("/"):
            raise ValueError("path doit commencer par / (relatif à base_url)")
        s = self._ready()
        url = self._base_url + path

        def _send():
            merged = {**(params or {}), **self._auth.query_params()}
            return s.request(method, url, params=merged or None, json=json,
                             timeout=self._timeout)

        r = _send()
        if r.status_code == 401:
            self._auth.refresh(s)
            r = _send()
        r.raise_for_status()
        return r.json() if r.content else {}

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, json: dict | list | None = None,
             params: dict | None = None) -> dict:
        return self.request("POST", path, params=params, json=json)
