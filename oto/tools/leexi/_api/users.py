"""Utilisateurs Leexi — et les licences qu'ils consomment.

Ce mixin n'est jamais instancié seul : il est composé dans `LeexiClient`, qui
fournit le transport (`_request`, `_list`).

⚠️ Les trois écritures d'ici exigent le scope `write_users`, **qui engage les
licences facturées** — un admin Leexi doit l'accorder explicitement, une clé
neuve ne l'a pas. Le cran est chez l'éditeur ; ce client ne le contourne pas.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class _UsersMixin:
    """Utilisateurs de l'espace de travail."""

    def list_users(self, page: Optional[int] = None,
                   items: Optional[int] = None) -> Any:
        """GET /v1/users — utilisateurs de l'espace de travail. Scope `read_users`."""
        return self._list("/users", page, items)

    def get_user(self, uuid: str) -> Any:
        """GET /v1/users/{uuid} — un utilisateur. Scope `read_users`."""
        return self._request("GET", f"/users/{uuid}")

    def create_user(self, payload: Dict[str, Any]) -> Any:
        """POST /v1/users — crée un utilisateur. Scope `write_users`.

        Requis : `email`, `name`, `team_uuid` (cf. `list_teams`). Optionnels :
        `active`, `license`, `roles` (liste), `send_welcome_email`.

        ⚠️ **Consomme une licence facturée**, et l'utilisateur reçoit un email de
        bienvenue sauf `send_welcome_email=False`. Un email déjà pris rend 409.
        """
        return self._request("POST", "/users", json=dict(payload))

    def update_user(self, uuid: str, payload: Dict[str, Any]) -> Any:
        """PATCH /v1/users/{uuid} — met à jour un utilisateur. Scope `write_users`.

        Champs : `active`, `email`, `license`, `name`, `roles`, `team_uuid`.
        `active=True` **réactive** un utilisateur désactivé, ce qui reprend une
        licence — c'est une écriture facturante, au même titre que la création.
        """
        return self._request("PATCH", f"/users/{uuid}", json=dict(payload))

    def deactivate_user(self, uuid: str) -> Any:
        """DELETE /v1/users/{uuid} — **désactive** (ne supprime pas). Scope `write_users`.

        Les appels et l'historique sont conservés, les sessions révoquées, et la
        licence cesse d'être consommée. Réactivation par `update_user(active=True)`.
        Le verbe HTTP dit « delete », l'effet est une désactivation : c'est le nom
        de cette méthode qui est exact, et il l'est délibérément.
        """
        return self._request("DELETE", f"/users/{uuid}")
