"""Fireflies.ai API client (https://docs.fireflies.ai) — GraphQL, single POST
endpoint (`https://api.fireflies.ai/graphql`), `Authorization: Bearer <key>`.

Unlike every other connector in this codebase so far (Ahrefs/Granola/Grain,
all REST), Fireflies is GraphQL — one endpoint, request body
`{"query": ..., "variables": ...}`, response `{"data": ..., "errors": ...}`.
A GraphQL call can fail with **HTTP 200** and a non-empty `errors` array
(e.g. `object_not_found`, `require_elevated_privilege`) — that shape needs
its own parsing, separate from `raise_for_upstream`'s HTTP-status-only
model. See `FirefliesGraphQLError` below.

⚠️ **No machine-readable spec exists** on the docs site — `docs.fireflies.ai/
api-reference/openapi.json` is listed in `llms.txt` but returns HTTP 404
("Asset not found") on fetch (confirmed via `curl` and WebFetch). Built from
doc-page research, then **live-tested 2026-08-20** against a real workspace
— including **GraphQL introspection** (`__schema`/`__type`, which the live
API has enabled), the closest thing to a real spec this API actually offers.
27 of the 30 methods known before introspection matched the schema exactly.
**3 real bugs found
and fixed**, all doc-vs-reality mismatches invisible without either live
introspection or an actual failed call:
- `list_transcripts`: doc named the `scope` arg's type `TranscriptsQueryScope`
  — that type doesn't exist; the real type is plain `String`. Also
  `organizers`/`participants` are `[String!]` (non-null items), not `[String]`
  as documented — sending the wrong variable type is a GraphQL validation
  error (HTTP 400), not a silent misbehavior, so this was easy to catch once
  actually called. On top of the type fix, the server's own runtime
  validation (a second, separate error — `invalid_arguments`, HTTP 400 with
  per-field metadata) revealed `scope`'s real semantics aren't what the docs
  implied either: values are lowercase (`"title"`/`"sentences"`/`"all"`, not
  `TITLE`), and `scope` REQUIRES `keyword` to be set — passing `scope` alone
  is rejected. See `list_transcripts`'s docstring for the confirmed contract.
- `get_askfred_thread`: the doc's own example query selects a `messages.error`
  field that does not exist on `AskFredMessage` (confirmed via introspection)
  — `Cannot query field "error"` at call time. Removed; `updated_at` (also in
  the doc's selection) DOES exist and was kept.
- `create_bite`: the real mutation's transcript arg is spelled `transcript_Id`
  (capital I) — inconsistent with every other `transcript_id` in this API,
  and not a typo introduced here; it's how Fireflies' own schema spells it.

Also confirmed live: the `sentences` (plural) discrepancy below resolved
correctly; `MeetingPrivacy` actually has 6 values, not the 5 the docs showed
(`participatingteammates` was missing); and two mutations
(`createUploadUrl`/`confirmUpload`, a direct-file-upload flow distinct from
`uploadAudio`'s public-URL flow) exist in the schema but appear on **no**
doc page at all — added here via `create_upload_url`/`upload_file_bytes`/
`confirm_upload`, discovered purely through introspection. ⚠️ Unlike
everything else in this list, these two mutations' shapes are schema-only,
**not** confirmed by a successful call: `createUploadUrl` returned
`forbidden` for the non-admin key used in live testing, so the happy path
(and therefore the exact meaning of `forbidden` here — plan-gated?
admin-gated? something else?) remains unverified.

**Doc discrepancy, resolved**: `schema/transcript.md`'s field table lists a
Transcript field as `sentence` (singular), but the actual working
`transcript` query example uses `sentences` (plural) returning `[Sentence]`.
This client uses `sentences`, trusting the working example over the field
table.

**Webhooks are dashboard-only** — both Webhooks V1
(`app.fireflies.ai/settings`) and V2 (`app.fireflies.ai/integrations/api/
webhook`) are configured exclusively through the Fireflies web UI. There is
no GraphQL query or mutation to create/list/delete a webhook subscription,
so this client (and the oto-backend tool layer built on top of it)
deliberately has no webhook-management surface.

**Rate limits** (tiered, undocumented as HTTP headers — enforced silently
upstream): Free 50 req/day, Pro 500 req/day, Business/Enterprise 60 req/min.
Per-mutation overrides: `deleteTranscript` 10/min, `shareMeeting` 10/hour
(max 50 emails/call), `createLiveActionItem`/`createLiveSoundbite`/
`updateMeetingState` 10/hour, `addToLiveMeeting` 3/20min.

33 methods across 4 areas: transcripts/meetings (2 queries + 7 mutations,
plus the 3 undocumented upload-flow methods above), live meetings + AskFred
(4 queries + 7 mutations), org/misc (12 queries + 2 mutations). One method
per GraphQL query/mutation. Default field selections mirror each operation's
documented example query (the only "verbatim, working" shape available
without a spec) — pass `fields=` to override the selection set for any
method that accepts it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connect, read) — never an unbounded wait
_ENDPOINT = "https://api.fireflies.ai/graphql"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drops `None` values — an omitted kwarg must not become a GraphQL
    variable bound to `null`."""
    return {k: v for k, v in params.items() if v is not None}


