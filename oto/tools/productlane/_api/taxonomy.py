"""Étiquettes et modèles de réponse Productlane — la taxonomie des fils.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`, `_list`).

⚠️ **Deux familles d'étiquettes portent presque le même nom, et ne sont PAS la
même chose** : celles d'ici (`/tags`) se posent sur des FILS et vivent dans des
groupes (`tag_group_id` obligatoire à la création) ; celles de `changelogs.py`
(`/changelog-tags`) se posent sur des CHANGELOGS, n'ont pas de groupe, et
appartiennent au plan Scale. Les confondre donne un 400 dont le message ne dit
pas laquelle des deux on visait.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class _TaxonomyMixin:
    """Étiquettes de fil, groupes d'étiquettes, extraits de réponse."""

    # --- étiquettes de fil ---------------------------------------------------

    def list_tags(self, limit: Optional[int] = None,
                  cursor: Optional[str] = None,
                  name_contains: Optional[str] = None,
                  tag_group_id: Optional[str] = None,
                  created_after: Optional[str] = None,
                  created_before: Optional[str] = None,
                  updated_after: Optional[str] = None,
                  updated_before: Optional[str] = None) -> Any:
        """GET /tags — étiquettes de FIL. Scope `tags:read`."""
        return self._list("/tags", limit, cursor, {
            "name_contains": name_contains, "tag_group_id": tag_group_id,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_tag(self, tag_id: str) -> Any:
        """GET /tags/{id} — une étiquette. Scope `tags:read`."""
        return self._request("GET", f"/tags/{tag_id}")

    def create_tag(self, name: str, color: str, icon: str,
                   tag_group_id: str) -> Any:
        """POST /tags — crée une étiquette de fil. Scope `tags:write`.

        Les **quatre** champs sont requis par l'amont, `tag_group_id` compris :
        une étiquette de fil vit toujours dans un groupe (cf. `list_tag_groups`).
        Ils sont nommés un par un plutôt que passés en dict, pour que l'oubli se
        voie à l'écriture et non au 400.
        """
        for label, value in (("name", name), ("color", color),
                             ("icon", icon), ("tag_group_id", tag_group_id)):
            if not value:
                raise ValueError(
                    f"`{label}` requis : l'API Productlane exige name, color, "
                    "icon ET tag_group_id pour créer une étiquette de fil.")
        return self._request("POST", "/tags", json={
            "name": name, "color": color, "icon": icon,
            "tag_group_id": tag_group_id})

    def update_tag(self, tag_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /tags/{id} — met à jour une étiquette. Scope `tags:write`.

        Champs : `name`, `color`, `icon`, `tag_group_id` (la déplacer de groupe).
        """
        return self._request("PATCH", f"/tags/{tag_id}", json=dict(payload))

    def delete_tag(self, tag_id: str) -> Any:
        """DELETE /tags/{id} — **soft-delete**. Scope `tags:write`.

        L'étiquette est retirée de tous les fils où elle était posée.
        """
        return self._request("DELETE", f"/tags/{tag_id}")

    # --- groupes d'étiquettes -----------------------------------------------

    def list_tag_groups(self, limit: Optional[int] = None,
                        cursor: Optional[str] = None) -> Any:
        """GET /tags/groups — groupes d'étiquettes. Scope `tags:read`.

        Un `id` rendu ici est le `tag_group_id` à passer à `create_tag`.
        """
        return self._list("/tags/groups", limit, cursor)

    def get_tag_group(self, group_id: str) -> Any:
        """GET /tags/groups/{id} — un groupe d'étiquettes. Scope `tags:read`."""
        return self._request("GET", f"/tags/groups/{group_id}")

    def create_tag_group(self, name: str, color: str) -> Any:
        """POST /tags/groups — crée un groupe d'étiquettes. Scope `tags:write`.

        `name` et `color` sont tous deux requis par l'amont.
        """
        if not name or not color:
            raise ValueError("`name` et `color` sont tous deux requis.")
        return self._request("POST", "/tags/groups",
                             json={"name": name, "color": color})

    def update_tag_group(self, group_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /tags/groups/{id} — met à jour un groupe. Scope `tags:write`.

        Champs : `name`, `color`.
        """
        return self._request("PATCH", f"/tags/groups/{group_id}",
                             json=dict(payload))

    def delete_tag_group(self, group_id: str) -> Any:
        """DELETE /tags/groups/{id} — **soft-delete d'un groupe VIDE**.

        Scope `tags:write`. Un groupe qui porte encore des étiquettes est refusé :
        les déplacer d'abord (`update_tag(tag_group_id=…)`) ou les supprimer.
        """
        return self._request("DELETE", f"/tags/groups/{group_id}")

    # --- extraits de réponse -------------------------------------------------

    def list_snippets(self, limit: Optional[int] = None,
                      cursor: Optional[str] = None,
                      title_contains: Optional[str] = None,
                      folder_id: Optional[str] = None,
                      created_after: Optional[str] = None,
                      created_before: Optional[str] = None,
                      updated_after: Optional[str] = None,
                      updated_before: Optional[str] = None) -> Any:
        """GET /snippets — extraits de réponse réutilisables. **Plan Pro requis**.

        Scope `snippets:read`.
        """
        return self._list("/snippets", limit, cursor, {
            "title_contains": title_contains, "folder_id": folder_id,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_snippet(self, snippet_id: str) -> Any:
        """GET /snippets/{id} — un extrait. Scope `snippets:read`, plan Pro."""
        return self._request("GET", f"/snippets/{snippet_id}")

    def create_snippet(self, title: str, html: str,
                       folder_id: Optional[str] = None) -> Any:
        """POST /snippets — crée un extrait. Scope `snippets:write`, plan Pro.

        ⚠️ Le corps est du **HTML** (`html`), pas du markdown — contrairement au
        `content` d'un article de doc.
        """
        if not title or not html:
            raise ValueError("`title` et `html` sont tous deux requis.")
        body: Dict[str, Any] = {"title": title, "html": html}
        if folder_id is not None:
            body["folder_id"] = folder_id
        return self._request("POST", "/snippets", json=body)

    def update_snippet(self, snippet_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /snippets/{id} — met à jour un extrait. Scope `snippets:write`.

        Champs : `title`, `html`, `folder_id`.
        """
        return self._request("PATCH", f"/snippets/{snippet_id}",
                             json=dict(payload))

    def delete_snippet(self, snippet_id: str) -> Any:
        """DELETE /snippets/{id} — **soft-delete**. Scope `snippets:write`."""
        return self._request("DELETE", f"/snippets/{snippet_id}")

    def list_snippet_folders(self, limit: Optional[int] = None,
                             cursor: Optional[str] = None) -> Any:
        """GET /snippets/folders — dossiers d'extraits. Scope `snippets:read`."""
        return self._list("/snippets/folders", limit, cursor)
