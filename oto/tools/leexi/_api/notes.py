"""Notes d'appel Leexi — ce que les prompts ont produit sur un appel.

Ce mixin n'est jamais instancié seul : il est composé dans `LeexiClient`, qui
fournit le transport (`_request`, `_list`).

⚠️ Les notes suivent la *call access scope* de la clé, comme les appels : les
notes d'un appel hors périmètre ne sont **pas** rendues — et l'amont répond par
une liste vide, pas par un refus. Une liste vide ne prouve donc pas qu'un appel
n'a pas de notes.
"""
from __future__ import annotations

from typing import Any, Optional


class _NotesMixin:
    """Notes d'appel."""

    def list_call_notes(self, call_uuid: str, page: Optional[int] = None,
                        items: Optional[int] = None,
                        prompt_uuid: Optional[str] = None) -> Any:
        """GET /v1/call_notes — notes d'un appel. Scope `read_calls`.

        `call_uuid` est **requis par l'amont** (il n'existe pas de liste globale
        des notes) ; `prompt_uuid` filtre sur le prompt qui les a produites.

        ⚠️ Seules les notes issues de prompts des catégories `summary` ou `text`
        sont rendues : les autres n'existent pas pour cette API, et leur absence
        n'est pas un défaut de la clé.
        """
        if not call_uuid:
            raise ValueError(
                "`call_uuid` est requis pour lister des notes d'appel "
                "(l'API Leexi n'expose pas de liste globale).")
        return self._list("/call_notes", page, items,
                          {"call_uuid": call_uuid, "prompt_uuid": prompt_uuid})

    def get_call_note(self, uuid: str) -> Any:
        """GET /v1/call_notes/{uuid} — une note. Scope `read_calls`."""
        return self._request("GET", f"/call_notes/{uuid}")

    def update_call_note(self, uuid: str, locale: str, text: str) -> Any:
        """PATCH /v1/call_notes/{uuid} — réécrit une note. Scope `write_calls`.

        `locale` ET `text` sont tous deux requis par l'amont : c'est un
        REMPLACEMENT du texte pour une langue donnée, pas une fusion — le contenu
        précédent de cette langue est perdu.
        """
        if not locale or not text:
            raise ValueError(
                "`locale` et `text` sont tous deux requis : l'API Leexi remplace "
                "le texte d'une langue, elle ne fusionne pas.")
        return self._request("PATCH", f"/call_notes/{uuid}",
                             json={"locale": locale, "text": text})

    def delete_call_note(self, uuid: str) -> Any:
        """DELETE /v1/call_notes/{uuid} — supprime une note. Scope `write_calls`.

        ⚠️ Suppression réelle, sans corbeille côté API — contrairement à
        `deactivate_user`, dont le DELETE ne fait que désactiver.
        """
        return self._request("DELETE", f"/call_notes/{uuid}")
