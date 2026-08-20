"""Granola API client (v1, https://docs.granola.ai) — meeting notes, transcripts,
folders, webhook endpoints.

Bearer token (`Authorization: Bearer grn_...`), confirmed on
docs.granola.ai/introduction and the OpenAPI spec's security scheme. One
method per REST endpoint (1 call = 1 endpoint) — the API itself is small (8
operations across 4 objects), so no passthrough/consolidation tricks are
needed here (contrast `AhrefsClient`, ~150 operations).

Verified against Granola's OpenAPI 3.1.0 spec
(`https://docs.granola.ai/api-reference/openapi.json`, 2026-08-20) rather than
guessed from doc prose — required fields, body shapes, and `page_size`
min/max/default are all spec-derived. **Live-tested 2026-08-20** with a real
workspace API key: list_notes/get_note/get_transcript/list_folders and the
full webhook-endpoint CRUD lifecycle (create → update → list → delete, no
state left behind) all matched exactly, including 400 validation errors
(`page_size` bounds, invalid `note_id`, `scopes` — a workspace key must pass
`scopes=["workspace"]` exactly, confirmed live). The spec's 9th operation,
`GET /v1/audit`, returned 404 on this key (likely plan-gated) and was dropped
rather than shipping a method nothing can currently call.

Two key kinds exist Granola-side (irrelevant to this client — same Bearer
header either way, Granola enforces the scope): **personal** keys (any
Business-plan member, self-serve) and **workspace** keys (admin-provisioned,
scoped to `["workspace"]` on webhook creation). Rate limits: 25 req/5s burst,
5 req/s (300/min) sustained — 429 on excess, not retried here (surfaced as
`UpstreamHTTPError`, same as every other client's non-2xx).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connect, read) — never an unbounded wait
_BASE_URL = "https://public-api.granola.ai"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drops `None` values — an omitted kwarg must not become the literal
    string 'None' in the querystring."""
    return {k: v for k, v in params.items() if v is not None}


class GranolaClient:
    """Granola API v1 client (https://public-api.granola.ai), Bearer auth."""

    BASE_URL = _BASE_URL

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: Granola API key (or env var `GRANOLA_API_KEY`), format
                `grn_...`. Created in the Granola desktop app under Settings →
                Connectors → API keys (personal key, any Business-plan
                member) or provisioned by a workspace admin (workspace key,
                Enterprise). byo-only — no platform-shared key.
        """
        self.api_key = api_key or require_secret("GRANOLA_API_KEY")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 json_body: Any = None) -> Any:
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}", params=_clean(params or {}), json=json_body,
            timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="granola")
        return resp.json()

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Dict[str, Any]) -> Any:
        return self._request("POST", path, json_body=_clean(body))

    def _patch(self, path: str, body: Dict[str, Any]) -> Any:
        return self._request("PATCH", path, json_body=_clean(body))

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # ================================================================
    # Notes
    # ================================================================

    def list_notes(self, **params: Any) -> Any:
        """GET /v1/notes — accessible meeting notes, paginated.

        Args:
            **params: `created_before`/`created_after`/`updated_after` (date
                or date-time), `folder_id` (also matches subfolders),
                `cursor`, `page_size` (1-30, default 10).
        """
        return self._get("/v1/notes", **params)

    def get_note(self, note_id: str, **params: Any) -> Any:
        """GET /v1/notes/{note_id} — one note's title, owner, calendar event,
        attendees, folder membership, and AI summary.

        Args:
            note_id: the note id (`not_...`).
            **params: `include="transcript"` inlines the transcript in the
                response — may return `TRANSCRIPT_TOO_LARGE` for long
                meetings, in which case use `get_transcript` instead.
        """
        return self._get(f"/v1/notes/{note_id}", **params)

    def get_transcript(self, note_id: str, **params: Any) -> Any:
        """GET /v1/notes/{note_id}/transcript — a note's transcript, paginated.

        Args:
            note_id: the note id (`not_...`).
            **params: `cursor`, `page_size` (1-100, default 50).
        """
        return self._get(f"/v1/notes/{note_id}/transcript", **params)

    # ================================================================
    # Folders
    # ================================================================

    def list_folders(self, **params: Any) -> Any:
        """GET /v1/folders — accessible folders, alphabetical, paginated.

        Args:
            **params: `cursor`, `page_size` (1-30, default 10). Each folder
                carries `parent_folder_id` (null = top-level) for hierarchy.
        """
        return self._get("/v1/folders", **params)

    # ================================================================
    # Webhook endpoints
    # ================================================================
    # (No `list_audit_events` here: GET /v1/audit is in Granola's OpenAPI
    # spec and the request matches it exactly, but live-tested 2026-08-20 it
    # returned 404 NOT_FOUND on a real workspace key whose every other
    # endpoint worked — likely a plan/Enterprise gate. Dropped rather than
    # shipping a method nothing can currently call.)

    def list_webhook_endpoints(self) -> Any:
        """GET /v1/webhook-endpoints — this key's registered webhook endpoints.
        `url` is redacted to its origin unless the caller is the endpoint's creator."""
        return self._get("/v1/webhook-endpoints")

    def create_webhook_endpoint(self, url: str, scopes: List[str], **body: Any) -> Any:
        """POST /v1/webhook-endpoints — register an HTTPS URL for event delivery.
        The response's `signing_secret` (Standard Webhooks HMAC-SHA256) is shown
        ONLY in this response — store it, it cannot be retrieved again.

        Args:
            url: publicly reachable HTTPS URL to deliver events to.
            scopes: which notes to receive events for — `["personal"]`,
                `["public"]`, or both; a Workspace API key must pass exactly
                `["workspace"]` (the key's own scope).
            **body: `events` (subset of "note.access_granted"/"note.edited"/
                "note.generated"; omit for all three), `folder_ids` (restrict
                delivery to these folders + subfolders, max 100; omit = every
                note matching `scopes`).
        """
        return self._post("/v1/webhook-endpoints", {"url": url, "scopes": scopes, **body})

    def update_webhook_endpoint(self, webhook_endpoint_id: str, **body: Any) -> Any:
        """PATCH /v1/webhook-endpoints/{webhook_endpoint_id} — change url/scopes/
        events/folder_ids/enabled. All fields optional; only supplied ones change."""
        return self._patch(f"/v1/webhook-endpoints/{webhook_endpoint_id}", body)

    def delete_webhook_endpoint(self, webhook_endpoint_id: str) -> Any:
        """DELETE /v1/webhook-endpoints/{webhook_endpoint_id} — stop deliveries to this endpoint."""
        return self._delete(f"/v1/webhook-endpoints/{webhook_endpoint_id}")
