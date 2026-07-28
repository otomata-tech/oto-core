"""Dropcontact API client — contact + company enrichment (email/phone/SIRENE).

Async bulk API: POST a batch (max 250 contacts, ≤15 kB per contact) → GET the
result by `request_id` once processing is done (typically ~30s+, no fixed SLA
documented). Same submit/fetch split as FullEnrich (signal #252): `submit`
returns as soon as Dropcontact acks the batch, `fetch` is a single un-cached
status check — polling belongs to the caller, never to this client.

Auth: header `X-Access-Token` (not `Authorization: Bearer`). Credits are
"pay on success" — a POST with a single empty contact (`{"data": [{}]}`) costs
0 credits and returns `credits_left`, used here for `check_credits`.

Requires: requests
"""
from __future__ import annotations

import json

import requests

from ...config import require_secret
from ..common import raise_for_upstream

# Plafond batch documenté (dépassement → l'API rejette la requête entière).
MAX_CONTACTS_PER_BATCH = 250
# "Single contact data must not exceed 15 kB" — vérifié client-side pour éviter
# un aller-retour HTTP voué à échouer sur un item surdimensionné.
MAX_CONTACT_BYTES = 15_000


class DropcontactClient:
    BASE_URL = "https://api.dropcontact.com/v1"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or require_secret("DROPCONTACT_API_KEY")

    def _headers(self) -> dict:
        return {
            "X-Access-Token": self.api_key,
            "Content-Type": "application/json",
        }

    def submit(
        self,
        contacts: list[dict],
        *,
        siren: bool = False,
        language: str | None = None,
        custom_callback_url: str | None = None,
    ) -> dict:
        """Soumet un batch d'enrichissement. Retourne immédiatement l'accusé de
        réception Dropcontact (`request_id`, `credits_left`, et l'écho par-item de
        `data` — chaque item peut porter ses propres `errors`/`warnings` de
        validation, Dropcontact traite le reste du batch malgré un item invalide).
        Le job tourne côté Dropcontact ; récupérer via `fetch(request_id)`.

        contacts: 1-250 objets. Chacun doit porter de quoi identifier un contact
        (email, OU linkedin, OU first_name+last_name+company, OU full_name+company)
        — Dropcontact traite quand même un item incomplet mais le rapporte en
        `errors`/`warnings` dans la réponse plutôt que de faire échouer le batch.
        Champs reconnus par item : email, first_name, last_name, full_name, phone,
        company, website, num_siren, siret, linkedin, company_linkedin, country,
        job, custom_fields (préservé tel quel dans le résultat).
        """
        if not contacts:
            raise ValueError("Dropcontact submit: aucun contact fourni.")
        if len(contacts) > MAX_CONTACTS_PER_BATCH:
            raise ValueError(
                f"Dropcontact submit: {len(contacts)} contacts > plafond "
                f"{MAX_CONTACTS_PER_BATCH}/requête — découper en plusieurs appels."
            )
        for i, c in enumerate(contacts):
            size = len(json.dumps(c, ensure_ascii=False).encode("utf-8"))
            if size > MAX_CONTACT_BYTES:
                raise ValueError(
                    f"Dropcontact submit: contact #{i} pèse {size} octets > "
                    f"plafond {MAX_CONTACT_BYTES} octets/contact."
                )

        payload: dict = {"data": contacts}
        if siren:
            payload["siren"] = True
        if language:
            payload["language"] = language
        if custom_callback_url:
            payload["custom_callback_url"] = custom_callback_url

        resp = requests.post(
            f"{self.BASE_URL}/enrich/all",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        raise_for_upstream(resp, service="dropcontact")
        body = resp.json()
        if not body.get("request_id"):
            raise RuntimeError(f"Dropcontact POST: pas de request_id dans la réponse: {resp.text[:200]}")
        return body

    # Sous-chaîne du SEUL libellé "pending" documenté ("Request not ready yet,
    # try again in 30 seconds") — le comportement de `reason` pour un
    # request_id inconnu/expiré n'est PAS documenté (peut être un 404, géré par
    # `raise_for_upstream`, ou un autre texte ici). Ne jamais dire à l'appelant
    # de réessayer sur un `reason` qu'on ne reconnaît pas.
    _PENDING_MARKER = "not ready"

    def fetch(self, request_id: str, *, force_results: bool = False) -> dict:
        """Un GET de statut, sans attente. Tant que le traitement n'est pas fini,
        Dropcontact répond 200 avec `success: false` (PAS une erreur HTTP) — on le
        traduit en `{"done": False, "pending": <bool>, "reason": <str>}` (`pending`
        distingue le SEUL cas "not ready yet" documenté d'un `reason` inconnu, que
        l'appelant ne doit pas traiter comme "réessaie plus tard"). Une fois fini :
        `{"done": True, "data": [...], "credits_left": <int>}`.

        force_results: renvoie les résultats partiels (items non encore traités
        laissés tels quels) au lieu d'attendre que le batch entier soit fini.
        """
        params = {"forceResults": "true"} if force_results else None
        resp = requests.get(
            f"{self.BASE_URL}/enrich/all/{request_id}",
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        raise_for_upstream(resp, service="dropcontact")
        body = resp.json()

        if not body.get("success"):
            reason = body.get("reason", "")
            return {
                "done": False,
                "pending": self._PENDING_MARKER in reason.lower(),
                "reason": reason,
            }

        return {
            "done": True,
            "data": body.get("data", []),
            "credits_left": body.get("credits_left"),
        }

    def check_credits(self) -> dict:
        """Sonde 0-crédit (POST avec un contact vide) — authentifie la clé et
        renvoie `credits_left` sans consommer de quota."""
        return self.submit([{}])
