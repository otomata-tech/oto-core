"""Tally API client (https://api.tally.so) — forms, questions, blocks,
submissions, analytics, workspaces, folders, organization members, webhooks.

Bearer token (`Authorization: Bearer tly-...`), confirmed on
developers.tally.so/api-reference/introduction and the spec's `bearerAuth`
security scheme. One method per REST endpoint (1 call = 1 endpoint): the API
has 38 operations across 9 objects, small enough that no passthrough or
consolidation trick is needed.

Derived from Tally's OpenAPI 3.0.1 spec
(`https://developers.tally.so/api-reference/openapi.json`, read 2026-08-31) —
required fields, body shapes, enums and `limit` bounds are all spec-derived,
not guessed from doc prose — **et testé en live le 2026-08-31** avec une vraie
clé `tly-` (compte FREE) : identité, formulaires (création → lecture → PATCH →
corbeille), questions, réponses, les cinq vues d'analytics et le cycle complet
d'un webhook (création → liste → journal → PATCH → suppression) répondent
exactement comme codé. Ce que ce compte n'a PAS permis d'exercer est dit plus
bas, nommément.

## The version header is not optional

Tally versions the API **by date, Stripe-style**. An API key is pinned at
creation time to the then-current version and **that pinning cannot be
changed afterwards**. A request may override it per-call with the
`tally-version` header.

This matters more than it sounds for a multi-tenant connector: without an
explicit header, the SAME endpoint returns a DIFFERENT shape depending on when
each customer happened to create their key. Tally's own docs demonstrate it —
the `fetching-form-submissions` page (written against an early version) shows
submissions with no `previewUrl` and no `pdfUrl`, both of which the current
version does return (verified live 2026-08-31).

⚠️ **`formattedAnswer` is documented by the spec and was NOT returned in live**
on any of the three question types exercised (INPUT_TEXT, INPUT_EMAIL,
FILE_UPLOAD), even with the header pinned. The plausible reading is that it
only appears where the raw `answer` is not already readable — a choice
question whose `answer` is an option id — but that is UNVERIFIED: no
choice-type question was tested. Never rely on it being present; the tool
layer falls back to `answer`.

So this client **always sends `tally-version`**, defaulting to
`DEFAULT_API_VERSION` below. Callers can override per client instance.

`DEFAULT_API_VERSION` is the date of the most recent entry in Tally's public
changelog (2026-08-04, "v0.10.0"), which is the version the current spec
describes. **Confirmed accepted in live on 2026-08-31** — every call in this
module was made with that header. `api_version=None` disables the header
entirely, falling back to whatever the key is pinned to.

## Les enveloppes de liste, relevées en live — le spec ne les tranchait pas

Il n'y a PAS une forme de liste, il y en a quatre, et deux d'entre elles ne se
devinent pas (relevé 2026-08-31) :

| appel | enveloppe |
|---|---|
| `GET /forms`, `GET /workspaces` | `{items, page, limit, total, hasMore}` |
| `GET /webhooks` | `{webhooks, page, limit, hasMore, totalCount}` — **pas** `items` |
| `GET /webhooks/{id}/events` | `{page, limit, hasMore, totalNumberOfEvents, events}` |
| `GET /forms/{id}/questions` | `{questions, hasResponses}` |
| `GET /forms/{id}/submissions` | `{page, limit, hasMore, totalNumberOfSubmissionsPerFilter, questions, submissions}` |
| `GET /organizations/{id}/users`, `.../invites` | **un tableau nu**, sans enveloppe |

`PATCH /webhooks/{id}` et les `DELETE` rendent un **corps vide** (d'où le
`return None` du transport sur un corps vide, pas seulement sur un 204).

## Un 401 de Tally ne veut pas dire « clé invalide »

Relevé en live, et c'est le piège le plus coûteux de cette API :

- `GET /webhooks` rend **401** tant qu'aucun webhook n'a JAMAIS été créé sur le
  compte. Après une première création il rend 200 — et continue de rendre 200
  même une fois tous les webhooks supprimés. Le 401 dit « l'intégration
  webhooks n'existe pas encore », pas « ta clé est mauvaise ».
- `GET /forms/{id}/blocks` et `POST /workspaces` rendent **401** sur un plan
  FREE (gate de plan, pas d'authentification).

La sonde qui tranche est `get_me()` : si elle répond, la clé est bonne.

## ⚠️ Les URL rendues portent des jetons signés

Trois champs d'une réponse embarquent un `accessToken` (un JWT) et une
`signature` dans leur query string — vérifié en live :

- `submissions[].previewUrl` — la page de la réponse ;
- `submissions[].pdfUrl` — le PDF de la réponse ;
- l'`answer` d'une question `FILE_UPLOAD` — chaque fichier déposé, sur
  `storage.tally.so/private/...`.

Ce ne sont pas des URL publiques : le jeton EST le droit d'accès. Elles
traversent donc le contexte de l'agent et tout ce qui journalise un résultat
d'outil. Le client ne les retire pas — sans elles le fichier est inatteignable,
et c'est précisément ce à quoi il sert — mais quiconque journalise, met en
cache ou re-publie une réponse doit savoir qu'il manipule un porteur de droit,
pas une référence inerte.

## Rate limit

100 requests/minute, documented. Tally's own guidance is to prefer webhooks
over polling because deliveries do not consume the quota. A 429 is surfaced as
`UpstreamHTTPError` like any other non-2xx — not retried here.

## Two spec details that will bite

- **`PATCH /webhooks/{id}` is a full replace despite being a PATCH.** Its
  required fields are `formId`, `url`, `eventTypes` AND `isEnabled` — omit one
  and you are not "leaving it unchanged", you are failing validation or
  clearing it. Read the current webhook (`list_webhooks`) and merge before
  calling. `update_webhook` does not do that merge for you: it is a 1-call-1-
  endpoint transport, and the merge is a tool-layer decision.
- **`POST /organizations/{id}/invites` takes `emails` as a STRING**, while
  `workspaceIds` beside it is an array. That asymmetry is in the spec, not a
  transcription error here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connect, read) — never an unbounded wait
_BASE_URL = "https://api.tally.so"

#: Date-version sent as `tally-version` on every request. See the module
#: docstring: pinning is what keeps the response shape identical across
#: customers whose keys were created at different times.
DEFAULT_API_VERSION = "2026-08-04"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drops `None` values — an omitted kwarg must not become the literal
    string 'None' in the querystring, nor a null in a JSON body."""
    return {k: v for k, v in params.items() if v is not None}


