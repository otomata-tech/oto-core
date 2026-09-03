"""Changelogs Productlane — les notes de version, leurs étiquettes, leur diffusion.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`, `_list`).

⚠️ **`broadcast` est le seul appel de tout ce client qui écrit à des tiers.** Il
envoie un email aux contacts abonnés et/ou publie dans les canaux Slack
configurés. Il n'y a **ni annulation, ni brouillon, ni rappel** : un envoi parti
l'est pour de bon, auprès de gens qui ne sont pas l'utilisateur de la clé. Il est
donc traité à part — signature explicite plutôt qu'un dict opaque, et refus local
quand aucun canal n'est demandé.

**`published` et la diffusion sont deux choses distinctes**, et c'est écrit noir
sur blanc côté éditeur : « This endpoint never toggles `published` ». Publier
(rendre visible sur le portail) se fait par `update_changelog(published=True)` ;
diffuser (pousser vers des boîtes mail) se fait ici. Confondre les deux, c'est
soit publier sans prévenir, soit prévenir d'une page invisible.

**Traductions** : `language` crée/met à jour une LIGNE DE TRADUCTION au lieu de
la ligne de base. Les champs non traduisibles s'appliquent toujours à la base.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class _ChangelogsMixin:
    """Changelogs, étiquettes de changelog, diffusion."""

    # --- changelogs ---------------------------------------------------------

    def list_changelogs(self, limit: Optional[int] = None,
                        cursor: Optional[str] = None,
                        published: Optional[Any] = None,
                        archived: Optional[Any] = None,
                        language: Optional[str] = None,
                        title_contains: Optional[str] = None,
                        tag_id: Optional[str] = None,
                        portal_instance_id: Optional[str] = None,
                        created_after: Optional[str] = None,
                        created_before: Optional[str] = None,
                        updated_after: Optional[str] = None,
                        updated_before: Optional[str] = None) -> Any:
        """GET /changelogs — changelogs de l'espace de travail. Scope `changelogs:read`."""
        return self._list("/changelogs", limit, cursor, {
            "published": published, "archived": archived, "language": language,
            "title_contains": title_contains, "tag_id": tag_id,
            "portal_instance_id": portal_instance_id,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_changelog(self, changelog_id: str,
                      language: Optional[str] = None) -> Any:
        """GET /changelogs/{id} — un changelog. Scope `changelogs:read`.

        `language` sert la ligne de traduction correspondante plutôt que la base.
        """
        return self._request("GET", f"/changelogs/{changelog_id}",
                             params={"language": language})

    def create_changelog(self, payload: Dict[str, Any]) -> Any:
        """POST /changelogs — crée un changelog. Scope `changelogs:write`.

        Requis : `title`, `content`. Optionnels : `date`, `published`,
        `image_url`, `portal_instance_id`, `language`, `tag_ids`.

        `published=True` le rend visible sur le portail — **cela ne prévient
        personne** : la diffusion est un appel distinct (`broadcast_changelog`).
        `language` crée une ligne de TRADUCTION au lieu de remplir la base.
        """
        return self._request("POST", "/changelogs", json=dict(payload))

    def update_changelog(self, changelog_id: str,
                         payload: Dict[str, Any]) -> Any:
        """PATCH /changelogs/{id} — met à jour un changelog. Scope `changelogs:write`.

        Champs : `title`, `content`, `date`, `published`, `archived`,
        `image_url`, `portal_instance_id`, `tag_ids`, `language`.

        C'est **ici** que `published` se bascule, jamais dans `broadcast_changelog`.
        `language` upsert une ligne de traduction ; les champs non traduisibles
        s'appliquent toujours à la ligne de base.
        """
        return self._request("PATCH", f"/changelogs/{changelog_id}",
                             json=dict(payload))

    def delete_changelog(self, changelog_id: str) -> Any:
        """DELETE /changelogs/{id} — **soft-delete**. Scope `changelogs:write`."""
        return self._request("DELETE", f"/changelogs/{changelog_id}")

    # --- diffusion ----------------------------------------------------------

    def broadcast_changelog(self, changelog_id: str,
                            email: Optional[bool] = None,
                            slack: Optional[bool] = None,
                            message: Optional[str] = None,
                            subject: Optional[str] = None,
                            sender_name: Optional[str] = None,
                            from_email: Optional[str] = None) -> Any:
        """POST /changelogs/{id}/broadcast — **envoie le changelog à des tiers**.

        Scope `changelogs:write`, plan Pro ou supérieur.

        ⚠️ **Effet de bord externe et irréversible.** `email=True` écrit aux
        contacts ABONNÉS (intégration email requise) ; `slack=True` publie dans
        les canaux Slack configurés (Slack connecté requis). Rien de tout cela ne
        se rappelle ni ne s'annule.

        ⚠️ **Ne touche jamais `published`** (contrat éditeur explicite) : on peut
        donc diffuser un changelog non publié — les destinataires recevraient un
        lien vers une page qui n'est pas visible. Publier, c'est
        `update_changelog(published=True)`.

        Les paramètres sont nommés un par un, et non passés en dict, précisément
        parce qu'un appel qui envoie du courrier mérite d'être lisible sur son
        site d'appel.

        Args:
            changelog_id: le changelog à diffuser.
            email: écrire aux contacts abonnés.
            slack: publier dans les canaux Slack configurés.
            message: texte d'accompagnement.
            subject: objet de l'email.
            sender_name: nom d'expéditeur affiché.
            from_email: adresse d'expédition.
        """
        if not email and not slack:
            raise ValueError(
                "diffuser exige au moins un canal : `email=True` et/ou "
                "`slack=True`. Sans canal, l'amont refuse — et un appel qui ne "
                "diffuse rien serait de toute façon un malentendu.")
        body: Dict[str, Any] = {}
        for key, value in (("email", email), ("slack", slack),
                           ("message", message), ("subject", subject),
                           ("sender_name", sender_name),
                           ("from_email", from_email)):
            if value is not None:
                body[key] = value
        return self._request("POST", f"/changelogs/{changelog_id}/broadcast",
                             json=body)

    # --- étiquettes de changelog -------------------------------------------

    def list_changelog_tags(self) -> Any:
        """GET /changelog-tags — étiquettes attachables à un changelog.

        Scope `changelogs:read`. ⚠️ **Pas de pagination** sur cet endpoint,
        contrairement aux listes v2 : il rend tout d'un coup.
        """
        return self._request("GET", "/changelog-tags")

    def create_changelog_tag(self, name: str, color: Optional[str] = None,
                             icon: Optional[str] = None) -> Any:
        """POST /changelog-tags — crée une étiquette. Scope `changelogs:write`, plan Scale.

        Fournir **au moins** `color` ou `icon` : avec `color` seul, l'interface
        rend une pastille colorée ; avec `icon` (un nom d'icône Lucide), elle rend
        l'icône dans cette couleur.
        """
        if not name:
            raise ValueError("`name` requis.")
        if color is None and icon is None:
            raise ValueError(
                "fournir au moins `color` ou `icon` : une étiquette sans l'un "
                "des deux n'a pas de rendu.")
        body: Dict[str, Any] = {"name": name}
        if color is not None:
            body["color"] = color
        if icon is not None:
            body["icon"] = icon
        return self._request("POST", "/changelog-tags", json=body)

    def update_changelog_tag(self, tag_id: str,
                             payload: Dict[str, Any]) -> Any:
        """PATCH /changelog-tags/{id} — met à jour une étiquette.

        Scope `changelogs:write`, plan Scale. Champs : `name`, `color`, `icon`.
        `icon=None` **retire** l'icône et repasse à la pastille colorée — c'est
        une valeur signifiante, pas une absence, donc elle doit être présente
        dans le dict pour être prise en compte.
        """
        return self._request("PATCH", f"/changelog-tags/{tag_id}",
                             json=dict(payload))

    def delete_changelog_tag(self, tag_id: str) -> Any:
        """DELETE /changelog-tags/{id} — **suppression DURE**. Plan Scale.

        Scope `changelogs:write`. ⚠️ Contrairement aux autres suppressions de ce
        client (soft-delete), celle-ci est définitive, et l'étiquette est
        **détachée de tous les changelogs** où elle était posée.
        """
        return self._request("DELETE", f"/changelog-tags/{tag_id}")
