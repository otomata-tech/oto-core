"""
FullEnrich API Client — waterfall multi-provider contact enrichment.

Async bulk API: POST job → GET status (FINISHED after ~30s-4min).
Pricing: 10 cr/phone, 1 cr/work_email, 3 cr/personal_email (pay-per-result).
Phone hit rate ~70% vs Kaspr ~13%.

⚠️ Surface async assumée (signal #252, 2026-07-22) : l'ancien `enrich_linkedin`
synchrone pollait in-process (131-147s mesurés) → tout client MCP raccroche avant
(~60s), résultat perdu ET crédits consommés. Le couple `submit`/`fetch` rend chaque
appel court ; le POLLING appartient à l'APPELANT (agent), pas au client HTTP.

Requires: requests
"""

from __future__ import annotations

import time

import requests

from ...config import require_secret

# Plafond de l'endpoint bulk FullEnrich (contacts par job).
MAX_CONTACTS_PER_JOB = 100

DEFAULT_ENRICH_FIELDS = ["contact.work_emails", "contact.phones"]


class FullenrichProfile:
    """Parsed enrichment result for 1 LinkedIn profile."""

    def __init__(
        self,
        linkedin_slug: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
        full_name: str | None = None,
        title: str | None = None,
        company_name: str | None = None,
        phones: list[str] | None = None,
        work_emails: list[str] | None = None,
        personal_emails: list[str] | None = None,
        location: str | None = None,
        raw_data: dict | None = None,
        fetched_at: str | None = None,
    ):
        self.linkedin_slug = linkedin_slug
        self.first_name = first_name
        self.last_name = last_name
        self.full_name = full_name
        self.title = title
        self.company_name = company_name
        self.phones = phones or []
        self.work_emails = work_emails or []
        self.personal_emails = personal_emails or []
        self.location = location
        self.raw_data = raw_data
        self.fetched_at = fetched_at

    @property
    def found(self) -> bool:
        return bool(self.phones or self.work_emails or self.personal_emails)

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "linkedin_slug": self.linkedin_slug,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "title": self.title,
            "company_name": self.company_name,
            "phones": self.phones,
            "work_emails": self.work_emails,
            "personal_emails": self.personal_emails,
            "location": self.location,
            "fetched_at": self.fetched_at,
        }


class FullenrichClient:
    BASE_URL = "https://app.fullenrich.com/api/v2"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or require_secret("FULLENRICH_API_KEY")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def submit(
        self,
        contacts: list[dict],
        enrich_fields: list[str] | None = None,
    ) -> str:
        """Soumet un job d'enrichissement bulk. Retourne l'enrichment_id (le job
        tourne côté FullEnrich, ~30s-4min ; récupérer via `fetch`).

        contacts: [{first_name, last_name, linkedin_slug?, company_name?, domain?}, ...]
        Chaque contact doit porter `linkedin_slug` OU `domain` (site de
        l'entreprise) — l'API FullEnrich rejette sinon le job entier
        (`error.enrichment.domain.empty`, vérifié live 2026-07-22).
        """
        if not contacts:
            raise ValueError("FullEnrich submit: aucun contact fourni.")
        if len(contacts) > MAX_CONTACTS_PER_JOB:
            raise ValueError(
                f"FullEnrich submit: {len(contacts)} contacts > plafond "
                f"{MAX_CONTACTS_PER_JOB}/job — découper en plusieurs jobs."
            )
        fields = enrich_fields or DEFAULT_ENRICH_FIELDS

        data = []
        for c in contacts:
            first_name, last_name = c.get("first_name"), c.get("last_name")
            if not first_name or not last_name:
                raise ValueError(
                    f"FullEnrich submit: first_name et last_name requis par contact (reçu {c!r})."
                )
            entry: dict = {
                "first_name": first_name,
                "last_name": last_name,
                "enrich_fields": fields,
            }
            slug = (c.get("linkedin_slug") or "").strip().strip("/")
            if slug:
                entry["linkedin_url"] = f"https://www.linkedin.com/in/{slug}/"
                entry["custom"] = {"slug": slug}
            if c.get("domain"):
                entry["domain"] = c["domain"]
            if not slug and not c.get("domain"):
                raise ValueError(
                    f"FullEnrich submit: linkedin_slug OU domain requis par contact "
                    f"(l'API rejette sinon le job entier ; reçu {c!r})."
                )
            if c.get("company_name"):
                entry["company_name"] = c["company_name"]
            data.append(entry)

        payload = {"name": f"oto-{int(time.time())}", "data": data}
        resp = requests.post(
            f"{self.BASE_URL}/contact/enrich/bulk",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"FullEnrich POST {resp.status_code}: {resp.text[:200]}")

        enrichment_id = resp.json().get("enrichment_id")
        if not enrichment_id:
            raise RuntimeError(f"FullEnrich POST: no enrichment_id in response: {resp.text[:200]}")
        return enrichment_id

    def fetch(self, enrichment_id: str) -> dict:
        """Un GET de statut, sans attente. Retourne
        `{"status": <str>, "profiles": [FullenrichProfile] | None}` —
        `profiles` n'est peuplé que si status == FINISHED."""
        resp = requests.get(
            f"{self.BASE_URL}/contact/enrich/bulk/{enrichment_id}",
            headers=self._headers(),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"FullEnrich GET {resp.status_code}: {resp.text[:200]}")

        body = resp.json()
        status = body.get("status", "")

        if status == "CREDITS_INSUFFICIENT":
            raise RuntimeError("FullEnrich : crédits insuffisants. Recharger sur app.fullenrich.com.")

        if status != "FINISHED":
            return {"status": status, "profiles": None}

        profiles = [self._parse(item) for item in body.get("data", [])]
        return {"status": status, "profiles": profiles}

    def _parse(self, item: dict) -> FullenrichProfile:
        contact = item.get("contact_info") or {}
        profile = item.get("profile") or {}
        employment = (profile.get("employment") or {}).get("all") or []
        loc = profile.get("location") or {}
        slug = (item.get("custom") or {}).get("slug")

        phones = [p["number"] for p in (contact.get("phones") or []) if p.get("number")]
        work_emails = [e["email"] for e in (contact.get("work_emails") or []) if e.get("email")]
        personal_emails = [e["email"] for e in (contact.get("personal_emails") or []) if e.get("email")]

        title = employment[0].get("title") if employment else None
        company = employment[0].get("company", {}).get("name") if employment else None
        location_parts = [loc.get("city"), loc.get("country")]
        location_str = ", ".join(p for p in location_parts if p) or None

        from datetime import datetime, timezone

        return FullenrichProfile(
            linkedin_slug=slug,
            first_name=profile.get("first_name"),
            last_name=profile.get("last_name"),
            full_name=profile.get("full_name"),
            title=title,
            company_name=company,
            phones=phones,
            work_emails=work_emails,
            personal_emails=personal_emails,
            location=location_str,
            raw_data=item,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
