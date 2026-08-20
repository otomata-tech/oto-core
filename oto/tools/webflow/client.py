"""Webflow Data API v2 Client — https://developers.webflow.com/data/reference

Scope: CMS (site/collections/items/publish), webhooks, forms/submissions,
and pages (metadata + read-only static content + site-level publish). No
assets, ecommerce, comments, or custom code — custom code injects arbitrary
JS into every visitor's browser (site- or page-wide), a categorically
different blast radius than data CRUD; deliberately left out until there's
a concrete need.

Auth = Webflow **Site API token** (generated per-site in Site Settings →
Apps & Integrations → API access, scoped with `cms:read`/`cms:write`/
`sites:read`). Site tokens are bound to exactly one site (confirmed against
developers.webflow.com/data/v2.0.0/reference/authentication/site-token —
"Site tokens are created per site"), so the caller never needs to know or
paste a `site_id`: it's resolved lazily on first use via `GET /sites`
(requires the `sites:read` scope) and cached — that call returns exactly one
site for a genuine Site token. An explicit `site_id` can still be passed to
the constructor to skip that resolution (e.g. tests, or a future token type
that spans sites), but it is optional everywhere.

Webflow's item endpoints natively support batching (an `items` array in one
HTTP call) for create/update/delete — unlike e.g. Folk, this client does NOT
need a client-side loop for bulk; `create_items`/`update_items`/`delete_items`
always take a list, and the caller decides whether that list has 1 or N
elements.
"""

