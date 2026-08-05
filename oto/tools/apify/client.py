"""Apify client — exécution d'« actors » de scraping hébergés (apify.com).

API v2, auth Bearer. Apify n'est pas UN scraper mais un **catalogue de scrapers**
(les *actors*, ~5000 au Store : Google Maps, LinkedIn, Instagram, Amazon,
Booking…) que l'on lance avec un JSON d'entrée et dont on lit la sortie dans un
*dataset*. D'où le parcours métier :

1. `store_search("google maps")` → repérer l'actor et son identifiant.
2. `actor(actor_id)` → lire ses options par défaut avant de le lancer.
3. `run_sync_dataset_items(actor_id, input)` → lancer ET récupérer les résultats
   (jusqu'à 300 s), ou `run()` + `run_status()` + `dataset_items()` pour un job long.

Un actor se facture à l'usage : `max_items` / `timeout_secs` / `max_total_charge_usd`
sont les garde-fous à poser au lancement, pas après.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class ApifyClient:
    """Client Apify v2 (https://api.apify.com/v2), auth Bearer `apify_api_…`."""

    BASE_URL = "https://api.apify.com/v2"

    def __init__(self, api_key: str = None):
        """
        Args:
            api_key: token Apify (ou variable d'env `APIFY_API_KEY`).
        """
        self.api_key = api_key or require_secret("APIFY_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    # --- transport ----------------------------------------------------------

    @staticmethod
    def _actor_path_id(actor_id: str) -> str:
        """Normalise `username/actor-name` → `username~actor-name` (forme d'URL).

        Le Store affiche l'actor en `apify/website-content-crawler`, l'API l'attend
        en `apify~website-content-crawler` — un slash non converti donnerait un 404
        sur une route qui n'existe pas.
        """
        return actor_id.replace("/", "~")

    def _request(self, method: str, path: str, *, timeout: int = 60, **kwargs) -> Any:
        resp = self.session.request(method, f"{self.BASE_URL}{path}", timeout=timeout, **kwargs)
        raise_for_upstream(resp, service="apify")
        return resp.json() if resp.content else {}

    @staticmethod
    def _compact(params: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in params.items() if v is not None}

    # --- catalogue ----------------------------------------------------------

    def store_search(
        self,
        search: Optional[str] = None,
        limit: int = 20,
        offset: Optional[int] = None,
        category: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /store — cherche un actor public dans le Store Apify.

        Args:
            search: termes libres (ex. "google maps reviews", "linkedin profile").
            category: catégorie du Store (ex. "ECOMMERCE", "SOCIAL_MEDIA").
            sort_by: `"relevance"` | `"popularity"` | `"newest"` | `"lastUpdate"`.

        Returns: `{data: {items: [{id, name, username, title, description, stats,
            pricingInfos, …}], total, …}}`. L'identifiant à lancer est
            `username/name` (ou `id`).
        """
        params = self._compact({
            "search": search, "limit": limit, "offset": offset,
            "category": category, "sortBy": sort_by,
        })
        return self._request("GET", "/store", params=params)

    def actors(self, limit: int = 50, offset: Optional[int] = None,
               desc: Optional[bool] = None) -> Dict[str, Any]:
        """GET /actors — les actors du compte (les siens, pas le Store public)."""
        params = self._compact({"limit": limit, "offset": offset,
                                "desc": 1 if desc else None})
        return self._request("GET", "/actors", params=params)

    def actor(self, actor_id: str) -> Dict[str, Any]:
        """GET /actors/{id} — fiche d'un actor : builds, `defaultRunOptions`
        (mémoire et timeout par défaut), versions. À lire avant un premier
        lancement pour dimensionner `memory_mbytes`/`timeout_secs`."""
        return self._request("GET", f"/actors/{self._actor_path_id(actor_id)}")

    # --- exécution ----------------------------------------------------------

    def run_sync_dataset_items(
        self,
        actor_id: str,
        run_input: Optional[Dict[str, Any]] = None,
        max_items: Optional[int] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[List[str]] = None,
        timeout_secs: Optional[int] = None,
        memory_mbytes: Optional[int] = None,
        max_total_charge_usd: Optional[float] = None,
        build: Optional[str] = None,
        timeout: int = 310,
    ) -> Any:
        """POST /actors/{id}/run-sync-get-dataset-items — lance ET rend les résultats.

        Le chemin nominal : un seul appel, les items du dataset en retour. L'API
        **coupe à 300 s** (408 au-delà) — pour un scraping long, passer par `run()`
        puis `run_status()`/`dataset_items()`.

        Args:
            run_input: JSON d'entrée de l'actor (ses champs sont propres à chaque
                actor — voir sa fiche du Store).
            max_items: plafond d'items FACTURÉS (actors pay-per-result).
            limit / offset / fields: pagination et projection de la sortie.
            timeout_secs / memory_mbytes: budget d'exécution côté Apify.
            max_total_charge_usd: plafond de coût du run.

        Returns: la LISTE des items du dataset (pas une enveloppe `{data: …}`).
        """
        params = self._compact({
            "maxItems": max_items, "limit": limit, "offset": offset,
            "fields": ",".join(fields) if fields else None,
            "timeout": timeout_secs, "memory": memory_mbytes,
            "maxTotalChargeUsd": max_total_charge_usd, "build": build,
        })
        return self._request(
            "POST", f"/actors/{self._actor_path_id(actor_id)}/run-sync-get-dataset-items",
            params=params, json=run_input or {}, timeout=timeout,
        )

    def run(
        self,
        actor_id: str,
        run_input: Optional[Dict[str, Any]] = None,
        max_items: Optional[int] = None,
        timeout_secs: Optional[int] = None,
        memory_mbytes: Optional[int] = None,
        max_total_charge_usd: Optional[float] = None,
        build: Optional[str] = None,
        wait_for_finish: Optional[int] = None,
        timeout: int = 90,
    ) -> Dict[str, Any]:
        """POST /actors/{id}/runs — lance un actor SANS attendre sa fin.

        Args:
            wait_for_finish: secondes d'attente max avant de rendre la main (≤60) —
                pratique pour capter un run très court sans repoller.

        Returns: `{data: {id, actId, status, defaultDatasetId, startedAt, …}}` —
            `id` pour `run_status`, `defaultDatasetId` pour `dataset_items`.
        """
        params = self._compact({
            "maxItems": max_items, "timeout": timeout_secs, "memory": memory_mbytes,
            "maxTotalChargeUsd": max_total_charge_usd, "build": build,
            "waitForFinish": wait_for_finish,
        })
        return self._request(
            "POST", f"/actors/{self._actor_path_id(actor_id)}/runs",
            params=params, json=run_input or {}, timeout=timeout,
        )

    def run_status(self, run_id: str, wait_for_finish: Optional[int] = None,
                   timeout: int = 90) -> Dict[str, Any]:
        """GET /actor-runs/{id} — état d'un run.

        `status` ∈ READY, RUNNING, SUCCEEDED, FAILED, TIMED-OUT, ABORTED. La
        réponse porte `defaultDatasetId` (où lire la sortie) et `usageTotalUsd`
        (ce que le run a coûté).
        """
        params = self._compact({"waitForFinish": wait_for_finish})
        return self._request("GET", f"/actor-runs/{run_id}", params=params, timeout=timeout)

    def abort_run(self, run_id: str, gracefully: Optional[bool] = None) -> Dict[str, Any]:
        """POST /actor-runs/{id}/abort — arrête un run (stoppe la facturation)."""
        params = self._compact({"gracefully": "true" if gracefully else None})
        return self._request("POST", f"/actor-runs/{run_id}/abort", params=params)

    # --- sortie -------------------------------------------------------------

    def dataset_items(
        self,
        dataset_id: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[List[str]] = None,
        omit: Optional[List[str]] = None,
        desc: Optional[bool] = None,
        clean: Optional[bool] = None,
        timeout: int = 120,
    ) -> Any:
        """GET /datasets/{id}/items — les résultats produits par un run.

        Args:
            dataset_id: `defaultDatasetId` du run.
            fields / omit: projection (utile — certains actors rendent des objets
                très larges).
            clean: écarter les items vides / masqués.

        Returns: la LISTE des items (format JSON).
        """
        params = self._compact({
            "limit": limit, "offset": offset,
            "fields": ",".join(fields) if fields else None,
            "omit": ",".join(omit) if omit else None,
            "desc": 1 if desc else None,
            "clean": "true" if clean else None,
        })
        return self._request("GET", f"/datasets/{dataset_id}/items",
                             params=params, timeout=timeout)