class FirefliesGraphQLError(Exception):
    """Fireflies answered HTTP 200 with a non-empty GraphQL `errors` array.

    `errors` = the raw list (each `{message, friendly?, code?, extensions?}`
    per the docs). `code`/`message` surface the first error for convenient
    display; the full list is still on `.errors` for a caller that wants
    every one (a single request can return several).
    """

    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        first = errors[0] if errors else {}
        self.message = first.get("message", "unknown GraphQL error")
        self.code = (first.get("extensions") or {}).get("code") or first.get("code")
        super().__init__(f"fireflies GraphQL error{f' ({self.code})' if self.code else ''}: {self.message}")


class FirefliesClient:
    """Fireflies.ai GraphQL API client (https://api.fireflies.ai/graphql)."""

    ENDPOINT = _ENDPOINT

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: Fireflies API key (or env var `FIREFLIES_API_KEY`).
                Created at app.fireflies.ai/integrations/api — byo-only, no
                platform-shared key.
        """
        self.api_key = api_key or require_secret("FIREFLIES_API_KEY")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"
        self.session.headers["Content-Type"] = "application/json"

    def _execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Any:
        """POST the query, raise on HTTP-level failure OR a GraphQL `errors`
        array, return the `data` object."""
        resp = self.session.post(
            self.ENDPOINT, json={"query": query, "variables": _clean(variables or {})},
            timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="fireflies")
        body = resp.json()
        if body.get("errors"):
            raise FirefliesGraphQLError(body["errors"])
        return body.get("data")

    # ================================================================
    # Transcripts & meetings
    # ================================================================

    _TRANSCRIPT_FIELDS = """
        id
        dateString
        privacy
        analytics {
          sentiments { negative_pct neutral_pct positive_pct }
          categories { questions date_times metrics tasks }
          speakers {
            speaker_id name duration word_count longest_monologue
            monologues_count filler_words questions duration_pct words_per_minute
          }
        }
        speakers { id name }
        sentences {
          index speaker_name speaker_id text raw_text start_time end_time
          ai_filters { task pricing metric question date_and_time text_cleanup sentiment }
        }
        title
        host_email
        organizer_email
        calendar_id
        user { user_id email name num_transcripts recent_meeting minutes_consumed is_admin integrations }
        fireflies_users
        participants
        date
        transcript_url
        audio_url
        video_url
        duration
        meeting_attendees { displayName email phoneNumber name location }
        meeting_attendance { name join_time leave_time }
        summary {
          keywords action_items outline shorthand_bullet overview bullet_gist
          gist short_summary short_overview meeting_type topics_discussed transcript_chapters
        }
        cal_id
        calendar_type
        meeting_info { fred_joined silent_meeting summary_status }
        apps_preview { outputs { transcript_id user_id app_id created_at title prompt response } }
        meeting_link
        is_live
        channels { id }
    """

    def get_transcript(self, transcript_id: str, *, fields: Optional[str] = None) -> Any:
        """`transcript(id: ...)` — one transcript with full analytics/summary/
        sentences. `fields` overrides the (large) default selection set."""
        query = f"""
            query Transcript($transcriptId: String!) {{
              transcript(id: $transcriptId) {{ {fields or self._TRANSCRIPT_FIELDS} }}
            }}
        """
        return self._execute(query, {"transcriptId": transcript_id})["transcript"]

    def list_transcripts(self, *, fields: Optional[str] = None, **filters: Any) -> Any:
        """`transcripts(...)` — search/list transcripts.

        Args:
            **filters: `keyword` (searches title + spoken words, max 255
                chars), `scope` (plain string, lowercase — one of `"title"`,
                `"sentences"`, `"all"`; REQUIRES `keyword` to be set when
                given, and defaults to `"title"` if `keyword` is set without
                it — confirmed live 2026-08-20, the docs' `TITLE`-style
                casing and "no type" description were both misleading),
                `fromDate`/`toDate` (ISO 8601), `limit` (max 50), `skip`,
                `host_email`, `user_id`, `mine` (bool — key owner's own
                meetings), `organizers` ([email]), `participants` ([email]),
                `channel_id`. (Deprecated params `title`/`date`/
                `organizer_email`/`participant_email` are intentionally not
                exposed — the docs name their replacements above.)
            fields: overrides the default per-item selection
                (`id title speakers{id name} host_email date duration`).
        """
        query = f"""
            query Transcripts(
              $keyword: String, $scope: String, $fromDate: DateTime,
              $toDate: DateTime, $limit: Int, $skip: Int, $host_email: String,
              $user_id: String, $mine: Boolean, $organizers: [String!], $participants: [String!],
              $channel_id: String
            ) {{
              transcripts(
                keyword: $keyword, scope: $scope, fromDate: $fromDate, toDate: $toDate,
                limit: $limit, skip: $skip, host_email: $host_email, user_id: $user_id,
                mine: $mine, organizers: $organizers, participants: $participants,
                channel_id: $channel_id
              ) {{ {fields or "id title speakers { id name } host_email date duration"} }}
            }}
        """
        return self._execute(query, filters)["transcripts"]

    def delete_transcript(self, transcript_id: str) -> Any:
        """`deleteTranscript(id: ...)` — permanently deletes a transcript.
        Own meetings only, unless team admin. Rate limit: 10/min."""
        query = """
            mutation deleteTranscript($id: String!) {
              deleteTranscript(id: $id) {
                id title host_email organizer_email fireflies_users participants
                date transcript_url audio_url duration
              }
            }
        """
        return self._execute(query, {"id": transcript_id})["deleteTranscript"]

    def upload_audio(self, url: str, **input_fields: Any) -> Any:
        """`uploadAudio(input: AudioUploadInput)` — queue a publicly
        accessible media URL for transcription.

        Args:
            url: HTTPS URL of the media file (required, must be public).
            **input_fields: `title`, `webhook`, `custom_language`,
                `save_video` (bool), `attendees` ([{displayName, email,
                phoneNumber}]), `client_reference_id`, `bypass_size_check`
                (bool), `download_auth`.
        """
        query = """
            mutation uploadAudio($input: AudioUploadInput) {
              uploadAudio(input: $input) { success title message }
            }
        """
        return self._execute(query, {"input": {"url": url, **_clean(input_fields)}})["uploadAudio"]

    def create_upload_url(self, content_type: str, file_size: int, **input_fields: Any) -> Any:
        """`createUploadUrl(input: CreateUploadUrlInput)` — get a one-time
        presigned URL to upload a local file directly (bytes-in-hand),
        instead of `upload_audio`'s "transcribe this public URL" flow.

        ⚠️ Undocumented on docs.fireflies.ai (not found on any research page)
        — discovered live 2026-08-20 via GraphQL introspection alongside
        `confirmUpload`. Mirrors `GrainClient.create_upload_url`'s two-step
        shape: get a URL here, PUT the bytes with `upload_file_bytes`, then
        call `confirm_upload(meeting_id)` to finalize.

        ⚠️ **Return shape is from introspection only, NOT a successful live
        call** — calling this with the same non-admin key used for the rest
        of this connector's live testing returned `forbidden` ("You are not
        authorized to perform this action"), so the happy path is unverified.
        Could be admin-gated, plan-gated, or something else entirely; treat
        the field names below as a best guess until confirmed with a key
        that succeeds.

        Args:
            content_type: MIME type of the file being uploaded.
            file_size: size in bytes.
            **input_fields: `title`, `custom_language`, `attendees`
                ([{displayName, email, phoneNumber}]).

        Returns: `{upload_url, meeting_id, expires_at}` per the introspected
            schema (NOT confirmed by a successful call — see warning above) —
            pass `upload_url` to `upload_file_bytes`, `meeting_id` to
            `confirm_upload`.
        """
        query = """
            mutation CreateUploadUrl($input: CreateUploadUrlInput!) {
              createUploadUrl(input: $input) { upload_url meeting_id expires_at }
            }
        """
        variables = {"input": {"content_type": content_type, "file_size": file_size, **_clean(input_fields)}}
        return self._execute(query, variables)["createUploadUrl"]

    def upload_file_bytes(self, upload_url: str, data: bytes, content_type: Optional[str] = None) -> None:
        """PUT file bytes to the URL from `create_upload_url`.

        Deliberately does NOT reuse `self.session` — `upload_url` is a
        pre-authorized one-time URL (potentially a different host), and
        forwarding this client's Fireflies Bearer token to an arbitrary
        upload host would leak the credential to a third party (same
        reasoning as `GrainClient.upload_recording_file`, not live-tested
        here — would require an actual file to upload)."""
        headers = {"Content-Type": content_type} if content_type else {}
        resp = requests.put(upload_url, data=data, headers=headers, timeout=(10, 300))
        raise_for_upstream(resp, service="fireflies")

    def confirm_upload(self, meeting_id: str) -> Any:
        """`confirmUpload(input: ConfirmUploadInput)` — finalizes a
        `create_upload_url` upload once the bytes have been PUT. Returns
        `{success, meeting_id, message}` per the introspected schema — like
        `create_upload_url`, not exercised by an actual successful call (the
        `createUploadUrl` step it depends on returned `forbidden` for the
        non-admin key used in live testing)."""
        query = """
            mutation ConfirmUpload($input: ConfirmUploadInput!) {
              confirmUpload(input: $input) { success meeting_id message }
            }
        """
        return self._execute(query, {"input": {"meeting_id": meeting_id}})["confirmUpload"]

    def update_meeting_title(self, transcript_id: str, title: str) -> Any:
        """`updateMeetingTitle(input: UpdateMeetingTitleInput)` — admin-only."""
        query = """
            mutation UpdateMeetingTitle($input: UpdateMeetingTitleInput!) {
              updateMeetingTitle(input: $input) { title }
            }
        """
        return self._execute(query, {"input": {"id": transcript_id, "title": title}})["updateMeetingTitle"]

    def update_meeting_privacy(self, transcript_id: str, privacy: str) -> Any:
        """`updateMeetingPrivacy(input: UpdateMeetingPrivacyInput)`.

        Args:
            privacy: one of `link`, `owner`, `participants`,
                `participatingteammates`, `teammatesandparticipants`,
                `teammates` (the 6-value `MeetingPrivacy` enum, confirmed
                live 2026-08-20 via introspection — the docs only showed 5).
        """
        query = """
            mutation UpdateMeetingPrivacy($input: UpdateMeetingPrivacyInput!) {
              updateMeetingPrivacy(input: $input) { id title privacy }
            }
        """
        return self._execute(query, {"input": {"id": transcript_id, "privacy": privacy}})["updateMeetingPrivacy"]

    def update_meeting_channel(self, transcript_ids: List[str], channel_id: str) -> Any:
        """`updateMeetingChannel(input: UpdateMeetingChannelInput)` — assigns
        a channel to 1-5 transcripts at once, all-or-nothing (any invalid id
        fails the whole call). Returns a LIST, one entry per transcript.

        ⚠️ Live-confirmed 2026-08-20: `channel_id` is **not validated against
        existing channels** — Fireflies happily writes an arbitrary, even
        nonexistent, string as the transcript's channel id (`channels[].id`
        becomes exactly whatever was passed, no `object_not_found`, no
        "all-or-nothing" rejection). "All-or-nothing" per the docs describes
        multi-transcript atomicity, not channel_id existence checking. There
        is also no mutation to UNSET a transcript's channel once assigned —
        each call REPLACES the value, it never clears it. Verify the channel
        exists first (`get_channel`/`list_channels`) before calling this —
        the API will not catch a typo for you."""
        query = """
            mutation UpdateMeetingChannel($input: UpdateMeetingChannelInput!) {
              updateMeetingChannel(input: $input) { id title channels { id } }
            }
        """
        return self._execute(
            query, {"input": {"transcript_ids": transcript_ids, "channel_id": channel_id}}
        )["updateMeetingChannel"]

    def share_meeting(self, meeting_id: str, emails: List[str], *, expiry_days: Optional[int] = None) -> Any:
        """`shareMeeting(input: ShareMeetingInput)` — up to 50 emails/call.
        Owner/team-admin only. Rate limit: 10/hour. Returns
        `{success, message}` — `success: false` if invitees are already
        invited (not an exception)."""
        query = """
            mutation ShareMeeting($input: ShareMeetingInput!) {
              shareMeeting(input: $input) { success message }
            }
        """
        return self._execute(
            query, {"input": _clean({"meeting_id": meeting_id, "emails": emails, "expiry_days": expiry_days})}
        )["shareMeeting"]

    def revoke_shared_meeting_access(self, meeting_id: str, email: str) -> Any:
        """`revokeSharedMeetingAccess(input: RevokeSharedMeetingAccessInput)`.
        Owner/team-admin only."""
        query = """
            mutation RevokeSharedMeetingAccess($input: RevokeSharedMeetingAccessInput!) {
              revokeSharedMeetingAccess(input: $input) { success message }
            }
        """
        return self._execute(query, {"input": {"meeting_id": meeting_id, "email": email}})["revokeSharedMeetingAccess"]

    # ================================================================
    # Live meetings & AskFred
    # ================================================================

    def list_active_meetings(self, *, email: Optional[str] = None, states: Optional[List[str]] = None) -> Any:
        """`active_meetings(input: {email, states})` — meetings currently
        being recorded. `states` values: `active`, `paused` (defaults to
        both). Non-admins can only pass their own `email`."""
        query = """
            query ActiveMeetings($email: String, $states: [MeetingState!]) {
              active_meetings(input: { email: $email, states: $states }) {
                id title organizer_email meeting_link start_time end_time privacy state
              }
            }
        """
        return self._execute(query, {"email": email, "states": states})["active_meetings"]

    def list_live_action_items(self, meeting_id: str) -> Any:
        """`live_action_items(meeting_id: ...)` — action items captured so
        far during an in-progress meeting."""
        query = """
            query LiveActionItems($meeting_id: ID!) {
              live_action_items(meeting_id: $meeting_id) { name action_item }
            }
        """
        return self._execute(query, {"meeting_id": meeting_id})["live_action_items"]

    def list_askfred_threads(self, *, transcript_id: Optional[str] = None) -> Any:
        """`askfred_threads(transcript_id: ...)` — AskFred Q&A threads,
        optionally filtered to one transcript."""
        query = """
            query GetAskFredThreads($transcript_id: String) {
              askfred_threads(transcript_id: $transcript_id) {
                id title transcript_id user_id created_at
              }
            }
        """
        return self._execute(query, {"transcript_id": transcript_id})["askfred_threads"]

    def get_askfred_thread(self, thread_id: str) -> Any:
        """`askfred_thread(id: ...)` — one AskFred thread with its full
        message history."""
        query = """
            query GetAskFredThread($threadId: String!) {
              askfred_thread(id: $threadId) {
                id title transcript_id user_id created_at
                messages {
                  id thread_id query answer suggested_queries status created_at updated_at
                }
              }
            }
        """
        return self._execute(query, {"threadId": thread_id})["askfred_thread"]

    def add_to_live_meeting(self, meeting_link: str, **fields: Any) -> Any:
        """`addToLiveMeeting(meeting_link: ..., ...)` — drops the Fireflies
        bot into an already-running meeting. Rate limit: 3/20min.

        Args:
            meeting_link: valid http(s) URL (Zoom/Meet/Teams/etc).
            **fields: `title` (max 256 chars), `meeting_password` (max 32),
                `duration` (minutes, 15-120, default 60), `language`
                (max 5 chars, default English), `attendees` ([Attendee]).
        """
        query = """
            mutation AddToLiveMeeting(
              $meeting_link: String!, $title: String, $meeting_password: String,
              $duration: Int, $language: String, $attendees: [AttendeeInput!]
            ) {
              addToLiveMeeting(
                meeting_link: $meeting_link, title: $title, meeting_password: $meeting_password,
                duration: $duration, language: $language, attendees: $attendees
              ) { success }
            }
        """
        return self._execute(query, {"meeting_link": meeting_link, **fields})["addToLiveMeeting"]

    def create_live_action_item(self, meeting_id: str, prompt: str) -> Any:
        """`createLiveActionItem(input: CreateLiveActionItemInput)` — AI-
        drafts a new action item during a live meeting. Requires AI credits;
        organizer/team-admin only. Rate limit: 10/hour."""
        query = """
            mutation CreateLiveActionItem($input: CreateLiveActionItemInput!) {
              createLiveActionItem(input: $input) { success }
            }
        """
        return self._execute(query, {"input": {"meeting_id": meeting_id, "prompt": prompt}})["createLiveActionItem"]

    def create_live_soundbite(self, meeting_id: str, prompt: str) -> Any:
        """`createLiveSoundbite(input: CreateLiveSoundbiteInput)` — AI-clips
        a soundbite during a live meeting. Requires AI credits. Rate limit:
        10/hour."""
        query = """
            mutation CreateLiveSoundbite($input: CreateLiveSoundbiteInput!) {
              createLiveSoundbite(input: $input) { success }
            }
        """
        return self._execute(query, {"input": {"meeting_id": meeting_id, "prompt": prompt}})["createLiveSoundbite"]

    def update_meeting_state(self, meeting_id: str, action: str) -> Any:
        """`updateMeetingState(input: UpdateMeetingStateInput)` — controls an
        in-progress meeting.

        Args:
            action: `"pause_recording"` or `"resume_recording"` — the full
                `MeetingStateAction` enum, confirmed live 2026-08-20 via
                GraphQL introspection (the docs linked out to a schema
                sub-page never actually fetched during research). Rate
                limit: 10/hour."""
        query = """
            mutation UpdateMeetingState($input: UpdateMeetingStateInput!) {
              updateMeetingState(input: $input) { success action }
            }
        """
        return self._execute(query, {"input": {"meeting_id": meeting_id, "action": action}})["updateMeetingState"]

    def create_askfred_thread(self, query_text: str, **input_fields: Any) -> Any:
        """`createAskFredThread(input: CreateAskFredThreadInput)` — starts a
        new AskFred Q&A thread.

        Args:
            query_text: the question (max 2000 chars).
            **input_fields: `transcript_id` (scopes to one meeting; if set,
                `filters` is ignored), `filters` (`AskFredMeetingFiltersInput`:
                `start_time`, `end_time`, `channel_ids`, `organizers`,
                `participants`, `transcript_ids`), `response_language`
                (e.g. `en`), `format_mode` (`markdown`|`plaintext`).
        """
        query = """
            mutation CreateThreadForMeeting($input: CreateAskFredThreadInput!) {
              createAskFredThread(input: $input) {
                message { id thread_id query answer suggested_queries status created_at }
              }
            }
        """
        return self._execute(
            query, {"input": {"query": query_text, **_clean(input_fields)}}
        )["createAskFredThread"]["message"]

    def continue_askfred_thread(self, thread_id: str, query_text: str, **input_fields: Any) -> Any:
        """`continueAskFredThread(input: ContinueAskFredThreadInput)` — asks
        a follow-up in an existing AskFred thread.

        Args:
            thread_id: the thread to continue.
            query_text: the follow-up question (max 2000 chars).
            **input_fields: `response_language`, `format_mode`.
        """
        query = """
            mutation ContinueThread($input: ContinueAskFredThreadInput!) {
              continueAskFredThread(input: $input) {
                message { id thread_id query answer suggested_queries status created_at }
              }
            }
        """
        return self._execute(
            query, {"input": {"thread_id": thread_id, "query": query_text, **_clean(input_fields)}}
        )["continueAskFredThread"]["message"]

    def delete_askfred_thread(self, thread_id: str) -> Any:
        """`deleteAskFredThread(id: ...)` — permanent, irreversible."""
        query = """
            mutation DeleteThread($id: String!) {
              deleteAskFredThread(id: $id) { id title transcript_id user_id created_at }
            }
        """
        return self._execute(query, {"id": thread_id})["deleteAskFredThread"]

    # ================================================================
    # Org & misc
    # ================================================================

    _USER_FIELDS = """
        user_id recent_transcript recent_meeting num_transcripts name minutes_consumed
        is_admin integrations email
        user_groups { id name handle members { user_id first_name last_name email } }
    """

    def get_user(self, user_id: Optional[str] = None) -> Any:
        """`user(id: ...)` — omit `id` for the API key owner's own profile."""
        query = f"""
            query User($userId: String) {{
              user(id: $userId) {{ {self._USER_FIELDS} }}
            }}
        """
        return self._execute(query, {"userId": user_id})["user"]

    def list_users(self) -> Any:
        """`users` — every user on the team."""
        query = f"query Users {{ users {{ {self._USER_FIELDS} }} }}"
        return self._execute(query)["users"]

    def list_user_groups(self, *, mine: Optional[bool] = None) -> Any:
        """`user_groups(mine: ...)` — `mine=True` scopes to the caller's groups."""
        query = """
            query UserGroups($mine: Boolean) {
              user_groups(mine: $mine) {
                id name handle members { user_id first_name last_name email }
              }
            }
        """
        return self._execute(query, {"mine": mine})["user_groups"]

    _CHANNEL_FIELDS = "id title is_private created_by created_at updated_at members { user_id email name }"

    def get_channel(self, channel_id: str) -> Any:
        """`channel(id: ...)`."""
        query = f"""
            query Channel($channelId: ID!) {{
              channel(id: $channelId) {{ {self._CHANNEL_FIELDS} }}
            }}
        """
        return self._execute(query, {"channelId": channel_id})["channel"]

    def list_channels(self) -> Any:
        """`channels` — public channels in the team plus private ones the
        caller belongs to."""
        query = f"query Channels {{ channels {{ {self._CHANNEL_FIELDS} }} }}"
        return self._execute(query)["channels"]

    def list_contacts(self) -> Any:
        """`contacts` — everyone the caller has met, derived from meeting
        history (not a separate CRM)."""
        query = "query Contacts { contacts { email name picture last_meeting_date } }"
        return self._execute(query)["contacts"]

    _BITE_FIELDS = """
        transcript_id name id thumbnail preview status summary user_id start_time end_time
        summary_status media_type created_at
        created_from { description duration id name type }
        captions { end_time index speaker_id speaker_name start_time text }
        sources { src type }
        privacies
        user { first_name last_name picture name id }
    """

    def get_bite(self, bite_id: str) -> Any:
        """`bite(id: ...)` — one Soundbite (a short clip cut from a meeting)."""
        query = f"""
            query Bite($biteId: ID!) {{
              bite(id: $biteId) {{ {self._BITE_FIELDS} }}
            }}
        """
        return self._execute(query, {"biteId": bite_id})["bite"]

    def list_bites(self, *, mine: Optional[bool] = None, transcript_id: Optional[str] = None,
                   my_team: Optional[bool] = None, limit: Optional[int] = None,
                   skip: Optional[int] = None) -> Any:
        """`bites(...)` — Soundbites. At least one of `mine`, `transcript_id`,
        `my_team` is required by the API. `limit` caps at 50/query."""
        query = f"""
            query Bites($mine: Boolean, $transcript_id: ID, $my_team: Boolean, $limit: Int, $skip: Int) {{
              bites(mine: $mine, transcript_id: $transcript_id, my_team: $my_team, limit: $limit, skip: $skip) {{
                {self._BITE_FIELDS}
              }}
            }}
        """
        return self._execute(
            query, {"mine": mine, "transcript_id": transcript_id, "my_team": my_team, "limit": limit, "skip": skip}
        )["bites"]

    def get_analytics(self, *, start_time: Optional[str] = None, end_time: Optional[str] = None) -> Any:
        """`analytics(start_time, end_time)` — team + per-user conversation
        and meeting stats. `start_time`/`end_time` are ISO 8601."""
        query = """
            query Analytics($startTime: String, $endTime: String) {
              analytics(start_time: $startTime, end_time: $endTime) {
                team {
                  conversation {
                    average_filler_words average_filler_words_diff_pct average_monologues_count
                    average_monologues_count_diff_pct average_questions average_questions_diff_pct
                    average_sentiments { negative_pct neutral_pct positive_pct }
                    average_silence_duration average_silence_duration_diff_pct average_talk_listen_ratio
                    average_words_per_minute longest_monologue_duration_sec longest_monologue_duration_diff_pct
                    total_filler_words total_filler_words_diff_pct total_meeting_notes_count
                    total_meetings_count total_monologues_count total_monologues_diff_pct
                    teammates_count total_questions total_questions_diff_pct total_silence_duration
                    total_silence_duration_diff_pct
                  }
                  meeting {
                    count count_diff_pct duration duration_diff_pct average_count
                    average_count_diff_pct average_duration average_duration_diff_pct
                  }
                }
                users {
                  user_id user_name user_email
                  conversation {
                    talk_listen_pct talk_listen_ratio total_silence_duration total_silence_duration_compare_to
                    total_silence_pct total_silence_ratio total_speak_duration total_speak_duration_with_user
                    total_word_count user_filler_words user_filler_words_compare_to user_filler_words_diff_pct
                    user_longest_monologue_sec user_longest_monologue_compare_to user_longest_monologue_diff_pct
                    user_monologues_count user_monologues_count_compare_to user_monologues_count_diff_pct
                    user_questions user_questions_compare_to user_questions_diff_pct user_speak_duration
                    user_word_count user_words_per_minute user_words_per_minute_compare_to
                    user_words_per_minute_diff_pct
                  }
                  meeting {
                    count count_diff count_diff_compared_to count_diff_pct duration duration_diff
                    duration_diff_compared_to duration_diff_pct
                  }
                }
              }
            }
        """
        return self._execute(query, {"startTime": start_time, "endTime": end_time})["analytics"]

    def list_apps(self, *, app_id: Optional[str] = None, transcript_id: Optional[str] = None,
                  skip: Optional[int] = None, limit: Optional[int] = None) -> Any:
        """`apps(app_id, transcript_id, skip, limit)` — AI App outputs."""
        query = """
            query GetAIAppsOutputs($appId: String, $transcriptId: String, $skip: Float, $limit: Float) {
              apps(app_id: $appId, transcript_id: $transcriptId, skip: $skip, limit: $limit) {
                outputs { transcript_id user_id app_id created_at title prompt response }
              }
            }
        """
        return self._execute(query, {"appId": app_id, "transcriptId": transcript_id, "skip": skip, "limit": limit})["apps"]

    def list_rule_executions_by_meeting(self, *, limit: Optional[int] = None, cursor: Optional[str] = None,
                                        logs_per_meeting: Optional[int] = None,
                                        filters: Optional[Dict[str, Any]] = None) -> Any:
        """`rule_executions_by_meeting(...)` — automation-rule execution logs
        grouped by meeting. Enterprise plan only.

        Args:
            limit: max meeting groups (1-50, default 10).
            cursor: pagination cursor (from a previous `next_cursor`).
            logs_per_meeting: max execution logs per meeting (1-20, default 5).
            filters: `RuleExecutionFiltersInput` — `rule_id`, `meeting_id`,
                `date_from`, `date_to`, `is_test`.
        """
        query = """
            query RuleExecutionsByMeeting($limit: Int, $cursor: String, $logs_per_meeting: Int, $filters: RuleExecutionFiltersInput) {
              rule_executions_by_meeting(limit: $limit, cursor: $cursor, logs_per_meeting: $logs_per_meeting, filters: $filters) {
                meetings {
                  meeting_id
                  meeting { id title organizer_email }
                  executions {
                    extension_id extension_title stopped_at user_name
                    share { group_ids }
                    channel { channel_id }
                    meeting_privacy { privacy }
                  }
                }
                has_more
                next_cursor
              }
            }
        """
        return self._execute(
            query, {"limit": limit, "cursor": cursor, "logs_per_meeting": logs_per_meeting, "filters": filters}
        )["rule_executions_by_meeting"]

    def list_audit_events(self, category: str, *, limit: Optional[int] = None, cursor: Optional[str] = None,
                          action: Optional[str] = None, date_from: Optional[str] = None,
                          date_to: Optional[str] = None, actor_user_id: Optional[str] = None,
                          actor_email: Optional[str] = None) -> Any:
        """`auditEvents(limit, cursor, filters)` — compliance audit log.
        Enterprise plan + team-admin only. **Beta.**

        Args:
            category: required — one of `MEETING_OPERATIONS`,
                `TEAM_OPERATIONS`, `USER_OPERATIONS`, `AUTHENTICATION`.
            limit: 1-50, default 20.
            cursor: pagination cursor.
            action: filter to a specific `AuditEventAction`.
            date_from/date_to: ISO 8601.
            actor_user_id/actor_email: filter by who performed the action.
        """
        filters = _clean({
            "category": category, "action": action, "date_from": date_from, "date_to": date_to,
            "actor_user_id": actor_user_id, "actor_email": actor_email,
        })
        query = """
            query AuditEvents($limit: Int, $cursor: String, $filters: AuditEventFiltersInput!) {
              auditEvents(limit: $limit, cursor: $cursor, filters: $filters) {
                events {
                  id time category action severity status message
                  actor { user_id email full_name ip_address }
                  resource { type id }
                  metadata
                }
                has_more
                next_cursor
              }
            }
        """
        return self._execute(query, {"limit": limit, "cursor": cursor, "filters": filters})["auditEvents"]

    def set_user_role(self, user_id: str, role: str) -> Any:
        """`setUserRole(user_id, role)`. `role`: `admin` or `user`."""
        query = """
            mutation setUserRole($userId: String!, $role: Role!) {
              setUserRole(user_id: $userId, role: $role) { id name email role }
            }
        """
        return self._execute(query, {"userId": user_id, "role": role})["setUserRole"]

    def create_bite(self, transcript_id: str, start_time: float, end_time: float, **fields: Any) -> Any:
        """`createBite(transcript_Id, start_time, end_time, ...)` — cuts a
        new Soundbite from a transcript.

        ⚠️ Live-confirmed 2026-08-20 via GraphQL introspection: the mutation's
        transcript arg is actually named `transcript_Id` (capital I) — NOT
        `transcript_id` like every other field/arg in this API. Not a typo
        introduced here; it's how Fireflies' own schema spells it.

        Args:
            transcript_id: source transcript.
            start_time/end_time: clip bounds, in seconds.
            **fields: `name` (max 256 chars), `media_type` (`video`|`audio`),
                `privacies` ([`public`|`team`|`participants`]), `summary`
                (max 500 chars).
        """
        query = """
            mutation Mutation($transcriptId: ID!, $startTime: Float!, $endTime: Float!, $name: String,
                               $mediaType: String, $privacies: [BitePrivacy!], $summary: String) {
              createBite(transcript_Id: $transcriptId, start_time: $startTime, end_time: $endTime,
                          name: $name, media_type: $mediaType, privacies: $privacies, summary: $summary) {
                status name id
              }
            }
        """
        variables = {
            "transcriptId": transcript_id, "startTime": start_time, "endTime": end_time,
            "name": fields.get("name"), "mediaType": fields.get("media_type"),
            "privacies": fields.get("privacies"), "summary": fields.get("summary"),
        }
        return self._execute(query, variables)["createBite"]
