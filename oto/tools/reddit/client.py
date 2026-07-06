"""
Reddit read-only client — **RSS feeds** (no auth, no app).

Reddit closed the unauthenticated `www.reddit.com/*.json` endpoints (HTTP 403
"Blocked"). The `*.rss` Atom feeds are still served without authentication, so this
client reads those. Trade-off: RSS exposes title / author / link / date / HTML body,
but **not** score, upvote ratio, num_comments, nor a nested comment tree — those fields
come back as ``None``.

⚠️ **Rate limit** : anonymous RSS is throttled hard per IP (a couple of calls then
HTTP 429 for ~30-45s). A 429 is surfaced as a clear error rather than silently
retried — this is a best-effort reader, not a high-throughput one.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET  # types Element uniquement — parsing via defusedxml

import requests
# XML externe non fiable (RSS Reddit) → parseur durci contre XXE / billion-laughs.
from defusedxml.ElementTree import fromstring as _xml_fromstring

_ATOM = "{http://www.w3.org/2005/Atom}"
_KIND_BY_PREFIX = {"t1": "comment", "t3": "post", "t5": "subreddit"}


class RedditRateLimited(RuntimeError):
    """Reddit a renvoyé 429 (limite anonyme RSS serrée) — réessaie dans ~30 s."""


class RedditClient:
    """
    Reddit read-only via RSS (public feeds, no credential).

    Covers subreddit feeds, search (global or per-sub), subreddit discovery, and a
    post's comments. Fields absent from RSS (score, votes, num_comments, nesting)
    are returned as ``None``.
    """

    BASE_URL = "https://www.reddit.com"
    DEFAULT_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    def __init__(self, user_agent: Optional[str] = None, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent or self.DEFAULT_UA
        self.timeout = timeout

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> ET.Element:
        url = f"{self.BASE_URL}{path}" if path.startswith("/") else path
        r = self.session.get(url, params=params, timeout=self.timeout, allow_redirects=True)
        if r.status_code == 429:
            raise RedditRateLimited(
                "Reddit a rate-limité la requête (flux RSS anonyme, limite serrée par IP). "
                "Réessaie dans ~30 s."
            )
        r.raise_for_status()
        try:
            return _xml_fromstring(r.content)
        except Exception as e:  # ParseError, EntitiesForbidden, DTDForbidden…
            raise RuntimeError(f"réponse Reddit non-XML ou XML rejeté (bloqué ?): {e}") from e

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
        params: Dict[str, Any] = {"limit": min(limit, 100)}
        if time and sort in {"top", "controversial"}:
            params["t"] = time
        if after:
            params["after"] = after
        return _parse_feed(self._get(f"/r/{name}/{sort}.rss", params))

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
            "q": query, "sort": sort, "t": time,
            "limit": min(limit, 100), "type": "link",
        }
        if after:
            params["after"] = after
        if subreddit:
            params["restrict_sr"] = "1"
            path = f"/r/{subreddit}/search.rss"
        else:
            path = "/search.rss"
        return _parse_feed(self._get(path, params))

    def search_subreddits(self, query: str, limit: int = 25) -> Dict[str, Any]:
        """Discover subreddits by name/description match."""
        return _parse_feed(
            self._get("/subreddits/search.rss", {"q": query, "limit": min(limit, 100)})
        )

    # ── Post + comments ───────────────────────────────────────────────────

    def post(self, url_or_id: str, comment_limit: int = 100, depth: int = 5) -> Dict[str, Any]:
        """Fetch a post and its comments (flat, via the post's RSS feed).

        `depth` is accepted for signature compatibility but ignored — RSS returns a
        flat comment list, not a nested tree.
        """
        path = _post_path(url_or_id)
        feed = self._get(f"{path}.rss", {"limit": comment_limit})
        # Métadonnées du post = niveau feed (title/link/updated) ; les <entry> sont
        # les commentaires (t1). Certains flux incluent aussi le post en entry (t3).
        post = {
            "kind": "post",
            "id": _strip_prefix(_text(feed.find(f"{_ATOM}id"))),
            "title": _text(feed.find(f"{_ATOM}title")),
            "permalink": _link(feed),
            "url": _link(feed),
            "created": _text(feed.find(f"{_ATOM}updated")),
            "score": None, "upvote_ratio": None, "num_comments": None,
        }
        comments: List[Dict[str, Any]] = []
        for e in feed.findall(f"{_ATOM}entry"):
            item = _entry_to_item(e)
            if item["kind"] == "post":
                # le post lui-même remonté en entry → enrichit selftext/author
                post["author"] = item["author"]
                post["selftext"] = item["content_html"]
            else:
                comments.append({
                    "id": item["id"], "author": item["author"],
                    "body": item["content_html"], "permalink": item["permalink"],
                    "created": item["created"], "score": None, "replies": [],
                })
        return {"post": post, "comments": comments, "source": "rss"}


# ── Parsing helpers ───────────────────────────────────────────────────────


def _text(el: Optional[ET.Element]) -> Optional[str]:
    return el.text if el is not None and el.text else None


def _link(el: ET.Element) -> Optional[str]:
    ln = el.find(f"{_ATOM}link")
    return ln.get("href") if ln is not None else None


def _strip_prefix(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return raw
    return raw.split("_", 1)[1] if "_" in raw else raw


def _entry_to_item(e: ET.Element) -> Dict[str, Any]:
    raw_id = _text(e.find(f"{_ATOM}id")) or ""
    prefix = raw_id.split("_", 1)[0] if "_" in raw_id else ""
    author_el = e.find(f"{_ATOM}author")
    author = _text(author_el.find(f"{_ATOM}name")) if author_el is not None else None
    if author and author.startswith("/u/"):
        author = author[3:]
    cat = e.find(f"{_ATOM}category")
    content = e.find(f"{_ATOM}content")
    return {
        "kind": _KIND_BY_PREFIX.get(prefix, "unknown"),
        "id": _strip_prefix(raw_id),
        "title": _text(e.find(f"{_ATOM}title")),
        "author": author,
        "subreddit": cat.get("term") if cat is not None else None,
        "permalink": _link(e),
        "url": _link(e),
        "created": _text(e.find(f"{_ATOM}published")) or _text(e.find(f"{_ATOM}updated")),
        "content_html": content.text if content is not None else None,
        # Champs indisponibles via RSS :
        "score": None, "upvote_ratio": None, "num_comments": None,
        "flair": None, "over_18": None,
    }


def _parse_feed(feed: ET.Element) -> Dict[str, Any]:
    items = [_entry_to_item(e) for e in feed.findall(f"{_ATOM}entry")]
    return {
        "items": items,
        "after": None,   # RSS ne renvoie pas de curseur de pagination
        "before": None,
        "source": "rss",
        "note": "via RSS — score / votes / num_comments / arbre de commentaires indisponibles",
    }


def _post_path(url_or_id: str) -> str:
    s = url_or_id.strip()
    if s.startswith("http"):
        from urllib.parse import urlparse
        return urlparse(s).path.rstrip("/")
    if s.startswith("/r/"):
        return s.rstrip("/")
    return f"/comments/{quote_plus(s)}"
