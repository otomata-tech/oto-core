"""Centre d'aide Productlane — articles, groupes, et la file de brouillons.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`, `_list`, `_check_choice`).

**Deux chemins d'écriture, et ils ne servent pas la même chose :**

- l'écriture DIRECTE (`create_article`, `update_article`, `delete_article`)
  applique tout de suite ;
- le BROUILLON (`create_draft` puis `accept_draft` / `decline_draft`) propose un
  changement à relire. `kind="edit"` modifie un article existant, `create` en
  propose un nouveau, `delete` propose son retrait.

⚠️ `accept_draft` peut répondre **`superseded` au lieu de `accepted`** : le
brouillon ne s'applique plus proprement (l'article a bougé sous lui). C'est un
succès HTTP qui n'a rien appliqué — lire le statut rendu, pas seulement le code.

⚠️ La `visibility` d'un article n'est pas binaire : `public`, `agent` (visible
des agents IA), `internal`, `unlisted`. `all` n'existe qu'en FILTRE de liste — un
article ne peut pas « être » de visibilité `all`, d'où deux constantes distinctes.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..const import (DOC_KIND_FILTERS, DOC_VISIBILITIES,
                     DOC_VISIBILITY_FILTERS, DRAFT_KINDS, DRAFT_STATUSES)


class _DocsMixin:
    """Articles, groupes et brouillons du centre d'aide."""

    # --- articles -----------------------------------------------------------

    def list_articles(self, limit: Optional[int] = None,
                      cursor: Optional[str] = None,
                      group_id: Optional[str] = None,
                      published: Optional[Any] = None,
                      archived: Optional[Any] = None,
                      visibility: Optional[str] = None,
                      kind: Optional[str] = None,
                      title_contains: Optional[str] = None,
                      portal_instance_id: Optional[str] = None,
                      created_after: Optional[str] = None,
                      created_before: Optional[str] = None,
                      updated_after: Optional[str] = None,
                      updated_before: Optional[str] = None,
                      language: Optional[str] = None) -> Any:
        """GET /docs/articles — articles du centre d'aide. Scope `docs:read`.

        `visibility` et `kind` acceptent ici `all`, qui n'est PAS une valeur
        d'écriture (cf. l'en-tête de module).
        """
        self._check_choice("visibility", visibility, DOC_VISIBILITY_FILTERS)
        self._check_choice("kind", kind, DOC_KIND_FILTERS)
        return self._list("/docs/articles", limit, cursor, {
            "group_id": group_id, "published": published, "archived": archived,
            "visibility": visibility, "kind": kind,
            "title_contains": title_contains,
            "portal_instance_id": portal_instance_id,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
            "language": language,
        })

    def get_article(self, article_id: str,
                    language: Optional[str] = None) -> Any:
        """GET /docs/articles/{id} — un article. Scope `docs:read`."""
        return self._request("GET", f"/docs/articles/{article_id}",
                             params={"language": language})

    def create_article(self, payload: Dict[str, Any]) -> Any:
        """POST /docs/articles — crée un article. Scope `docs:write`.

        Requis : `title`, `content`, `group_id`. Optionnels : `summary`,
        `portal_instance_id`, `published`, `visibility`, `icon`, `language`.

        `content` est du **markdown** (titres, listes à puces, listes numérotées…).
        `group_id` est requis : un article naît dans un groupe (cf. `list_groups`).
        """
        self._check_choice("visibility", payload.get("visibility"),
                           DOC_VISIBILITIES)
        return self._request("POST", "/docs/articles", json=dict(payload))

    def update_article(self, article_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /docs/articles/{id} — met à jour un article. Scope `docs:write`.

        Champs : `title`, `content`, `allow_image_removal`, `summary`,
        `published`, `visibility`, `archived`, `show_on_home_page`, `group_id`,
        `portal_instance_id`, `icon`, `language`.

        ⚠️ `allow_image_removal` autorise la réécriture du contenu à SUPPRIMER
        des images qui n'y figurent plus. Sans lui, elles sont conservées — c'est
        un garde-fou de l'éditeur contre une perte par recopie partielle.
        """
        self._check_choice("visibility", payload.get("visibility"),
                           DOC_VISIBILITIES)
        return self._request("PATCH", f"/docs/articles/{article_id}",
                             json=dict(payload))

    def delete_article(self, article_id: str) -> Any:
        """DELETE /docs/articles/{id} — supprime un article. Scope `docs:write`."""
        return self._request("DELETE", f"/docs/articles/{article_id}")

    def move_articles(self, article_ids: Any, group_id: Optional[str]) -> Any:
        """POST /docs/articles/move — réaffecte des articles à un groupe.

        Scope `docs:write`. `group_id=None` les **dégroupe** — c'est une valeur
        signifiante, pas une absence, donc elle est envoyée telle quelle.
        """
        if not article_ids:
            raise ValueError("`article_ids` requis : au moins un article à déplacer.")
        return self._request("POST", "/docs/articles/move",
                             json={"article_ids": list(article_ids),
                                   "group_id": group_id})

    # --- groupes ------------------------------------------------------------

    def list_groups(self, portal_instance_id: Optional[str] = None) -> Any:
        """GET /docs/groups — groupes d'articles. Scope `docs:read`.

        ⚠️ **Pas de pagination** sur cet endpoint : il rend tout d'un coup.
        """
        return self._request("GET", "/docs/groups",
                             params={"portal_instance_id": portal_instance_id})

    def create_group(self, name: str,
                     portal_instance_id: Optional[str] = None) -> Any:
        """POST /docs/groups — crée un groupe d'articles. Scope `docs:write`."""
        if not name:
            raise ValueError("`name` requis.")
        body: Dict[str, Any] = {"name": name}
        if portal_instance_id is not None:
            body["portal_instance_id"] = portal_instance_id
        return self._request("POST", "/docs/groups", json=body)

    def update_group(self, group_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /docs/groups/{id} — met à jour un groupe. Scope `docs:write`.

        Champs : `name`, `order`, `portal_instance_id`.
        """
        return self._request("PATCH", f"/docs/groups/{group_id}",
                             json=dict(payload))

    def delete_group(self, group_id: str) -> Any:
        """DELETE /docs/groups/{id} — supprime un groupe. Scope `docs:write`.

        Les articles qu'il contenait ne sont PAS supprimés : ils sont dégroupés.
        """
        return self._request("DELETE", f"/docs/groups/{group_id}")

    # --- brouillons ---------------------------------------------------------

    def list_drafts(self, limit: Optional[int] = None,
                    cursor: Optional[str] = None,
                    kind: Optional[str] = None,
                    status: Optional[str] = None,
                    article_id: Optional[str] = None,
                    group_id: Optional[str] = None,
                    portal_instance_id: Optional[str] = None,
                    created_after: Optional[str] = None,
                    created_before: Optional[str] = None,
                    updated_after: Optional[str] = None,
                    updated_before: Optional[str] = None) -> Any:
        """GET /docs/drafts — brouillons en attente de relecture. Scope `docs:read`.

        ⚠️ `status` prend ici les valeurs de BROUILLON (`draft`, `open`,
        `accepted`, `rejected`, `superseded`) — rien à voir avec le `status` d'un
        fil (`open`/`snoozed`/`done`), qui porte le même nom ailleurs.
        """
        self._check_choice("kind", kind, DRAFT_KINDS)
        self._check_choice("status", status, DRAFT_STATUSES)
        return self._list("/docs/drafts", limit, cursor, {
            "kind": kind, "status": status, "article_id": article_id,
            "group_id": group_id, "portal_instance_id": portal_instance_id,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_draft(self, draft_id: str) -> Any:
        """GET /docs/drafts/{id} — un brouillon. Scope `docs:read`."""
        return self._request("GET", f"/docs/drafts/{draft_id}")

    def create_draft(self, payload: Dict[str, Any]) -> Any:
        """POST /docs/drafts — propose un changement à relire. Scope `docs:write`.

        Requis : `kind` (`edit` | `create` | `delete`). Optionnels : `article_id`
        (requis en pratique pour `edit`/`delete`), `title`, `content`,
        `allow_image_removal`, `group_id`, `submit_for_review`.
        """
        self._check_choice("kind", payload.get("kind"), DRAFT_KINDS)
        if not payload.get("kind"):
            raise ValueError(
                "`kind` requis : 'edit', 'create' ou 'delete'.")
        return self._request("POST", "/docs/drafts", json=dict(payload))

    def accept_draft(self, draft_id: str) -> Any:
        """POST /docs/drafts/{id}/accept — applique le brouillon. Scope `docs:write`.

        `edit` écrit une nouvelle version de l'article, `create` matérialise un
        article (non publié), `delete` soft-delete l'article.

        ⚠️ **Peut répondre `superseded` au lieu de `accepted`** quand le brouillon
        ne s'applique plus proprement : succès HTTP, rien d'appliqué. Lire le
        statut rendu, pas seulement le code de retour.
        """
        return self._request("POST", f"/docs/drafts/{draft_id}/accept")

    def decline_draft(self, draft_id: str) -> Any:
        """POST /docs/drafts/{id}/decline — rejette le brouillon. Scope `docs:write`.

        La ligne est conservée pour l'audit, marquée `REJECTED`, et sort de la
        file ouverte.
        """
        return self._request("POST", f"/docs/drafts/{draft_id}/decline")
