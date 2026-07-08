"""Brevo — email transactionnel (`/smtp/*`) : envoi, logs, événements, templates.

Distinct des **campagnes** (`campaigns.py`, envoi de masse à des listes) : ici on
envoie un message unitaire à un ou quelques destinataires, en direct ou depuis un
template. Les statistiques de délivrabilité (`events`) sont la source de vérité
pour savoir ce qu'un email est devenu (delivered / opened / hardBounce / spam…).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._base import _BrevoBase


class TransactionalEmailMixin(_BrevoBase):

    # --- Envoi ---------------------------------------------------------------

    def send_email(
        self,
        to: List[Dict[str, str]],
        subject: Optional[str] = None,
        html_content: Optional[str] = None,
        text_content: Optional[str] = None,
        sender: Optional[Dict[str, str]] = None,
        template_id: Optional[int] = None,
        params: Optional[Dict[str, Any]] = None,
        cc: Optional[List[Dict[str, str]]] = None,
        bcc: Optional[List[Dict[str, str]]] = None,
        reply_to: Optional[Dict[str, str]] = None,
        attachment: Optional[List[Dict[str, str]]] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
        scheduled_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Envoie un email transactionnel. Renvoie `{"messageId": …}`.

        Deux modes exclusifs :
        - **template** : `template_id` (+ `params` pour les variables) — `subject`
          et `sender` viennent du template s'ils ne sont pas surchargés ;
        - **direct** : `subject` + `html_content` (ou `text_content`) + `sender`.

        Args:
            to: `[{"email": …, "name": …}, …]` (max 99 destinataires).
            sender: `{"email": …, "name": …}` ou `{"id": <senderId>}`. L'expéditeur
                doit être un sender vérifié du compte (cf. `list_senders`).
            attachment: `[{"url": …}]` ou `[{"content": <base64>, "name": …}]`.
            scheduled_at: ISO 8601 UTC, jusqu'à 72 h dans le futur.
        """
        body = self._clean({
            "to": to, "subject": subject, "htmlContent": html_content,
            "textContent": text_content, "sender": sender, "templateId": template_id,
            "params": params, "cc": cc, "bcc": bcc, "replyTo": reply_to,
            "attachment": attachment, "headers": headers, "tags": tags,
            "scheduledAt": scheduled_at,
        })
        return self._request("POST", "/smtp/email", json=body)

    # --- Logs & statistiques --------------------------------------------------

    def list_transactional_emails(
        self,
        email: Optional[str] = None,
        template_id: Optional[int] = None,
        message_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les emails transactionnels envoyés (métadonnées, `uuid` par email).

        Dates au format `YYYY-MM-DD`. Récupérer le corps HTML d'un envoi via
        `get_transactional_email_content(uuid)`.
        """
        params = self._clean({
            "email": email, "templateId": template_id, "messageId": message_id,
            "startDate": start_date, "endDate": end_date,
            "limit": min(limit, 500), "offset": offset, "sort": sort,
        })
        return self._request("GET", "/smtp/emails", params=params)

    def get_transactional_email_content(self, uuid: str) -> Dict[str, Any]:
        """Contenu HTML d'un email transactionnel envoyé (`uuid` vu dans les logs)."""
        return self._request("GET", f"/smtp/emails/{uuid}")

    def transactional_events(
        self,
        limit: int = 50,
        offset: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: Optional[int] = None,
        email: Optional[str] = None,
        event: Optional[str] = None,
        tags: Optional[str] = None,
        message_id: Optional[str] = None,
        template_id: Optional[int] = None,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Journal des événements de délivrabilité — la source de vérité par email.

        Args:
            event: `bounces` | `hardBounces` | `softBounces` | `delivered` |
                `spam` | `requests` | `opened` | `clicks` | `invalid` | `deferred`
                | `blocked` | `unsubscribed` | `error` | `loadedByProxy`.
            days: fenêtre glissante (jours) — alternative à `start_date`/`end_date`.
        """
        params = self._clean({
            "limit": min(limit, 100), "offset": offset,
            "startDate": start_date, "endDate": end_date, "days": days,
            "email": email, "event": event, "tags": tags,
            "messageId": message_id, "templateId": template_id, "sort": sort,
        })
        return self._request("GET", "/smtp/statistics/events", params=params)

    def transactional_report(
        self,
        by_day: bool = False,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: Optional[int] = None,
        tag: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Compteurs agrégés (requests, delivered, opens, clicks, bounces…).

        `by_day=False` (défaut) → un total sur la période (`/aggregatedReport`).
        `by_day=True` → une ligne par jour (`/reports`).
        """
        if by_day:
            params = self._clean({
                "limit": limit, "offset": offset, "startDate": start_date,
                "endDate": end_date, "days": days, "tag": tag,
            })
            return self._request("GET", "/smtp/statistics/reports", params=params)
        params = self._clean({
            "startDate": start_date, "endDate": end_date, "days": days, "tag": tag})
        return self._request(
            "GET", "/smtp/statistics/aggregatedReport", params=params or None)

    def list_blocked(
        self,
        domains: bool = False,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        senders: Optional[List[str]] = None,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Contacts bloqués (hard bounce, plainte spam, désinscription) ou domaines bloqués.

        `domains=True` → `/smtp/blockedDomains` (liste simple, sans pagination).
        """
        if domains:
            return self._request("GET", "/smtp/blockedDomains")
        params = self._clean({
            "startDate": start_date, "endDate": end_date, "limit": limit,
            "offset": offset, "senders": senders, "sort": sort,
        })
        return self._request("GET", "/smtp/blockedContacts", params=params)

    # --- Templates ------------------------------------------------------------

    def list_templates(
        self, template_id: Optional[int] = None, active_only: Optional[bool] = None,
        limit: int = 50, offset: int = 0, sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Templates transactionnels. Passer `template_id` pour n'en récupérer qu'un."""
        if template_id is not None:
            return self._request("GET", f"/smtp/templates/{int(template_id)}")
        params = self._clean({
            "templateStatus": active_only, "limit": min(limit, 1000),
            "offset": offset, "sort": sort,
        })
        return self._request("GET", "/smtp/templates", params=params)

    def create_template(
        self,
        template_name: str,
        subject: str,
        sender: Dict[str, str],
        html_content: Optional[str] = None,
        html_url: Optional[str] = None,
        reply_to: Optional[str] = None,
        to_field: Optional[str] = None,
        tag: Optional[str] = None,
        is_active: bool = True,
        attachment_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Crée un template transactionnel. Renvoie `{"id": …}`.

        Args:
            sender: `{"email": …, "name": …}` ou `{"id": <senderId>}`.
            html_content: HTML du corps. Alternative : `html_url` (page distante).
            to_field: personnalisation du destinataire, ex. `{{contact.NOM}}`.
        """
        body = self._clean({
            "templateName": template_name, "subject": subject, "sender": sender,
            "htmlContent": html_content, "htmlUrl": html_url, "replyTo": reply_to,
            "toField": to_field, "tag": tag, "isActive": is_active,
            "attachmentUrl": attachment_url,
        })
        return self._request("POST", "/smtp/templates", json=body)

    def update_template(
        self,
        template_id: int,
        template_name: Optional[str] = None,
        subject: Optional[str] = None,
        sender: Optional[Dict[str, str]] = None,
        html_content: Optional[str] = None,
        html_url: Optional[str] = None,
        reply_to: Optional[str] = None,
        to_field: Optional[str] = None,
        tag: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Met à jour un template (champs fournis seulement). Corps vide (204) au succès."""
        body = self._clean({
            "templateName": template_name, "subject": subject, "sender": sender,
            "htmlContent": html_content, "htmlUrl": html_url, "replyTo": reply_to,
            "toField": to_field, "tag": tag, "isActive": is_active,
        })
        return self._request("PUT", f"/smtp/templates/{int(template_id)}", json=body)
