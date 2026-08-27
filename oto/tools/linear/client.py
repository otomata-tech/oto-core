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

**Live-tested 2026-08-21** against a real workspace (`api.linear.app`, real
key) — GraphQL introspection first (all 31 query/mutation field names + the
9 mutation input-object shapes checked against the schema), then a full
read + create/get/update/delete/archive lifecycle exercised on issues,
comments, projects, labels, and webhooks, with every created object cleaned
up (deleted, or archived for the one issue — archive is the reversible op
by design). **6 real bugs found and fixed**, none of them catchable by
introspection alone — Linear's schema and its live behavior disagree in
several places:
- `Project` has no `state` field to read (`Cannot query field "state" on
  type "Project"` — the real field is `status`, an object with its own
  `id`/`name`/`type`) — and `ProjectCreateInput`/`ProjectUpdateInput` have
  no `state` input either, only `statusId` (an id into the workspace's
  configured project statuses). `create_project`/`update_project` take
  `status_id`, not a free-text state.
- **A GraphQL operation must not declare a variable it never references.**
  Every `list_*`/`search_issues` method here builds an optional filter from
  several independent kwargs; declaring all of them in the query signature
  unconditionally (an earlier draft did) fails with `GRAPHQL_VALIDATION_
  FAILED: Variable "$x" is never used` the instant any ONE filter is
  omitted — which, with 2+ independent filters, is nearly every call.
  Variable declarations are now built in lockstep with the filter clause.
- `Query.webhooks` has **no `filter` or `teamId` argument at all**
  (`Unknown argument "filter" on field "Query.webhooks". Did you mean
  "after"?`) — team-scoped webhooks only exist via the nested
  `Team.webhooks` field, which `list_webhooks(team_id=...)` now uses
  instead, faking the usual `{nodes, pageInfo}` shape (that nested field
  has no pagination args of its own).
- `issueSearch` **exists in the schema** (passes introspection) but is DEAD
  at call time — `INPUT_ERROR: "This endpoint deprecated."` on every call,
  any arguments. `search_issues` now uses the live replacement,
  `issues(filter: { searchableContent: { contains: ... } })`
  (`ContentComparator`, confirmed via introspection).
- `Project.accessibleTeams` (used to scope `list_projects` by team) is a
  `TeamCollectionFilter`, not a plain `TeamFilter` — it needs the `some:`
  wrapper (`accessibleTeams: { some: { id: { eq: ... } } }`); the bare
  `{ id: { eq: ... } }` form is syntactically valid (the collection filter
  type has its own top-level `id` field too) but semantically unclear.
  Confirmed correct with a real project: found via `some:`, both filtered
  and unfiltered.