import time
from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class WebflowClient:
    BASE_URL = "https://api.webflow.com/v2"

    def __init__(self, api_key: str = None, site_id: str = None):
        self.api_key = api_key or require_secret("WEBFLOW_API_KEY")
        self._site_id = site_id

    @property
    def site_id(self) -> str:
        """Résolu paresseusement via `GET /sites` si non fourni au
        constructeur, puis mis en cache — un Site API token Webflow est bound
        à UN site, donc rien à saisir : le token le sait déjà. Lève
        `ValueError` (pas `UpstreamHTTPError` : ce n'est pas un refus HTTP)
        si le token voit zéro ou plusieurs sites — un token de workspace/OAuth
        passé ici par erreur ne doit jamais faire deviner LEQUEL des sites
        visés est le bon."""
        if self._site_id is None:
            self._site_id = self._resolve_site_id()
        return self._site_id

    def _resolve_site_id(self) -> str:
        sites = self._request("GET", "sites").get("sites", [])
        if len(sites) == 1:
            return sites[0]["id"]
        if not sites:
            raise ValueError(
                "ce token Webflow n'a accès à aucun site — vérifie qu'il a "
                "été généré avec le scope sites:read et qu'il n'a pas été "
                "révoqué.")
        raise ValueError(
            f"ce token Webflow a accès à {len(sites)} sites — attendu "
            "exactement 1 pour un Site API token (généré depuis Site "
            "Settings → Apps & Integrations → API access DU site voulu, pas "
            f"un token de workspace). Sites vus : {[s.get('id') for s in sites]}.")

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {"Authorization": f"Bearer {self.api_key}", "accept": "application/json"}
        if method.upper() not in ("GET", "DELETE"):
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            resp = requests.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 429:
                # Webflow renvoie Retry-After (généralement 60s, cf. doc rate-limits).
                wait = int(resp.headers.get("Retry-After", 60))
                time.sleep(wait)
                continue
            raise_for_upstream(resp, service="webflow")
            return resp.json() if resp.content else {}
        raise Exception("Rate limit exceeded after retries")

    # --- Site ---

    def get_site(self) -> Dict:
        return self._request("GET", f"sites/{self.site_id}")

    # --- Collections ---

    def list_collections(self) -> List[Dict]:
        return self._request("GET", f"sites/{self.site_id}/collections").get("collections", [])

    def get_collection(self, collection_id: str) -> Dict:
        """Renvoie le schéma de la collection (dont `fields[]` — nécessaire pour
        valider les clés de `fieldData` avant un create/update)."""
        return self._request("GET", f"collections/{collection_id}")

    # --- Items (staged — draft/unpublished par défaut) ---

    def list_items(self, collection_id: str, *, offset: int = 0, limit: int = 100,
                    sort_by: Optional[str] = None, sort_order: Optional[str] = None,
                    cms_locale_id: Optional[str] = None, **filters) -> Dict:
        """Une page. `filters` = query params passthrough (name, slug, createdOn,
        lastPublished, lastUpdated — avec suffixe `[gte]`/`[lte]` côté appelant)."""
        params: Dict[str, Any] = {"offset": offset, "limit": min(limit, 100)}
        if sort_by:
            params["sortBy"] = sort_by
        if sort_order:
            params["sortOrder"] = sort_order
        if cms_locale_id:
            params["cmsLocaleId"] = cms_locale_id
        params.update(filters)
        return self._request("GET", f"collections/{collection_id}/items", params=params)

    def list_all_items(self, collection_id: str, *, cap: int = 500, **kwargs) -> List[Dict]:
        """Pagine `list_items` (page=100) jusqu'à épuisement ou `cap` items —
        évite qu'un appel agent énumère silencieusement une collection de 10k
        items d'un coup."""
        items: List[Dict] = []
        offset = 0
        while len(items) < cap:
            page = self.list_items(collection_id, offset=offset, limit=100, **kwargs)
            batch = page.get("items", [])
            if not batch:
                break
            items.extend(batch)
            total = page.get("pagination", {}).get("total", len(items))
            offset += len(batch)
            if offset >= total:
                break
        return items[:cap]

    def get_item(self, collection_id: str, item_id: str) -> Dict:
        return self._request("GET", f"collections/{collection_id}/items/{item_id}")

    def create_items(self, collection_id: str, items: List[Dict]) -> Dict:
        """`items` = liste de `{fieldData: {...}, isArchived?, isDraft?, cmsLocaleId?}`."""
        return self._request(
            "POST", f"collections/{collection_id}/items", json={"items": items},
        )

    def update_items(self, collection_id: str, items: List[Dict]) -> Dict:
        """`items` = liste de `{id, fieldData?, isArchived?, isDraft?}`."""
        return self._request(
            "PATCH", f"collections/{collection_id}/items", json={"items": items},
        )

    def delete_items(self, collection_id: str, item_ids: List[str]) -> Dict:
        return self._request(
            "DELETE", f"collections/{collection_id}/items",
            json={"items": [{"id": i} for i in item_ids]},
        )

    def publish_items(self, collection_id: str, item_ids: List[str]) -> Dict:
        """Fait passer des items STAGED en LIVE — le seul appel de ce client qui
        touche le site public."""
        return self._request(
            "POST", f"collections/{collection_id}/items/publish",
            json={"itemIds": item_ids},
        )

    # --- Webhooks ---
    #
    # Surface RÉELLE de l'API (vérifiée live 2026-08-20, pas seulement contre la
    # doc) : list + create sont scopés au SITE (`/sites/{id}/webhooks`), get +
    # delete sont scopés au WEBHOOK seul (`/webhooks/{id}`, pas de site_id dans
    # le chemin). AUCUN endpoint update/PATCH n'existe — reconfigurer un webhook
    # est delete + create. `filter` n'est accepté QUE pour
    # triggerType="form_submission" (400 `incompatible_webhook_filter` sinon,
    # confirmé live) — validé côté client pour épargner l'aller-retour.
    #
    # `secretKey` n'est renvoyé QU'À LA CRÉATION (absent de get/list, confirmé
    # live) — sers-toi-en pour vérifier les signatures `x-webflow-signature`
    # (HMAC-SHA256 de `f"{timestamp}:{body}"`), Webflow ne le remontre jamais.

    WEBHOOK_TRIGGER_TYPES = frozenset({
        "form_submission", "site_publish",
        "page_created", "page_metadata_updated", "page_deleted",
        "ecomm_new_order", "ecomm_order_changed", "ecomm_inventory_changed",
        "collection_item_created", "collection_item_changed",
        "collection_item_deleted", "collection_item_published",
        "collection_item_unpublished", "comment_created",
    })

    def list_webhooks(self) -> List[Dict]:
        return self._request(
            "GET", f"sites/{self.site_id}/webhooks").get("webhooks", [])

    def get_webhook(self, webhook_id: str) -> Dict:
        return self._request("GET", f"webhooks/{webhook_id}")

    def create_webhook(self, trigger_type: str, url: str,
                        filter: Optional[Dict] = None) -> Dict:
        """`filter` (form_submission uniquement) = `{"name": "<form name>"}`.
        La réponse porte `secretKey` en clair — UNE seule fois."""
        body: Dict[str, Any] = {"triggerType": trigger_type, "url": url}
        if filter is not None:
            body["filter"] = filter
        return self._request(
            "POST", f"sites/{self.site_id}/webhooks", json=body)

    def delete_webhook(self, webhook_id: str) -> None:
        self._request("DELETE", f"webhooks/{webhook_id}")

    # --- Forms & submissions ---
    #
    # Forme RÉELLE de l'API (vérifiée contre la doc source — `forms/forms/*` et
    # `forms/form-submissions/*`, pas `forms/submissions/*` qui 404) : lister
    # les FORMULAIRES est scopé site (`/sites/{id}/forms`), lister les
    # SOUMISSIONS d'UN formulaire est scopé aux deux (`/sites/{id}/forms/
    # {form_id}/submissions`), mais get/patch/delete D'UNE soumission ne
    # portent PLUS `form_id` dans le chemin (`/sites/{id}/form_submissions/
    # {submission_id}` — le tiret bas, pas le slash, contrairement à list).
    # AUCUNE création par API — une soumission n'existe que si un visiteur
    # remplit le formulaire côté site public.
    #
    # `update_submission` ne réécrit PAS les données soumises (le contenu du
    # formulaire n'est pas éditable après coup) : `formSubmissionData` ne
    # touche QUE les champs cachés (hidden fields) déclarés au schéma du
    # formulaire — un champ non déclaré comme hidden y est un no-op silencieux
    # côté Webflow, pas une erreur (pas de garde client possible sans le
    # schéma du formulaire en main : passer par `get_form` d'abord si le
    # champ cible doit être vérifié).

    def list_forms(self, *, offset: int = 0, limit: int = 100) -> Dict:
        return self._request(
            "GET", f"sites/{self.site_id}/forms",
            params={"offset": offset, "limit": min(limit, 100)})

    def get_form(self, form_id: str) -> Dict:
        return self._request("GET", f"forms/{form_id}")

    def list_form_submissions(self, form_id: str, *, offset: int = 0,
                               limit: int = 100) -> Dict:
        return self._request(
            "GET", f"sites/{self.site_id}/forms/{form_id}/submissions",
            params={"offset": offset, "limit": min(limit, 100)})

    def get_form_submission(self, submission_id: str) -> Dict:
        return self._request(
            "GET", f"sites/{self.site_id}/form_submissions/{submission_id}")

    def update_form_submission(self, submission_id: str,
                                form_submission_data: Dict) -> Dict:
        """`form_submission_data` touche UNIQUEMENT les hidden fields déclarés
        au schéma du formulaire — jamais les données soumises par le visiteur."""
        return self._request(
            "PATCH", f"sites/{self.site_id}/form_submissions/{submission_id}",
            json={"formSubmissionData": form_submission_data})

    def delete_form_submission(self, submission_id: str) -> None:
        self._request(
            "DELETE", f"sites/{self.site_id}/form_submissions/{submission_id}")

    # --- Pages ---
    #
    # Deux surfaces bien distinctes, vérifiées contre la doc source
    # (`pages-and-components/pages/*` — PAS `pages/*`, qui 404) :
    #
    # (1) MÉTADONNÉES (title/slug/seo/openGraph) — read+write, AUCUNE
    #     restriction de locale. `list` est scopée site, `get`/`update` sont
    #     scopées à la page seule (pas de site_id dans le chemin).
    #
    # (2) CONTENU STATIQUE (les text nodes — titres/paragraphes de la page) —
    #     `get_page_content` (lecture, `/pages/{id}/dom`) fonctionne SANS
    #     restriction (n'importe quelle locale, y compris la primaire/
    #     défaut). ⚠️ MAIS `update_page_content` (écriture, même endpoint en
    #     POST) est réservé aux locales SECONDAIRES — confirmé verbatim
    #     contre la doc : « This endpoint updates content on a static page in
    #     secondary locales » / « Ensure that the specified localeId is a
    #     valid secondary locale for the site otherwise the request will
    #     fail. » Sur un site MONO-locale (pas de locale secondaire
    #     configurée — le cas courant), il n'existe donc AUCUN chemin API
    #     pour éditer le corps d'une page statique : seule la lecture marche
    #     pleinement. `update_page_content` lève `ValueError` si appelée sans
    #     `locale_id` plutôt que de laisser un 400 Webflow opaque remonter —
    #     ce n'est pas un oubli de paramètre, c'est une contrainte structurelle
    #     de l'API qu'il faut nommer.

    def list_pages(self, *, offset: int = 0, limit: int = 100,
                    locale_id: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {"offset": offset, "limit": min(limit, 100)}
        if locale_id:
            params["localeId"] = locale_id
        return self._request(
            "GET", f"sites/{self.site_id}/pages", params=params)

    def get_page(self, page_id: str) -> Dict:
        """Métadonnées d'UNE page (title/slug/seo/openGraph) — pas son contenu
        (voir `get_page_content`)."""
        return self._request("GET", f"pages/{page_id}")

    def update_page(self, page_id: str, *, title: Optional[str] = None,
                     slug: Optional[str] = None, seo: Optional[Dict] = None,
                     open_graph: Optional[Dict] = None,
                     locale_id: Optional[str] = None) -> Dict:
        """Écrit UNIQUEMENT les métadonnées (title/slug/seo/openGraph) — jamais
        le contenu de la page (voir la restriction locale de
        `update_page_content`)."""
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if slug is not None:
            body["slug"] = slug
        if seo is not None:
            body["seo"] = seo
        if open_graph is not None:
            body["openGraph"] = open_graph
        params = {"localeId": locale_id} if locale_id else {}
        return self._request(
            "PUT", f"pages/{page_id}", json=body, params=params)

    def get_page_content(self, page_id: str, *, offset: int = 0,
                          limit: int = 100,
                          locale_id: Optional[str] = None) -> Dict:
        """Text nodes de la page — LECTURE seule fonctionne sur n'importe
        quelle locale (y compris la primaire), contrairement à l'écriture."""
        params: Dict[str, Any] = {"offset": offset, "limit": min(limit, 100)}
        if locale_id:
            params["localeId"] = locale_id
        return self._request(
            "GET", f"pages/{page_id}/dom", params=params)

    def update_page_content(self, page_id: str, nodes: List[Dict], *,
                             locale_id: str) -> Dict:
        """⚠️ `locale_id` DOIT être une locale SECONDAIRE du site (pas la
        primaire) — restriction Webflow, pas une omission de ce client :
        « Ensure that the specified localeId is a valid secondary locale for
        the site otherwise the request will fail. » Aucune valeur par défaut
        n'est devinée : un site sans locale secondaire configurée n'a AUCUN
        moyen d'écrire le contenu d'une page via l'API — seule sa lecture
        (`get_page_content`) fonctionne alors.

        `nodes` = liste de `{"nodeId": ..., "text": "<html>"}` (ou la forme
        propre au type de node — component instance/select/text input/
        submit/search button, cf. doc)."""
        if not locale_id:
            raise ValueError(
                "update_page_content requiert locale_id — Webflow ne permet "
                "d'écrire le contenu statique d'une page QUE sur une locale "
                "SECONDAIRE du site (jamais la primaire/défaut). Un site "
                "mono-locale (sans locale secondaire configurée) n'a aucun "
                "moyen d'éditer le corps d'une page via l'API — seule sa "
                "lecture (get_page_content) fonctionne alors.")
        return self._request(
            "POST", f"pages/{page_id}/dom", json={"nodes": nodes},
            params={"localeId": locale_id})

    def publish_site(self, *, custom_domains: Optional[List[str]] = None,
                      publish_to_webflow_subdomain: bool = False) -> Dict:
        """Publie le SITE ENTIER (toutes les pages) — à distinguer de
        `publish_items` (items CMS seuls). Rate-limité par Webflow à 1
        publish/minute. Au moins un des deux arguments doit désigner une
        cible réelle."""
        body: Dict[str, Any] = {}
        if custom_domains:
            body["customDomains"] = custom_domains
        if publish_to_webflow_subdomain:
            body["publishToWebflowSubdomain"] = True
        if not body:
            raise ValueError(
                "publish_site requiert custom_domains et/ou "
                "publish_to_webflow_subdomain=True — au moins une cible de "
                "publication doit être désignée.")
        return self._request(
            "POST", f"sites/{self.site_id}/publish", json=body)
