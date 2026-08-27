"""Types d'erreur du connecteur Unipile (et le parsing du délai 429).

Extrait de `client.py` — contenu inchangé, réexporté par `client.py`.
"""

from __future__ import annotations

import re
from typing import Optional


class UnipileError(RuntimeError):
    """Erreur API Unipile, message remonté tel quel.

    `status_code` = code HTTP amont quand l'erreur vient d'une réponse Unipile
    (même contrat que `oto.tools.common.UpstreamHTTPError` : permet aux
    consommateurs de router un 4xx comme erreur gérée, pas un bug), None sinon
    (erreur réseau, config, identity mismatch).
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class UnipileRateLimited(UnipileError):
    """429 Unipile : quota amont atteint. LinkedIn cappe les fiches société/profil
    à ~100/12h PAR COMPTE (« We only allow 100 requests. Retry in N hours »). Type
    dédié + délai parsé → l'appelant STOPPE au lieu de marteler (251 appels perdus
    en 12h vécu 2026-07-21). `retry_after` = secondes avant réessai, None si illisible."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


_RETRY_RE = re.compile(r"retry in\s+(\d+)\s*(hour|hr|minute|min|second|sec)", re.I)


def _parse_retry_after(msg: str) -> Optional[int]:
    """Secondes avant réessai depuis un corps 429 (« Retry in 12 hours »). None sinon."""
    m = _RETRY_RE.search(msg or "")
    if not m:
        return None
    return int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2).lower()[0]]
