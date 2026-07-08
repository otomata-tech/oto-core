"""Client de l'API PUBLIQUE Brevo v3 (ex-Sendinblue).

Auth = **clé API v3** en header `api-key`, créée dans Brevo :
Paramètres → SMTP & API → Clés API. Une clé porte tout le compte (pas de scope).

Couvre le cœur de la plateforme : contacts/listes/segments, email transactionnel
et templates, campagnes email, CRM natif (deals/companies/tasks/notes).

⚠️ **À ne pas confondre avec le connecteur `brevoauto`** (`workflow-apis.brevo.com`),
qui pilote les *automations* via l'API privée de l'éditeur et une session navigateur.
Les deux surfaces sont disjointes : la clé API v3 ne donne aucun accès à l'authoring
d'automations, et la session navigateur ne sert pas ici.

**Écritures volontairement absentes** (un appel LLM malheureux coûterait cher) :
envoi d'une campagne (`sendNow`, passage du statut à `sent`), suppression de
contact/liste/campagne/template, purge des hard bounces. La conception et la mesure
sont exposées ; le départ d'un envoi de masse et les suppressions restent dans l'UI.

Docs : https://developers.brevo.com/reference

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ._base import _BrevoBase
from .campaigns import CampaignsMixin
from .contacts import ContactsMixin
from .crm import CrmMixin
from .email import TransactionalEmailMixin


class BrevoClient(ContactsMixin, TransactionalEmailMixin, CampaignsMixin,
                  CrmMixin, _BrevoBase):
    """Client Brevo v3 — contacts, transactionnel, campagnes, CRM."""

    def get_account(self) -> Dict[str, Any]:
        """Compte Brevo : société, plan(s), crédits email/SMS restants."""
        return self._request("GET", "/account")

    def list_senders(self, ip: Optional[str] = None,
                     domain: Optional[str] = None) -> Dict[str, Any]:
        """Expéditeurs vérifiés du compte — leur `email`/`id` est requis pour envoyer."""
        params = self._clean({"ip": ip, "domain": domain})
        return self._request("GET", "/senders", params=params or None)
