"""Check CRM API client — https://enrichment-two.vercel.app (Julien's "enrichment" app).

Thin wrapper around the app's public /v1 API (see its docs/sf-api.md): send a batch
of contacts for an async LinkedIn job-change check, and manage subsidiary/parent
company relationships used by that check's matcher. Auth is a single `X-API-Key`
header (not `Authorization: Bearer` like most other connectors here) — the key is
minted per-network on the enrichment side, unrelated to any Salesforce credential
despite the endpoint names' SF-flavored origin.
"""
import time
from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class CheckCrmClient:
    BASE_URL = "https://enrichment-two.vercel.app/v1"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or require_secret("CHECKCRM_API_KEY")

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        for attempt in range(3):
            resp = requests.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
            raise_for_upstream(resp, service="checkcrm")
            return resp.json() if resp.content else {}
        raise Exception("Rate limit exceeded after retries")

    def send_contacts(
        self,
        account_id: str,
        contacts: List[Dict[str, Any]],
        account_linkedin_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /contacts — enqueue an async job-change check for a batch of contacts.

        `contacts[]` items: `id` (required, your own contact identifier — echoed back
        on results), `linkedinUrl` (required for a contact to actually be checked —
        contacts without one are skipped and counted in `skippedCount`), plus optional
        `firstName`/`lastName`/`name`/`title`/`email`. `account_linkedin_url` sets the
        expected employer for job-change matching (omit for "no expected company").

        Returns `{checkId, contactCount, skippedCount}` immediately — this call does
        NOT return check results (see the module docstring / this connector's own
        docs: results are pushed to a per-network webhook the enrichment app owns,
        not retrievable through this client).
        """
        body: Dict[str, Any] = {"accountId": account_id, "contacts": contacts}
        if account_linkedin_url is not None:
            body["accountLinkedinUrl"] = account_linkedin_url
        return self._request("POST", "contacts", json=body)

    def add_subsidiary(
        self,
        company_linkedin_url: str,
        subsidiary_linkedin_url: str,
        subsidiary_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /companies/subsidiaries — add a subsidiary brand under a parent company.

        Idempotent: calling again with the same pair returns the existing row
        (`duplicate: true`) instead of erroring. `subsidiary_linkedin_url` may be a
        numeric LinkedIn company ID — it's accepted and resolved to its vanity form in
        the background (response includes `resolving: true` when that happens);
        `company_linkedin_url` (the parent) must already be the vanity-slug form.
        """
        body: Dict[str, Any] = {
            "companyLinkedinUrl": company_linkedin_url,
            "subsidiaryLinkedinUrl": subsidiary_linkedin_url,
        }
        if subsidiary_name is not None:
            body["subsidiaryName"] = subsidiary_name
        return self._request("POST", "companies/subsidiaries", json=body)

    def list_subsidiaries(self, company_linkedin_url: str) -> Dict[str, Any]:
        """GET /companies/subsidiaries — list the subsidiaries of a parent company.

        Raises `UpstreamHTTPError(404)` if this network has no company row for
        `company_linkedin_url` yet (unlike `add_subsidiary`, this does not
        auto-create the parent).
        """
        return self._request(
            "GET",
            "companies/subsidiaries",
            params={"companyLinkedinUrl": company_linkedin_url},
        )
