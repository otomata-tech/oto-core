"""
Slack API Client.

Requires: requests
"""

import hmac
import hashlib
from typing import Optional, Dict, Any, List

import requests

from ...config import require_secret, get_secret


# Slack répond HTTP 200 avec `{"ok": false, "error": "<code>"}` pour les rejets
# logiques (raise_for_status ne les voit pas). On les traduit en erreur amont
# TYPÉE portant `.status` (même contrat que les autres connecteurs oto-core, ex.
# NinjaError) : un code de rejet client (channel introuvable, droits, scope…) est
# un 4xx amont — pas un bug backend — donc trié comme tel en aval (calllog,
# Sentry). Les vrais incidents Slack (internal_error…) restent en 5xx → reportés.
_SLACK_ERROR_STATUS = {
    # 401 — authentification
    "not_authed": 401, "invalid_auth": 401, "account_inactive": 401,
    "token_revoked": 401, "token_expired": 401,
    # 403 — autorisation / périmètre
    "missing_scope": 403, "not_allowed_token_type": 403, "ekm_access_denied": 403,
    "not_in_channel": 403, "is_archived": 403, "restricted_action": 403,
    "cant_post_message": 403, "no_permission": 403,
    # 404 — cible absente
    "channel_not_found": 404, "user_not_found": 404, "users_not_found": 404,
    "message_not_found": 404, "thread_not_found": 404, "file_not_found": 404,
    # 429 — quota
    "ratelimited": 429, "rate_limited": 429,
    # 5xx — incident côté Slack (à reporter, vrai bug amont)
    "internal_error": 502, "fatal_error": 502, "service_unavailable": 503,
    "request_timeout": 504,
}


class SlackError(RuntimeError):
    """Erreur API Slack (`ok:false`). `status` = équivalent HTTP du code Slack
    (défaut 400 = rejet client), `error` = le code Slack brut."""

    def __init__(self, error: Optional[str], status: Optional[int] = None):
        self.error = error or "unknown"
        self.status = status if status is not None else _SLACK_ERROR_STATUS.get(self.error, 400)
        super().__init__(f"Slack API error: {self.error}")


def verify_slack_signature(
    signing_secret: str,
    body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    """
    Verify Slack webhook signature.

    Args:
        signing_secret: Slack signing secret
        body: Request body bytes
        timestamp: X-Slack-Request-Timestamp header
        signature: X-Slack-Signature header

    Returns:
        True if valid
    """
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    my_signature = "v0=" + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_signature, signature)


DEFAULT_WORKSPACE = "otomata"


def _resolve_workspace_token(workspace: str, kind: str) -> Optional[str]:
    """Resolve a Slack token for a workspace + kind ('bot' or 'user').

    Naming convention: `SLACK_<WORKSPACE>_BOT_TOKEN` / `SLACK_<WORKSPACE>_USER_TOKEN`.
    For the default workspace, also accepts the legacy flat `SLACK_BOT_TOKEN` /
    `SLACK_USER_TOKEN` keys as fallback.
    """
    ws = workspace.upper()
    key = f"SLACK_{ws}_{kind.upper()}_TOKEN"
    tok = get_secret(key)
    if tok:
        return tok
    if workspace == DEFAULT_WORKSPACE:
        return get_secret(f"SLACK_{kind.upper()}_TOKEN")
    return None


