"""Équipes Leexi — la structure qui porte les utilisateurs et leurs appels.

Ce mixin n'est jamais instancié seul : il est composé dans `LeexiClient`, qui
fournit le transport (`_request`, `_list`). Scope `write_teams` pour les
écritures — il engage lui aussi les licences facturées, et s'accorde côté admin.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class _TeamsMixin:
    """Équipes de l'espace de travail."""

    def list_teams(self, page: Optional[int] = None,
                   items: Optional[int] = None) -> Any:
        """GET /v1/teams — équipes de l'espace de travail. Scope `read_teams`."""
        return self._list("/teams", page, items)

    def get_team(self, uuid: str) -> Any:
        """GET /v1/teams/{uuid} — une équipe. Scope `read_teams`."""
        return self._request("GET", f"/teams/{uuid}")

    def create_team(self, payload: Dict[str, Any]) -> Any:
        """POST /v1/teams — crée une équipe. Scope `write_teams`.

        Requis : `name`. Optionnel : `active`. L'équipe hérite des réglages de la
        société et reçoit les modèles d'email par défaut. Un nom déjà pris rend 409.
        """
        return self._request("POST", "/teams", json=dict(payload))

    def update_team(self, uuid: str, payload: Dict[str, Any]) -> Any:
        """PATCH /v1/teams/{uuid} — met à jour une équipe. Scope `write_teams`.

        Champs : `active`, `name`. `active=False` est la façon **recommandée par
        l'éditeur** de retirer une équipe qui porte encore des utilisateurs ou des
        appels — `delete_team` la refuserait.
        """
        return self._request("PATCH", f"/teams/{uuid}", json=dict(payload))

    def delete_team(self, uuid: str) -> Any:
        """DELETE /v1/teams/{uuid} — supprime une équipe. Scope `write_teams`.

        ⚠️ Ne passe QUE sur une équipe sans utilisateur ni appel ; sinon **422**.
        Pour toutes les autres, `update_team(uuid, {"active": False})`.
        """
        return self._request("DELETE", f"/teams/{uuid}")
