"""Snitcher REST API client — https://docs.snitcher.com/product/rest-api/introduction

Visitor identification: Snitcher resolves anonymous website traffic to the
companies ("organisations") behind it, with per-visit sessions/events, decision
-maker contacts (email reveal is a PAID credit action), segments, tags, and
custom fields. Built from the official OpenAPI spec
(https://app.snitcher.com/api/docs?api-docs.json, fetched 2026-08-23) — every
endpoint it declares is wrapped here, one method per endpoint.

Auth: Personal Access Token (dashboard → Settings → Account → API), sent as
`Authorization: Bearer …`. Base URL `https://api.snitcher.com/v1`. Rate limit:
**60 requests/minute per token** (429 `{"message": "Too Many Attempts."}` past
it). Methods return the parsed JSON body as-is, unwrapping nothing.

**Live-tested 2026-08-24** against a real trial-tier token (workspace
tulina.ai): 24 of 27 endpoints exercised — every read, the full tag cycle
(create → attach → verified on the organisation → detach), the full custom
-field cycle (definition create/get/update/list, value set/set_many/clear,
definition delete), and a no-op `update_workspace`. Deliberately NOT
exercised: `reveal_contact_email` (spends a credit), `create_workspace`/
`delete_workspace`/`invite_user` (account-mutating). Findings:

⚠️ **The response envelope is INCONSISTENT — do not assume `{"data": …}`.**
Confirmed live: list endpoints return Laravel-style TOP-LEVEL pagination
(`success`, `current_page`, `last_page`, `total`, `per_page`, `data: […]` —
no `meta` block despite the spec's Pagination component); `get_organisation`
and every custom-field definition/value op return the BARE object with no
wrapper at all; tag add/remove return `{"success", "message"}`; the DELETE
endpoints return an empty body (→ `None` here).

⚠️ **Nested FilterGroups are REJECTED by the live API** even though the spec
declares `conditions` items as `FilterCondition | FilterGroup` — a nested
group gets 422 `filters.conditions.N.field field is required`. Flat
conditions under one top-level operator only.

⚠️ **`filter_organisations` accepts a NARROW, visit-centric field set** the
spec nowhere enumerates. Probed live (32 candidates): accepted = `last_seen`,
`first_seen`, `tag`, `sessions`, `pageviews`, `time_on_site`, `url`,
`referrer`, `source`. Rejected (422 "Invalid filter field") = every
firmographic guess (`name`, `website`, `domain`, `industry`, `size`,
`country`, `employees`, `annual_revenue`, `icp_tier`…) AND custom-field keys
in three spellings (`<key>`, `custom_fields.<key>`, `custom.<key>`). For
firmographic narrowing use `list_organisations(name=…)` or segments.

Confirmed as spec'd: `set_custom_field_values` auto-creates unknown keys
with the type inferred from the value (a `42` created a `number` field);
`list_custom_field_values` also returns the FIXED system fields (name,
website, description…, `source: "fixed"`) alongside custom ones; contact
emails read `"[not-revealed]"` until the paid reveal; `list_contacts` by
`domain` works for any company, not just identified visitors (folk.app
returned 25 contacts).

⚠️ `reveal_contact_email` is the one call here that SPENDS credits (it
un-hides a contact's email). Every other method is a read or a free write
(tags, custom fields, workspace admin).

⚠️ Workspace `create`/`delete`/`update`/`invite` are ACCOUNT-ADMIN surface:
`delete_workspace` destroys the workspace and its collected visit history.
Wrapped because the spec exposes them, but callers (and the MCP layer above)
must treat delete as destructive, not plumbing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class SnitcherClient:
    BASE_URL = "https://api.snitcher.com/v1"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or require_secret("SNITCHER_API_KEY")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _request(self, method: str, path: str, *, json: Any = None, params: Dict = None) -> Any:
        resp = requests.request(
            method, f"{self.BASE_URL}{path}", headers=self._headers(), json=json, params=params
        , timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="snitcher")
        return resp.json() if resp.content else None

    @staticmethod
    def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in params.items() if v is not None}

    # ------------------------------------------------------------------
    # User / account

    def get_me(self) -> Dict[str, Any]:
        """`GET /me` — the authenticated user's profile. Free; the verify probe."""
        return self._request("GET", "/me")

    # ------------------------------------------------------------------
    # Workspaces — one workspace = one tracked website.

    def list_workspaces(self, *, page: int = None, size: int = None) -> Dict[str, Any]:
        """`GET /workspaces` — every workspace the token can see."""
        return self._request("GET", "/workspaces", params=self._clean({"page": page, "size": size}))

    def create_workspace(self, url: str) -> Dict[str, Any]:
        """`POST /workspaces` — create a workspace tracking `url`."""
        return self._request("POST", "/workspaces", json={"url": url})

    def get_workspace(self, workspace_uuid: str) -> Dict[str, Any]:
        """`GET /workspaces/{uuid}`."""
        return self._request("GET", f"/workspaces/{workspace_uuid}")

    def update_workspace(self, workspace_uuid: str, *, usage_limit: int = None) -> Dict[str, Any]:
        """`PATCH /workspaces/{uuid}` — the spec's only writable field is `usage_limit`."""
        return self._request(
            "PATCH", f"/workspaces/{workspace_uuid}",
            json=self._clean({"usage_limit": usage_limit}),
        )

    def delete_workspace(self, workspace_uuid: str) -> Any:
        """`DELETE /workspaces/{uuid}` — ⚠️ destroys the workspace and its history."""
        return self._request("DELETE", f"/workspaces/{workspace_uuid}")

    def invite_user(self, workspace_uuid: str, email: str) -> Dict[str, Any]:
        """`POST /workspaces/{uuid}/users/invite`."""
        return self._request("POST", f"/workspaces/{workspace_uuid}/users/invite", json={"email": email})

    def create_workspace_tag(self, workspace_uuid: str, tag_name: str) -> Dict[str, Any]:
        """`POST /workspaces/{uuid}/tags` — declare a tag; attach it to an
        organisation with `add_organisation_tag`."""
        return self._request("POST", f"/workspaces/{workspace_uuid}/tags", json={"tag_name": tag_name})

    # ------------------------------------------------------------------
    # Organisations — the identified companies.

    def list_organisations(
        self, workspace_uuid: str, *,
        segment_uuid: str = None, page: int = None, size: int = None,
        date: str = None, date_from: str = None, date_to: str = None, name: str = None,
    ) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/organisations` — companies that visited.

        `date` (one day) is mutually exclusive with `date_from`/`date_to`
        (a range) per the spec; `name` is a contains-match; `size` 1-1000.
        """
        return self._request(
            "GET", f"/workspaces/{workspace_uuid}/organisations",
            params=self._clean({
                "segmentUuid": segment_uuid, "page": page, "size": size,
                "date": date, "date_from": date_from, "date_to": date_to, "name": name,
            }),
        )

    def filter_organisations(
        self, workspace_uuid: str, filters: Dict[str, Any], *,
        segment_uuid: str = None, page: int = None, size: int = None,
    ) -> Dict[str, Any]:
        """`POST /workspaces/{ws}/organisations` — advanced filtering.

        `filters`: `{"operator": "AND"|"OR", "conditions": [{"field",
        "comparison", "value"?, "unit"?}, …]}` — ⚠️ FLAT conditions only:
        nesting a FilterGroup inside `conditions` is spec'd but 422s live
        (confirmed 2026-08-24). Fields accepted live: last_seen, first_seen,
        tag, sessions, pageviews, time_on_site, url, referrer, source —
        firmographics and custom-field keys are rejected (see module
        docstring). Comparisons: equal/not_equal/contains/not_contains/
        starts_with/ends_with/doesnt_start_with/doesnt_end_with/in/not_in/
        between/not_between/greater_than/less_than/greater_than_or_equal/
        less_than_or_equal/less_than_x_units_ago/more_than_x_units_ago
        (those two need numeric `value` + `unit` second..year)/set/not_set/
        is_true/is_false.
        """
        return self._request(
            "POST", f"/workspaces/{workspace_uuid}/organisations",
            json={"filters": filters},
            params=self._clean({"segmentUuid": segment_uuid, "page": page, "size": size}),
        )

    def get_organisation(self, workspace_uuid: str, organisation_uuid: str) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/organisations/{org}`."""
        return self._request("GET", f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}")

    def add_organisation_tag(self, workspace_uuid: str, organisation_uuid: str, tag_name: str) -> Any:
        """`POST /workspaces/{ws}/organisations/{org}/tags`."""
        return self._request(
            "POST", f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}/tags",
            json={"tag_name": tag_name},
        )

    def remove_organisation_tag(self, workspace_uuid: str, organisation_uuid: str, tag_name: str) -> Any:
        """`DELETE /workspaces/{ws}/organisations/{org}/tags` — tag named in the
        JSON body (the spec's own shape for this DELETE)."""
        return self._request(
            "DELETE", f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}/tags",
            json={"tag_name": tag_name},
        )

    # ------------------------------------------------------------------
    # Contacts — decision-makers at an identified organisation.

    def list_contacts(
        self, workspace_uuid: str, *,
        organisation_uuid: str = None, domain: str = None,
        page: int = None, size: int = None,
    ) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/contacts` — one of `organisation_uuid` /
        `domain` is required by the API (both spec'd optional, but the
        description says "Either organisation_uuid or domain must be provided")."""
        if organisation_uuid is None and domain is None:
            raise ValueError("snitcher: list_contacts needs organisation_uuid or domain")
        return self._request(
            "GET", f"/workspaces/{workspace_uuid}/contacts",
            params=self._clean({
                "organisation_uuid": organisation_uuid, "domain": domain,
                "page": page, "size": size,
            }),
        )

    def reveal_contact_email(self, workspace_uuid: str, contact_uuid: str) -> Dict[str, Any]:
        """`PUT /workspaces/{ws}/contacts/{contact}/reveal-email` — ⚠️ SPENDS a
        credit to un-hide this contact's email address."""
        return self._request("PUT", f"/workspaces/{workspace_uuid}/contacts/{contact_uuid}/reveal-email")

    # ------------------------------------------------------------------
    # Sessions — per-visit data, with an `events` array (pageviews, form
    # submissions incl. field values, custom `track` events, clicks,
    # downloads). The spec's `views` array is deprecated (pageviews only).

    def list_sessions(
        self, workspace_uuid: str, *,
        segment_uuid: str = None, date: str = None,
        date_from: str = None, date_to: str = None,
        url: str = None, referrer: str = None,
        page: int = None, size: int = None,
    ) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/sessions` — all sessions in a date window.

        Either `date` or `date_from` is required by the API. `url`/`referrer`
        are contains-matches.
        """
        if date is None and date_from is None:
            raise ValueError("snitcher: list_sessions needs date or date_from")
        return self._request(
            "GET", f"/workspaces/{workspace_uuid}/sessions",
            params=self._clean({
                "segmentUuid": segment_uuid, "date": date,
                "date_from": date_from, "date_to": date_to,
                "url": url, "referrer": referrer, "page": page, "size": size,
            }),
        )

    def list_organisation_sessions(
        self, workspace_uuid: str, organisation_uuid: str, *,
        date: str = None, date_from: str = None, date_to: str = None,
        url: str = None, referrer: str = None,
        page: int = None, size: int = None,
    ) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/organisations/{org}/sessions` — one company's
        visit history. No date is required here (unlike `list_sessions`)."""
        return self._request(
            "GET", f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}/sessions",
            params=self._clean({
                "date": date, "date_from": date_from, "date_to": date_to,
                "url": url, "referrer": referrer, "page": page, "size": size,
            }),
        )

    # ------------------------------------------------------------------
    # Segments

    def list_segments(self, workspace_uuid: str) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/segments` — segment uuids feed
        `list_organisations`/`list_sessions` filtering."""
        return self._request("GET", f"/workspaces/{workspace_uuid}/segments")

    # ------------------------------------------------------------------
    # Custom field DEFINITIONS (workspace-level).

    def list_custom_fields(self, workspace_uuid: str) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/custom-fields`."""
        return self._request("GET", f"/workspaces/{workspace_uuid}/custom-fields")

    def create_custom_field(
        self, workspace_uuid: str, name: str, type: str, *,
        key: str = None, description: str = None, visible_in_spotter: bool = None,
        field_rules: List[Dict[str, Any]] = None, options: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`POST /workspaces/{ws}/custom-fields` — `key` is generated from
        `name` when omitted (lowercase letters/numbers/underscores).
        `options` items: {key, label, color?} (for select-type fields).
        ⚠️ `visible_in_spotter=True` exposes the field's values to any script
        on the tracked website (the Spotter response) — off by default."""
        return self._request(
            "POST", f"/workspaces/{workspace_uuid}/custom-fields",
            json=self._clean({
                "name": name, "type": type, "key": key, "description": description,
                "visible_in_spotter": visible_in_spotter,
                "field_rules": field_rules, "options": options,
            }),
        )

    def get_custom_field(self, workspace_uuid: str, key: str) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/custom-fields/{key}`."""
        return self._request("GET", f"/workspaces/{workspace_uuid}/custom-fields/{key}")

    def update_custom_field(
        self, workspace_uuid: str, key: str, *,
        name: str = None, description: str = None, visible_in_spotter: bool = None,
        field_rules: List[Dict[str, Any]] = None, options: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`PATCH /workspaces/{ws}/custom-fields/{key}` — omitted fields keep
        their current value (the key itself and the type are immutable)."""
        return self._request(
            "PATCH", f"/workspaces/{workspace_uuid}/custom-fields/{key}",
            json=self._clean({
                "name": name, "description": description,
                "visible_in_spotter": visible_in_spotter,
                "field_rules": field_rules, "options": options,
            }),
        )

    def delete_custom_field(self, workspace_uuid: str, key: str) -> Any:
        """`DELETE /workspaces/{ws}/custom-fields/{key}` — ⚠️ drops the
        definition AND its values on every organisation."""
        return self._request("DELETE", f"/workspaces/{workspace_uuid}/custom-fields/{key}")

    # ------------------------------------------------------------------
    # Custom field VALUES (per organisation).

    def list_custom_field_values(self, workspace_uuid: str, organisation_uuid: str) -> Dict[str, Any]:
        """`GET /workspaces/{ws}/organisations/{org}/custom-fields`."""
        return self._request(
            "GET", f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}/custom-fields"
        )

    def set_custom_field_values(
        self, workspace_uuid: str, organisation_uuid: str, custom_fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """`PATCH /workspaces/{ws}/organisations/{org}/custom-fields` — bulk
        set, `{key: value, …}`, ≤50 keys/request. Unknown keys are CREATED
        automatically (type inferred from the value). Empty values are
        refused by the API — clearing goes through `clear_custom_field_value`."""
        return self._request(
            "PATCH", f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}/custom-fields",
            json={"custom_fields": custom_fields},
        )

    def set_custom_field_value(
        self, workspace_uuid: str, organisation_uuid: str, key: str,
        value: Union[str, int, float, bool, List[Any]],
    ) -> Dict[str, Any]:
        """`PUT /workspaces/{ws}/organisations/{org}/custom-fields/{key}` —
        set ONE field's value."""
        return self._request(
            "PUT",
            f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}/custom-fields/{key}",
            json={"value": value},
        )

    def clear_custom_field_value(self, workspace_uuid: str, organisation_uuid: str, key: str) -> Any:
        """`DELETE /workspaces/{ws}/organisations/{org}/custom-fields/{key}` —
        removes the value (the definition stays)."""
        return self._request(
            "DELETE",
            f"/workspaces/{workspace_uuid}/organisations/{organisation_uuid}/custom-fields/{key}",
        )
