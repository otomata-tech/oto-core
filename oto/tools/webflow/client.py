"""Webflow Data API v2 Client — https://developers.webflow.com/data/reference

Scope (v1): CMS only — site (read), collections (read), items (CRUD on the
*staged* items, i.e. draft/unpublished by default) + publish. No pages,
assets, forms, or ecommerce endpoints here.

Auth = Webflow **Site API token** (generated per-site in Site Settings →
Apps & Integrations → API access, scoped with `cms:read`/`cms:write`).
Site tokens are bound to exactly one site (confirmed against
developers.webflow.com/data/v2.0.0/reference/authentication/site-token —
"Site tokens are created per site") — so `site_id` is a constructor arg,
not a per-call parameter, and there is no multi-site discovery endpoint
worth calling per request.

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
        self.site_id = site_id or require_secret("WEBFLOW_SITE_ID")

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
