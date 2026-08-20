"""Grain API client (v2, https://developers.grain.com) — meeting recordings,
transcripts, sharing, webhooks, workspace org data.

Bearer token PLUS a second required header (`Public-Api-Version`) — the one
connector in this codebase that needs two auth-adjacent headers, not one.
Confirmed on developers.grain.com's Authentication section: `Authorization:
Bearer TOKEN` + `Public-Api-Version: 2025-10-31`.

⚠️ **No machine-readable spec exists for this API** (openapi.json/docs.json/
mint.json/llms.txt all returned HTTP 403, consistently — a WAF block, not a
missing-file 404). Unlike `AhrefsClient`/`GranolaClient`, everything here was
first derived from doc-PAGE research (WebFetch summaries), never a spec.

**Live-tested 2026-08-20** against a real workspace (Personal Access Token,
folk.app) — 20 of 21 methods matched exactly on the first try: list/get
recordings, all 4 transcript formats, tag/untag, share/unshare with a user,
update (rename), download (21MB video, real bytes), create_upload_url (a
real presigned S3 URL — confirming the deliberate choice not to send the
Grain Bearer token there), and the full hook lifecycle (create against a
real reachable URL, list, delete). **One real bug found and fixed**:
`share_with_team` was coded from the doc page as `PUT .../teams/{team_id}`
(team_id in the path) — that 404s for real (a plain-text "Not Found", a
routing miss). The actual shape mirrors `share_with_user`: `PUT .../teams`
(plural) with `team_id` in the JSON body. Confirmed by re-testing: share →
`get_recording` shows the team in `teams[]` → unshare (the path-based
DELETE, which WAS correct as documented) removes it again. Exactly the kind
of bug the Ahrefs OpenAPI check caught that doc-only research missed —
proof this class of connector needs live verification, not just doc reading.

Also confirmed live, worth knowing before writing tool-layer error messages:
mutation endpoints (`add_tag`, `remove_tag`, `update_recording`,
`share_with_*`/`unshare_from_*`, `delete_hook`) all return `{"success":
true}`, not the updated object — unlike `create_hook`/`list_hooks`, which do
return full objects.

Two key kinds (same Bearer header either way, Grain enforces the scope):
**Personal Access Token** (PAT, per-user — some filters/includes are
PAT-only: `attendance`, `private_notes`) and **Workspace Access Token** (WAT,
admin-provisioned, "access to ALL DATA from your workspace" — required for
`user_id` on upload). OAuth2 + PKCE also exists for third-party apps but is
NOT implemented here (bigger lift — authorize/token/refresh flow, closer to
Slack/Google than to a simple keyed connector; add later if actually needed).

One method per REST endpoint (1 call = 1 endpoint), ~22 operations across 4
areas: recordings (CRUD + transcript formats + sharing), files (upload/
download — raw bytes, not JSON), hooks (webhook CRUD), org (users/teams/
meeting types, all list-only). List endpoints are POST with a JSON body
(`filter`/`include`/`cursor`), not GET+querystring — confirmed across every
list endpoint on the doc page, not a transcription slip.

Highlights are NOT separate endpoints — they're nested under a recording via
`include={"highlights": True}` on `get_recording`/`list_recordings`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connect, read) — never an unbounded wait
_BASE_URL = "https://api.grain.com"
_API_VERSION = "2025-10-31"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drops `None` values — an omitted kwarg must not become the literal
    string 'None' in the querystring/body."""
    return {k: v for k, v in params.items() if v is not None}


