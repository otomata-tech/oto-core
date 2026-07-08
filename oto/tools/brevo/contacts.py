"""Brevo — contacts, attributs, listes, dossiers, segments.

Vocabulaire Brevo : un **contact** porte des `attributes` (colonnes typées,
déclarées au niveau du compte) ; il appartient à des **listes** ; une liste vit
dans un **dossier** (`folderId` obligatoire à la création) ; un **segment** est
une liste dynamique définie par un filtre (lecture seule via l'API).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._base import _BrevoBase


class ContactsMixin(_BrevoBase):

    # --- Contacts -----------------------------------------------------------

    def list_contacts(
        self,
        limit: int = 50,
        offset: int = 0,
        modified_since: Optional[str] = None,
        created_since: Optional[str] = None,
        sort: Optional[str] = None,
        segment_id: Optional[int] = None,
        list_ids: Optional[List[int]] = None,
        ids: Optional[List[int]] = None,
        filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les contacts (paginé).

        Args:
            limit: max 1000 côté Brevo.
            modified_since / created_since: ISO 8601 UTC (`YYYY-MM-DDTHH:mm:ss.SSSZ`).
            sort: `asc` | `desc` (défaut `desc`, par date de création).
            segment_id: filtre par segment. **Exclusif avec `list_ids`.**
            ids: max 20 ids de contact.
            filter: filtre sur attributs, opérateur `equals` uniquement
                (ex. `equals(FIRSTNAME,"Alex")`).
        """
        params = self._clean({
            "limit": min(limit, 1000), "offset": offset,
            "modifiedSince": modified_since, "createdSince": created_since,
            "sort": sort, "segmentId": segment_id, "listIds": list_ids,
            "ids": ids, "filter": filter,
        })
        return self._request("GET", "/contacts", params=params)

    def get_contact(
        self,
        identifier: str,
        identifier_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Récupère un contact.

        Args:
            identifier: email, id numérique, téléphone ou EXT_ID selon `identifier_type`.
            identifier_type: `email_id` | `contact_id` | `phone_id` | `ext_id` |
                `whatsapp_id` | `landline_number_id`. Défaut Brevo = email.
        """
        params = self._clean({
            "identifierType": identifier_type,
            "startDate": start_date, "endDate": end_date,
        })
        return self._request("GET", f"/contacts/{identifier}", params=params or None)

    def upsert_contact(
        self,
        email: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
        list_ids: Optional[List[int]] = None,
        update_enabled: bool = True,
        ext_id: Optional[str] = None,
        email_blacklisted: Optional[bool] = None,
        sms_blacklisted: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Crée un contact — ou le met à jour si `update_enabled` (défaut).

        Renvoie `{"id": …}` à la création ; **corps vide (204) sur une mise à jour**.
        """
        body = self._clean({
            "email": email, "attributes": attributes, "listIds": list_ids,
            "updateEnabled": update_enabled, "ext_id": ext_id,
            "emailBlacklisted": email_blacklisted, "smsBlacklisted": sms_blacklisted,
        })
        return self._request("POST", "/contacts", json=body)

    def update_contact(
        self,
        identifier: str,
        attributes: Optional[Dict[str, Any]] = None,
        list_ids: Optional[List[int]] = None,
        unlink_list_ids: Optional[List[int]] = None,
        identifier_type: Optional[str] = None,
        email_blacklisted: Optional[bool] = None,
        sms_blacklisted: Optional[bool] = None,
        ext_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Met à jour un contact existant. `unlink_list_ids` le retire de ces listes.

        À préférer à `upsert_contact` quand on cible par id/ext_id, ou pour
        désinscrire d'une liste. Renvoie un corps vide (204) en cas de succès.
        """
        body = self._clean({
            "attributes": attributes, "listIds": list_ids,
            "unlinkListIds": unlink_list_ids, "ext_id": ext_id,
            "emailBlacklisted": email_blacklisted, "smsBlacklisted": sms_blacklisted,
        })
        params = self._clean({"identifierType": identifier_type})
        return self._request(
            "PUT", f"/contacts/{identifier}", json=body, params=params or None)

    def contact_campaign_stats(
        self, identifier: str,
        start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stats de campagnes d'un contact (ouvertures, clics, bounces…)."""
        params = self._clean({"startDate": start_date, "endDate": end_date})
        return self._request(
            "GET", f"/contacts/{identifier}/campaignStats", params=params or None)

    def import_contacts(
        self,
        list_ids: Optional[List[int]] = None,
        json_body: Optional[List[Dict[str, Any]]] = None,
        file_url: Optional[str] = None,
        file_body: Optional[str] = None,
        update_existing_contacts: bool = True,
        empty_contacts_attributes: bool = False,
        new_list: Optional[Dict[str, Any]] = None,
        notify_url: Optional[str] = None,
        disable_notification: bool = True,
    ) -> Dict[str, Any]:
        """Import de masse **asynchrone** — renvoie `{"processId": …}`.

        La voie à prendre au-delà de 150 contacts (au lieu de `add_to_list`).
        Fournir **une** source : `json_body` (liste de `{"email", "attributes", …}`),
        `file_url` (CSV distant) ou `file_body` (CSV inline, `;` en séparateur).

        Args:
            new_list: `{"listName": …, "folderId": …}` pour créer la liste au vol.
            empty_contacts_attributes: `True` écrase par du vide les attributs
                absents du fichier. Destructif — laisser `False`.
        """
        body = self._clean({
            "listIds": list_ids, "jsonBody": json_body, "fileUrl": file_url,
            "fileBody": file_body, "updateExistingContacts": update_existing_contacts,
            "emptyContactsAttributes": empty_contacts_attributes,
            "newList": new_list, "notifyUrl": notify_url,
            "disableNotification": disable_notification,
        })
        return self._request("POST", "/contacts/import", json=body)

    def export_contacts(
        self,
        contact_filter: Optional[Dict[str, Any]] = None,
        export_attributes: Optional[List[str]] = None,
        notify_url: Optional[str] = None,
        disable_notification: bool = True,
    ) -> Dict[str, Any]:
        """Export **asynchrone** des contacts — renvoie `{"processId": …}`.

        Args:
            contact_filter: `{"listIds": [1]}` | `{"segmentId": 2}` |
                `{"emailBlacklisted": true}`. Défaut = tous les contacts.
        """
        body = self._clean({
            "customContactFilter": contact_filter or {"emailBlacklisted": False},
            "exportAttributes": export_attributes, "notifyUrl": notify_url,
            "disableNotification": disable_notification,
        })
        return self._request("POST", "/contacts/export", json=body)

    # --- Attributs & segments -----------------------------------------------

    def list_attributes(self) -> Dict[str, Any]:
        """Liste les attributs de contact du compte (nom, catégorie, type)."""
        return self._request("GET", "/contacts/attributes")

    def list_segments(self, limit: int = 50, offset: int = 0,
                      sort: Optional[str] = None) -> Dict[str, Any]:
        """Liste les segments (listes dynamiques). Lecture seule via l'API."""
        params = self._clean({"limit": limit, "offset": offset, "sort": sort})
        return self._request("GET", "/contacts/segments", params=params)

    # --- Listes & dossiers ---------------------------------------------------

    def list_lists(self, limit: int = 50, offset: int = 0,
                   sort: Optional[str] = None,
                   folder_id: Optional[int] = None) -> Dict[str, Any]:
        """Liste les listes de contacts, du compte ou d'un dossier."""
        params = self._clean({"limit": limit, "offset": offset, "sort": sort})
        path = f"/contacts/folders/{folder_id}/lists" if folder_id else "/contacts/lists"
        return self._request("GET", path, params=params)

    def get_list(self, list_id: int) -> Dict[str, Any]:
        """Détail d'une liste (nom, dossier, nombre de contacts, blacklistés)."""
        return self._request("GET", f"/contacts/lists/{int(list_id)}")

    def list_contacts_of_list(
        self, list_id: int, limit: int = 50, offset: int = 0,
        modified_since: Optional[str] = None, sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Contacts d'une liste (paginé)."""
        params = self._clean({
            "limit": min(limit, 500), "offset": offset,
            "modifiedSince": modified_since, "sort": sort,
        })
        return self._request(
            "GET", f"/contacts/lists/{int(list_id)}/contacts", params=params)

    def create_list(self, name: str, folder_id: int) -> Dict[str, Any]:
        """Crée une liste. `folder_id` est **obligatoire** côté Brevo (cf. `list_folders`)."""
        return self._request("POST", "/contacts/lists",
                             json={"name": name, "folderId": int(folder_id)})

    def update_list(self, list_id: int, name: Optional[str] = None,
                    folder_id: Optional[int] = None) -> Dict[str, Any]:
        """Renomme une liste ou la déplace de dossier."""
        body = self._clean({"name": name, "folderId": folder_id})
        return self._request("PUT", f"/contacts/lists/{int(list_id)}", json=body)

    def _list_membership(self, list_id: int, action: str, emails, ids, ext_ids, all_):
        given = [x for x in (emails, ids, ext_ids) if x]
        if len(given) != 1 and not all_:
            raise ValueError(
                "Fournir exactement UN type d'identifiant (emails, ids ou ext_ids).")
        body = self._clean({
            "emails": emails, "ids": ids, "extIds": ext_ids, "all": all_ or None})
        return self._request(
            "POST", f"/contacts/lists/{int(list_id)}/contacts/{action}", json=body)

    def add_to_list(
        self, list_id: int, emails: Optional[List[str]] = None,
        ids: Optional[List[int]] = None, ext_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Ajoute des contacts EXISTANTS à une liste.

        **Max 150 contacts par appel**, et un SEUL type d'identifiant à la fois.
        Au-delà → `import_contacts`. Renvoie `{contacts: {success: [], failure: []}}`.
        """
        return self._list_membership(list_id, "add", emails, ids, ext_ids, None)

    def remove_from_list(
        self, list_id: int, emails: Optional[List[str]] = None,
        ids: Optional[List[int]] = None, ext_ids: Optional[List[str]] = None,
        all_contacts: bool = False,
    ) -> Dict[str, Any]:
        """Retire des contacts d'une liste (ne supprime pas les contacts).

        **Max 150 par appel**, un seul type d'identifiant. `all_contacts=True`
        vide la liste.
        """
        return self._list_membership(
            list_id, "remove", emails, ids, ext_ids, all_contacts or None)

    def list_folders(self, limit: int = 50, offset: int = 0,
                     sort: Optional[str] = None) -> Dict[str, Any]:
        """Liste les dossiers de listes (leurs `id` servent à `create_list`)."""
        params = self._clean({"limit": limit, "offset": offset, "sort": sort})
        return self._request("GET", "/contacts/folders", params=params)
