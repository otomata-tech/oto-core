"""Linear API client (https://linear.app/developers) — GraphQL, single POST
endpoint (`https://api.linear.app/graphql`).

Second GraphQL connector in this codebase after Fireflies — same overall
shape (one `_execute` helper, HTTP-200-with-`errors[]` handling), but two
concrete differences worth calling out:

- **Auth header has no `Bearer` prefix for personal API keys**: Linear wants
  `Authorization: <key>` verbatim (confirmed on the official GraphQL
  reference page). This is Linear's own inconsistency with almost every
  other bearer-token API in this codebase — passing `Bearer <key>` here
  would silently authenticate as nobody, not raise a clear 401 (this is
  called out on the docs page itself, not discovered live).
- **Rate-limit errors are HTTP 400, not 429**: exceeding 5,000 requests/hour
  or 3,000,000 complexity-points/hour returns HTTP 400 with a GraphQL
  `errors[].extensions.code == "RATELIMITED"` — indistinguishable from a
  generic validation error by status code alone. `_execute` checks the error
  code explicitly and raises `LinearRateLimited` (carrying the reset time
  from the `X-RateLimit-Requests-Reset` response header, epoch ms UTC)
  instead of the generic `LinearGraphQLError`, mirroring `UnipileRateLimited`
  (oto/tools/unipile/client.py) — the caller should STOP, not retry blindly.

⚠️ **No live key was available while building this client** — built from
Linear's public developer docs (graphql reference + example queries/
mutations) and general familiarity with Linear's GraphQL schema shape
(Relay-style `nodes`/`pageInfo{hasNextPage,endCursor}` cursor pagination,
`<Type>Filter` input objects). Default field selections below are
conservative, doc-example-derived shapes, not introspection-verified.
**Treat every method here as unverified until exercised against a real
Linear API key** — see this repo's Fireflies client for what that pass
tends to surface (doc-vs-reality mismatches in arg types, required-together
params, renamed fields). In particular:
- Whether `issue(id: ...)` accepts Linear's human-readable identifier
  (`"ENG-123"`) as well as the UUID is asserted by long-standing community
  usage, not confirmed in this session — verify before relying on it from a
  caller that only has the human identifier.
- `issueSearch` is the documented full-text search field; its argument shape
  (query string vs a structured filter) should be confirmed against a real
  response before exposing it beyond a keyword string.

**Webhooks are a real GraphQL surface here** (`webhookCreate`/`webhookUpdate`
/`webhookDelete`/`webhooks`), unlike Fireflies where webhook management is
dashboard-only — so this client, unlike Fireflies', DOES expose webhook
management methods.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connect, read) — never an unbounded wait
_ENDPOINT = "https://api.linear.app/graphql"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drops `None` values — an omitted kwarg must not become a GraphQL
    variable bound to `null`."""
    return {k: v for k, v in params.items() if v is not None}


