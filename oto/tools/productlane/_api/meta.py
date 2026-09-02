"""Identité de la clé, portail public, et import de fichiers.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`).

`me()` est la sonde du connecteur : **n'importe quelle clé authentifiée peut
l'appeler**, quels que soient ses scopes. C'est ce qui en fait le bon test de
connexion — elle distingue « clé invalide » (401) de « clé valide mais sans le
droit demandé » (403 ailleurs), là où sonder une ressource confondrait les deux.
Elle rend en prime les scopes accordés et la sélection d'équipe Linear du
workspace, donc de quoi expliquer un refus AVANT de le provoquer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class _MetaMixin:
    """Identité, portail, fichiers."""

    # --- identité -----------------------------------------------------------

    def me(self) -> Any:
        """GET /me — identité de la clé, scopes accordés, équipes Linear du workspace.

        **Aucun scope requis** : appelable par toute clé authentifiée. C'est la
        sonde d'authentification du connecteur.
        """
        return self._request("GET", "/me")

    # --- portail public ------------------------------------------------------

    def get_roadmap(self, contact_email: Optional[str] = None,
                    language: Optional[str] = None) -> Any:
        """GET /portal/roadmap — la roadmap publique, telle que le portail la rend.

        Scope `portal:read`. `contact_email` la rend du point de vue d'un contact
        (ce qu'il a voté, ce qui le concerne).
        """
        return self._request("GET", "/portal/roadmap",
                             params={"contact_email": contact_email,
                                     "language": language})

    def get_customer_portal(self, email: str) -> Any:
        """GET /portal/customer-portal — ce qu'un contact voit dans son portail.

        Scope `portal:read`, **plan Scale requis**. `email` est obligatoire : la
        vue est celle d'une personne précise, il n'y a pas de vue « générale ».
        """
        if not email:
            raise ValueError(
                "`email` requis : cette vue est celle d'un contact donné.")
        return self._request("GET", "/portal/customer-portal",
                             params={"email": email})

    def list_portal_instances(self) -> Any:
        """GET /portal/instances — instances de portail du workspace. Scope `portal:read`.

        ⚠️ Le portail **Main (Root) est implicite** : il n'apparaît pas dans cette
        liste, et se désigne ailleurs par un `portal_instance_id` à `null`. Une
        liste vide ne veut donc pas dire « pas de portail ».
        """
        return self._request("GET", "/portal/instances")

    # --- fichiers ------------------------------------------------------------

    def import_file(self, url: Optional[str] = None,
                    content_base64: Optional[str] = None,
                    file_name: Optional[str] = None,
                    content_type: Optional[str] = None) -> Any:
        """POST /files/import — stocke un fichier depuis une URL publique ou en base64.

        Rend une URL CDN utilisable dans un changelog, un article de doc ou une
        pièce jointe. Fournir **soit** `url`, **soit** `content_base64` — pas les
        deux : l'amont ne dit pas lequel il privilégierait.
        """
        if not url and not content_base64:
            raise ValueError(
                "fournir `url` (source publique) ou `content_base64` (contenu "
                "en ligne).")
        if url and content_base64:
            raise ValueError(
                "`url` et `content_base64` sont exclusifs — passer l'un OU "
                "l'autre.")
        body: Dict[str, Any] = {}
        for key, value in (("url", url), ("content_base64", content_base64),
                           ("file_name", file_name),
                           ("content_type", content_type)):
            if value is not None:
                body[key] = value
        return self._request("POST", "/files/import", json=body)
