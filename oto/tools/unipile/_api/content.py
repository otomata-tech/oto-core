"""Posts, engagement, feed d'accueil et activité d'un membre.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from ..const import FEED_QUERY_ID
from ..feed import _unpack_cursor, parse_feed


class _ContentMixin:
    """Posts, engagement, feed d'accueil et activité d'un membre."""

    def _member_id(self, identifier: str) -> str:
        """Résout un identifiant de membre vers le **provider_id (URN, `ACoAA…`)**
        attendu par les endpoints posts/comments/reactions v2 : le slug public y
        renvoie 400 « Invalid User ID » (delta v2 relevé en live 2026-07-06). URN
        déjà opaque → tel quel ; slug → résolu via le profil (1 appel)."""
        ident = str(identifier).strip()
        if ident.startswith(("ACoA", "urn:")):
            return ident
        prof = self.get_profile(ident)
        return str((prof or {}).get("provider_id") or (prof or {}).get("id") or ident)

    def list_member_posts(self, identifier: str, cursor: Optional[str] = None,
                          limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(self._member_id(identifier), safe='')}/posts"),
            params=params,
        ))

    def get_post(self, post_id: str) -> dict:
        return self._request(
            "GET", self._acct(f"/posts/{quote(post_id, safe='')}")
        )

    def list_comments(self, post_id: str, cursor: Optional[str] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return self._norm(self._request(
            "GET", self._acct(f"/posts/{quote(post_id, safe='')}/comments"),
            params=params,
        ))

    def list_reactions(self, post_id: str, cursor: Optional[str] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return self._norm(self._request(
            "GET", self._acct(f"/posts/{quote(post_id, safe='')}/reactions"),
            params=params,
        ))

    def create_post(self, text: str) -> dict:
        return self._request("POST", self._acct("/posts"), json={"text": text})

    def comment_post(self, post_id: str, text: str) -> dict:
        return self._request(
            "POST", self._acct(f"/posts/{quote(post_id, safe='')}/comments"),
            json={"text": text},
        )

    def react_post(self, post_id: str, value: str = "LIKE") -> dict:
        """Réagit à un post. v2 : corps `{reaction}`."""
        return self._request(
            "POST", self._acct(f"/posts/{quote(post_id, safe='')}/reactions"),
            json={"reaction": value},
        )

    # ---- feed (Voyager passthrough via proxyRequest v2) -----------------

    def linkedin_raw(
        self,
        request_url: str,
        method: str = "GET",
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        encoding: bool = False,
        force_api: bool = False,
    ) -> dict:
        """Relaie une requête Voyager brute — v2 : `POST /v2/{account}/linkedin/`
        (proxyRequest), corps `{url, method, bypass_url_encoding, …}`."""
        payload: dict[str, Any] = {
            "url": request_url,
            "method": method,
            "bypass_url_encoding": not encoding,
        }
        if body is not None:
            payload["body"] = body
        if headers:
            payload["headers"] = headers
        return self._request("POST", self._acct("/linkedin/"), json=payload)

    def get_feed(
        self,
        count: int = 20,
        cursor: Optional[str] = None,
        raw: bool = False,
        sort_order: str = "MEMBER_SETTING",
    ) -> dict:
        """Feed d'accueil LinkedIn via la Magic Route Voyager."""
        start, token = _unpack_cursor(cursor)
        if token:
            variables = (
                f"(start:{start},count:{count},"
                f"paginationToken:{token},sortOrder:{sort_order})"
            )
            request_url = (
                "https://www.linkedin.com/voyager/api/graphql"
                f"?variables={variables}&queryId={FEED_QUERY_ID}"
            )
        else:
            request_url = (
                "https://www.linkedin.com/voyager/api/graphql"
                f"?queryId={FEED_QUERY_ID}"
            )
        resp = self.linkedin_raw(request_url, method="GET", encoding=False)
        if raw:
            return resp
        return parse_feed(resp, count=count, start=start)

    # ---- moi / followers / activité d'un membre -------------------------

    def get_own_profile(self) -> dict:
        """Profil du compte connecté. v2 : `GET /users/me` (pas de garde #153 :
        l'id rendu ≠ le littéral « me »)."""
        return self._request("GET", self._acct("/users/me"))

    def list_followers(self, user_id: Optional[str] = None,
                      cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        uid = user_id or "me"
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(uid, safe='')}/followers"),
            params=params,
        ))

    def list_following(self, user_id: Optional[str] = None,
                      cursor: Optional[str] = None,
                      limit: Optional[int] = None) -> dict:
        uid = user_id or "me"
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(uid, safe='')}/following"),
            params=params,
        ))

    def list_member_comments(self, identifier: str,
                            cursor: Optional[str] = None,
                            limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(self._member_id(identifier), safe='')}/comments"),
            params=params,
        ))

    def list_member_reactions(self, identifier: str,
                             cursor: Optional[str] = None,
                             limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct(f"/users/{quote(self._member_id(identifier), safe='')}/reactions"),
            params=params,
        ))

