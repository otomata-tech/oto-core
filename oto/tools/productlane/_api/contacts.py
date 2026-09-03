"""Contacts Productlane — les personnes, leurs entreprises, et les bloqués.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`, `_list`, `_check_choice`).

⚠️ **Bloquer un expéditeur a un effet durable et invisible côté client** : une
adresse (ou un domaine entier) bloquée ne peut plus ouvrir de fil ni écrire sur
un fil existant, et l'émetteur n'en est pas informé. Bloquer un DOMAINE coupe
toute une organisation d'un coup.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..const import BLOCKED_SENDER_TYPES


class _ContactsMixin:
    """Contacts, appartenances aux entreprises, expéditeurs bloqués."""

    def list_contacts(self, limit: Optional[int] = None,
                      cursor: Optional[str] = None,
                      email: Optional[str] = None,
                      name_contains: Optional[str] = None,
                      company_id: Optional[str] = None,
                      external_id: Optional[str] = None,
                      created_after: Optional[str] = None,
                      created_before: Optional[str] = None,
                      updated_after: Optional[str] = None,
                      updated_before: Optional[str] = None) -> Any:
        """GET /contacts — contacts de l'espace de travail. Scope `contacts:read`."""
        return self._list("/contacts", limit, cursor, {
            "email": email, "name_contains": name_contains,
            "company_id": company_id, "external_id": external_id,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_contact(self, contact_id: str) -> Any:
        """GET /contacts/{id} — un contact. Scope `contacts:read`."""
        return self._request("GET", f"/contacts/{contact_id}")

    def create_contact(self, payload: Dict[str, Any]) -> Any:
        """POST /contacts — crée un contact. Scope `contacts:write`.

        Requis : `email`. Optionnels : `name`, `image_url`, `is_subscribed`,
        `external_ids`, `company_id`, `company_name`, `company_external_id`.

        Les trois champs `company_*` rattachent le contact à une entreprise :
        par id, par nom, ou par identifiant externe — au choix, pas tous.
        """
        return self._request("POST", "/contacts", json=dict(payload))

    def update_contact(self, contact_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /contacts/{id} — met à jour un contact. Scope `contacts:write`.

        Champs : `external_ids`, `name`, `email`, `image_url`, `is_subscribed`,
        `company_id`, `company_name`, `company_external_id`.

        ⚠️ `is_subscribed=False` **désabonne** le contact des diffusions de
        changelog : c'est une préférence de communication, pas un simple champ.
        """
        return self._request("PATCH", f"/contacts/{contact_id}",
                             json=dict(payload))

    def delete_contact(self, contact_id: str) -> Any:
        """DELETE /contacts/{id} — **soft-delete**. Scope `contacts:write`."""
        return self._request("DELETE", f"/contacts/{contact_id}")

    # --- appartenance aux entreprises --------------------------------------

    def list_contact_companies(self, contact_id: str) -> Any:
        """GET /contacts/{id}/companies — ses entreprises, **la principale d'abord**.

        Scopes `contacts:read` ET `companies:read` : sans le second, refus.
        """
        return self._request("GET", f"/contacts/{contact_id}/companies")

    def add_contact_to_company(self, contact_id: str,
                               company_id: Optional[str] = None,
                               company_name: Optional[str] = None,
                               company_external_id: Optional[str] = None) -> Any:
        """POST /contacts/{id}/companies — rattache une entreprise. Scope `contacts:write`.

        **Idempotent**, et devient l'entreprise principale si le contact n'en
        avait aucune. Désigner l'entreprise par id, par nom ou par id externe.
        """
        body = {"company_id": company_id, "company_name": company_name,
                "company_external_id": company_external_id}
        body = {k: v for k, v in body.items() if v is not None}
        if not body:
            raise ValueError(
                "désigner l'entreprise par `company_id`, `company_name` ou "
                "`company_external_id`.")
        return self._request("POST", f"/contacts/{contact_id}/companies",
                             json=body)

    def remove_contact_from_company(self, contact_id: str,
                                    company_id: str) -> Any:
        """DELETE /contacts/{id}/companies/{company_id} — retire une appartenance.

        Scope `contacts:write`. Si c'était la principale, une autre prend le relais.
        """
        return self._request(
            "DELETE", f"/contacts/{contact_id}/companies/{company_id}")

    # --- ce à quoi le contact est relié -------------------------------------

    def list_contact_issues(self, contact_id: str, limit: Optional[int] = None,
                            cursor: Optional[str] = None) -> Any:
        """GET /contacts/{id}/issues — issues reliées via les customer needs de ses fils.

        Scopes `contacts:read` ET `issues:read`.
        """
        return self._list(f"/contacts/{contact_id}/issues", limit, cursor)

    def list_contact_projects(self, contact_id: str, limit: Optional[int] = None,
                              cursor: Optional[str] = None) -> Any:
        """GET /contacts/{id}/projects — projets reliés via les customer needs de ses fils.

        Scopes `contacts:read` ET `projects:read`.
        """
        return self._list(f"/contacts/{contact_id}/projects", limit, cursor)

    # --- expéditeurs bloqués -------------------------------------------------

    def list_blocked_senders(self, limit: Optional[int] = None,
                             cursor: Optional[str] = None,
                             type: Optional[str] = None) -> Any:
        """GET /contacts/blocked-senders — adresses et domaines bloqués.

        Scope `contacts:read`. `type` filtre sur `EMAIL` ou `DOMAIN`.
        """
        self._check_choice("type", type, BLOCKED_SENDER_TYPES)
        return self._list("/contacts/blocked-senders", limit, cursor,
                          {"type": type})

    def block_sender(self, type: str, value: str) -> Any:
        """POST /contacts/blocked-senders — bloque une adresse ou un domaine.

        Scope `contacts:write`. `type="EMAIL"` pour une adresse,
        `type="DOMAIN"` pour **tout un domaine**.

        ⚠️ Un expéditeur bloqué ne peut plus ouvrir de fil ni écrire sur un fil
        existant, **et n'en est pas informé**. Un blocage de domaine coupe toute
        une organisation d'un seul appel.
        """
        self._check_choice("type", type, BLOCKED_SENDER_TYPES)
        if not value:
            raise ValueError("`value` requis : l'adresse ou le domaine à bloquer.")
        return self._request("POST", "/contacts/blocked-senders",
                             json={"type": type, "value": value})

    def unblock_sender(self, blocked_id: str) -> Any:
        """DELETE /contacts/blocked-senders/{id} — débloque. Scope `contacts:write`."""
        return self._request("DELETE",
                             f"/contacts/blocked-senders/{blocked_id}")