- `get_issue("OPE-9")` (Linear's human-readable identifier) works exactly
  like `get_issue("<uuid>")` — same field, both forms resolve.

Everything above reflects the CURRENT client, post-fix — no residual
workarounds needed by a caller.

**Webhooks are a real GraphQL surface here** (`webhookCreate`/`webhookUpdate`
/`webhookDelete`/`webhooks`/`Team.webhooks`), unlike Fireflies where webhook
management is dashboard-only.
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
        """`issue(id: ...)` — one issue. `id` accepts either the UUID or
        Linear's human-readable identifier (`"ENG-123"`), live-confirmed
        2026-08-21 (`get_issue("OPE-9")` resolved identically to the UUID)."""
        query = f"""
            query Issue($id: String!) {{
              issue(id: $id) {{ {fields or self._ISSUE_FIELDS} }}
            }}
        """
        return self._execute(query, {"id": issue_id})["issue"]

    # `PaginationOrderBy` (SDL @linear/sdk 92.0.0) : deux valeurs, pas plus.
    ORDER_BY = ("createdAt", "updatedAt")

    def list_issues(self, *, team_id: Optional[str] = None,
                     project_id: Optional[str] = None,
                     cycle_id: Optional[str] = None,
                     assignee_id: Optional[str] = None,
                     state_id: Optional[str] = None,
                     updated_after: Optional[str] = None,
                     updated_before: Optional[str] = None,
                     created_after: Optional[str] = None,
                     created_before: Optional[str] = None,
                     order_by: Optional[str] = None,
                     first: int = 50, after: Optional[str] = None,
                     fields: Optional[str] = None) -> Any:
        """`issues(filter:, orderBy:, first:, after:)` — list/filter issues.
        Returns the raw `{nodes, pageInfo{hasNextPage,endCursor}}` shape; pass
        the previous call's `endCursor` as `after` to page further.

        **Fenêtre de date, côté SERVEUR** (signal #561 : sans elle, tout run qui
        lit un jour donné devait rapatrier puis jeter). `updated_after` /
        `updated_before` / `created_after` / `created_before` deviennent des
        bornes `gte`/`lte` du `DateComparator` que `IssueFilter.updatedAt` et
        `IssueFilter.createdAt` portent déjà dans le schéma. Horodatages ISO
        8601 UTC (scalaire `DateTimeOrDuration`). Les deux bornes d'un même
        champ tiennent dans UN seul comparateur — deux clauses `updatedAt:`
        séparées seraient un objet d'entrée invalide.

        **Ordre** (signal #568 : « le tri n'est documenté nulle part »). Linear
        ordonne ses connexions par `createdAt` par défaut, décroissant — relevé
        le 24/08/2026 contre un workspace réel : la liste revient par identifiant
        décroissant, et une issue créée le 26 juillet mais modifiée le 21 août
        se trouve loin dans la pagination. Une lecture de deltas passe donc par
        `order_by="updatedAt"`, et mieux encore par les bornes ci-dessus.

        ⚠️ Live-confirmed 2026-08-21: a GraphQL operation must not DECLARE a
        variable it never references in the selection — declaring all 5
        filter variables unconditionally (as an earlier draft did) fails
        with `GRAPHQL_VALIDATION_FAILED: Variable "$x" is never used` the
        moment any ONE filter is omitted. Variable declarations are built
        alongside the filter clause below, in lockstep — les bornes de date et
        `orderBy` suivent la même discipline."""
        if order_by is not None and order_by not in self.ORDER_BY:
            raise ValueError(
                f"order_by doit valoir {' ou '.join(self.ORDER_BY)} "
                f"(enum PaginationOrderBy de Linear) ; reçu {order_by!r}")
        filters = [("teamId", "ID", "team", team_id),
                   ("projectId", "ID", "project", project_id),
                   ("cycleId", "ID", "cycle", cycle_id),
                   ("assigneeId", "ID", "assignee", assignee_id),
                   ("stateId", "ID", "state", state_id)]
        var_decls = ["$first: Int", "$after: String"]
        filter_parts = []
        variables: Dict[str, Any] = {"first": first, "after": after}
        for var_name, gql_type, filter_key, value in filters:
            if value is None:
                continue
            var_decls.append(f"${var_name}: {gql_type}")
            filter_parts.append(f"{filter_key}: {{ id: {{ eq: ${var_name} }} }}")
            variables[var_name] = value
        # Une seule clause par champ de date : `gte` et `lte` sont deux clés du
        # MÊME DateComparator, pas deux filtres.
        for filter_key, bounds in (
            ("updatedAt", (("gte", "updatedAfter", updated_after),
                           ("lte", "updatedBefore", updated_before))),
            ("createdAt", (("gte", "createdAfter", created_after),
                           ("lte", "createdBefore", created_before))),
        ):
            comparators = []
            for cmp_key, var_name, value in bounds:
                if value is None:
                    continue
                var_decls.append(f"${var_name}: DateTimeOrDuration")
                comparators.append(f"{cmp_key}: ${var_name}")
                variables[var_name] = value
            if comparators:
                filter_parts.append(f"{filter_key}: {{ {', '.join(comparators)} }}")
        filter_clause = f"filter: {{ {', '.join(filter_parts)} }}" if filter_parts else ""
        order_clause = ""
        if order_by is not None:
            var_decls.append("$orderBy: PaginationOrderBy")
            variables["orderBy"] = order_by
            order_clause = "orderBy: $orderBy, "
        query = f"""
            query Issues({', '.join(var_decls)}) {{
              issues({filter_clause} {order_clause}first: $first, after: $after) {{
                nodes {{ {fields or self._ISSUE_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, variables)["issues"]

    def search_issues(self, query_text: str, *, team_id: Optional[str] = None,
                       first: int = 50, after: Optional[str] = None,
                       fields: Optional[str] = None) -> Any:
        """Full-text search across title + description, via
        `issues(filter: { searchableContent: { contains: ... } })`.

        ⚠️ Live-confirmed 2026-08-21: the documented `issueSearch` query
        still EXISTS in the schema (passes introspection) but is dead at
        call time — `INPUT_ERROR: "This endpoint deprecated."` on every
        call, regardless of arguments. `IssueFilter.searchableContent`
        (a `ContentComparator` — `contains`/`notContains`, confirmed via
        introspection) on the plain `issues` query is the real, live
        replacement; this method uses it instead."""
        var_decls = ["$text: String!", "$first: Int", "$after: String"]
        filter_parts = ["searchableContent: { contains: $text }"]
        variables: Dict[str, Any] = {"text": query_text, "first": first, "after": after}
        if team_id is not None:
            var_decls.append("$teamId: ID")
            filter_parts.append("team: { id: { eq: $teamId } }")
            variables["teamId"] = team_id
        query = f"""
            query IssueSearch({', '.join(var_decls)}) {{
              issues(filter: {{ {', '.join(filter_parts)} }}, first: $first, after: $after) {{
                nodes {{ {fields or self._ISSUE_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, variables)["issues"]

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
        status { id name type }
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
        """`projects(filter:)` — optionally scoped to one team via
        `accessibleTeams.some.id`. `accessibleTeams` is a
        `TeamCollectionFilter`, which — unlike the plain `TeamFilter` used
        by `team:` filters elsewhere in this file — wraps a per-item filter
        under `some:`/`every:`. Live-confirmed 2026-08-21 against a real
        project: `some:` correctly finds a project scoped to the given team
        (both with and without the filter). The collection filter's own
        bare top-level `id` field was not tested — this method doesn't use it."""
        var_decls = ["$first: Int", "$after: String"]
        filter_clause = ""
        variables: Dict[str, Any] = {"first": first, "after": after}
        if team_id is not None:
            var_decls.append("$teamId: ID")
            filter_clause = "filter: { accessibleTeams: { some: { id: { eq: $teamId } } } }"
            variables["teamId"] = team_id
        query = f"""
            query Projects({', '.join(var_decls)}) {{
              projects({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._PROJECT_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, variables)["projects"]

    def create_project(self, name: str, team_ids: List[str], *,
                        description: Optional[str] = None,
                        status_id: Optional[str] = None,
                        lead_id: Optional[str] = None,
                        target_date: Optional[str] = None,
                        fields: Optional[str] = None) -> Any:
        """`projectCreate(input:)`. `team_ids` is required — a project
        belongs to at least one team. `status_id` (NOT a free-text state —
        live-confirmed 2026-08-21 via introspection: `ProjectCreateInput`
        has no `state` field at all, only `statusId`, an id into the
        workspace's configured `ProjectStatus` list) references one of the
        team's/workspace's project statuses — resolve one by reading an
        existing project's `status.id` via `get_project`/`list_projects`."""
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
            "statusId": status_id, "leadId": lead_id, "targetDate": target_date,
        })
        return self._execute(query, {"input": input_})["projectCreate"]

    def update_project(self, project_id: str, *,
                        name: Optional[str] = None,
                        description: Optional[str] = None,
                        status_id: Optional[str] = None,
                        lead_id: Optional[str] = None,
                        target_date: Optional[str] = None,
                        fields: Optional[str] = None) -> Any:
        """`projectUpdate(id:, input:)`. `status_id`, not a free-text state
        — see `create_project`'s docstring."""
        query = f"""
            mutation ProjectUpdate($id: String!, $input: ProjectUpdateInput!) {{
              projectUpdate(id: $id, input: $input) {{
                success
                project {{ {fields or self._PROJECT_FIELDS} }}
              }}
            }}
        """
        input_ = _clean({
            "name": name, "description": description, "statusId": status_id,
            "leadId": lead_id, "targetDate": target_date,
        })
        return self._execute(query, {"id": project_id, "input": input_})["projectUpdate"]

    def delete_project(self, project_id: str) -> Any:
        """`projectDelete(id:)`."""
        query = """
            mutation ProjectDelete($id: String!) {
              projectDelete(id: $id) { success }
            }
        """
        return self._execute(query, {"id": project_id})["projectDelete"]

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
        var_decls = ["$first: Int", "$after: String"]
        filter_clause = ""
        variables: Dict[str, Any] = {"first": first, "after": after}
        if team_id is not None:
            var_decls.append("$teamId: ID")
            filter_clause = "filter: { team: { id: { eq: $teamId } } }"
            variables["teamId"] = team_id
        query = f"""
            query Cycles({', '.join(var_decls)}) {{
              cycles({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._CYCLE_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, variables)["cycles"]

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
        var_decls = ["$first: Int", "$after: String"]
        filter_clause = ""
        variables: Dict[str, Any] = {"first": first, "after": after}
        if team_id is not None:
            var_decls.append("$teamId: ID")
            filter_clause = "filter: { team: { id: { eq: $teamId } } }"
            variables["teamId"] = team_id
        query = f"""
            query IssueLabels({', '.join(var_decls)}) {{
              issueLabels({filter_clause} first: $first, after: $after) {{
                nodes {{ {fields or self._LABEL_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, variables)["issueLabels"]

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

    def delete_label(self, label_id: str) -> Any:
        """`issueLabelDelete(id:)`."""
        query = """
            mutation IssueLabelDelete($id: String!) {
              issueLabelDelete(id: $id) { success }
            }
        """
        return self._execute(query, {"id": label_id})["issueLabelDelete"]

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
        """`webhooks(first:, after:)` (workspace-wide) or, when `team_id`
        is given, `team(id:){webhooks{...}}` (team-scoped).

        ⚠️ Live-confirmed 2026-08-21: `Query.webhooks` has NO `filter` (nor
        a `teamId`) argument at all — `GRAPHQL_VALIDATION_FAILED: Unknown
        argument "filter" on field "Query.webhooks"` on the first attempt.
        Team-scoping only exists via the nested `Team.webhooks` field,
        which has no pagination args of its own (no `pageInfo` either) —
        this method fakes the same `{nodes, pageInfo}` shape as every other
        list method here so callers don't need a special case, but a large
        team's webhook list is NOT actually paginated by Linear in this path."""
        if team_id is not None:
            query = """
                query TeamWebhooks($teamId: String!) {
                  team(id: $teamId) { webhooks { nodes { %s } } }
                }
            """ % (fields or self._WEBHOOK_FIELDS)
            nodes = self._execute(query, {"teamId": team_id})["team"]["webhooks"]["nodes"]
            return {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
        query = f"""
            query Webhooks($first: Int, $after: String) {{
              webhooks(first: $first, after: $after) {{
                nodes {{ {fields or self._WEBHOOK_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
        """
        return self._execute(query, {"first": first, "after": after})["webhooks"]

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