class GrainClient:
    """Grain API v2 client (https://api.grain.com), Bearer + Public-Api-Version."""

    BASE_URL = _BASE_URL

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: Grain Personal or Workspace Access Token (or env var
                `GRAIN_API_KEY`). Created at grain.com/app/settings/
                integrations?tab=api. byo-only — no platform-shared key.
        """
        self.api_key = api_key or require_secret("GRAIN_API_KEY")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"
        self.session.headers["Public-Api-Version"] = _API_VERSION

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 json_body: Any = None) -> requests.Response:
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}", params=_clean(params or {}), json=json_body,
            timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="grain")
        return resp

    def _json(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
              json_body: Any = None) -> Any:
        return self._request(method, path, params=params, json_body=json_body).json()

    def _text(self, path: str) -> str:
        return self._request("GET", path).text

    def _bytes(self, path: str) -> bytes:
        return self._request("GET", path).content

    # ================================================================
    # Recordings
    # ================================================================

    def list_recordings(self, **body: Any) -> Any:
        """POST /v2/recordings — accessible recordings, filtered, cursor-paginated.

        Live-confirmed 2026-08-20: `filter.team`, `filter.meeting_type`,
        `filter.title_search` (case-insensitive substring), and
        `filter.participant_scope` all narrow results correctly. Without a
        `filter.attendance` value, results include every workspace-public
        recording, not just this key's own — see the module docstring's
        scope warning before calling this unfiltered on a Personal key.

        Args:
            **body: `cursor` (pagination), `filter` ({"before_datetime"?,
                "after_datetime"?, "attendance"? (PAT-only, "hosted"|"attended"
                — the way to scope to "my recordings"), "participant_scope"?
                ("internal"|"external"), "title_search"?, "team"? (uuid),
                "meeting_type"? (uuid)}), `include`
                ({"highlights"?, "participants"?, "ai_action_items"?,
                "ai_summary"?, "private_notes"? (PAT-only), "calendar_event"?,
                "hubspot"?, "screenshares"?, "ai_template_sections"?: {"format":
                "json"|"markdown"|"text"}}). `include={"hubspot": True}`
                returns `{"hubspot_company_ids": [...], "hubspot_deal_ids":
                [...]}` (confirmed live); `include={"participants": True}`
                items carry `hs_contact_id` (null if unlinked).
        """
        return self._json("POST", "/_/public-api/v2/recordings", json_body=_clean(body))

    def get_recording(self, recording_id: str, **body: Any) -> Any:
        """POST /v2/recordings/{recording_id} — one recording (POST, not GET —
        confirmed from the doc page AND live 2026-08-20).

        Args:
            recording_id: the recording id (UUID).
            **body: `include` — same shape as `list_recordings`.
        """
        return self._json("POST", f"/_/public-api/v2/recordings/{recording_id}", json_body=_clean(body))

    def get_transcript(self, recording_id: str) -> Any:
        """GET /v2/recordings/{recording_id}/transcript — transcript as JSON."""
        return self._json("GET", f"/_/public-api/v2/recordings/{recording_id}/transcript")

    def get_transcript_text(self, recording_id: str) -> str:
        """GET /v2/recordings/{recording_id}/transcript.txt — transcript as plain text."""
        return self._text(f"/_/public-api/v2/recordings/{recording_id}/transcript.txt")

    def get_transcript_vtt(self, recording_id: str) -> str:
        """GET /v2/recordings/{recording_id}/transcript.vtt — transcript as WebVTT."""
        return self._text(f"/_/public-api/v2/recordings/{recording_id}/transcript.vtt")

    def get_transcript_srt(self, recording_id: str) -> str:
        """GET /v2/recordings/{recording_id}/transcript.srt — transcript as SRT."""
        return self._text(f"/_/public-api/v2/recordings/{recording_id}/transcript.srt")

    def download_recording(self, recording_id: str) -> bytes:
        """GET /v2/recordings/{recording_id}/download — the raw recording file
        (audio/video bytes, not JSON). Caller writes `content` to disk."""
        return self._bytes(f"/_/public-api/v2/recordings/{recording_id}/download")

    def update_recording(self, recording_id: str, title: str) -> Any:
        """PATCH /v2/recordings/{recording_id} — rename a recording."""
        return self._json("PATCH", f"/_/public-api/v2/recordings/{recording_id}",
                           json_body={"title": title})

    def add_tag(self, recording_id: str, tag: str) -> Any:
        """PUT /v2/recordings/{recording_id}/tags — add a tag to a recording."""
        return self._json("PUT", f"/_/public-api/v2/recordings/{recording_id}/tags",
                           json_body={"tag": tag})

    def remove_tag(self, recording_id: str, tag: str) -> Any:
        """DELETE /v2/recordings/{recording_id}/tags/{tag} — remove a tag."""
        return self._json("DELETE", f"/_/public-api/v2/recordings/{recording_id}/tags/{tag}")

    def share_with_user(self, recording_id: str, user_id: str) -> Any:
        """PUT /v2/recordings/{recording_id}/users — share a recording with a user."""
        return self._json("PUT", f"/_/public-api/v2/recordings/{recording_id}/users",
                           json_body={"user_id": user_id})

    def unshare_from_user(self, recording_id: str, user_id: str) -> Any:
        """DELETE /v2/recordings/{recording_id}/users/{user_id} — revoke a user's access."""
        return self._json("DELETE", f"/_/public-api/v2/recordings/{recording_id}/users/{user_id}")

    def share_with_team(self, recording_id: str, team_id: str) -> Any:
        """PUT /v2/recordings/{recording_id}/teams — share a recording with a team.

        Live-tested 2026-08-20: the doc page listed this as `PUT .../teams/
        {team_id}` (team_id in the path, mirroring `unshare_from_team`'s
        DELETE) — that 404s for real (plain-text "Not Found", a routing
        miss, not an app-level error). The real shape mirrors
        `share_with_user` instead: PUT to the plural `/teams` path with
        `team_id` in the JSON body. Confirmed live: share → `get_recording`
        shows the team in `teams[]` → unshare (path-based DELETE, which DID
        match as documented) removes it again."""
        return self._json("PUT", f"/_/public-api/v2/recordings/{recording_id}/teams",
                           json_body={"team_id": team_id})

    def unshare_from_team(self, recording_id: str, team_id: str) -> Any:
        """DELETE /v2/recordings/{recording_id}/teams/{team_id} — revoke a team's access."""
        return self._json("DELETE", f"/_/public-api/v2/recordings/{recording_id}/teams/{team_id}")

    # ================================================================
    # Files — upload/download (raw bytes, not JSON)
    # ================================================================

    def create_upload_url(self, filename: str, **body: Any) -> Any:
        """POST /v2/recordings/upload — get a one-time upload URL for a new
        recording (.mov/.mp4/.mp3/.m4a). Processing progress after upload
        posts to an `upload_status` hook if one is configured.

        Args:
            filename: the file being uploaded.
            **body: `user_id` — REQUIRED with a Workspace Access Token (who
                owns the resulting recording); not used with a PAT.

        Returns: `{url, uuid, max_duration_sec, max_upload_bytes}` — pass
            `url` and the file bytes to `upload_recording_file`.
        """
        return self._json("POST", "/_/public-api/v2/recordings/upload",
                           json_body={"filename": filename, **_clean(body)})

    def upload_recording_file(self, upload_url: str, data: bytes,
                               content_type: Optional[str] = None) -> None:
        """PUT the file bytes to the URL from `create_upload_url`.

        Deliberately does NOT reuse `self.session` — `upload_url` is a
        pre-authorized one-time URL (potentially a different host, e.g. S3),
        and forwarding this client's Grain Bearer token to an arbitrary
        upload host would leak the credential to a third party.
        """
        headers = {"Content-Type": content_type} if content_type else {}
        resp = requests.put(upload_url, data=data, headers=headers, timeout=(10, 300))
        raise_for_upstream(resp, service="grain")

    # ================================================================
    # Hooks (webhooks)
    # ================================================================

    def create_hook(self, hook_url: str, hook_type: str, **body: Any) -> Any:
        """POST /v2/hooks/create — register a webhook. Grain tests
        reachability at creation time: `hook_url` must answer 2xx immediately
        or the call fails — this is not a client-side retry, it's Grain's
        own validation. Live-confirmed 2026-08-20 against a real reachable
        URL: succeeds synchronously, returns the created Hook object.

        This is the entire Hook surface — Grain has no update/PATCH
        endpoint, only create/list/delete (confirmed against the doc page,
        not just inferred from an endpoint list: there is no 4th operation
        to miss here).

        Args:
            hook_url: HTTPS endpoint Grain will call.
            hook_type: one of "recording_added", "recording_updated",
                "recording_deleted", "highlight_added", "highlight_updated",
                "highlight_deleted", "story_added", "story_updated",
                "story_deleted", "upload_status". Stories have NO queryable
                REST endpoint of their own (confirmed) — `story_*` hooks are
                the only way to observe them; the push payload IS the data.
            **body: `include` — for recording_added/recording_updated, the
                same shape as `list_recordings`'s include; for
                highlight_added/highlight_updated, `{"transcript": bool,
                "speakers": bool}` (confirmed from the doc page); other
                hook_types take {} (omit `include`).
        """
        return self._json("POST", "/_/public-api/v2/hooks/create",
                           json_body={"hook_url": hook_url, "hook_type": hook_type, **_clean(body)})

    def list_hooks(self, **body: Any) -> Any:
        """POST /v2/hooks — registered webhooks.

        Args:
            **body: `filter` ({"hook_type"?, "state"?: "enabled"|"disabled"}).
        """
        return self._json("POST", "/_/public-api/v2/hooks", json_body=_clean(body))

    def delete_hook(self, hook_id: str) -> Any:
        """DELETE /v2/hooks/{hook_id} — remove a webhook."""
        return self._json("DELETE", f"/_/public-api/v2/hooks/{hook_id}")

    # ================================================================
    # Org — users, teams, meeting types (all list-only, no filters documented)
    # ================================================================

    def list_users(self) -> Any:
        """POST /v2/users — workspace users ({id, name, email})."""
        return self._json("POST", "/_/public-api/v2/users", json_body={})

    def list_teams(self) -> Any:
        """POST /v2/teams — workspace teams ({id, name})."""
        return self._json("POST", "/_/public-api/v2/teams", json_body={})

    def list_meeting_types(self) -> Any:
        """POST /v2/meeting_types — workspace meeting types ({id, name, scope})."""
        return self._json("POST", "/_/public-api/v2/meeting_types", json_body={})
