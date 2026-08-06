"""Lusha API Client — https://docs.lusha.com/apis/openapi/

Auth is a flat `api_key` header (confirmed against Lusha's own auth docs) —
no OAuth, no `Authorization: Bearer` scheme.
"""
from typing import Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class LushaClient:
    BASE_URL = "https://api.lusha.com"

    # Limite DURE Lusha (search-and-enrich) — pas une politique oto.
    MAX_CONTACTS_PER_CALL = 100

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or require_secret("LUSHA_API_KEY")

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"api_key": self.api_key, "Content-Type": "application/json"}
        resp = requests.request(method, f"{self.BASE_URL}{path}", headers=headers, **kwargs)
        raise_for_upstream(resp, service="lusha")
        return resp.json() if resp.content else {}

    def search_and_enrich(
        self,
        contacts: list[dict],
        reveal: Optional[list[str]] = None,
        include_partial_profiles: Optional[bool] = None,
    ) -> dict:
        """Search for contacts and reveal fields (emails/phones) in ONE call
        (`POST /v3/contacts/search-and-enrich`) — up to 100 contacts per
        request, each identified by any combination of id / linkedinUrl /
        email / firstName+lastName+company.

        A contact Lusha can't resolve or reveal does NOT fail the whole
        call — it comes back with a per-record `error` (NOT_FOUND,
        COMPLIANCE_RESTRICTED, ENRICH_FAILED) inside `results`, alongside
        successful entries. Only a genuine HTTP-level failure (auth, rate
        limit, malformed request) raises here.

        Billing: TWO charges apply per Lusha's own docs — one for the
        search, one PER revealed field — `billing.creditsCharged` in the
        response is the actual total charged for this call.
        """
        if len(contacts) > self.MAX_CONTACTS_PER_CALL:
            raise ValueError(
                f"{len(contacts)} contacts — Lusha search-and-enrich caps at "
                f"{self.MAX_CONTACTS_PER_CALL} per call.")
        body: dict = {"contacts": contacts}
        if reveal:
            body["reveal"] = reveal
        if include_partial_profiles is not None:
            body["options"] = {"includePartialProfiles": include_partial_profiles}
        return self._request("POST", "/v3/contacts/search-and-enrich", json=body)
