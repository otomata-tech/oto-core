"""
LightOn API client (v3, api.lighton.ai) — plateforme d'indexation documentaire
souveraine : ingestion (upload/parse/extract) + retrieval (search hybride,
ask RAG groundé).

⚠️ v3 = la plateforme cible (post-Paradigm). L'ancienne API v2
`paradigm.lighton.ai/api/v2` (applicatif Paradigm : chat alfred, query,
ask-question) est en cours de dépréciation — ce client ne la couvre plus.
La même clé Console (console.lighton.ai) vaut pour les deux.

Auth = Bearer. Base par défaut = `https://api.lighton.ai` ; une instance
privée/on-prem se cible via `base_url`.

Endpoints couverts (spec 3.12.0, developers.lighton.ai) :
- POST /api/v3/search           — retrieval hybride (dense + BM25 + rerank
                                  multivectoriel), mode vision, facettes
- POST /api/v3/ask              — RAG complet : search + réponse LLM groundée
- POST /api/v3/parse (+GET {id})— document → Markdown (sync/async)
- POST /api/v3/extract (+GET)   — extraction structurée par JSON Schema
- GET/POST /api/v3/files        — liste (filtres + recherche sémantique) / upload
- GET/DELETE /api/v3/files/{id} — fiche / suppression
- GET /api/v3/workspaces        — workspaces accessibles (manual ou synced
                                  SharePoint/Google Drive)

Facturation LightOn : ingestion par page, retrieval par requête (search/ask),
stockage vectoriel au Go — cf. lighton.ai/pricing.

Requires: requests
"""

from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret


class LightOnClient:
    """Client pour l'API LightOn v3 (indexation + retrieval documentaire)."""

    DEFAULT_BASE_URL = "https://api.lighton.ai"
    TIMEOUT = 60

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        """
        Args:
            api_key: clé API LightOn (Bearer, créée sur console.lighton.ai).
                À défaut, lue de l'env `LIGHTON_API_KEY`.
            base_url: base de l'API pour une instance privée/on-prem
                (défaut = SaaS `https://api.lighton.ai`).
        """
        self.api_key = api_key or require_secret("LIGHTON_API_KEY")
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        files: Optional[dict] = None,
        data: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        url = f"{self.base_url}/api/v3/{endpoint.lstrip('/')}"
        headers = self._headers()
        if json is not None:
            headers["Content-Type"] = "application/json"
        resp = requests.request(
            method, url, headers=headers, json=json, params=params,
            files=files, data=data, timeout=timeout or self.TIMEOUT,
        )
        if not resp.ok:
            # LightOn renvoie {"code", "error", "detail"} (ou un dict de
            # validation DRF) — surfacer le plus parlant.
            try:
                body = resp.json()
                if isinstance(body, dict):
                    msg = body.get("detail") or body.get("error") or _json.dumps(body)
                else:
                    msg = resp.text
            except Exception:
                msg = resp.text
            raise RuntimeError(f"LightOn {resp.status_code}: {msg}")
        if not resp.content:
            return None
        return resp.json()

    # ---- retrieval ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        workspace_ids: Optional[List[int]] = None,
        tag_ids: Optional[List[int]] = None,
        file_ids: Optional[List[int]] = None,
        max_results: Optional[int] = None,
        mode: Optional[str] = None,
        relevance_scoring: Optional[str] = None,
        content_type: Optional[List[str]] = None,
        attribute: Optional[List[str]] = None,
        include_image: bool = False,
        include_bboxes: bool = False,
    ) -> Dict[str, Any]:
        """Retrieval de chunks (hybride dense + BM25, rerank multivectoriel).
        1 crédit retrieval par requête. Pas de génération LLM.

        Args:
            query: requête en langage naturel (max 1500 caractères).
            workspace_ids / tag_ids / file_ids: scoping (file_ids exclusif
                des deux autres). Sans filtre = tout le corpus autorisé.
            max_results: nombre de chunks après rerank (1-50).
            mode: `text` (défaut) ou `vision` (pages images VLM).
            relevance_scoring: `scoring_and_filtering` (défaut) /
                `scoring_only` / `none`.
            content_type / attribute: filtres facettes (cf. doc LightOn).
            include_image: joint l'image de page en base64 par résultat.
            include_bboxes: joint les bounding boxes PDF par résultat.
        """
        payload: Dict[str, Any] = {"query": query}
        if workspace_ids:
            payload["workspace_id"] = workspace_ids
        if tag_ids:
            payload["tag_id"] = tag_ids
        if file_ids:
            payload["file_id"] = file_ids
        if max_results is not None:
            payload["max_results"] = max_results
        if mode:
            payload["mode"] = mode
        if relevance_scoring:
            payload["relevance_scoring"] = relevance_scoring
        if content_type:
            payload["content_type"] = content_type
        if attribute:
            payload["attribute"] = attribute
        if include_image:
            payload["include_image"] = True
        if include_bboxes:
            payload["include_bboxes"] = True
        return self._request("POST", "search", json=payload)

    def ask(
        self,
        query: str,
        *,
        workspace_ids: Optional[List[int]] = None,
        tag_ids: Optional[List[int]] = None,
        file_ids: Optional[List[int]] = None,
        max_results: Optional[int] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """RAG complet : search sur le corpus puis réponse LLM groundée dans
        les passages retrouvés (avec provenance). Mode synchrone (pas de SSE).

        Args:
            query: question en langage naturel (max 1500 caractères).
            workspace_ids / tag_ids / file_ids: scoping (mêmes règles que
                `search`).
            max_results: nombre de chunks de contexte (1-50).
            model: LLM de génération (ex. `mistral-large-latest`) — défaut
                plateforme si omis.
        """
        payload: Dict[str, Any] = {"query": query, "stream": False}
        if workspace_ids:
            payload["workspace_id"] = workspace_ids
        if tag_ids:
            payload["tag_id"] = tag_ids
        if file_ids:
            payload["file_id"] = file_ids
        if max_results is not None:
            payload["max_results"] = max_results
        if model:
            payload["model"] = model
        return self._request("POST", "ask", json=payload, timeout=120)

    # ---- parse / extract (traitement de document, hors index) ---------------

    def parse_bytes(
        self, data: bytes, filename: str, *, async_: bool = False,
    ) -> Dict[str, Any]:
        """Parse un document (upload direct) → Markdown structuré.

        Args:
            async_: True = job async (gros fichiers, 202 + job id à poller
                via `parse_job`). Sync : ~20 MB / 15 pages max.
        """
        form = {"options": _json.dumps({"async": True})} if async_ else None
        return self._request(
            "POST", "parse", files={"file": (filename, data)}, data=form,
            timeout=300,
        )

    def parse_url(self, document_url: str, *, async_: bool = False) -> Dict[str, Any]:
        """Parse un document accessible par URL publique → Markdown."""
        payload: Dict[str, Any] = {"document": document_url}
        if async_:
            payload["options"] = {"async": True}
        return self._request("POST", "parse", json=payload, timeout=300)

    def parse_job(self, job_id: str) -> Dict[str, Any]:
        """Statut/résultat d'un job de parse async."""
        return self._request("GET", f"parse/{job_id}")

    def extract_bytes(
        self, data: bytes, filename: str, schema: dict, *, async_: bool = False,
    ) -> Dict[str, Any]:
        """Extraction structurée : sort les champs décrits par un JSON Schema
        depuis un document (upload direct).

        Args:
            schema: JSON Schema objet des champs à extraire.
            async_: True = job async (poll via `extract_job`).
        """
        form: Dict[str, Any] = {"schema": _json.dumps(schema)}
        if async_:
            form["options"] = _json.dumps({"async": True})
        return self._request(
            "POST", "extract", files={"file": (filename, data)}, data=form,
            timeout=300,
        )

    def extract_url(
        self, document_url: str, schema: dict, *, async_: bool = False,
    ) -> Dict[str, Any]:
        """Extraction structurée depuis un document accessible par URL."""
        payload: Dict[str, Any] = {"document": document_url, "schema": schema}
        if async_:
            payload["options"] = {"async": True}
        return self._request("POST", "extract", json=payload, timeout=300)

    def extract_job(self, job_id: str) -> Dict[str, Any]:
        """Statut/résultat d'un job d'extract async (`ext_…`)."""
        return self._request("GET", f"extract/{job_id}")

    # ---- files (l'index) ----------------------------------------------------

    def list_files(
        self,
        *,
        workspace_ids: Optional[List[int]] = None,
        tag_ids: Optional[List[int]] = None,
        search: Optional[str] = None,
        status: Optional[str] = None,
        filename: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Documents indexés accessibles à la clé (paginé).

        Args:
            workspace_ids / tag_ids: filtres.
            search: recherche sémantique — les résultats sont ordonnés par
                pertinence (léger « find my doc » sans passer par `search`).
            status: filtre statut d'ingestion (ex. `pending,embedded`).
            filename: filtre par nom (partiel, insensible à la casse).
        """
        params: Dict[str, Any] = {}
        if workspace_ids:
            params["workspace_id"] = ",".join(str(w) for w in workspace_ids)
        if tag_ids:
            params["tag_id"] = ",".join(str(t) for t in tag_ids)
        if search:
            params["search"] = search
        if status:
            params["status"] = status
        if filename:
            params["filename"] = filename
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["page_size"] = page_size
        return self._request("GET", "files", params=params or None)

    def get_file(self, file_id: int) -> Dict[str, Any]:
        """Fiche d'un document (métadonnées, statut d'ingestion)."""
        return self._request("GET", f"files/{file_id}")

    def upload_file_bytes(
        self,
        data: bytes,
        filename: str,
        workspace_id: int,
        *,
        title: Optional[str] = None,
        tag_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Upload + indexation d'un document dans un workspace (multipart).
        Facturé à la page ingérée.

        Args:
            workspace_id: workspace de destination (REQUIS en v3).
            title: titre affiché (défaut = filename sans extension).
            tag_ids: tags à poser à la création.
        """
        form: Dict[str, Any] = {"workspace_id": str(workspace_id)}
        if title:
            form["title"] = title
        if tag_ids:
            form["tags"] = [str(t) for t in tag_ids]
        return self._request(
            "POST", "files", files={"file": (filename, data)}, data=form,
            timeout=300,
        )

    def delete_file(self, file_id: int) -> None:
        """Supprime définitivement un document et son index."""
        return self._request("DELETE", f"files/{file_id}")

    # ---- workspaces ---------------------------------------------------------

    def list_workspaces(
        self,
        *,
        name: Optional[str] = None,
        workspace_type: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Workspaces accessibles à la clé (⚠️ endpoint marqué alpha par
        LightOn). `workspace_type`: shared | personal | public."""
        params: Dict[str, Any] = {}
        if name:
            params["name"] = name
        if workspace_type:
            params["workspace_type"] = workspace_type
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["page_size"] = page_size
        return self._request("GET", "workspaces", params=params or None)