class SlackClient:
    """
    Slack API client. Multi-workspace.

    Token resolution: pass `workspace="<slug>"` (default: "otomata"). The client
    reads `SLACK_<SLUG>_BOT_TOKEN` and `SLACK_<SLUG>_USER_TOKEN` from secrets.
    For the default workspace, legacy `SLACK_BOT_TOKEN` / `SLACK_USER_TOKEN`
    keys are accepted as fallback.

    Two tokens are supported per workspace:
    - **bot token** (`xoxb-`) — messages appear as the bot app. Use for
      automated/agentic actions (notifications, reactions, scheduled posts).
    - **user token** (`xoxp-`) — messages appear as the human user who installed
      the app. Use for outbound human-style com sent on behalf of that user.

    Reads route by Slack surface (see `_prefer`): channel reads (list channels,
    channel history) go through the **bot** token — the bot is invited and this
    keeps the user token's scopes minimal — while DM reads (own DMs, `open_dm`,
    `search`) go through the **user** token, since only the user sees their own
    conversations. When a single token is configured, reads fall through to it.
    Writes (`post_message`, `update_message`, `add_reaction`) accept
    `as_user=True/False`; omitted → `default_as_user` (construction, default
    False = bot-style). Every read also accepts an explicit `as_user` override.
    """

    BASE_URL = "https://slack.com/api"

    def __init__(
        self,
        bot_token: Optional[str] = None,
        user_token: Optional[str] = None,
        default_as_user: bool = False,
        workspace: Optional[str] = None,
    ):
        """
        Initialize Slack client.

        Args:
            bot_token: Explicit bot token (overrides workspace lookup).
            user_token: Explicit user token (overrides workspace lookup).
            default_as_user: Default mode when a method's `as_user` arg is None.
            workspace: Workspace slug (default: "otomata"). Selects which
                SLACK_<SLUG>_*_TOKEN secrets are read.
        """
        self.workspace = workspace or DEFAULT_WORKSPACE
        self.bot_token = bot_token or _resolve_workspace_token(self.workspace, "bot")
        self.user_token = user_token or _resolve_workspace_token(self.workspace, "user")
        self.default_as_user = default_as_user
        if not self.bot_token and not self.user_token:
            raise ValueError(
                f"No Slack token for workspace '{self.workspace}'. "
                f"Set SLACK_{self.workspace.upper()}_BOT_TOKEN or "
                f"SLACK_{self.workspace.upper()}_USER_TOKEN in secrets."
            )

    def _resolve_token(self, as_user: Optional[bool]) -> str:
        mode = self.default_as_user if as_user is None else as_user
        if mode:
            if not self.user_token:
                raise ValueError("as_user=True requires SLACK_USER_TOKEN")
            return self.user_token
        if not self.bot_token:
            raise ValueError("as_user=False requires SLACK_BOT_TOKEN")
        return self.bot_token

    def _prefer(self, want_user: bool) -> bool:
        """Capability routing for reads → the `as_user` flag of the token that
        fits the operation, falling through to the other when only one token is
        configured. Channel reads fit the **bot** (invited, keeps the user
        token's scopes minimal); DM/search reads fit the **user** (only they see
        their own conversations, and `search:read` is a user-token-only scope).
        Not a legacy fallback: routes to the only usable token, and Slack still
        returns a typed error if that token genuinely can't do the operation."""
        if want_user:
            return bool(self.user_token)   # user token if present, else bot
        return not self.bot_token          # bot token if present, else user

    def _request(
        self,
        method: str,
        endpoint: str,
        as_user: Optional[bool] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Make API request. Picks token based on as_user (None = default)."""
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {"Authorization": f"Bearer {self._resolve_token(as_user)}"}

        response = requests.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()

        data = response.json()
        if not data.get("ok"):
            raise SlackError(data.get("error"))

        return data

    def post_message(
        self,
        channel: str,
        text: str = None,
        blocks: List[Dict] = None,
        thread_ts: str = None,
        as_user: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Send a message to a channel or DM.

        Args:
            channel: Channel ID or name
            text: Message text (fallback for blocks)
            blocks: Block Kit blocks
            thread_ts: Thread timestamp for reply
            as_user: True = post via user token (appears as the human user).
                False = bot token (appears as the bot app). None = client default.

        Returns:
            Message data with ts
        """
        data = {"channel": channel}
        if text:
            data["text"] = text
        if blocks:
            data["blocks"] = blocks
        if thread_ts:
            data["thread_ts"] = thread_ts

        return self._request("POST", "chat.postMessage", as_user=as_user, json=data)

    def delete_message(
        self,
        channel: str,
        ts: str,
        as_user: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Delete a message. Must be deleted with the same token that posted it
        (user-posted → user token, bot-posted → bot token).

        Args:
            channel: Channel ID
            ts: Message timestamp
            as_user: Same conventions as post_message.

        Returns:
            Response data
        """
        return self._request(
            "POST", "chat.delete", as_user=as_user, json={"channel": channel, "ts": ts}
        )

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str = None,
        blocks: List[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Update an existing message.

        Args:
            channel: Channel ID
            ts: Message timestamp
            text: New text
            blocks: New blocks

        Returns:
            Updated message data
        """
        data = {"channel": channel, "ts": ts}
        if text:
            data["text"] = text
        if blocks:
            data["blocks"] = blocks

        return self._request("POST", "chat.update", json=data)

    def post_ephemeral(
        self,
        channel: str,
        user: str,
        text: str = None,
        blocks: List[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Send an ephemeral message (visible only to one user).

        Args:
            channel: Channel ID
            user: User ID
            text: Message text
            blocks: Block Kit blocks

        Returns:
            Message data
        """
        data = {"channel": channel, "user": user}
        if text:
            data["text"] = text
        if blocks:
            data["blocks"] = blocks

        return self._request("POST", "chat.postEphemeral", json=data)

    def get_user_info(self, user_id: str, as_user: Optional[bool] = None) -> Dict[str, Any]:
        """
        Get user information.

        Args:
            user_id: User ID
            as_user: Token override; None → bot (either token works).

        Returns:
            User data
        """
        if as_user is None:
            as_user = self._prefer(want_user=False)
        return self._request("GET", "users.info", as_user=as_user, params={"user": user_id})

    def list_channels(
        self, types: str = "public_channel", as_user: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """
        List channels.

        Args:
            types: Channel types (public_channel, private_channel, mpim, im)
            as_user: Token override; None → route by `types` — DM-only listings
                (im/mpim) via the user token, channels via the bot token.

        Returns:
            List of channels
        """
        if as_user is None:
            wanted = {t.strip() for t in types.split(",") if t.strip()}
            as_user = self._prefer(want_user=bool(wanted) and wanted <= {"im", "mpim"})
        data = self._request("GET", "conversations.list", as_user=as_user, params={"types": types})
        return data.get("channels", [])

    def add_reaction(self, channel: str, ts: str, name: str) -> Dict[str, Any]:
        """
        Add a reaction to a message.

        Args:
            channel: Channel ID
            ts: Message timestamp
            name: Emoji name (without colons)

        Returns:
            Response data
        """
        return self._request("POST", "reactions.add", json={
            "channel": channel,
            "timestamp": ts,
            "name": name,
        })

    def history(
        self,
        channel: str,
        limit: int = 50,
        cursor: Optional[str] = None,
        oldest: Optional[str] = None,
        latest: Optional[str] = None,
        as_user: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Read recent messages from a channel (or DM/group).

        Args:
            channel: Channel ID
            limit: Max messages (capped at 100 by Slack)
            cursor: Pagination cursor from a previous call
            oldest: Only messages after this ts
            latest: Only messages before this ts
            as_user: Token override; None → route by channel id — DMs (`D…`) via
                the user token, channels (`C…`/`G…`) via the bot token.

        Returns:
            Response data with "messages" array + "response_metadata.next_cursor"
        """
        if as_user is None:
            as_user = self._prefer(want_user=channel.startswith("D"))
        params: Dict[str, Any] = {"channel": channel, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if oldest:
            params["oldest"] = oldest
        if latest:
            params["latest"] = latest
        return self._request("GET", "conversations.history", as_user=as_user, params=params)

    def open_dm(self, user: str, as_user: Optional[bool] = None) -> Dict[str, Any]:
        """
        Open (or return) a direct-message channel with a user.

        Args:
            user: User ID
            as_user: Token override; None → user token (opens *your* DM with the
                person so you can read it), falling through to bot if no user token.

        Returns:
            Response data with "channel.id" usable as channel for post_message
        """
        if as_user is None:
            as_user = self._prefer(want_user=True)
        return self._request("POST", "conversations.open", as_user=as_user, json={"users": user})

    def find_user_by_email(self, email: str, as_user: Optional[bool] = None) -> Dict[str, Any]:
        """
        Look up a user by email.

        Args:
            email: Email address
            as_user: Token override; None → bot (either token works).

        Returns:
            User data
        """
        if as_user is None:
            as_user = self._prefer(want_user=False)
        return self._request("GET", "users.lookupByEmail", as_user=as_user, params={"email": email})

    def search_messages(self, query: str, count: int = 20) -> Dict[str, Any]:
        """
        Search messages across accessible channels. Requires `search:read` scope
        (only available on user tokens, not bot tokens) → always via the user token.

        Args:
            query: Slack search query (supports `in:#channel`, `from:@user`, etc.)
            count: Max results

        Returns:
            Response data with "messages.matches"
        """
        return self._request("GET", "search.messages", as_user=True, params={"query": query, "count": count})

    def file_info(self, file_id: str) -> Dict[str, Any]:
        """Get file metadata (requires files:read scope)."""
        return self._request("GET", "files.info", params={"file": file_id})

    def download_file(self, file_id: str, dest: str) -> str:
        """Download a Slack file to a local path.

        Uses the user token (files in private channels / DMs need user-level access).
        Returns the destination path.
        """
        info = self.file_info(file_id)
        url = info["file"]["url_private_download"]
        token = self.user_token or self.bot_token
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest
