"""Messagerie : inboxes, fils, messages, participants, état du fil.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

from ..errors import UnipileError

logger = logging.getLogger(__name__)


class _MessagingMixin:
    """Messagerie : inboxes, fils, messages, participants, état du fil."""

    def list_inboxes(self) -> dict:
        """Inboxes du compte (v2 : `GET /v2/{account}/inboxes`). LinkedIn classic :
        `CLASSIC_PRIMARY` (principale), `CLASSIC_ARCHIVED`, `CLASSIC_SPAM`,
        `CLASSIC_JOBS`, `CLASSIC_INMAIL`, `CLASSIC_STARRED`."""
        return self._norm(self._request("GET", self._acct("/inboxes")))

    def list_chats(self, limit: int = 20, cursor: Optional[str] = None,
                   with_attendee_names: bool = False,
                   inbox: str = "CLASSIC_PRIMARY") -> dict:
        """Fils de messagerie, dans la forme d'endpoint du provider (`_by_shape`) :
        **par inbox** pour LinkedIn (`GET /v2/{account}/inboxes/{inbox}/chats` —
        l'ancien `/chats` y renvoie 501 « Use List inbox Chats endpoint », delta live
        2026-07-06), **à plat** pour les providers sans inbox (WhatsApp, Telegram,
        Instagram, Messenger, Twitter), où c'est la forme inbox qui rend 501.
        `inbox` (LinkedIn) défaut = `CLASSIC_PRIMARY` ; autres via `list_inboxes`."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._norm(self._by_shape(
            lambda: self._request(
                "GET", self._acct(f"/inboxes/{quote(inbox, safe='')}/chats"),
                params=params),
            lambda: self._request("GET", self._acct("/chats"), params=params),
            "list_chats"))
        if with_attendee_names:
            self._annotate_chat_attendees(data)
        return data

    def resolve_attendee_names(self, provider_ids, max_pages: int = 10,
                               page_limit: int = 100) -> dict:
        """Résout des `attendee_provider_id` via le carnet de contacts v2
        (`/v2/{account}/contacts`, paginé). Best-effort."""
        wanted = {str(p) for p in provider_ids if p}
        out: dict[str, dict] = {}
        cursor = None
        for _ in range(max_pages):
            if not wanted - out.keys():
                break
            page = self.list_attendees(cursor=cursor, limit=page_limit)
            items = (page or {}).get("items") or []
            for att in items:
                if not isinstance(att, dict):
                    continue
                pid = str(att.get("provider_id") or att.get("id") or "")
                if pid in wanted:
                    out[pid] = att
            cursor = (page or {}).get("cursor")
            if not items or not cursor:
                break
        return out

    def _annotate_chat_attendees(self, data: Any) -> None:
        """Enrichit in-place les fils d'un `/chats` avec le nom de l'interlocuteur.

        Best-effort : ne lève jamais (la liste prime sur l'enrichissement). Mais
        best-effort ne veut pas dire MUET — l'appelant a demandé cet enrichissement, et
        la description de l'outil le lui promet ; s'il n'a pas eu lieu il doit
        l'apprendre par la réponse, pas le déduire d'une absence.

        Le cas qui a coûté (signal #682, 03/09/2026) : sur ~40 fils, l'agent a conclu
        que « l'enrichissement annoncé dans la doc n'apparaît pas », sans pouvoir
        distinguer une panne de résolution d'un carnet de contacts incomplet. Le
        journal, lui, portait déjà l'avertissement — côté serveur, où l'appelant ne le
        lit jamais.

        Le pire des quatre silences n'est pas la panne, c'est la résolution PARTIELLE :
        elle rend une liste hétérogène où l'absence de `attendee_name` se lit « ce fil
        n'a pas d'interlocuteur » au lieu de « je n'ai pas su le nommer ».

        Ne se dit QUE sur écart : tout résolu ⟹ rien à annoncer, l'appelant voit les
        noms — et une page de fils pèse déjà ~105 Ko, on n'y ajoute pas du bruit."""
        items = (data or {}).get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return

        def _dire(status: str, **kw) -> None:
            if isinstance(data, dict):
                data["attendee_names"] = {"status": status, **kw}

        ids = {str(it.get("attendee_provider_id"))
               for it in items
               if isinstance(it, dict) and it.get("attendee_provider_id")}
        if not ids:
            if items:
                _dire("unavailable", asked=0, resolved=0,
                      reason="aucun fil ne porte d'`attendee_provider_id` : rien à "
                             "résoudre. Le nom de l'interlocuteur reste lisible dans "
                             "`name` (fils 1-à-1) ou via `last_message.sender`.")
            return
        try:
            resolved = self.resolve_attendee_names(ids)
        except Exception as e:  # noqa: BLE001 — enrichissement best-effort voulu
            logger.warning("unipile chats: résolution attendees échouée, "
                           "liste servie sans enrichissement", exc_info=True)
            _dire("unavailable", asked=len(ids), resolved=0,
                  reason=f"la résolution des contacts a échoué ({type(e).__name__}) : "
                         "les fils sont servis SANS `attendee_name`. Ce n'est pas "
                         "l'absence d'interlocuteur — replie-toi sur `name` ou "
                         "`last_message.sender`, ou rejoue l'appel.")
            return
        manquants = []
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("attendee_provider_id") or "")
            att = resolved.get(pid)
            if not att:
                if pid:
                    manquants.append(pid)
                continue
            it["attendee_name"] = att.get("name")
            it["attendee_headline"] = (att.get("specifics") or {}).get("occupation")
            it["attendee_profile_url"] = att.get("profile_url")
        if manquants:
            _dire("partial", asked=len(ids), resolved=len(ids) - len(set(manquants)),
                  missing_ids=sorted(set(manquants))[:20],
                  reason="ces interlocuteurs sont absents du carnet de contacts : leurs "
                         "fils n'ont PAS de `attendee_name`, ce qui ne veut pas dire "
                         "qu'ils n'ont pas d'interlocuteur. Nomme-les par "
                         "`last_message.sender`.")

    def list_messages(self, chat_id: str, limit: int = 50) -> dict:
        params = {"limit": limit}
        return self._norm(self._request(
            "GET", self._acct(f"/chats/{quote(chat_id, safe='')}/messages"),
            params=params,
        ))

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        attendee_id: Optional[str] = None,
        inbox: str = "CLASSIC_PRIMARY",
    ) -> dict:
        if chat_id:
            return self._request(
                "POST", self._acct(f"/chats/{quote(chat_id, safe='')}/messages/send"),
                json={"text": text},
            )
        if not attendee_id:
            raise UnipileError("send_message : chat_id ou attendee_id requis.")
        # v2 : pour un provider à INBOX (LinkedIn), le nouveau fil passe par l'inbox —
        # `POST /v2/{account}/inboxes/{inbox}/chats/send`. Le `/chats/send` générique y
        # renvoie 501 « Use Start a Chat in the given inbox endpoint for this provider »
        # (relevé live 2026-07-08 — même modèle inbox que list_chats, signal #199/#200) ;
        # sans inbox (WhatsApp & co.), c'est l'inverse. Même corps des deux côtés
        # (`users_ids`, qui remplace `attendees_ids` de v1) : seule la route change.
        body = {"users_ids": [attendee_id], "text": text}
        return self._by_shape(
            lambda: self._request(
                "POST", self._acct(f"/inboxes/{quote(inbox, safe='')}/chats/send"),
                json=body),
            lambda: self._request("POST", self._acct("/chats/send"), json=body),
            "send_message",
        )

    def list_chat_attendees(self, chat_id: str) -> dict:
        """Participants d'un fil. v2 : `/chats/{chat_id}/participants`."""
        return self._norm(self._request(
            "GET", self._acct(f"/chats/{quote(chat_id, safe='')}/participants")
        ))

    def list_attendees(self, cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        """Carnet de contacts. v2 : `/v2/{account}/contacts`."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/contacts"), params=params
        ))

    # v2 updateChat : champs dédiés (plus le couple {action, value}).
    _CHAT_ACTION_FIELD = {
        "setReadStatus": "read_status",
        "setMuteStatus": "muted_until",
        "setArchiveStatus": "archive_status",
        "setPinnedStatus": "pin_status",
        "setLabel": "label",
    }

    def patch_chat(self, chat_id: str, action: str, value: Any = None) -> dict:
        """Modifie l'état d'un fil (`PATCH /chats/{id}`)."""
        field = self._CHAT_ACTION_FIELD.get(action)
        if field is None:
            raise UnipileError(
                f"patch_chat : action {action!r} non supportée "
                f"({', '.join(self._CHAT_ACTION_FIELD)})."
            )
        return self._request(
            "PATCH", self._acct(f"/chats/{quote(chat_id, safe='')}"),
            json={field: value},
        )

    def react_message(self, message_id: str, reaction: str,
                      chat_id: Optional[str] = None) -> dict:
        """Réagit à un message. v2 exige le `chat_id` (route sous le fil)."""
        if not chat_id:
            raise UnipileError(
                "react_message : chat_id requis "
                "(route /chats/{chat_id}/messages/{message_id}/reactions)."
            )
        return self._request(
            "POST",
            self._acct(
                f"/chats/{quote(chat_id, safe='')}"
                f"/messages/{quote(message_id, safe='')}/reactions"
            ),
            json={"reaction": reaction},
        )

