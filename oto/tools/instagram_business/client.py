"""Instagram Business — publication de contenu & insights via la Graph API Meta.

Cible : un compte **Instagram Business ou Creator** lié à une Page Facebook.
Couvre la **publication** (image / reel / carousel / story) et les **insights**
(compte + média) — PAS la messagerie (les DM passent par le connecteur Unipile
`instagram_*`).

Auth = **access token** (long-lived user/page token, scopes `instagram_basic`,
`instagram_content_publish`, `instagram_manage_insights`) + l'**IG user id** du
compte business (le « IG User ID » numérique, ≠ l'identifiant Page Facebook).
Les deux passés au constructeur (ou `IG_BUSINESS_ACCESS_TOKEN` /
`IG_BUSINESS_USER_ID` en fallback).

Publication = flux en 2 temps de la Graph API : on crée d'abord un **conteneur**
média (`POST /{ig-user-id}/media`) puis on le **publie** (`POST /{ig-user-id}/
media_publish`). Les images se publient de façon synchrone ; la vidéo / le reel
est traité de façon asynchrone côté Meta → créer le conteneur, **sonder le
statut** (`status_code=FINISHED`) avant de publier.

Docs : https://developers.facebook.com/docs/instagram-platform/content-publishing

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream


class InstagramBusinessClient:
    """Client Graph API — publication & insights d'un compte Instagram Business."""

    DEFAULT_API_VERSION = "v21.0"

    def __init__(
        self,
        access_token: Optional[str] = None,
        ig_user_id: Optional[str] = None,
        api_version: Optional[str] = None,
    ):
        """Initialise le client.

        Args:
            access_token: token Graph API (ou env `IG_BUSINESS_ACCESS_TOKEN`).
            ig_user_id: IG User ID du compte business (ou env `IG_BUSINESS_USER_ID`).
            api_version: version de la Graph API (défaut `v21.0`).
        """
        self.access_token = access_token or require_secret("IG_BUSINESS_ACCESS_TOKEN")
        self.ig_user_id = str(ig_user_id or require_secret("IG_BUSINESS_USER_ID"))
        self.api_version = api_version or self.DEFAULT_API_VERSION
        self.base_url = f"https://graph.facebook.com/{self.api_version}"
        self.session = requests.Session()

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 data: Optional[Dict[str, Any]] = None) -> Any:
        params = dict(params or {})
        params["access_token"] = self.access_token
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.request(method, url, params=params, data=data, timeout=60)
        raise_for_upstream(resp, service="instagram")
        return resp.json() if resp.content else {}

    # --- conteneurs (étape 1 de la publication) -----------------------------

    def create_media_container(
        self,
        *,
        image_url: Optional[str] = None,
        video_url: Optional[str] = None,
        media_type: Optional[str] = None,
        caption: Optional[str] = None,
        location_id: Optional[str] = None,
        user_tags: Optional[List[Dict[str, Any]]] = None,
        is_carousel_item: bool = False,
        children: Optional[List[str]] = None,
        share_to_feed: Optional[bool] = None,
        thumb_offset: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Crée un conteneur média (étape 1). Renvoie `{"id": <creation_id>}`.

        `media_type` : None (image simple), `REELS` (vidéo/reel), `STORIES`
        (story), `CAROUSEL` (album, nécessite `children` = ids de conteneurs
        enfants). Pour un enfant de carousel, poser `is_carousel_item=True`.
        """
        data: Dict[str, Any] = {}
        if image_url:
            data["image_url"] = image_url
        if video_url:
            data["video_url"] = video_url
        if media_type:
            data["media_type"] = media_type
        if caption is not None:
            data["caption"] = caption
        if location_id:
            data["location_id"] = location_id
        if user_tags is not None:
            import json
            data["user_tags"] = json.dumps(user_tags)
        if is_carousel_item:
            data["is_carousel_item"] = "true"
        if children:
            data["children"] = ",".join(children)
        if share_to_feed is not None:
            data["share_to_feed"] = "true" if share_to_feed else "false"
        if thumb_offset is not None:
            data["thumb_offset"] = thumb_offset
        return self._request("POST", f"{self.ig_user_id}/media", data=data)

    def container_status(self, creation_id: str) -> Dict[str, Any]:
        """Statut d'un conteneur média. `status_code` ∈ EXPIRED | ERROR |
        FINISHED | IN_PROGRESS | PUBLISHED. Publier seulement sur FINISHED."""
        return self._request(
            "GET", str(creation_id),
            params={"fields": "status_code,status"})

    # --- publication (étape 2) ----------------------------------------------

    def publish_container(self, creation_id: str) -> Dict[str, Any]:
        """Publie un conteneur préalablement créé. Renvoie `{"id": <media_id>}`."""
        return self._request(
            "POST", f"{self.ig_user_id}/media_publish",
            data={"creation_id": str(creation_id)})

    def publish_image(self, image_url: str, caption: Optional[str] = None) -> Dict[str, Any]:
        """Raccourci synchrone : crée le conteneur image PUIS le publie.
        Renvoie le média publié `{"id": <media_id>}`."""
        container = self.create_media_container(image_url=image_url, caption=caption)
        return self.publish_container(container["id"])

    # --- lecture média ------------------------------------------------------

    def list_media(self, limit: int = 25, fields: Optional[str] = None) -> Dict[str, Any]:
        """Liste les médias publiés du compte (paginé)."""
        return self._request(
            "GET", f"{self.ig_user_id}/media",
            params={
                "limit": limit,
                "fields": fields or "id,caption,media_type,media_url,permalink,timestamp",
            })

    def get_media(self, media_id: str, fields: Optional[str] = None) -> Dict[str, Any]:
        """Détail d'un média publié."""
        return self._request(
            "GET", str(media_id),
            params={
                "fields": fields or
                "id,caption,media_type,media_url,permalink,timestamp,"
                "like_count,comments_count",
            })

    # --- insights -----------------------------------------------------------

    def account_insights(
        self,
        metrics: List[str],
        period: str = "day",
        *,
        metric_type: Optional[str] = None,
        since: Optional[int] = None,
        until: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Insights du **compte** (`GET /{ig-user-id}/insights`).

        Args:
            metrics: métriques, ex. `reach`, `impressions`, `profile_views`,
                `follower_count`, `accounts_engaged`.
            period: `day` | `week` | `days_28` | `lifetime`.
            metric_type: `total_value` pour les métriques modernes (reach…).
            since/until: bornes epoch (UNIX) optionnelles.
        """
        params: Dict[str, Any] = {"metric": ",".join(metrics), "period": period}
        if metric_type:
            params["metric_type"] = metric_type
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        return self._request("GET", f"{self.ig_user_id}/insights", params=params)

    def media_insights(self, media_id: str, metrics: Optional[List[str]] = None) -> Dict[str, Any]:
        """Insights d'un **média** publié (`GET /{ig-media-id}/insights`).
        Métriques par défaut : reach, likes, comments, saved, shares."""
        ms = metrics or ["reach", "likes", "comments", "saved", "shares"]
        return self._request(
            "GET", f"{media_id}/insights",
            params={"metric": ",".join(ms)})
