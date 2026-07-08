"""Brevo — campagnes email (envoi de masse à des listes/segments).

Distinct du transactionnel (`email.py`) : une campagne cible des listes entières
et se planifie. **L'API expose ici la conception et la mesure, pas le
déclenchement** : `sendNow`, le passage de statut à `sent` et la suppression ne
sont volontairement pas wrappés (un appel LLM malheureux enverrait à toute la
base). On crée/édite un brouillon, on s'envoie un test, on lit les stats ; le
départ se déclenche depuis l'UI Brevo.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._base import _BrevoBase


class CampaignsMixin(_BrevoBase):

    def list_campaigns(
        self,
        type: Optional[str] = None,
        status: Optional[str] = None,
        statistics: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        sort: Optional[str] = None,
        exclude_html_content: bool = True,
    ) -> Dict[str, Any]:
        """Liste les campagnes email.

        Args:
            type: `classic` | `trigger`.
            status: `suspended` | `archive` | `sent` | `queued` | `draft` |
                `inProcess` | `replicate` | `replicateTemplate`.
            statistics: `globalStats` | `linksStats` | `statsByDomain` |
                `statsByDevice` | `statsByBrowser` — enrichit chaque campagne.
            exclude_html_content: `True` (défaut) allège fortement la réponse.
        """
        params = self._clean({
            "type": type, "status": status, "statistics": statistics,
            "startDate": start_date, "endDate": end_date,
            "limit": min(limit, 100), "offset": offset, "sort": sort,
            "excludeHtmlContent": exclude_html_content,
        })
        return self._request("GET", "/emailCampaigns", params=params)

    def get_campaign(
        self, campaign_id: int, statistics: Optional[str] = None,
        exclude_html_content: bool = True,
    ) -> Dict[str, Any]:
        """Détail d'une campagne, avec ses stats si `statistics` est fourni."""
        params = self._clean({
            "statistics": statistics, "excludeHtmlContent": exclude_html_content})
        return self._request(
            "GET", f"/emailCampaigns/{int(campaign_id)}", params=params or None)

    def create_campaign(
        self,
        name: str,
        sender: Dict[str, str],
        subject: Optional[str] = None,
        html_content: Optional[str] = None,
        html_url: Optional[str] = None,
        template_id: Optional[int] = None,
        recipients: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[str] = None,
        reply_to: Optional[str] = None,
        preview_text: Optional[str] = None,
        tag: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Crée une campagne (brouillon si `scheduled_at` est omis). Renvoie `{"id": …}`.

        Args:
            sender: `{"email": …, "name": …}` ou `{"id": <senderId>}` — sender vérifié.
            recipients: `{"listIds": [1,2], "exclusionListIds": [3], "segmentIds": [4]}`.
            scheduled_at: ISO 8601 UTC. **Planifie réellement l'envoi.** Omettre pour
                rester en brouillon.
            template_id: partir d'un template au lieu de `html_content`.
        """
        body = self._clean({
            "name": name, "sender": sender, "subject": subject,
            "htmlContent": html_content, "htmlUrl": html_url,
            "templateId": template_id, "recipients": recipients,
            "scheduledAt": scheduled_at, "replyTo": reply_to,
            "previewText": preview_text, "tag": tag, "params": params,
        })
        return self._request("POST", "/emailCampaigns", json=body)

    def update_campaign(self, campaign_id: int, **fields: Any) -> Dict[str, Any]:
        """Met à jour une campagne **non encore envoyée** (champs fournis seulement).

        Accepte les mêmes clés que `create_campaign`, en camelCase Brevo
        (`htmlContent`, `scheduledAt`, `recipients`…). Corps vide (204) au succès.
        """
        return self._request(
            "PUT", f"/emailCampaigns/{int(campaign_id)}", json=self._clean(fields))

    def send_campaign_test(self, campaign_id: int,
                           email_to: List[str]) -> Dict[str, Any]:
        """Envoie un test de la campagne aux adresses données.

        Ces adresses doivent exister comme contacts du compte Brevo. N'envoie
        **pas** la campagne à ses destinataires réels.
        """
        return self._request("POST", f"/emailCampaigns/{int(campaign_id)}/sendTest",
                             json={"emailTo": email_to})

    def campaign_ab_test_result(self, campaign_id: int) -> Dict[str, Any]:
        """Résultat d'un A/B test (gagnant, critère, stats par variante)."""
        return self._request(
            "GET", f"/emailCampaigns/{int(campaign_id)}/abTestCampaignResult")

    def campaign_shared_url(self, campaign_id: int) -> Dict[str, Any]:
        """URL publique de partage (vue navigateur) d'une campagne envoyée."""
        return self._request("GET", f"/emailCampaigns/{int(campaign_id)}/sharedUrl")
