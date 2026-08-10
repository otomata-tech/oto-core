"""
Serper API Client for Google search (web, images, videos, news, places, maps,
shopping, scholar, patents, lens, reviews, autocomplete) and web scraping.

Serper expose une famille d'endpoints Google sous `https://google.serper.dev`
(POST, header `X-API-KEY`) + un scraper sous `https://scrape.serper.dev`. Tous
partagent le même socle de paramètres (`q`, `gl`, `hl`, `location`, `num`,
`page`, `tbs`, `autocorrect`).

Requires: requests
"""

import math
import time
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

import requests

from ...config import require_secret

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class SerperClient:
    """
    Serper API client. Une méthode par endpoint Google + scrape :
    - search           — recherche web (`/search`)
    - search_images    — images (`/images`)
    - search_videos    — vidéos (`/videos`)
    - search_news      — actualités (`/news`)
    - search_places    — lieux / Google Local (`/places`)
    - search_maps      — Google Maps (`/maps`)
    - search_reviews   — avis d'un lieu (`/reviews`)
    - search_shopping  — shopping (`/shopping`)
    - search_scholar   — Google Scholar (`/scholar`)
    - search_patents   — brevets (`/patents`)
    - search_lens      — Google Lens / reverse image (`/lens`)
    - get_suggestions  — autocomplete (`/autocomplete`)
    - scrape_page      — scraping d'une page (`scrape.serper.dev`)
    """

    BASE_URL = "https://google.serper.dev"
    SCRAPE_URL = "https://scrape.serper.dev"

    def __init__(self, api_key: str = None):
        """
        Initialize Serper client.

        Args:
            api_key: Serper API key (or set SERPER_API_KEY env var)
        """
        self.api_key = api_key or require_secret("SERPER_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json"
        })
        self._last_request = 0.0
        self._min_interval = 0.5

    def _rate_limit(self):
        """Ensure minimum time between requests."""
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    def _post(self, url: str, json_data: Dict, label: str) -> Dict:
        """POST + gestion d'erreur. Surface le message d'erreur Serper plutôt
        qu'un opaque "400 Bad Request" (Serper renvoie 400 + {"message":...}
        pour "Not enough credits", clé invalide, etc.)."""
        self._rate_limit()
        response = self.session.post(url, json=json_data, timeout=_HTTP_TIMEOUT)
        if response.status_code >= 400:
            try:
                msg = response.json().get("message") or response.text
            except Exception:
                msg = response.text
            raise RuntimeError(f"Serper {label} {response.status_code}: {msg}")
        return response.json()

    def _request(self, endpoint: str, json_data: Dict) -> Dict:
        """Make API request to a `google.serper.dev` endpoint."""
        return self._post(f"{self.BASE_URL}{endpoint}", json_data, endpoint.lstrip("/"))

    @staticmethod
    def _common_payload(
        query: str,
        num: Optional[int] = None,
        page: Optional[int] = None,
        location: Optional[str] = None,
        country: Optional[str] = None,
        language: Optional[str] = None,
        tbs: Optional[str] = None,
        autocorrect: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Construit le payload commun aux endpoints de recherche Serper.

        Mappe les noms ergonomiques (country/language) vers les clés Serper
        (`gl`/`hl`) et n'inclut que les champs fournis.
        """
        payload: Dict[str, Any] = {"q": query}
        if num is not None:
            payload["num"] = min(num, 100)
        if page is not None:
            payload["page"] = page
        if location:
            payload["location"] = location
        if country:
            payload["gl"] = country
        if language:
            payload["hl"] = language
        if tbs:
            payload["tbs"] = tbs
        if autocorrect is not None:
            payload["autocorrect"] = autocorrect
        return payload

    # ------------------------------------------------------------------ web

    def search(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        location: str = None,
        country: str = None,
        language: str = None,
        tbs: str = None,
        site_filter: str = None,
        autocorrect: bool = None,
    ) -> Dict[str, Any]:
        """
        Perform web search.

        Args:
            query: Search query
            num: Number of results (max 100)
            page: Page number
            location: Geographic location
            country: Country code (e.g., 'us', 'fr')
            language: Language code (e.g., 'en', 'fr')
            tbs: Google time filter (e.g., 'qdr:d' for past day)
            site_filter: Limit to site (e.g., 'linkedin.com')
            autocorrect: Toggle Google autocorrect

        Returns:
            Search results with 'organic' array
        """
        payload = self._common_payload(
            query=query if not site_filter else f"site:{site_filter} {query}",
            num=num, page=page, location=location, country=country,
            language=language, tbs=tbs, autocorrect=autocorrect,
        )
        return self._request("/search", payload)

    # --------------------------------------------------------------- images

    def search_images(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        location: str = None,
        country: str = None,
        language: str = None,
        tbs: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google Images.

        Returns:
            Results with an 'images' array (title, imageUrl, source, link…)
        """
        payload = self._common_payload(
            query=query, num=num, page=page, location=location,
            country=country, language=language, tbs=tbs,
        )
        return self._request("/images", payload)

    # --------------------------------------------------------------- videos

    def search_videos(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        location: str = None,
        country: str = None,
        language: str = None,
        tbs: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google Videos.

        Returns:
            Results with a 'videos' array (title, link, source, duration…)
        """
        payload = self._common_payload(
            query=query, num=num, page=page, location=location,
            country=country, language=language, tbs=tbs,
        )
        return self._request("/videos", payload)

    # ----------------------------------------------------------------- news

    def search_news(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        tbs: str = None,
        country: str = None,
        language: str = None,
        location: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google News.

        Args:
            query: Search query
            num: Number of results (max 100)
            page: Page number
            tbs: Time filter (e.g., 'qdr:w' for past week)
            country: Country code
            language: Language code
            location: Geographic location

        Returns:
            News results with 'news' array
        """
        payload = self._common_payload(
            query=query, num=num, page=page, location=location,
            country=country, language=language, tbs=tbs,
        )
        return self._request("/news", payload)

    # --------------------------------------------------------------- places

    def search_places(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        location: str = None,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google Local / Places (businesses near a location).

        Returns:
            Results with a 'places' array (title, address, rating, cid…)
        """
        payload = self._common_payload(
            query=query, num=num, page=page, location=location,
            country=country, language=language,
        )
        return self._request("/places", payload)

    # ----------------------------------------------------------------- maps

    def search_maps(
        self,
        query: str = None,
        ll: str = None,
        place_id: str = None,
        cid: str = None,
        num: int = 10,
        page: int = 1,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google Maps.

        Args:
            query: Search query (e.g. "coffee shops")
            ll: Latitude/longitude + zoom anchor, format "@lat,lng,zoom"
                (e.g. "@40.6973709,-74.1444871,11z")
            place_id: Google place id to look up directly
            cid: Google customer id of a place
            num: Number of results (max 100)
            page: Page number
            country: Country code
            language: Language code

        Returns:
            Results with a 'places' array (rich Maps records)
        """
        payload: Dict[str, Any] = {}
        if query:
            payload["q"] = query
        if ll:
            payload["ll"] = ll
        if place_id:
            payload["placeId"] = place_id
        if cid:
            payload["cid"] = cid
        if num is not None:
            payload["num"] = min(num, 100)
        if page is not None:
            payload["page"] = page
        if country:
            payload["gl"] = country
        if language:
            payload["hl"] = language
        return self._request("/maps", payload)

    # ----------------------------------------------------------- maps census

    @staticmethod
    def _grid_anchors(
        center: str, radius_km: float, grid: int, zoom: int
    ) -> List[str]:
        """Pave une zone carrée (center ± radius_km) en `grid`×`grid` ancres
        `@lat,lng,zoomz`. Conversion km→degrés : 1° lat ≈ 111 km, 1° lng ≈
        111·cos(lat) km. Une seule ancre si grid ≤ 1."""
        lat0, lng0 = (float(x) for x in center.split(","))
        grid = max(1, grid)
        if grid == 1:
            offsets = [0.0]
        else:
            offsets = [
                -radius_km + 2 * radius_km * i / (grid - 1) for i in range(grid)
            ]
        km_per_deg_lng = 111.0 * max(math.cos(math.radians(lat0)), 1e-6)
        anchors: List[str] = []
        for dlat_km in offsets:
            for dlng_km in offsets:
                lat = lat0 + dlat_km / 111.0
                lng = lng0 + dlng_km / km_per_deg_lng
                anchors.append(f"@{lat:.6f},{lng:.6f},{zoom}z")
        return anchors

    @staticmethod
    def _place_key(place: Dict[str, Any]) -> str:
        """Clé de déduplication stable d'un lieu Maps : id Google si présent
        (cid > placeId > fid), sinon repli titre+adresse normalisés."""
        for k in ("cid", "placeId", "fid"):
            v = place.get(k)
            if v:
                return f"{k}:{v}"
        title = str(place.get("title", "")).strip().lower()
        address = str(place.get("address", "")).strip().lower()
        return f"ta:{title}|{address}"

    def census_maps(
        self,
        query: str,
        center: str = None,
        radius_km: float = 5.0,
        grid: int = 3,
        zoom: int = 14,
        ll_anchors: List[str] = None,
        max_pages: int = 3,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """Recensement EXHAUSTIF d'un type de commerce sur une zone.

        `search_maps` plafonne à ~20 résultats/appel et biaise vers le point
        d'ancrage `ll` → il **sous-compte silencieusement**. Ce recensement
        supprime les deux défauts côté serveur : il **pave** la zone en une
        grille d'ancres géographiques, **pagine** chacune, et **déduplique** par
        id de lieu. Le `count` rendu est donc le total réel dédupliqué.

        Fournir soit `center` "lat,lng" (+ radius_km, grid) pour paver la zone,
        soit `ll_anchors` explicites (qui priment sur le pavage).

        Args:
            query: Ce qu'on énumère (ex. "laverie automatique").
            center: Centre de zone "lat,lng" (requis sauf si ll_anchors).
            radius_km: Demi-largeur de la zone carrée autour du centre (défaut 5).
            grid: Densité du pavage grid×grid (défaut 3 → 9 ancres).
            zoom: Niveau de zoom Maps par ancre (défaut 14).
            ll_anchors: Ancres "@lat,lng,zoomz" explicites (priment sur le pavage).
            max_pages: Pages maxi paginées par ancre (défaut 3).
            country: Code pays (gl).
            language: Code langue (hl).

        Returns:
            {query, count, places[], anchors_used, pages_fetched} — `count` =
            total dédupliqué, à préférer à tout comptage d'un `search_maps` seul.
        """
        if not query:
            raise ValueError("census_maps requires a non-empty query")
        anchors = ll_anchors or (
            self._grid_anchors(center, radius_km, grid, zoom) if center else []
        )
        if not anchors:
            raise ValueError(
                "census_maps requires `center` ('lat,lng') or explicit `ll_anchors`"
            )

        seen: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        pages_fetched = 0
        for anchor in anchors:
            for page in range(1, max_pages + 1):
                res = self.search_maps(
                    query=query, ll=anchor, num=100, page=page,
                    country=country, language=language,
                )
                pages_fetched += 1
                places = res.get("places") or []
                if not places:
                    break
                new = 0
                for p in places:
                    key = self._place_key(p)
                    if key in seen:
                        continue
                    seen[key] = p
                    order.append(key)
                    new += 1
                # Page entièrement déjà vue → ancre épuisée ou recouvrante,
                # inutile de paginer plus loin (les pages profondes divergent).
                if new == 0:
                    break

        return {
            "query": query,
            "count": len(order),
            "places": [seen[k] for k in order],
            "anchors_used": len(anchors),
            "pages_fetched": pages_fetched,
        }

    # -------------------------------------------------------------- reviews

    def search_reviews(
        self,
        cid: str = None,
        fid: str = None,
        place_id: str = None,
        query: str = None,
        sort_by: str = None,
        topic_id: str = None,
        next_page_token: str = None,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Fetch reviews for a Google place.

        Identify the place by one of `cid` / `fid` / `place_id` (from a
        `search_places` / `search_maps` result), or by free-text `query`.

        Args:
            cid: Google customer id of the place
            fid: Google feature id of the place
            place_id: Google place id
            query: Free-text place lookup (alternative to ids)
            sort_by: 'mostRelevant' | 'newest' | 'highestRating' | 'lowestRating'
            topic_id: Filter reviews by topic id
            next_page_token: Pagination cursor from a previous response
            country: Country code
            language: Language code

        Returns:
            Results with a 'reviews' array + pagination token
        """
        payload: Dict[str, Any] = {}
        if cid:
            payload["cid"] = cid
        if fid:
            payload["fid"] = fid
        if place_id:
            payload["placeId"] = place_id
        if query:
            payload["q"] = query
        if sort_by:
            payload["sortBy"] = sort_by
        if topic_id:
            payload["topicId"] = topic_id
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        if country:
            payload["gl"] = country
        if language:
            payload["hl"] = language
        return self._request("/reviews", payload)

    def reviews_all(
        self,
        cid: str = None,
        fid: str = None,
        place_id: str = None,
        query: str = None,
        sort_by: str = None,
        topic_id: str = None,
        max_reviews: int = 200,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """TOUS les avis d'un lieu — pagine `nextPageToken` jusqu'à épuisement.

        `search_reviews` ne rend qu'une page (~10 avis) : un seul appel
        sous-représente silencieusement les avis d'un lieu (le total réel vit
        dans `ratingCount` côté lieu, pas ici). Cette méthode suit le curseur
        `nextPageToken` jusqu'à ce qu'il n'y ait plus de page, ou jusqu'au
        plafond `max_reviews` (borne le coût — un lieu peut avoir des milliers
        d'avis).

        Identifier le lieu par `cid`/`fid`/`place_id` ou `query` (comme
        search_reviews). Returns {count, reviews[], pages_fetched, truncated}.
        `truncated=True` = le plafond a coupé avant épuisement.
        """
        collected: List[Dict[str, Any]] = []
        token: Optional[str] = None
        seen_tokens: set = set()
        pages = 0
        while len(collected) < max_reviews:
            res = self.search_reviews(
                cid=cid, fid=fid, place_id=place_id, query=query,
                sort_by=sort_by, topic_id=topic_id, next_page_token=token,
                country=country, language=language,
            )
            pages += 1
            reviews = res.get("reviews") or []
            if not reviews:
                break
            collected.extend(reviews)
            token = res.get("nextPageToken")
            # Plus de curseur, ou curseur qui se répète (garde anti-boucle).
            if not token or token in seen_tokens:
                token = None
                break
            seen_tokens.add(token)
        return {
            "count": len(collected[:max_reviews]),
            "reviews": collected[:max_reviews],
            "pages_fetched": pages,
            "truncated": len(collected) >= max_reviews and bool(token),
        }

    # ------------------------------------------------------------- shopping

    def search_shopping(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        location: str = None,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google Shopping.

        Returns:
            Results with a 'shopping' array (title, price, source, rating…)
        """
        payload = self._common_payload(
            query=query, num=num, page=page, location=location,
            country=country, language=language,
        )
        return self._request("/shopping", payload)

    # -------------------------------------------------------------- scholar

    def search_scholar(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google Scholar (academic papers).

        Returns:
            Results with an 'organic' array (title, publication, year, citedBy…)
        """
        payload = self._common_payload(
            query=query, num=num, page=page, country=country, language=language,
        )
        return self._request("/scholar", payload)

    # -------------------------------------------------------------- patents

    def search_patents(
        self,
        query: str,
        num: int = 10,
        page: int = 1,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Search Google Patents.

        Returns:
            Results with an 'organic'/'patents' array (title, inventor,
            assignee, publicationNumber…)
        """
        payload = self._common_payload(
            query=query, num=num, page=page, country=country, language=language,
        )
        return self._request("/patents", payload)

    # ----------------------------------------------------------------- lens

    def search_lens(
        self,
        url: str,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Google Lens — reverse image search from an image URL.

        Args:
            url: Public URL of the image to analyse
            country: Country code
            language: Language code

        Returns:
            Results with an 'organic' array of visual matches
        """
        payload: Dict[str, Any] = {"url": url}
        if country:
            payload["gl"] = country
        if language:
            payload["hl"] = language
        return self._request("/lens", payload)

    # --------------------------------------------------------------- scrape

    # Hôtes qui refusent SYSTÉMATIQUEMENT un scrape serveur (mur de connexion ou
    # anti-bot permanent). Y envoyer Serper coûte ~45 s — son propre timeout — pour
    # revenir bredouille à tous les coups : mesuré sur 4 jours de journal, six échecs
    # à 45-48 s dont quatre sur des profils LinkedIn. Mieux vaut refuser tout de suite
    # en nommant l'outil qui, lui, sait lire la source.
    #
    # La liste reste COURTE et ne contient que des refus structurels. Un site qui
    # bloque parfois n'a rien à y faire : ce garde supprime une attente inutile, il ne
    # doit pas devenir une liste noire qui prive d'un scrape qui aurait marché.
    _NEVER_SCRAPABLE = {
        "linkedin.com": "les profils et pages LinkedIn se lisent avec les outils "
                        "`unipile_*` (compte connecté), jamais par scrape.",
        "instagram.com": "Instagram exige une session ; passe par le connecteur "
                         "messagerie ou une autre source.",
        "facebook.com": "Facebook exige une session ; cherche une autre source.",
        "x.com": "X exige une session ; cherche une autre source.",
        "twitter.com": "X exige une session ; cherche une autre source.",
    }

    @classmethod
    def _refuses_scraping(cls, url: str) -> Optional[str]:
        """La raison de refuser d'emblée, ou None. Compare sur le domaine
        enregistrable pour couvrir les sous-domaines (`fr.`, `uk.`, `www.`)."""
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return None
        for domain, why in cls._NEVER_SCRAPABLE.items():
            if host == domain or host.endswith("." + domain):
                return why
        return None

    def scrape_page(
        self,
        url: str,
        include_markdown: bool = False,
    ) -> Dict[str, Any]:
        """
        Scrape a web page.

        Args:
            url: URL to scrape
            include_markdown: Include markdown version

        Returns:
            Page data with text, metadata, and JSON-LD

        Raises:
            RuntimeError: si l'hôte refuse structurellement le scrape serveur — le
                message nomme la source à utiliser à la place.
        """
        why = self._refuses_scraping(url)
        if why:
            raise RuntimeError(f"Serper scrape refusé pour {url} : {why}")
        payload: Dict[str, Any] = {"url": url}
        if include_markdown:
            payload["includeMarkdown"] = True
        return self._post(self.SCRAPE_URL, payload, "scrape")

    # --------------------------------------------------------- autocomplete

    def autocomplete(
        self,
        query: str,
        country: str = None,
        language: str = None,
    ) -> Dict[str, Any]:
        """
        Raw autocomplete endpoint (full Serper response).

        Returns:
            Results with a 'suggestions' array of {value} objects
        """
        payload: Dict[str, Any] = {"q": query}
        if country:
            payload["gl"] = country
        if language:
            payload["hl"] = language
        return self._request("/autocomplete", payload)

    def get_suggestions(self, query: str, country: str = None) -> List[str]:
        """
        Get search autocomplete suggestions (flattened to a list of strings).

        Args:
            query: Base query
            country: Country code

        Returns:
            List of suggested queries
        """
        try:
            result = self.autocomplete(query, country=country)
            suggestions = result.get("suggestions", [])
            return [s.get("value", "") for s in suggestions if s.get("value")]
        except Exception:
            return []

    # ---------------------------------------------------------------- batch

    def batch_search(
        self,
        queries: List[str],
        num_per_query: int = 10,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """
        Perform multiple web searches.

        Args:
            queries: List of queries
            num_per_query: Results per query
            **kwargs: Additional search params

        Returns:
            List of results per query
        """
        results = []
        for query in queries:
            try:
                result = self.search(query, num=num_per_query, **kwargs)
                results.append({"query": query, "results": result})
            except Exception as e:
                results.append({"query": query, "error": str(e), "results": {}})
        return results
