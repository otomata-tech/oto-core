"""Réseau & outreach : relations et invitations.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from ..const import cursor_with_limit
from ..errors import UnipileError


class _NetworkMixin:
    """Réseau & outreach : relations et invitations."""

    def list_relations(self, cursor: Optional[str] = None,
                       limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            # Le limit de l'appel prime sur celui figé dans le cursor (#179).
            params["cursor"] = cursor_with_limit(cursor, limit) if limit else cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/users/me/relations"), params=params
        ))

    def list_invitations(self, direction: str = "received",
                         limit: Optional[int] = None,
                         cursor: Optional[str] = None) -> dict:
        """Invitations — v2 : `GET /v2/{account}/users/me/relation-requests`,
        `type=sent|received`. `limit` est un vrai param serveur (plus de curseur
        qui fige le limit, cf. #179)."""
        params: dict[str, Any] = {
            "type": "sent" if direction == "sent" else "received"
        }
        if limit:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return self._norm(self._request(
            "GET", self._acct("/users/me/relation-requests"), params=params
        ))

    def send_invitation(self, provider_id: str,
                        message: Optional[str] = None) -> dict:
        """v2 : `POST /users/me/relation-requests`, corps `{user_id, message}`."""
        body: dict[str, Any] = {"user_id": provider_id}
        if message:
            body["message"] = message
        return self._request(
            "POST", self._acct("/users/me/relation-requests"), json=body
        )

    def handle_invitation(
        self, invitation_id: str, shared_secret: str, action: str = "accept"
    ) -> dict:
        """Accepte/refuse une invitation REÇUE. v2 : `request_id` suffit (plus de
        `shared_secret`, gardé dans la signature pour compat appelant). accept →
        `/accept` ; decline → `/cancel`."""
        if action not in ("accept", "decline"):
            raise UnipileError("handle_invitation : action = 'accept' ou 'decline'.")
        verb = "accept" if action == "accept" else "cancel"
        return self._request(
            "POST",
            self._acct(
                f"/users/me/relation-requests/{quote(invitation_id, safe='')}/{verb}"
            ),
        )

    def cancel_invitation(self, invitation_id: str) -> dict:
        """Annule une invitation ENVOYÉE. v2 : `/relation-requests/{id}/cancel`."""
        return self._request(
            "POST",
            self._acct(
                f"/users/me/relation-requests/{quote(invitation_id, safe='')}/cancel"
            ),
        )

