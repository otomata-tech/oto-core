"""Firecrawl client — scraping & crawl de sites en markdown/JSON (firecrawl.dev).

API v2, auth Bearer. Quatre surfaces métier :
- **scrape** (sync) : une URL → markdown/html/liens/screenshot, JS exécuté.
- **crawl** (async) : un domaine entier → job id, puis pagination des pages extraites.
- **map** (sync) : découverte rapide de toutes les URLs d'un site (pas de contenu).
- **search** (sync) : recherche web + contenu complet des résultats en un appel.
- **extract** (async) : extraction structurée guidée par un prompt/schéma sur N URLs.

Le crawl et l'extract sont des **jobs** : le start rend un `id`, le statut se relit
tant que `status != "completed"`. Les corps de requête sont passés tels quels à
l'API (l'appelant choisit ses options) — voir https://docs.firecrawl.dev.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class FirecrawlClient:
    """Client Firecrawl v2 (https://api.firecrawl.dev/v2), auth Bearer `fc-…`."""

    BASE_URL = "https://api.firecrawl.dev/v2"

    def __init__(self, api_key: str = None):
        """
        Args:
            api_key: clé Firecrawl (ou variable d'env `FIRECRAWL_API_KEY`).
        """
        self.api_key = api_key or require_secret("FIRECRAWL_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *, timeout: int = 120, **kwargs) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=timeout, **kwargs)
        raise_for_upstream(resp, service="firecrawl")
        return resp.json() if resp.content else {}

    @staticmethod
    def _compact(body: Dict[str, Any]) -> Dict[str, Any]:
        """Retire les clés à None — l'API applique alors SES défauts."""
        return {k: v for k, v in body.items() if v is not None}

    # --- scrape (sync) ------------------------------------------------------

    def scrape(
        self,
        url: str,
        formats: Optional[List[Any]] = None,
        only_main_content: Optional[bool] = None,
        include_tags: Optional[List[str]] = None,
        exclude_tags: Optional[List[str]] = None,
        wait_for: Optional[int] = None,
        actions: Optional[List[Dict[str, Any]]] = None,
        headers: Optional[Dict[str, str]] = None,
        mobile: Optional[bool] = None,
        proxy: Optional[str] = None,
        max_age: Optional[int] = None,
        location: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """POST /scrape — extrait UNE page (JS exécuté côté Firecrawl).

        Args:
            url: page à extraire.
            formats: sortie(s) voulue(s) — `["markdown"]` par défaut côté API.
                Accepte les formes objet de l'API (ex. `[{"type": "json",
                "schema": {...}}]` pour de l'extraction structurée en un appel,
                `[{"type": "screenshot", "fullPage": true}]`).
            only_main_content: retirer nav/footer/pubs (défaut API: true).
            include_tags / exclude_tags: sélecteurs CSS à garder / retirer.
            wait_for: ms d'attente avant capture (pages qui peuplent en JS).
            actions: séquence d'interactions avant capture (click, write, scroll,
                wait…) — permet de passer un formulaire ou un cookie wall.
            max_age: âge max (ms) d'une version en cache acceptée — un cache hit
                est bien plus rapide et moins cher qu'un scrape neuf.
            timeout_ms: budget côté Firecrawl (max 60000 par défaut).
            timeout: timeout HTTP local (secondes).

        Returns: `{success, data: {markdown?, html?, links?, screenshot?, json?,
            metadata: {title, description, sourceURL, statusCode, …}}}`.
        """
        body = self._compact({
            "url": url,
            "formats": formats,
            "onlyMainContent": only_main_content,
            "includeTags": include_tags,
            "excludeTags": exclude_tags,
            "waitFor": wait_for,
            "actions": actions,
            "headers": headers,
            "mobile": mobile,
            "proxy": proxy,
            "maxAge": max_age,
            "location": location,
            "timeout": timeout_ms,
        })
        return self._request("POST", "/scrape", json=body, timeout=timeout)

    # --- crawl (async) ------------------------------------------------------

    def crawl(
        self,
        url: str,
        limit: Optional[int] = None,
        include_paths: Optional[List[str]] = None,
        exclude_paths: Optional[List[str]] = None,
        max_discovery_depth: Optional[int] = None,
        crawl_entire_domain: Optional[bool] = None,
        allow_subdomains: Optional[bool] = None,
        allow_external_links: Optional[bool] = None,
        sitemap: Optional[str] = None,
        delay: Optional[float] = None,
        prompt: Optional[str] = None,
        scrape_options: Optional[Dict[str, Any]] = None,
        webhook: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """POST /crawl — lance le crawl d'un site. **Asynchrone** : rend un job id.

        Args:
            url: URL de départ.
            limit: plafond de pages (défaut API 10000 — le poser est la première
                protection contre une facture surprise).
            include_paths / exclude_paths: regex de chemins à suivre / ignorer.
            max_discovery_depth: profondeur max de découverte depuis l'URL de départ.
            crawl_entire_domain: sortir de l'arborescence de l'URL de départ.
            allow_subdomains / allow_external_links: élargir au-delà de l'hôte.
            sitemap: `"include"` (défaut) | `"skip"` | `"only"`.
            delay: secondes entre deux requêtes (politesse / anti-blocage).
            prompt: consigne en langage naturel dont Firecrawl dérive les options.
            scrape_options: options de scrape appliquées à CHAQUE page (mêmes clés
                que `scrape`, camelCase).
            webhook: notification `{url, events: ["completed", …]}` au lieu de poller.

        Returns: `{success, id, url}` — `id` à passer à `crawl_status`.
        """
        body = self._compact({
            "url": url,
            "limit": limit,
            "includePaths": include_paths,
            "excludePaths": exclude_paths,
            "maxDiscoveryDepth": max_discovery_depth,
            "crawlEntireDomain": crawl_entire_domain,
            "allowSubdomains": allow_subdomains,
            "allowExternalLinks": allow_external_links,
            "sitemap": sitemap,
            "delay": delay,
            "prompt": prompt,
            "scrapeOptions": scrape_options,
            "webhook": webhook,
        })
        return self._request("POST", "/crawl", json=body, timeout=timeout)

    def crawl_status(
        self,
        crawl_id: Optional[str] = None,
        next_url: Optional[str] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """GET /crawl/{id} — état + pages déjà extraites d'un crawl.

        Args:
            crawl_id: id rendu par `crawl`.
            next_url: URL `next` d'une réponse précédente — la réponse est
                plafonnée à 10 Mo, `next` sert à récupérer la tranche suivante.
                Passer `next_url` ignore `crawl_id`.

        Returns: `{status: scraping|completed|failed, total, completed,
            creditsUsed, expiresAt, next?, data: [pages]}`.
        """
        if not next_url and not crawl_id:
            raise ValueError("crawl_status: crawl_id ou next_url requis.")
        path = next_url or f"/crawl/{crawl_id}"
        return self._request("GET", path, timeout=timeout)

    def cancel_crawl(self, crawl_id: str, timeout: int = 60) -> Dict[str, Any]:
        """DELETE /crawl/{id} — arrête un crawl en cours (stoppe la consommation)."""
        return self._request("DELETE", f"/crawl/{crawl_id}", timeout=timeout)

    # --- map (sync) ---------------------------------------------------------

    def map_site(
        self,
        url: str,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        sitemap: Optional[str] = None,
        include_subdomains: Optional[bool] = None,
        ignore_query_parameters: Optional[bool] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """POST /map — liste les URLs d'un site, SANS extraire le contenu.

        Bien plus rapide et moins cher qu'un crawl : sert à repérer les pages qui
        valent un scrape (`search` filtre les URLs, ex. "pricing", "carriere").

        Returns: `{success, links: [{url, title?, description?}]}`.
        """
        body = self._compact({
            "url": url,
            "search": search,
            "limit": limit,
            "sitemap": sitemap,
            "includeSubdomains": include_subdomains,
            "ignoreQueryParameters": ignore_query_parameters,
        })
        return self._request("POST", "/map", json=body, timeout=timeout)

    # --- search (sync) ------------------------------------------------------

    def search(
        self,
        query: str,
        limit: Optional[int] = None,
        sources: Optional[List[Any]] = None,
        categories: Optional[List[Any]] = None,
        tbs: Optional[str] = None,
        location: Optional[str] = None,
        country: Optional[str] = None,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        scrape_options: Optional[Dict[str, Any]] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """POST /search — recherche web, avec le contenu des pages si demandé.

        Args:
            query: requête (opérateurs supportés : `"exact"`, `-exclu`, `site:`,
                `filetype:`). Max 500 caractères.
            limit: nombre de résultats (défaut 10, max 100).
            sources: `[{"type": "web"|"news"|"images"}]` — défaut web.
            categories: `[{"type": "github"|"research"|"pdf"}]`.
            tbs: filtre temporel (ex. `"qdr:w"` = dernière semaine).
            include_domains / exclude_domains: restreindre / écarter des domaines
                (mutuellement exclusifs côté API).
            scrape_options: si fourni, chaque résultat est AUSSI scrapé (ex.
                `{"formats": ["markdown"]}`) — sinon seuls titre/description/URL.

        Returns: `{success, data: {web?: [...], news?: [...], images?: [...]},
            creditsUsed}`.
        """
        body = self._compact({
            "query": query,
            "limit": limit,
            "sources": sources,
            "categories": categories,
            "tbs": tbs,
            "location": location,
            "country": country,
            "includeDomains": include_domains,
            "excludeDomains": exclude_domains,
            "scrapeOptions": scrape_options,
        })
        return self._request("POST", "/search", json=body, timeout=timeout)

    # --- extract (async) ----------------------------------------------------

    def extract(
        self,
        urls: List[str],
        prompt: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
        enable_web_search: Optional[bool] = None,
        show_sources: Optional[bool] = None,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """POST /extract — extraction structurée sur N URLs. **Asynchrone**.

        Args:
            urls: pages à traiter ; un `/*` en suffixe étend au site entier
                (ex. `"https://acme.com/*"`).
            prompt: ce qu'on cherche, en langage naturel.
            schema: JSON Schema de la sortie voulue (plus fiable qu'un prompt seul).
            enable_web_search: autoriser Firecrawl à compléter hors des URLs données.

        Returns: `{success, id}` — à relire via `extract_status`.
        """
        body = self._compact({
            "urls": urls,
            "prompt": prompt,
            "schema": schema,
            "enableWebSearch": enable_web_search,
            "showSources": show_sources,
        })
        return self._request("POST", "/extract", json=body, timeout=timeout)

    def extract_status(self, job_id: str, timeout: int = 120) -> Dict[str, Any]:
        """GET /extract/{id} — état + données d'un job d'extraction.

        Returns: `{success, status: processing|completed|failed, data?, sources?}`.
        """
        return self._request("GET", f"/extract/{job_id}", timeout=timeout)
