"""Waalaxy REST API client — https://docs.waalaxy.com/api

Waalaxy automates LinkedIn prospecting (invitations, messages, email
sequences). Its public API (Advanced/Business plans) is deliberately small
and **import-only** — four operations, all wrapped here, one method each:
`test_connection`, `list_prospect_lists`, `list_campaigns` (running/paused
only), `add_prospects` (create prospects in a list, optionally enrolling them
in a campaign). There is NO read/search/delete of prospects, no campaign
create/start, no inbox, no stats: everything else happens in the app, and
outbound "CRM Sync" webhooks are a campaign-level feature, not an API one.

Auth: `Authorization: Bearer wa_live_…` (app → Settings → CRM Sync →
Generate API key, shown once). Base URL `https://developers.waalaxy.com` —
the prose docs say `api.waalaxy.com`, which does NOT resolve (checked
2026-08-26); the OpenAPI `servers` block and the reference pages use
`developers.waalaxy.com`. No pagination (lists return everything), no
published numeric rate limit (429 = RFC-7807 problem+json). Methods return
the parsed JSON body as-is.

⚠️ `add_prospects` answers **HTTP 200 even when every item failed** — the
outcome is per item in `result[i].importCode` (`success`,
`duplicated_prospect`, `prospect_successfully_moved_to_another_list`,
`failed_to_change_prospect_list`, `max_limit_crm`,
`custom_variable_exceeds_1000_chars`, `profile_deleted`, `server_error`,
`unknown_error`) and, when a `campaign_id` was given,
`result[i].addToCampaignCode` (`success`, `already_in_campaign`,
`cant_add_prospect_campaign_not_exist`, `cant_add_prospect_campaign_is_archived`,
`prospect_does_not_match_preconditions`, `unknown_error`). The client
returns the body untouched; the MCP layer summarises it.

Not live-tested (no key available at build time, 2026-08-26): built from
the official reference pages + the OpenAPI schema embedded in them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

IMPORT_CODES = (
    "success", "duplicated_prospect", "prospect_successfully_moved_to_another_list",
    "failed_to_change_prospect_list", "server_error", "profile_deleted",
    "unknown_error", "max_limit_crm", "custom_variable_exceeds_1000_chars",
)
ADD_TO_CAMPAIGN_CODES = (
    "success", "cant_add_prospect_campaign_not_exist",
    "cant_add_prospect_campaign_is_archived", "prospect_does_not_match_preconditions",
    "already_in_campaign", "unknown_error",
)
CUSTOM_VARIABLE_MAX_LEN = 1000


class WaalaxyClient:
    BASE_URL = "https://developers.waalaxy.com"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or require_secret("WAALAXY_API_KEY")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        resp = requests.request(method, f"{self.BASE_URL}{path}", headers=self._headers(), json=json)
        raise_for_upstream(resp, service="waalaxy")
        return resp.json() if resp.content else None

    def test_connection(self) -> Any:
        """`GET /integrations/test` — the verify probe. Returns the bare literal `true`."""
        return self._request("GET", "/integrations/test")

    def list_prospect_lists(self) -> List[Dict[str, Any]]:
        """`GET /prospectLists/getProspectLists` — every list of the key's user:
        `[{_id, user, name, totalProspects, iconColor, iconLabel}]`."""
        return self._request("GET", "/prospectLists/getProspectLists")

    def list_campaigns(self) -> Dict[str, Any]:
        """`GET /campaigns/getAll` — `{total, campaigns: [{_id, name}]}`.
        ⚠️ Only `running`/`paused` campaigns; archived/finished ones are absent."""
        return self._request("GET", "/campaigns/getAll")

    def add_prospects(
        self,
        prospects: List[Dict[str, Any]],
        prospect_list_id: str,
        *,
        origin: str = "oto",
        campaign_id: Optional[str] = None,
        can_create_duplicates: Optional[bool] = None,
        move_duplicates_to_other_list: Optional[bool] = None,
        should_overwrite_custom_profile_data: Optional[bool] = None,
        add_existing_prospect_in_campaign: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """`POST /prospects/addProspectFromIntegration` — import prospects into
        `prospect_list_id`, optionally enrolling them in `campaign_id`.

        Each prospect: `{"url": "<linkedin profile url>", "customProfile": {...},
        "customVariables": [{"label", "value"}]}` — `customProfile` fields:
        firstName, lastName, occupation, email, region,
        company{name, linkedinUrl, website}, phoneNumbers[{type, number}],
        birthday{day, month}. `origin` is a pure label shown in the app as
        `API-<origin>` (make/zapier/n8n are rendered natively).

        Flags (all default false server-side; None = not sent):
        `can_create_duplicates` (needs the account's `import_duplicates`
        permission), `move_duplicates_to_other_list`,
        `should_overwrite_custom_profile_data` (else only fills blanks),
        `add_existing_prospect_in_campaign`.

        Raises ValueError on an empty batch, a prospect without `url`, or a
        custom variable value over 1000 chars (the API would 200 with a
        per-item error code; failing early is cheaper).
        """
        if not prospects:
            raise ValueError("prospects must contain at least one prospect")
        if not prospect_list_id:
            raise ValueError("prospect_list_id is required")
        if not origin:
            raise ValueError("origin must be a non-empty label")
        for i, p in enumerate(prospects):
            if not isinstance(p, dict) or not p.get("url"):
                raise ValueError(f"prospects[{i}] must have a LinkedIn profile `url`")
            for v in p.get("customVariables") or ():
                if not isinstance(v, dict) or "label" not in v or "value" not in v:
                    raise ValueError(f"prospects[{i}].customVariables items need `label` and `value`")
                if len(str(v["value"])) > CUSTOM_VARIABLE_MAX_LEN:
                    raise ValueError(
                        f"prospects[{i}] custom variable {v['label']!r} exceeds "
                        f"{CUSTOM_VARIABLE_MAX_LEN} chars")
        body: Dict[str, Any] = {
            "prospects": prospects,
            "prospectListId": prospect_list_id,
            "origin": {"name": origin},
        }
        if campaign_id:
            body["campaignId"] = campaign_id
        for key, val in (
            ("canCreateDuplicates", can_create_duplicates),
            ("moveDuplicatesToOtherList", move_duplicates_to_other_list),
            ("shouldOverwriteCustomProfileData", should_overwrite_custom_profile_data),
            ("addExistingProspectInCampaign", add_existing_prospect_in_campaign),
        ):
            if val is not None:
                body[key] = bool(val)
        return self._request("POST", "/prospects/addProspectFromIntegration", json=body)
