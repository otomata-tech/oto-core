"""Événements de réunion Leexi — et l'assistant qu'on y envoie.

Ce mixin n'est jamais instancié seul : il est composé dans `LeexiClient`, qui
fournit le transport (`_request`, `_list`, `_check_choice`).

Un « meeting event » est une réunion CONNUE de Leexi (venue du calendrier, d'une
saisie manuelle, ou de cette API) — distincte d'un « call », qui est un
enregistrement déjà traité. L'assistant se lance sur le premier et produit le second.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..const import (MEETING_DATE_FILTERS, MEETING_ORDERS, MEETING_ORIGINS)


class _MeetingsMixin:
    """Événements de réunion et assistant."""

    def list_meeting_events(self, page: Optional[int] = None,
                            items: Optional[int] = None,
                            order: Optional[str] = None,
                            origin: Optional[str] = None,
                            date_filter: Optional[str] = None,
                            date_from: Optional[str] = None,
                            date_to: Optional[str] = None) -> Any:
        """GET /v1/meeting_events — réunions connues. Scope `read_meeting_events`.

        `origin` distingue ce qui vient du calendrier, d'une saisie manuelle ou de
        l'API (`calendar` / `manual` / `api`). `date_from`/`date_to` bornent le
        champ nommé par `date_filter` (défaut `start_time`) — préfixés ici parce
        que `from` est un mot réservé de Python, envoyés en `from`/`to` sur le fil.
        """
        self._check_choice("order", order, MEETING_ORDERS)
        self._check_choice("origin", origin, MEETING_ORIGINS)
        self._check_choice("date_filter", date_filter, MEETING_DATE_FILTERS)
        return self._list("/meeting_events", page, items, {
            "order": order, "origin": origin, "date_filter": date_filter,
            "from": date_from, "to": date_to,
        })

    def get_meeting_event(self, uuid: str) -> Any:
        """GET /v1/meeting_events/{uuid} — une réunion. Scope `read_meeting_events`."""
        return self._request("GET", f"/meeting_events/{uuid}")

    def create_meeting_event(self, payload: Dict[str, Any]) -> Any:
        """POST /v1/meeting_events — déclare une réunion. Scope `write_meeting_events`.

        Requis : `end_time`, `internal`, `meeting_url`, `organizer`, `owned`,
        `start_time`, `to_record`, `user_uuid`. Optionnels : `attendees`,
        `description`, `direction` (`inbound`/`outbound`), `title`.

        `to_record=True` demande l'enregistrement de la réunion. Une réunion déjà
        déclarée (même URL, même créneau) rend **409**.
        """
        return self._request("POST", "/meeting_events", json=dict(payload))

    def delete_meeting_event(self, uuid: str) -> Any:
        """DELETE /v1/meeting_events/{uuid} — retire une réunion. Scope `write_meeting_events`."""
        return self._request("DELETE", f"/meeting_events/{uuid}")

    def launch_meeting_assistant(self, uuid: str,
                                 stop_task: Optional[bool] = None) -> Any:
        """POST /v1/meeting_events/{uuid}/launch_bot — envoie (ou retire) l'assistant.

        Scope `write_meeting_events`.

        ⚠️ **Un seul endpoint pour les deux sens** : `stop_task=True` ARRÊTE un bot
        en cours au lieu d'en lancer un. Le nom amont (`launch_bot`) ne le dit pas,
        d'où ce paramètre explicite plutôt que deux méthodes qui mentiraient.

        Un bot déjà lancé rend **409** ; **405** signale une action impossible pour
        cet événement (réunion passée, sans URL exploitable…).
        """
        body: Dict[str, Any] = {}
        if stop_task is not None:
            body["stop_task"] = stop_task
        return self._request("POST", f"/meeting_events/{uuid}/launch_bot",
                             json=body or None)
