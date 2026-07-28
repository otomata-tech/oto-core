"""
Reddit read-only client — **redditapis.com** (REST proxy, bearer token).

Reddit closed self-service OAuth app registration (Responsible Builder Policy,
late 2025) and blocks the anonymous ``*.json`` endpoints (HTTP 403 on datacenter
IPs), so neither the official Data API nor direct scraping is available to us. This
client reads through **redditapis.com**, a hosted REST proxy that returns clean
JSON — score, comment count, upvote ratio, real publication date, working ``after``
pagination, and the native nested comment tree — behind a single ``Authorization:
Bearer <key>`` header.

Contract (base ``https://api.redditapis.com``, all reads GET) :
- ``/api/reddit/posts?subreddit=&sort=&t=&after=`` — subreddit listing, any sort
- ``/api/reddit/search?q=&subreddit=&sort=&t=&after=`` — post search
- ``/api/reddit/search/communities?q=&after=`` — subreddit discovery (with subscribers)
- ``/api/reddit/comments/:id?limit=`` — a post + its nested comment tree
"""

from typing import Any, Dict, List, Optional

import requests

_REDDIT_WEB = "https://reddit.com"


class RedditClient:
    """Reddit read-only via the redditapis.com REST proxy (bearer token).

    Covers subreddit listings, post search, subreddit discovery, and a post's
    nested comment tree — all with engagement metrics (``score``,
    ``num_comments``, ``upvote_ratio``) and real timestamps.
    """

    BASE_URL = "https://api.redditapis.com"

    def __init__(self, api_key: str, timeout: int = 20):
        if not api_key:
            raise ValueError("reddit: clé API redditapis.com requise")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.session.headers["Accept"] = "application/json"
        self.timeout = timeout

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = self.session.get(f"{self.BASE_URL}{path}", params=params, timeout=self.timeout)
        # Le proxy renvoie parfois 200 + {"error": ...} sur une requête invalide.
        try:
            data = r.json()
        except ValueError:
            r.raise_for_status()
            raise RuntimeError(f"réponse redditapis non-JSON (HTTP {r.status_code})")
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"redditapis: {data['error']}")
        r.raise_for_status()
        return data

    # ── Listings ──────────────────────────────────────────────────────────

    def subreddit(
        self,
        name: str,
        sort: str = "hot",
        limit: int = 25,
        time: Optional[str] = None,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List posts from a subreddit. sort: hot|new|top|rising|controversial."""
        if sort not in {"hot", "new", "top", "rising", "controversial"}:
            raise ValueError(f"invalid sort: {sort}")
        params: Dict[str, Any] = {"subreddit": name, "sort": sort, "limit": min(limit, 100)}
        if time and sort in {"top", "controversial"}:
            params["t"] = time
        if after:
            params["after"] = after
        return _feed(self._get("/api/reddit/posts", params))

    def search(
        self,
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        time: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search posts. If `subreddit` is set, restricts to that sub."""
        params: Dict[str, Any] = {
            "q": query, "sort": sort, "t": time, "limit": min(limit, 100),
        }
        if subreddit:
            params["subreddit"] = subreddit
        if after:
            params["after"] = after
        return _feed(self._get("/api/reddit/search", params))

    def search_subreddits(self, query: str, limit: int = 25) -> Dict[str, Any]:
        """Discover subreddits by name/description match (with subscriber counts)."""
        data = self._get("/api/reddit/search/communities", {"q": query, "limit": min(limit, 100)})
        return {
            "items": [_community(c) for c in data.get("communities", [])],
            "after": data.get("after"),
            "source": "redditapis",
        }

    # ── Post + comments ───────────────────────────────────────────────────

    def post(self, url_or_id: str, comment_limit: int = 100, depth: int = 5) -> Dict[str, Any]:
        """Fetch a post and its **nested** comment tree.

        `depth` bounds how deep the reply tree is walked (0 = top-level only).
        """
        pid = _post_id(url_or_id)
        data = self._get(f"/api/reddit/comments/{pid}", {"limit": comment_limit})
        comments = [
            _comment(c, 0, depth)
            for c in data.get("comments", [])
            if isinstance(c, dict) and c.get("kind") == "t1"
        ]
        return {
            "post": _post(data.get("post") or {}),
            "comments": comments,
            "source": "redditapis",
        }


# ── Parsing helpers ───────────────────────────────────────────────────────


def _abs(permalink: Optional[str]) -> Optional[str]:
    if not permalink:
        return None
    return permalink if permalink.startswith("http") else f"{_REDDIT_WEB}{permalink}"


def _post(p: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a redditapis post record to the connector's stable shape."""
    return {
        "kind": "post",
        "id": p.get("id"),
        "title": p.get("title"),
        "author": p.get("author"),
        "subreddit": p.get("subreddit"),
        "score": p.get("upvotes"),
        "num_comments": p.get("comments"),
        "upvote_ratio": p.get("upvote_ratio"),
        "created": p.get("created"),            # ISO 8601
        "created_utc": p.get("created_utc"),    # epoch seconds
        "permalink": _abs(p.get("permalink")),
        "url": p.get("url"),                    # canonical reddit URL
        "external_url": p.get("link_url"),      # link the post points to (if any)
        "selftext": p.get("text") or None,
        "over_18": p.get("over_18"),
        "stickied": p.get("stickied"),
        "locked": p.get("locked"),
        "spoiler": p.get("spoiler"),
    }


def _feed(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "items": [_post(p) for p in data.get("posts", [])],
        "after": data.get("after"),
        "before": data.get("before"),
        "source": "redditapis",
    }


def _community(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": c.get("name") or c.get("display_name_prefixed"),
        "title": c.get("title"),
        "description": c.get("public_description") or c.get("description"),
        "subscribers": c.get("subscribers"),
        "url": _abs(c.get("url")),
        "over_18": c.get("over_18"),
        "created_utc": c.get("created_utc"),
    }


def _comment(node: Dict[str, Any], depth: int, max_depth: int) -> Dict[str, Any]:
    """Recursively normalize a native Reddit t1 comment into a nested node."""
    d = node.get("data") or {}
    out = {
        "id": d.get("id"),
        "author": d.get("author"),
        "body": d.get("body"),
        "score": d.get("score"),
        "created_utc": d.get("created_utc"),
        "permalink": _abs(d.get("permalink")),
        "replies": [],
    }
    if depth < max_depth:
        replies = d.get("replies")
        children = (
            (replies.get("data") or {}).get("children", [])
            if isinstance(replies, dict) else []
        )
        out["replies"] = [
            _comment(ch, depth + 1, max_depth)
            for ch in children
            if isinstance(ch, dict) and ch.get("kind") == "t1"
        ]
    return out


def _post_id(url_or_id: str) -> str:
    """Extract a bare post id from a full URL, a permalink, or a t3_ fullname."""
    s = (url_or_id or "").strip()
    if "/comments/" in s:
        return s.split("/comments/", 1)[1].split("/", 1)[0]
    if s.startswith("t3_"):
        return s[3:]
    return s
