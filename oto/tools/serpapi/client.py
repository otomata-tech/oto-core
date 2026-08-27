"""
SerpAPI Client for Google Jobs and search.

Requires: requests
"""

import datetime as dt
import time
from typing import Optional, Dict, Any

import requests

from ...config import require_secret

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée

# Au-delà de cet âge, un payload n'a pas été constaté « à l'instant » : il a été
# resservi par un cache. La borne n'est pas arbitraire — `created_at` date le
# moment où SerpApi commence à interroger Google, et notre timeout de LECTURE est
# de 60 s : un payload réellement frais ne peut donc pas nous parvenir plus vieux
# que ~60 s. 120 s laisse autant de marge (dérive d'horloge comprise) tout en
# restant très en deçà de l'heure de rétention des deux caches amont.
_EMPTY_MAX_AGE = 120.0


class SerpAPIClient:
    """
    SerpAPI client — accès générique à **tous** les moteurs SerpApi.

    `search(engine, params)` est l'entrée générique (engine = 'google', 'bing',
    'google_trends', 'youtube', 'walmart', 'google_jobs'…). `search_jobs` /
    `get_job_details` sont des raccourcis typés Google Jobs construits par-dessus.
    """

    BASE_URL = "https://serpapi.com/search"

    def __init__(self, api_key: str = None):
        """
        Initialize SerpAPI client.

        Args:
            api_key: SerpAPI key (or set SERPAPI_API_KEY env var)
        """
        self.api_key = api_key or require_secret("SERPAPI_API_KEY")
        self.session = requests.Session()
        self._last_request = 0.0
        self._min_interval = 1.0

    def _rate_limit(self):
        """Ensure minimum time between requests."""
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    def _request(self, params: Dict) -> Dict:
        """Make API request."""
        self._rate_limit()
        params["api_key"] = self.api_key

        response = self.session.get(self.BASE_URL, params=params, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _payload_age(result: Dict) -> Optional[float]:
        """Âge du payload en secondes, ou None s'il ne se date pas lui-même.

        `search_metadata.created_at` est l'horodatage UTC que SerpApi appose au
        moment où il interroge réellement Google. Resservi par un cache il reste
        FIGÉ à ce moment-là — c'est donc le seul témoin d'âge que porte le corps
        de la réponse, et il vaut pour les deux caches en série mesurés le
        2026-08-27 (celui de SerpApi et l'arête Cloudflare devant lui, qui le
        recopient tel quel).
        """
        created = (result.get("search_metadata") or {}).get("created_at")
        if not isinstance(created, str):
            return None
        try:
            stamp = dt.datetime.strptime(created.replace(" UTC", ""),
                                         "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()

    def _empty_must_be_fresh(
        self,
        params: Dict,
        result: Dict,
        results_key: str,
    ) -> tuple[Dict, Dict]:
        """Refuse un résultat VIDE que le cache a resservi ; le refait une fois.

        Défaut du signal d'usage #456 (mission Audiens, 2026-08-27) : la même
        requête rendait 0 offre, puis 1 avec `no_cache=True`. Un zéro s'était
        installé dans le cache amont, qui le resservait pendant une heure. Ce
        connecteur sert d'INDICATEUR D'ACTIVITÉ — un zéro mémorisé y devient une
        absence fausse et persistante, que rien ne distingue d'une vraie absence
        puisque c'est précisément ce que le champ est censé pouvoir dire.

        Le cache n'est pas le nôtre (aucun cache dans oto-core ni dans le
        backend) : on ne peut donc pas décider de ne PAS y ranger le vide. Ce
        qu'on décide, c'est de ne pas le LIRE. D'où l'asymétrie assumée :
        - un résultat vide n'est acceptable que **constaté frais** ; périmé, il
          est refait en forçant `no_cache` — le coût d'un appel refait est très
          inférieur au coût d'une absence fausse propagée sur toute une campagne ;
        - un résultat NON vide garde le droit au cache : une liste d'offres
          vieille de quarante minutes reste un signal d'activité valide, et c'est
          elle qui paie les 0,0 s plutôt que les 5 à 20 s de scraping amont.

        Ce que la règle ne fait PAS, et qu'il ne faut pas lui prêter : elle ne
        protège pas du martèlement. Mesuré le 2026-08-27 sur l'API réelle, un
        appel forcé ne remplace PAS l'entrée que liront les appels ordinaires —
        deux appels consécutifs sur une requête durablement vide reprennent tous
        les deux (6,3 s puis 17,4 s). Une requête vide répétée coûte donc un
        scraping complet à chaque fois, soit exactement ce que coûtait le
        `no_cache=True` que les appelants posaient déjà à la main. On s'y tient :
        borner ce coût demanderait un cache à NOUS, que le serveur ne peut pas
        porter (il construit un client par appel MCP, donc un cache d'instance
        ne servirait jamais). Et le cas dominant d'une campagne — une entreprise
        interrogée une seule fois — ne paie rien de plus : son vide est un vrai
        défaut de cache, donc déjà frais, donc rendu sans reprise.

        Un payload qui ne se date pas ne peut pas certifier sa fraîcheur : on le
        traite comme périmé. On penche du côté de la réponse juste, jamais du
        côté de la réponse rapide.
        """
        age = self._payload_age(result)
        refetched = False
        if not result.get(results_key) and (age is None or age > _EMPTY_MAX_AGE):
            params["no_cache"] = "true"
            result = self._request(params)
            age = self._payload_age(result)
            refetched = True
        return result, {
            "age_seconds": None if age is None else round(age),
            "refetched": refetched,
        }

    def _paginate(
        self,
        params: Dict,
        result: Dict,
        results_key: str,
        max_results: int,
    ) -> Dict:
        """Suit `serpapi_pagination.next_page_token` jusqu'à `max_results`.

        Concatène `result[results_key]` au fil des pages et tronque à
        `max_results`. Mute et renvoie `result` (la dernière page sert de
        métadonnées). No-op si le moteur ne renvoie pas de token.
        """
        all_items = list(result.get(results_key, []))
        while len(all_items) < max_results:
            next_token = result.get("serpapi_pagination", {}).get("next_page_token")
            if not next_token:
                break
            params["next_page_token"] = next_token
            result = self._request(params)
            new_items = result.get(results_key, [])
            if not new_items:
                break
            all_items.extend(new_items)

        result[results_key] = all_items[:max_results]
        return result

    def search(
        self,
        engine: str,
        params: Dict[str, Any] = None,
        max_results: int = None,
        results_key: str = None,
        **extra,
    ) -> Dict[str, Any]:
        """
        Generic SerpApi call — reach ANY engine.

        Args:
            engine: SerpApi engine id, e.g. 'google', 'bing', 'duckduckgo',
                'youtube', 'walmart', 'amazon', 'ebay', 'google_trends',
                'google_finance', 'google_flights', 'google_hotels',
                'google_events', 'google_jobs'… (full list: serpapi.com).
            params: engine-specific parameters (e.g. {'q': 'pizza', 'gl': 'us'}).
            max_results: if set with `results_key`, auto-paginate up to this many.
            results_key: the result array to paginate/cap (e.g. 'organic_results',
                'jobs_results'). Required to enable pagination.
            **extra: extra params merged into the query (convenience).

        Returns:
            Raw SerpApi JSON payload. When `results_key` is given, an EMPTY
            result is guaranteed to have been observed fresh (see
            `_empty_must_be_fresh`) and the payload carries an extra
            `oto_freshness` block: `age_seconds` (how old the answer SerpApi
            served actually is — 0 means just scraped, a large value means a
            cache served it) and `refetched` (whether we had to re-issue the
            call to avoid returning a stale empty).
        """
        payload: Dict[str, Any] = {"engine": engine}
        if params:
            payload.update(params)
        if extra:
            payload.update(extra)

        result = self._request(payload)
        if not results_key:
            # Sans tableau nommé, le client ne sait pas lequel porte la réponse :
            # ni garantie de fraîcheur, ni pagination — on ne DEVINE pas ce que
            # « vide » veut dire pour un moteur qu'on ne connaît pas. Brut tel quel.
            return result

        result, freshness = self._empty_must_be_fresh(payload, result, results_key)
        if max_results:
            result = self._paginate(payload, result, results_key, max_results)
        # Posé APRÈS la pagination : `_paginate` rebranche `result` sur la
        # DERNIÈRE page lue, et emporterait le bloc avec l'ancienne.
        result["oto_freshness"] = freshness
        return result

    def search_jobs(
        self,
        query: str = None,
        company: str = None,
        location: str = None,
        country: str = None,
        language: str = "en",
        max_results: int = 100,
        no_cache: bool = False,
    ) -> Dict[str, Any]:
        """
        Search Google Jobs.

        Args:
            query: free-text job query (e.g. 'data engineer Paris', 'senior
                python remote'). Use this for general job sourcing.
            company: convenience shortcut — if `query` is omitted, searches
                "<company> jobs" (backward-compatible).
            location: Geographic location (e.g., 'Paris, France')
            country: Country code (e.g., 'fr', 'us')
            language: Language code
            max_results: Maximum results (handles pagination)
            no_cache: force a fresh scrape even when the answer is NOT empty.
                No longer needed for correctness: an empty `jobs_results` is
                always observed fresh (a cached empty is re-issued for you).

        Returns:
            Dict with a `jobs_results` array, plus an `oto_freshness` block
            saying how old the answer is and whether it had to be re-issued.
        """
        q = query or (f"{company} jobs" if company else None)
        if not q:
            raise ValueError("search_jobs requires `query` or `company`")

        params: Dict[str, Any] = {"q": q, "hl": language, "no_cache": str(no_cache).lower()}
        if location:
            params["location"] = location
        if country:
            params["gl"] = country

        return self.search(
            "google_jobs", params=params,
            max_results=max_results, results_key="jobs_results",
        )

    def get_job_details(self, job_id: str) -> Dict[str, Any]:
        """
        Get detailed job information.

        Args:
            job_id: Google Jobs job ID

        Returns:
            Detailed job information
        """
        return self.search("google_jobs_listing", params={"q": job_id})