class LinearError(Exception):
    """Base error for anything that isn't a clean 2xx-with-no-errors response."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class LinearGraphQLError(LinearError):
    """Linear answered with a non-empty GraphQL `errors` array (any code
    other than `RATELIMITED`, which gets its own subclass below).

    `errors` = the raw list (each `{message, extensions: {code, ...}, ...}`
    per Linear's error shape). `code`/`message` surface the first error for
    convenient display; the full list is still on `.errors`.
    """

    def __init__(self, errors: List[Dict[str, Any]], status_code: Optional[int] = None):
        self.errors = errors
        first = errors[0] if errors else {}
        message = first.get("message", "unknown GraphQL error")
        code = (first.get("extensions") or {}).get("code")
        self.code = code
        super().__init__(
            f"linear GraphQL error{f' ({code})' if code else ''}: {message}",
            status_code=status_code,
        )


class LinearRateLimited(LinearGraphQLError):
    """`RATELIMITED`: hourly request quota (5,000/hr) or complexity-point
    quota (3,000,000/hr) exhausted for this API key. `reset_at` = epoch ms
    UTC from `X-RateLimit-Requests-Reset` when the response carried it
    (None if the header was absent). The caller should STOP calling until
    then, not retry immediately — there is no `Retry-After` header, the
    reset headers are the only signal Linear gives."""

    def __init__(self, errors: List[Dict[str, Any]], reset_at: Optional[int] = None):
        super().__init__(errors, status_code=400)
        self.reset_at = reset_at


def _parse_reset(headers: Any) -> Optional[int]:
    raw = headers.get("X-RateLimit-Requests-Reset")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class LinearClient:
    """Linear GraphQL API client (https://api.linear.app/graphql)."""

    ENDPOINT = _ENDPOINT

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: Linear personal or workspace API key (or env var
                `LINEAR_API_KEY`). Created at linear.app/settings/api —
                byo-only, no platform-shared key (workspace-scoped by
                nature, no shared-credit pool to draw from).
        """
        self.api_key = api_key or require_secret("LINEAR_API_KEY")
        self.session = requests.Session()
        # No "Bearer " prefix — see module docstring.
        self.session.headers["Authorization"] = self.api_key
        self.session.headers["Content-Type"] = "application/json"

    def _execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Any:
        """POST the query, raise on a GraphQL `errors` array (rate-limit
        first, generic GraphQL error otherwise) or an HTTP-level failure
        with no parseable errors body, return the `data` object."""
        resp = self.session.post(
            self.ENDPOINT, json={"query": query, "variables": _clean(variables or {})},
            timeout=_HTTP_TIMEOUT)
        try:
            body = resp.json()
        except ValueError:
            raise_for_upstream(resp, service="linear")
            raise LinearError(f"linear: réponse non-JSON (HTTP {resp.status_code})",
                               status_code=resp.status_code)
        errors = body.get("errors")
        if errors:
            codes = {(e.get("extensions") or {}).get("code") for e in errors}
            if "RATELIMITED" in codes:
                raise LinearRateLimited(errors, reset_at=_parse_reset(resp.headers))
            raise LinearGraphQLError(errors, status_code=resp.status_code)
        if resp.status_code >= 400:
            raise_for_upstream(resp, service="linear")
        return body.get("data")

    # ================================================================
    # Issues
    # ================================================================

    _ISSUE_FIELDS = """
        id
        identifier
        title
        description
        priority
        estimate
        dueDate
        url
        createdAt
        updatedAt
        state { id name type }
        assignee { id name email }
        team { id name key }
        project { id name }
        cycle { id number }
        labels { nodes { id name color } }
        parent { id identifier title }
    """

    def get_issue(self, issue_id: str, *, fields: Optional[str] = None) -> Any:
        """`issue(id: ...)` — one issue. `id` is documented to accept the
        UUID; whether Linear's human-readable identifier (`"ENG-123"`) also
        resolves here is unverified (see module docstring)."""
        query = f"""
            query Issue($id: String!) {{
              issue(id: $id) {{ {fields or self._ISSUE_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": issue_id})["issue"]

    def list_issues(self, *, team_id: Optional[str] = None,
                     project_id: Optional[str] = None,
                     cycle_id: Optional[str] = None,
                     assignee_id: Optional[str] = None,
                     state_id: Optional[str] = None,
                     first: int = 50, after: Optional[str] = None,
                     fields: Optional[str] = None) -> Any:
        """`issues(filter:, first:, after:)` — list/filter issues. Returns
        the raw `{nodes, pageInfo{hasNextPage,endCursor}}` shape; pass the
        previous call's `endCursor` as `after` to page further."""
        filter_parts = []
        if team_id: filter_parts.append('team: { id: { eq: $teamId } }')
        if project_id: filter_parts.append('project: { id: { eq: $projectId } }')
        if cycle_id: filter_parts.append('cycle: { id: { eq: $cycleId } }')
        if assignee_id: filter_parts.append('assignee: { id: { eq: $assigneeId } }')
        if state_id: filter_parts.append('state: { id: { eq: $stateId } }')
        filter_clause = f"filter: {{ {', '.join(filter_parts)} }}" if filter_parts else ""
        query = f"""
            query Issues($teamId: ID, $projectId: ID, $cycleId: ID,
                          $assigneeId: ID, $stateId: ID,
                          $first: Int, $after: String) {{
              issues({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._ISSUE_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "teamId": team_id, "projectId": project_id, "cycleId": cycle_id,
            "assigneeId": assignee_id, "stateId": state_id,
            "first": first, "after": after,
        })["issues"]

    def search_issues(self, query_text: str, *, team_id: Optional[str] = None,
                       first: int = 50, after: Optional[str] = None,
                       fields: Optional[str] = None) -> Any:
        """`issueSearch(query:, filter:, first:, after:)` — full-text search
        across title + description. Argument shape unverified live (see
        module docstring) — built from the documented example."""
        filter_clause = 'filter: { team: { id: { eq: $teamId } } },' if team_id else ""
        query = f"""
            query IssueSearch($query: String!, $teamId: ID,
                               $first: Int, $after: String) {{
              issueSearch(query: $query, {filter_clause}
                          first: $first, after: $after) {{
                nodes {{ {fields or self._ISSUE_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "query": query_text, "teamId": team_id, "first": first, "after": after,
        })["issueSearch"]

    def create_issue(self, title: str, team_id: str, *,
                      description: Optional[str] = None,
                      assignee_id: Optional[str] = None,
                      state_id: Optional[str] = None,
                      priority: Optional[int] = None,
                      label_ids: Optional[List[str]] = None,
                      project_id: Optional[str] = None,
                      cycle_id: Optional[str] = None,
                      parent_id: Optional[str] = None,
                      due_date: Optional[str] = None,
                      estimate: Optional[int] = None,
                      fields: Optional[str] = None) -> Any:
        """`issueCreate(input:)`. `priority`: 0=none, 1=urgent, 2=high,
        3=normal, 4=low (Linear's own scale). `due_date`: ISO 8601 date."""
        query = f"""
            mutation IssueCreate($input: IssueCreateInput!) {{
              issueCreate(input: $input) {{
                success
                issue {{ {fields or self._ISSUE_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({
            "title": title, "teamId": team_id, "description": description,
            "assigneeId": assignee_id, "stateId": state_id, "priority": priority,
            "labelIds": label_ids, "projectId": project_id, "cycleId": cycle_id,
            "parentId": parent_id, "dueDate": due_date, "estimate": estimate,
        })
        return self._execute(query, {"input": input_})["issueCreate"]

    def update_issue(self, issue_id: str, *,
                      title: Optional[str] = None,
                      description: Optional[str] = None,
                      assignee_id: Optional[str] = None,
                      state_id: Optional[str] = None,
                      priority: Optional[int] = None,
                      label_ids: Optional[List[str]] = None,
                      project_id: Optional[str] = None,
                      cycle_id: Optional[str] = None,
                      due_date: Optional[str] = None,
                      estimate: Optional[int] = None,
                      fields: Optional[str] = None) -> Any:
        """`issueUpdate(id:, input:)`."""
        query = f"""
            mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {{
              issueUpdate(id: $id, input: $input) {{
                success
                issue {{ {fields or self._ISSUE_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({
            "title": title, "description": description, "assigneeId": assignee_id,
            "stateId": state_id, "priority": priority, "labelIds": label_ids,
            "projectId": project_id, "cycleId": cycle_id, "dueDate": due_date,
            "estimate": estimate,
        })
        return self._execute(query, {"id": issue_id, "input": input_})["issueUpdate"]

    def archive_issue(self, issue_id: str) -> Any:
        """`issueArchive(id:)` — soft-archive, reversible from Linear's UI."""
        query = """
            mutation IssueArchive($id: String!) {
              issueArchive(id: $id) { success }
            }
        """
        return self._execute(query, {"id": issue_id})["issueArchive"]

    def delete_issue(self, issue_id: str) -> Any:
        """`issueDelete(id:)` — moves to trash (Linear retains a restore
        window; not an instant hard-delete, per Linear's own documented
        behaviour)."""
        query = """
            mutation IssueDelete($id: String!) {
              issueDelete(id: $id) { success }
            }
        """
        return self._execute(query, {"id": issue_id})["issueDelete"]

    # ================================================================
    # Comments
    # ================================================================

    _COMMENT_FIELDS = """
        id
        body
        createdAt
        updatedAt
        user { id name }
        issue { id identifier }
        parent { id }
    """

    def get_comment(self, comment_id: str, *, fields: Optional[str] = None) -> Any:
        """`comment(id: ...)`."""
        query = f"""
            query Comment($id: String!) {{
              comment(id: $id) {{ {fields or self._COMMENT_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": comment_id})["comment"]

    def list_comments(self, issue_id: str, *, first: int = 50,
                       after: Optional[str] = None,
                       fields: Optional[str] = None) -> Any:
        """`comments(filter: {issue: {id: {eq: ...}}})` for one issue."""
        query = f"""
            query Comments($issueId: ID!, $first: Int, $after: String) {{
              comments(filter: {{ issue: {{ id: {{ eq: $issueId }} }} }},
                       first: $first, after: $after) {{
                nodes {{ {fields or self._COMMENT_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "issueId": issue_id, "first": first, "after": after,
        })["comments"]

    def create_comment(self, issue_id: str, body: str, *,
                        parent_id: Optional[str] = None,
                        fields: Optional[str] = None) -> Any:
        """`commentCreate(input:)`. `parent_id` threads a reply under another comment."""
        query = f"""
            mutation CommentCreate($input: CommentCreateInput!) {{
              commentCreate(input: $input) {{
                success
                comment {{ {fields or self._COMMENT_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({"issueId": issue_id, "body": body, "parentId": parent_id})
        return self._execute(query, {"input": input_})["commentCreate"]

    def update_comment(self, comment_id: str, body: str, *,
                        fields: Optional[str] = None) -> Any:
        """`commentUpdate(id:, input:)`."""
        query = f"""
            mutation CommentUpdate($id: String!, $input: CommentUpdateInput!) {{
              commentUpdate(id: $id, input: $input) {{
                success
                comment {{ {fields or self._COMMENT_FIELDS} }}
              }}
            }}
        """
        return self._execute(query, {"id": comment_id, "input": {"body": body}})["commentUpdate"]

    def delete_comment(self, comment_id: str) -> Any:
        """`commentDelete(id:)`."""
        query = """
            mutation CommentDelete($id: String!) {
              commentDelete(id: $id) { success }
            }
        """
        return self._execute(query, {"id": comment_id})["commentDelete"]

    # ================================================================
    # Projects
    # ================================================================

    _PROJECT_FIELDS = """
        id
        name
        description
        state
        url
        targetDate
        createdAt
        updatedAt
        lead { id name }
        teams { nodes { id name key } }
    """

    def get_project(self, project_id: str, *, fields: Optional[str] = None) -> Any:
        """`project(id: ...)`."""
        query = f"""
            query Project($id: String!) {{
              project(id: $id) {{ {fields or self._PROJECT_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": project_id})["project"]

    def list_projects(self, *, team_id: Optional[str] = None,
                       first: int = 50, after: Optional[str] = None,
                       fields: Optional[str] = None) -> Any:
        """`projects(filter:)` — optionally scoped to one team."""
        filter_clause = 'filter: { accessibleTeams: { id: { eq: $teamId } } }' if team_id else ""
        query = f"""
            query Projects($teamId: ID, $first: Int, $after: String) {{
              projects({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._PROJECT_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "teamId": team_id, "first": first, "after": after,
        })["projects"]

    def create_project(self, name: str, team_ids: List[str], *,
                        description: Optional[str] = None,
                        state: Optional[str] = None,
                        lead_id: Optional[str] = None,
                        target_date: Optional[str] = None,
                        fields: Optional[str] = None) -> Any:
        """`projectCreate(input:)`. `team_ids` is required — a project
        belongs to at least one team."""
        query = f"""
            mutation ProjectCreate($input: ProjectCreateInput!) {{
              projectCreate(input: $input) {{
                success
                project {{ {fields or self._PROJECT_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({
            "name": name, "teamIds": team_ids, "description": description,
            "state": state, "leadId": lead_id, "targetDate": target_date,
        })
        return self._execute(query, {"input": input_})["projectCreate"]

    def update_project(self, project_id: str, *,
                        name: Optional[str] = None,
                        description: Optional[str] = None,
                        state: Optional[str] = None,
                        lead_id: Optional[str] = None,
                        target_date: Optional[str] = None,
                        fields: Optional[str] = None) -> Any:
        """`projectUpdate(id:, input:)`."""
        query = f"""
            mutation ProjectUpdate($id: String!, $input: ProjectUpdateInput!) {{
              projectUpdate(id: $id, input: $input) {{
                success
                project {{ {fields or self._PROJECT_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({
            "name": name, "description": description, "state": state,
            "leadId": lead_id, "targetDate": target_date,
        })
        return self._execute(query, {"id": project_id, "input": input_})["projectUpdate"]

    # ================================================================
    # Teams & workflow states
    # ================================================================

    _TEAM_FIELDS = "id name key description private"

    def get_team(self, team_id: str, *, fields: Optional[str] = None) -> Any:
        """`team(id: ...)`."""
        query = f"""
            query Team($id: String!) {{
              team(id: $id) {{ {fields or self._TEAM_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": team_id})["team"]

    def list_teams(self, *, first: int = 50, after: Optional[str] = None,
                    fields: Optional[str] = None) -> Any:
        """`teams` — every team in the workspace visible to this key."""
        query = f"""
            query Teams($first: Int, $after: String) {{
              teams(first: $first, after: $after) {{
                nodes {{ {fields or self._TEAM_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {"first": first, "after": after})["teams"]

    def list_workflow_states(self, team_id: str, *, first: int = 50,
                              after: Optional[str] = None,
                              fields: Optional[str] = None) -> Any:
        """`workflowStates(filter: {team: {id: {eq: ...}}})` — a team's
        status list (Backlog/Todo/In Progress/Done/Cancelled buckets, each
        with its own states) — resolve a `state_id` for `update_issue` here."""
        query = f"""
            query WorkflowStates($teamId: ID!, $first: Int, $after: String) {{
              workflowStates(filter: {{ team: {{ id: {{ eq: $teamId }} }} }},
                              first: $first, after: $after) {{
                nodes {{ {fields or "id name type position color"} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "teamId": team_id, "first": first, "after": after,
        })["workflowStates"]

    # ================================================================
    # Cycles
    # ================================================================

    _CYCLE_FIELDS = "id number name startsAt endsAt completedAt team { id name }"

    def get_cycle(self, cycle_id: str, *, fields: Optional[str] = None) -> Any:
        """`cycle(id: ...)`."""
        query = f"""
            query Cycle($id: String!) {{
              cycle(id: $id) {{ {fields or self._CYCLE_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": cycle_id})["cycle"]

    def list_cycles(self, *, team_id: Optional[str] = None,
                     first: int = 50, after: Optional[str] = None,
                     fields: Optional[str] = None) -> Any:
        """`cycles(filter:)` — optionally scoped to one team."""
        filter_clause = 'filter: { team: { id: { eq: $teamId } } }' if team_id else ""
        query = f"""
            query Cycles($teamId: ID, $first: Int, $after: String) {{
              cycles({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._CYCLE_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "teamId": team_id, "first": first, "after": after,
        })["cycles"]

    # ================================================================
    # Labels
    # ================================================================

    _LABEL_FIELDS = "id name color description team { id name }"

    def get_label(self, label_id: str, *, fields: Optional[str] = None) -> Any:
        """`issueLabel(id: ...)`."""
        query = f"""
            query IssueLabel($id: String!) {{
              issueLabel(id: $id) {{ {fields or self._LABEL_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": label_id})["issueLabel"]

    def list_labels(self, *, team_id: Optional[str] = None,
                     first: int = 50, after: Optional[str] = None,
                     fields: Optional[str] = None) -> Any:
        """`issueLabels(filter:)` — optionally scoped to one team (workspace
        labels have no `team`)."""
        filter_clause = 'filter: { team: { id: { eq: $teamId } } }' if team_id else ""
        query = f"""
            query IssueLabels($teamId: ID, $first: Int, $after: String) {{
              issueLabels({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._LABEL_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "teamId": team_id, "first": first, "after": after,
        })["issueLabels"]

    def create_label(self, name: str, *, team_id: Optional[str] = None,
                      color: Optional[str] = None,
                      description: Optional[str] = None,
                      fields: Optional[str] = None) -> Any:
        """`issueLabelCreate(input:)`. Omitting `team_id` creates a
        workspace-level label (unverified — Linear's UI supports both
        team-scoped and workspace labels; whether `teamId: null` is how the
        API distinguishes them, vs a separate mutation, is unconfirmed)."""
        query = f"""
            mutation IssueLabelCreate($input: IssueLabelCreateInput!) {{
              issueLabelCreate(input: $input) {{
                success
                issueLabel {{ {fields or self._LABEL_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({
            "name": name, "teamId": team_id, "color": color, "description": description,
        })
        return self._execute(query, {"input": input_})["issueLabelCreate"]

    # ================================================================
    # Users
    # ================================================================

    _USER_FIELDS = "id name email displayName active admin createdAt"

    def get_viewer(self, *, fields: Optional[str] = None) -> Any:
        """`viewer` — the user who owns the API key."""
        query = f"""
            query Viewer {{ viewer {{ {fields or self._USER_FIELDS} }} }}
        """
        return self._execute(query)["viewer"]

    def get_user(self, user_id: str, *, fields: Optional[str] = None) -> Any:
        """`user(id: ...)`."""
        query = f"""
            query User($id: String!) {{
              user(id: $id) {{ {fields or self._USER_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": user_id})["user"]

    def list_users(self, *, first: int = 50, after: Optional[str] = None,
                    fields: Optional[str] = None) -> Any:
        """`users` — every member of the workspace visible to this key."""
        query = f"""
            query Users($first: Int, $after: String) {{
              users(first: $first, after: $after) {{
                nodes {{ {fields or self._USER_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {"first": first, "after": after})["users"]

    # ================================================================
    # Webhooks
    # ================================================================

    _WEBHOOK_FIELDS = "id url enabled resourceTypes team { id name } createdAt"

    def list_webhooks(self, *, team_id: Optional[str] = None,
                       first: int = 50, after: Optional[str] = None,
                       fields: Optional[str] = None) -> Any:
        """`webhooks(filter:)` — optionally scoped to one team."""
        filter_clause = 'filter: { team: { id: { eq: $teamId } } }' if team_id else ""
        query = f"""
            query Webhooks($teamId: ID, $first: Int, $after: String) {{
              webhooks({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._WEBHOOK_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {
            "teamId": team_id, "first": first, "after": after,
        })["webhooks"]

    def create_webhook(self, url: str, *, team_id: Optional[str] = None,
                        resource_types: Optional[List[str]] = None,
                        secret: Optional[str] = None,
                        enabled: bool = True,
                        all_public_teams: bool = False,
                        fields: Optional[str] = None) -> Any:
        """`webhookCreate(input:)`. `resource_types`: Linear's documented
        values include `"Issue"`, `"Comment"`, `"IssueLabel"`, `"Project"`,
        `"Cycle"`, `"ProjectUpdate"`, `"Reaction"` — passed through raw and
        validated server-side, not re-validated here (the full enum wasn't
        confirmed via introspection). `all_public_teams=True` subscribes
        across every public team instead of one `team_id`."""
        query = f"""
            mutation WebhookCreate($input: WebhookCreateInput!) {{
              webhookCreate(input: $input) {{
                success
                webhook {{ {fields or self._WEBHOOK_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({
            "url": url, "teamId": team_id, "resourceTypes": resource_types,
            "secret": secret, "enabled": enabled,
            "allPublicTeams": all_public_teams or None,
        })
        return self._execute(query, {"input": input_})["webhookCreate"]

    def update_webhook(self, webhook_id: str, *,
                        url: Optional[str] = None,
                        resource_types: Optional[List[str]] = None,
                        enabled: Optional[bool] = None,
                        fields: Optional[str] = None) -> Any:
        """`webhookUpdate(id:, input:)`."""
        query = f"""
            mutation WebhookUpdate($id: String!, $input: WebhookUpdateInput!) {{
              webhookUpdate(id: $id, input: $input) {{
                success
                webhook {{ {fields or self._WEBHOOK_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({"url": url, "resourceTypes": resource_types, "enabled": enabled})
        return self._execute(query, {"id": webhook_id, "input": input_})["webhookUpdate"]

    def delete_webhook(self, webhook_id: str) -> Any:
        """`webhookDelete(id:)`."""
        query = """
            mutation WebhookDelete($id: String!) {
              webhookDelete(id: $id) { success }
            }
        """
        return self._execute(query, {"id": webhook_id})["webhookDelete"]