class TallyClient:
    """Tally API client (https://api.tally.so), Bearer auth, date-versioned."""

    BASE_URL = _BASE_URL

    def __init__(self, api_key: Optional[str] = None,
                 api_version: Optional[str] = DEFAULT_API_VERSION):
        """
        Args:
            api_key: Tally API key (or env var `TALLY_API_KEY`), format
                `tly-...`. Created at https://tally.so/settings/api-keys.
                The key is tied to ONE user and inherits that user's
                permissions — there are no fine-grained scopes, and the key
                stops working if that user leaves the organization. byo-only,
                no platform-shared key is possible.
            api_version: value of the `tally-version` header. Defaults to
                `DEFAULT_API_VERSION`; pass `None` to send no header at all
                and inherit whatever version the key is pinned to.
        """
        self.api_key = api_key or require_secret("TALLY_API_KEY")
        self.api_version = api_version
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"
        if api_version:
            self.session.headers["tally-version"] = api_version

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 json_body: Any = None) -> Any:
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}", params=_clean(params or {}),
            json=json_body, timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="tally")
        if resp.status_code == 204 or not (resp.content or b"").strip():
            return None
        return resp.json()

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("POST", path, json_body=_clean(body or {}) if body else None)

    def _patch(self, path: str, body: Dict[str, Any]) -> Any:
        return self._request("PATCH", path, json_body=_clean(body))

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # ================================================================
    # Account — the current user
    # ================================================================

    def get_me(self, **params: Any) -> Any:
        """GET /users/me — the authenticated user.

        Also the only place `organizationId` is handed to you directly, which
        every `/organizations/{organizationId}/...` call needs. (It also
        appears on each form returned by `list_forms`.)

        Args:
            **params: `timezone` (IANA name) to render dates in that zone.
        """
        return self._get("/users/me", **params)

    # ================================================================
    # Forms
    # ================================================================

    def list_forms(self, **params: Any) -> Any:
        """GET /forms — the forms this key can see, paginated.

        Args:
            **params: `page` (1-based, default 1), `limit` (1-500, default
                50), `workspaceIds` (array of workspace ids to filter on —
                resolve them with `list_workspaces`).
        """
        return self._get("/forms", **params)

    def get_form(self, form_id: str) -> Any:
        """GET /forms/{formId} — one form with all its blocks and settings."""
        return self._get(f"/forms/{form_id}")

    def create_form(self, blocks: List[Dict[str, Any]], status: str, **body: Any) -> Any:
        """POST /forms — create a form.

        Args:
            blocks: the form's blocks, in order. Each is one of 39 block
                shapes (`{uuid, type, groupUuid, groupType, payload}`) —
                see https://developers.tally.so/blocks-reference. Required
                by the API even to create an empty form (pass `[]`).
            status: "BLANK", "DRAFT" or "PUBLISHED". ⚠️ "DELETED" was
                accepted before the 2026-08-04 version and is now rejected
                with a 400 — it left the form unrecoverable; use
                `delete_form` instead.
            **body: `workspaceId`, `folderId`, `templateId`, `settings`
                (language, isClosed, closeDate/Time, submissionsLimit,
                redirect, self/respondent email notifications, password,
                data-retention…).
        """
        return self._post("/forms", {"blocks": blocks, "status": status, **body})

    def update_form(self, form_id: str, **body: Any) -> Any:
        """PATCH /forms/{formId} — change `name`, `status`, `blocks` or `settings`.

        ⚠️ `blocks` is a REPLACEMENT of the whole array, not a merge: sending a
        subset deletes every block you left out. Read the current set with
        `get_blocks` first.
        """
        return self._patch(f"/forms/{form_id}", body)

    def delete_form(self, form_id: str) -> Any:
        """DELETE /forms/{formId} — move a form to the trash.

        Recoverable: Tally trashes rather than purges, and the form can be
        restored from the dashboard.
        """
        return self._delete(f"/forms/{form_id}")

    # ================================================================
    # Questions and blocks
    # ================================================================

    def list_questions(self, form_id: str) -> Any:
        """GET /forms/{formId}/questions — the form's questions.

        Each carries `id`, `type`, `title`, `numberOfResponses` and its
        `fields`. The `id` here is what `submissions[].responses[].questionId`
        points at — this is the lookup table that makes a submission readable.
        """
        return self._get(f"/forms/{form_id}/questions")

    def update_question(self, form_id: str, question_id: str, **body: Any) -> Any:
        """PATCH /forms/{formId}/questions/{questionId} — rename a question.

        Args:
            **body: `title`. It is the only editable field on this endpoint.
        """
        return self._patch(f"/forms/{form_id}/questions/{question_id}", body)

    def get_blocks(self, form_id: str) -> Any:
        """GET /forms/{formId}/blocks — the form's blocks, the authoring view.

        `list_questions` is the answering view (what was asked, how often it
        was answered); this is the editing view (every block, including layout
        ones — titles, dividers, page breaks — that are never answered).
        """
        return self._get(f"/forms/{form_id}/blocks")

    def update_blocks(self, form_id: str, blocks: List[Dict[str, Any]], **body: Any) -> Any:
        """PATCH /forms/{formId}/blocks — replace the form's blocks.

        ⚠️ Same replacement semantics as `update_form(blocks=…)`: the array is
        the new complete set. A block absent from it is deleted.

        Args:
            blocks: the full ordered block list.
            **body: `settings`.
        """
        return self._patch(f"/forms/{form_id}/blocks", {"blocks": blocks, **body})

    # ================================================================
    # Submissions
    # ================================================================

    def list_submissions(self, form_id: str, **params: Any) -> Any:
        """GET /forms/{formId}/submissions — the form's submissions, paginated.

        The response is RELATIONAL, not self-describing: `questions` is
        returned once per page, and each `submissions[].responses[]` entry
        points into it by `questionId`. Joining the two is what turns this
        into readable answers.

        Args:
            **params: `page` (1-based), `limit` (1-500, default 50),
                `filter` ("all" | "completed" | "partial"), `startDate` /
                `endDate` (ISO 8601), `afterId` (return submissions that came
                after this submission id — the right cursor for incremental
                ingestion, and cheaper than date windows).
        """
        return self._get(f"/forms/{form_id}/submissions", **params)

    def get_submission(self, form_id: str, submission_id: str) -> Any:
        """GET /forms/{formId}/submissions/{submissionId} — one submission,
        with its responses and the form's questions."""
        return self._get(f"/forms/{form_id}/submissions/{submission_id}")

    def delete_submission(self, form_id: str, submission_id: str) -> Any:
        """DELETE /forms/{formId}/submissions/{submissionId} — delete one
        submission.

        ⚠️ Unlike forms and workspaces, Tally's docs do NOT describe a trash
        for submissions. Treat this as destroying a respondent's answer.
        """
        return self._delete(f"/forms/{form_id}/submissions/{submission_id}")

    # ================================================================
    # Analytics
    # ================================================================

    def analytics_metrics(self, form_id: str, period: str) -> Any:
        """GET /forms/{formId}/analytics/metrics — visits, submissions,
        completion rate and friends, aggregated.

        Args:
            period: REQUIRED — "today", "yesterday", "24h", "7d", "30d",
                "3m", "6m", "12m" or "all".
        """
        return self._get(f"/forms/{form_id}/analytics/metrics", period=period)

    def analytics_visits(self, form_id: str, period: str) -> Any:
        """GET /forms/{formId}/analytics/visits — visit counts over time."""
        return self._get(f"/forms/{form_id}/analytics/visits", period=period)

    def analytics_submissions(self, form_id: str, period: str) -> Any:
        """GET /forms/{formId}/analytics/submissions — completed and partial
        submission counts over time."""
        return self._get(f"/forms/{form_id}/analytics/submissions", period=period)

    def analytics_dimensions(self, form_id: str, period: str) -> Any:
        """GET /forms/{formId}/analytics/dimensions — visitor breakdown by
        source, browser, OS, device and location."""
        return self._get(f"/forms/{form_id}/analytics/dimensions", period=period)

    def analytics_drop_off(self, form_id: str, period: str) -> Any:
        """GET /forms/{formId}/analytics/drop-off — per-question drop-off.

        Where respondents abandon the form, question by question.
        """
        return self._get(f"/forms/{form_id}/analytics/drop-off", period=period)

    # ================================================================
    # Workspaces
    # ================================================================

    def list_workspaces(self, **params: Any) -> Any:
        """GET /workspaces — workspaces with their members, pending invites
        and folders, paginated.

        Args:
            **params: `page` (1-based).
        """
        return self._get("/workspaces", **params)

    def get_workspace(self, workspace_id: str) -> Any:
        """GET /workspaces/{workspaceId} — one workspace with its members and folders."""
        return self._get(f"/workspaces/{workspace_id}")

    def create_workspace(self, name: str) -> Any:
        """POST /workspaces — create a workspace and join it. Requires a Pro plan."""
        return self._post("/workspaces", {"name": name})

    def update_workspace(self, workspace_id: str, name: str) -> Any:
        """PATCH /workspaces/{workspaceId} — rename a workspace. `name` is required."""
        return self._patch(f"/workspaces/{workspace_id}", {"name": name})

    def delete_workspace(self, workspace_id: str) -> Any:
        """DELETE /workspaces/{workspaceId} — trash a workspace AND every form in it.

        Recoverable: workspace and forms go to the trash; forms in DRAFT or
        PUBLISHED are marked DELETED and can be restored.
        """
        return self._delete(f"/workspaces/{workspace_id}")

    # ================================================================
    # Folders (Pro)
    # ================================================================

    def list_folders(self, workspace_id: str) -> Any:
        """GET /workspaces/{workspaceId}/folders — a workspace's folders. Pro plan.

        Each folder carries `parentId` (null = top level) for the hierarchy.
        """
        return self._get(f"/workspaces/{workspace_id}/folders")

    def create_folder(self, workspace_id: str, name: str, **body: Any) -> Any:
        """POST /workspaces/{workspaceId}/folders — create a folder. Pro plan.

        Args:
            **body: `parentId` to nest it inside another folder.
        """
        return self._post(f"/workspaces/{workspace_id}/folders", {"name": name, **body})

    def update_folder(self, workspace_id: str, folder_id: str, name: str) -> Any:
        """PATCH /workspaces/{workspaceId}/folders/{id} — rename a folder. Pro plan."""
        return self._patch(f"/workspaces/{workspace_id}/folders/{folder_id}", {"name": name})

    def delete_folder(self, workspace_id: str, folder_id: str) -> Any:
        """DELETE /workspaces/{workspaceId}/folders/{id} — delete a folder and
        its ENTIRE subtree, moving any forms they contain to the trash. Pro plan."""
        return self._delete(f"/workspaces/{workspace_id}/folders/{folder_id}")

    # ================================================================
    # Organization — members and invites
    # ================================================================

    def list_organization_users(self, organization_id: str) -> Any:
        """GET /organizations/{organizationId}/users — everyone in the organization.

        `organization_id` comes from `get_me()` or from any form's
        `organizationId`.
        """
        return self._get(f"/organizations/{organization_id}/users")

    def remove_organization_user(self, organization_id: str, user_id: str) -> Any:
        """DELETE /organizations/{organizationId}/users/{userId} — remove a
        person from the organization.

        ⚠️ Only the organization creator can remove someone else; anyone may
        remove themselves. Removing a user also **kills every API key that
        user created** — including, potentially, the one making this call.
        """
        return self._delete(f"/organizations/{organization_id}/users/{user_id}")

    def list_invites(self, organization_id: str) -> Any:
        """GET /organizations/{organizationId}/invites — pending invitations."""
        return self._get(f"/organizations/{organization_id}/invites")

    def create_invites(self, organization_id: str, workspace_ids: List[str],
                       emails: str) -> Any:
        """POST /organizations/{organizationId}/invites — invite people to workspaces.

        Args:
            workspace_ids: workspaces to invite into (array).
            emails: ⚠️ a STRING, not an array — that asymmetry with
                `workspace_ids` is in Tally's spec, not a mistake here.
        """
        return self._post(f"/organizations/{organization_id}/invites",
                          {"workspaceIds": workspace_ids, "emails": emails})

    def cancel_invite(self, organization_id: str, invite_id: str) -> Any:
        """DELETE /organizations/{organizationId}/invites/{inviteId} — cancel a
        pending invitation. Only its creator can cancel it."""
        return self._delete(f"/organizations/{organization_id}/invites/{invite_id}")

    # ================================================================
    # Webhooks
    # ================================================================

    def list_webhooks(self, **params: Any) -> Any:
        """GET /webhooks — every webhook across the forms and workspaces this
        key can see, paginated.

        Also the only way to read one webhook back: there is no
        `GET /webhooks/{id}`.

        Args:
            **params: `page` (1-based), `limit` (1-100, default 25).
        """
        return self._get("/webhooks", **params)

    def create_webhook(self, form_id: str, url: str, event_types: List[str],
                       **body: Any) -> Any:
        """POST /webhooks — deliver a form's events to an HTTPS URL.

        Args:
            form_id: the form to watch.
            url: where to POST the events.
            event_types: currently `["FORM_RESPONSE"]` is the only value the
                spec defines.
            **body: `signingSecret` (⚠️ CALLER-supplied, unlike providers that
                mint one for you — without it deliveries are unsigned and the
                receiver cannot tell a real payload from a forged one),
                `httpHeaders` (`[{"name": ..., "value": ...}]`, e.g. an auth
                header for the receiving service), `externalSubscriber` (your
                own identifier for whoever owns this subscription).
        """
        return self._post("/webhooks", {
            "formId": form_id, "url": url, "eventTypes": event_types, **body})

    def update_webhook(self, webhook_id: str, form_id: str, url: str,
                       event_types: List[str], is_enabled: bool, **body: Any) -> Any:
        """PATCH /webhooks/{webhookId} — replace a webhook's configuration.

        ⚠️ **A full replace despite the verb.** `formId`, `url`, `eventTypes`
        and `isEnabled` are all REQUIRED by the spec: this is not a partial
        patch, and a field you omit is not "left alone". Read the current
        webhook with `list_webhooks` and merge before calling — that merge is
        deliberately NOT done here, because this class is a 1-call-1-endpoint
        transport.

        Args:
            **body: `signingSecret`, `httpHeaders`.
        """
        return self._patch(f"/webhooks/{webhook_id}", {
            "formId": form_id, "url": url, "eventTypes": event_types,
            "isEnabled": is_enabled, **body})

    def delete_webhook(self, webhook_id: str) -> Any:
        """DELETE /webhooks/{webhookId} — stop deliveries.

        If it is the form's last webhook, Tally also marks the form's webhooks
        integration deleted.
        """
        return self._delete(f"/webhooks/{webhook_id}")

    def list_webhook_events(self, webhook_id: str, **params: Any) -> Any:
        """GET /webhooks/{webhookId}/events — delivery attempts for one webhook,
        with status, response code and retry state. Paginated (`page`)."""
        return self._get(f"/webhooks/{webhook_id}/events", **params)

    def retry_webhook_event(self, webhook_id: str, event_id: str) -> Any:
        """POST /webhooks/{webhookId}/events/{eventId} — re-deliver a failed event.

        Fires a real HTTP request at the configured endpoint. If that endpoint
        is not idempotent, a retry is a second delivery, not a correction.
        """
        return self._post(f"/webhooks/{webhook_id}/events/{event_id}")
