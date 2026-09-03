"""Fils Productlane — la boîte de retours clients, ses messages et ses commentaires.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`, `_list`, `_check_choice`, `_check_limit`).

Deux plans à ne pas confondre, parce que l'un est PUBLIC et l'autre non :

- un **message** (`/threads/{id}/messages`) part au contact, par le canal d'où
  vient le fil (email, Slack, live chat, Teams) — c'est une communication
  sortante réelle ;
- un **commentaire interne** (`/threads/{id}/comments`) n'est visible que de
  l'équipe.

Les deux s'écrivent avec un champ `content` et des `attachments` : rien dans la
forme de l'appel ne rappelle lequel sort de l'organisation. C'est pourquoi les
deux méthodes portent des noms explicites (`send_message` / `post_comment`) et
que la première le redit dans sa docstring.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..const import (MESSAGE_DIRECTIONS, MESSAGE_ORDERS, MESSAGE_TYPES,
                     PAIN_LEVELS, THREAD_EXPANDS, THREAD_ORIGINS,
                     THREAD_STATUSES, THREAD_TABS)


class _ThreadsMixin:
    """Fils, messages, commentaires internes et liens Linear."""

    # --- fils ---------------------------------------------------------------

    def list_threads(self, limit: Optional[int] = None,
                     cursor: Optional[str] = None,
                     status: Optional[str] = None, tab: Optional[str] = None,
                     assignee_id: Optional[str] = None,
                     contact_id: Optional[str] = None,
                     company_id: Optional[str] = None,
                     tag_id: Optional[str] = None,
                     pain_level: Optional[str] = None,
                     origin: Optional[str] = None,
                     issue_id: Optional[str] = None,
                     project_id: Optional[str] = None,
                     external_id: Optional[str] = None,
                     created_after: Optional[str] = None,
                     created_before: Optional[str] = None,
                     updated_after: Optional[str] = None,
                     updated_before: Optional[str] = None) -> Any:
        """GET /threads — fils de l'espace de travail. Scope `threads:read`.

        Paginé par curseur (`page.cursor` / `page.has_more`), trié
        `created_at DESC` sans possibilité de changer l'ordre côté serveur.
        """
        self._check_choice("status", status, THREAD_STATUSES)
        self._check_choice("tab", tab, THREAD_TABS)
        self._check_choice("pain_level", pain_level, PAIN_LEVELS)
        self._check_choice("origin", origin, THREAD_ORIGINS)
        return self._list("/threads", limit, cursor, {
            "status": status, "tab": tab, "assignee_id": assignee_id,
            "contact_id": contact_id, "company_id": company_id,
            "tag_id": tag_id, "pain_level": pain_level, "origin": origin,
            "issue_id": issue_id, "project_id": project_id,
            "external_id": external_id,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_thread(self, thread_id: str, expand: Optional[Any] = None) -> Any:
        """GET /threads/{id} — un fil. Scope `threads:read`.

        `expand` inline les ressources liées : `messages`, `comments`, ou les
        deux. Accepte une liste ou une chaîne séparée par des virgules.

        ⚠️ **L'amont IGNORE une valeur d'`expand` inconnue** au lieu de la
        refuser : une faute de frappe rendrait un fil sans ses messages, sans un
        mot d'explication. Les valeurs sont donc vérifiées ici.
        """
        if expand is not None:
            values = expand.split(",") if isinstance(expand, str) else list(expand)
            values = [v.strip() for v in values if str(v).strip()]
            for v in values:
                self._check_choice("expand", v, THREAD_EXPANDS)
            expand = ",".join(values) or None
        return self._request("GET", f"/threads/{thread_id}",
                             params={"expand": expand})

    def create_thread(self, payload: Dict[str, Any]) -> Any:
        """POST /threads — crée un fil et **upsert son contact par email**.

        Scope `threads:write`. Requis : `text`, `pain_level`, `contact_email`.
        Optionnels : `external_ids`, `title`, `status`, `origin`, `contact_name`,
        `assignee_id`, `company_id`, `project_id`, `issue_id`, `created_at`,
        `updated_at`, `notify`.

        ⚠️ Le contact est **créé s'il n'existe pas** : cet appel écrit donc dans
        deux tables, et `contact_email` n'est pas qu'un pointeur.
        ⚠️ `notify` déclenche une notification sortante — le laisser absent est
        le comportement discret.
        """
        self._check_choice("pain_level", payload.get("pain_level"), PAIN_LEVELS)
        self._check_choice("status", payload.get("status"), THREAD_STATUSES)
        self._check_choice("origin", payload.get("origin"), THREAD_ORIGINS)
        return self._request("POST", "/threads", json=dict(payload))

    def update_thread(self, thread_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /threads/{id} — met à jour un fil. Scope `threads:write`.

        Champs : `external_ids`, `text`, `title`, `pain_level`, `assignee_id`,
        `contact_id`, `company_id`, `tag_ids`, `project_id`, `status`,
        `snoozed_until`, `closed_loop`, `ai_draft_html`, `ai_draft_sources`,
        `clear_ai_draft_error`, `notify`.

        ⚠️ `tag_ids` REMPLACE la liste des étiquettes, il ne l'enrichit pas.
        """
        self._check_choice("pain_level", payload.get("pain_level"), PAIN_LEVELS)
        self._check_choice("status", payload.get("status"), THREAD_STATUSES)
        return self._request("PATCH", f"/threads/{thread_id}", json=dict(payload))

    def delete_thread(self, thread_id: str) -> Any:
        """DELETE /threads/{id} — **soft-delete** d'un fil. Scope `threads:write`."""
        return self._request("DELETE", f"/threads/{thread_id}")

    # --- messages (SORTANTS) -----------------------------------------------

    def list_messages(self, thread_id: str, limit: Optional[int] = None,
                      cursor: Optional[str] = None, order: Optional[str] = None,
                      type: Optional[str] = None,
                      direction: Optional[str] = None,
                      user_id: Optional[str] = None, q: Optional[str] = None,
                      created_after: Optional[str] = None,
                      created_before: Optional[str] = None,
                      updated_after: Optional[str] = None,
                      updated_before: Optional[str] = None) -> Any:
        """GET /threads/{id}/messages — la conversation, **tous canaux fondus**.

        Scope `threads:read`. Triée du plus ancien au plus récent par défaut
        (`order="asc"`), contrairement aux autres listes v2 qui sont en
        `created_at DESC` : c'est une conversation, elle se lit dans l'ordre.
        """
        self._check_choice("order", order, MESSAGE_ORDERS)
        self._check_choice("type", type, MESSAGE_TYPES)
        self._check_choice("direction", direction, MESSAGE_DIRECTIONS)
        return self._list(f"/threads/{thread_id}/messages", limit, cursor, {
            "order": order, "type": type, "direction": direction,
            "user_id": user_id, "q": q,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def send_message(self, thread_id: str, payload: Dict[str, Any]) -> Any:
        """POST /threads/{id}/messages — **envoie un message au contact**.

        Scope `threads:write`. Requis : `content`. Optionnels : `cc`, `bcc`,
        `attachments`, `channel_id`, `author`.

        ⚠️ **Communication sortante réelle** : le canal (email, Slack, live chat,
        Microsoft Teams) est déduit de l'origine du fil, et la réponse dit lequel
        a servi. Pour une note qui ne sort pas de l'équipe, c'est `post_comment`.

        Un **400 `validation_failed`** signale que l'intégration correspondant au
        canal déduit n'est pas configurée pour l'espace de travail — ce n'est donc
        pas un défaut du contenu envoyé.
        """
        return self._request("POST", f"/threads/{thread_id}/messages",
                             json=dict(payload))

    # --- commentaires internes ---------------------------------------------

    def list_comments(self, thread_id: str, limit: Optional[int] = None,
                      cursor: Optional[str] = None) -> Any:
        """GET /threads/{id}/comments — commentaires internes. Scope `threads:read`.

        Visibles de l'équipe seulement.
        """
        return self._list(f"/threads/{thread_id}/comments", limit, cursor)

    def post_comment(self, thread_id: str, content: str,
                     attachments: Optional[Any] = None) -> Any:
        """POST /threads/{id}/comments — commentaire **interne**. Scope `comments:write`.

        Visible des coéquipiers seulement : rien ne part au contact. C'est la
        contrepartie discrète de `send_message`.
        """
        body: Dict[str, Any] = {"content": content}
        if attachments is not None:
            body["attachments"] = attachments
        return self._request("POST", f"/threads/{thread_id}/comments", json=body)

    def update_comment(self, thread_id: str, comment_id: str,
                       payload: Dict[str, Any]) -> Any:
        """PATCH /threads/{id}/comments/{comment_id} — édite un commentaire interne.

        Scope `comments:write`. Champs : `content`, `attachments`.
        """
        return self._request("PATCH",
                             f"/threads/{thread_id}/comments/{comment_id}",
                             json=dict(payload))

    def delete_comment(self, thread_id: str, comment_id: str) -> Any:
        """DELETE /threads/{id}/comments/{comment_id} — **soft-delete**.

        Scope `comments:write`.
        """
        return self._request("DELETE",
                             f"/threads/{thread_id}/comments/{comment_id}")

    # --- lien vers Linear ---------------------------------------------------

    def link_thread(self, thread_id: str,
                    issue_ids: Optional[Any] = None,
                    project_ids: Optional[Any] = None,
                    priority: Optional[Any] = None) -> Any:
        """POST /threads/{id}/customer-needs — relie le fil à des issues/projets.

        Scope `threads:write`. Passe par le pipeline « customer need » de Linear,
        **qui doit donc être connecté** : sans Linear, l'amont refuse.

        C'est le geste qui transforme un retour client en demande tracée sur la
        roadmap — et ce qui fait qu'un projet Productlane porte un « score ».
        """
        body: Dict[str, Any] = {}
        if issue_ids is not None:
            body["issue_ids"] = issue_ids
        if project_ids is not None:
            body["project_ids"] = project_ids
        if priority is not None:
            body["priority"] = priority
        if not body:
            raise ValueError(
                "relier un fil demande au moins `issue_ids` ou `project_ids`.")
        return self._request("POST", f"/threads/{thread_id}/customer-needs",
                             json=body)
