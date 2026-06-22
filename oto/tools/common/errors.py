"""Erreurs typées des connecteurs.

`UpstreamHTTPError` distingue un **refus de l'API tierce** (status HTTP >= 400 :
input rejeté, credential invalide, cible absente, rate limit…) d'un **bug interne**
du code. Le `status_code` permet aux consommateurs (adaptateur MCP, error tracking)
de router un 4xx comme *erreur de connecteur gérée* — tracée dans le backlog d'appels,
renvoyée proprement à l'agent — plutôt que comme un défaut du backend à alerter.

`raise_for_upstream(resp, service=...)` remplace le bloc dupliqué
`if resp.status_code >= 400: parse body; raise Exception(...)` présent dans chaque
client. Agnostique `requests`/`httpx` (mêmes `.status_code` / `.json()` / `.text`).
"""
from __future__ import annotations

from typing import Any, Optional


class UpstreamHTTPError(Exception):
    """Une API tierce a répondu en erreur (status >= 400).

    `status_code` = code HTTP amont, `body` = corps parsé (dict) ou texte brut,
    `service` = nom du connecteur (préfixe le message, ex. « folk HTTP 422: … »).
    """

    def __init__(self, status_code: int, body: Any = None, *, service: Optional[str] = None):
        self.status_code = status_code
        self.body = body
        self.service = service
        prefix = f"{service} " if service else ""
        super().__init__(f"{prefix}HTTP {status_code}: {body}")

    @property
    def is_client_error(self) -> bool:
        """4xx — la requête était mauvaise (notre input / nos credentials)."""
        return 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        """5xx — l'amont est cassé."""
        return 500 <= self.status_code < 600


def raise_for_upstream(resp: Any, *, service: Optional[str] = None) -> None:
    """Lève `UpstreamHTTPError` si `resp.status_code >= 400`, sinon no-op.

    Parse le corps en JSON, retombe sur le texte brut. Compatible `requests.Response`
    et `httpx.Response`.
    """
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise UpstreamHTTPError(resp.status_code, body, service=service)
