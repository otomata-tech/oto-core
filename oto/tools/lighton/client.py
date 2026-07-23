"""
LightOn Paradigm API client — plateforme GenAI souveraine (modèles hébergés en
Europe + base documentaire RAG d'entreprise).

API REST synchrone (docs.lighton.ai). Auth = clé API en Bearer
(`Authorization: Bearer <key>`). Base par défaut = l'instance SaaS publique
`https://paradigm.lighton.ai/api/v2` ; une instance Paradigm privée/on-prem se
cible via `base_url`.

Endpoints couverts :
- GET  /models                    — modèles configurés sur l'instance
- POST /chat/completions          — chat completion (OpenAI-compatible)
- POST /query                     — extraction de chunks de la base documentaire (RAG)
- GET  /files                     — documents accessibles (scopes privé/société/workspace)
- GET  /files/{id}                — fiche d'un document
- POST /files                     — upload d'un document (multipart)
- POST /files/{id}/ask-question   — réponse générée sur UN document
- DELETE /files/{id}              — suppression d'un document

Requires: requests
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import requests

from ...config import require_secret


class LightOnClient:
    """Client pour l'API Paradigm de LightOn (chat + base documentaire)."""

    DEFAULT_BASE_URL = "https://paradigm.lighton.ai/api/v2"
    TIMEOUT = 60

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        """
        Args:
            api_key: clé API Paradigm (Bearer). À défaut, lue de l'env
                `LIGHTON_API_KEY`.
            base_url: base de l'API pour une instance Paradigm privée
                (ex. `https://paradigm.acme.fr/api/v2`). Défaut = SaaS public.
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
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._headers()
        if json is not None:
            headers["Content-Type"] = "application/json"
        resp = requests.request(
            method, url, headers=headers, json=json, params=params,
            files=files, data=data, timeout=timeout or self.TIMEOUT,
        )
        if not resp.ok:
            # Paradigm renvoie {"code", "error", "detail"} — surfacer le detail.
            try:
                body = resp.json()
                msg = body.get("detail") or body.get("error") or resp.text
            except Exception:
                msg = resp.text
            raise RuntimeError(f"LightOn {resp.status_code}: {msg}")
        if not resp.content:
            return None
        return resp.json()

    # ---- modèles ------------------------------------------------------------

    def list_models(self) -> Dict[str, Any]:
        """Modèles configurés sur l'instance. Réponse brute Paradigm
        (`data[]` — inclut des champs de template de prompt très verbeux)."""
        return self._request("GET", "models")

    # ---- chat (OpenAI-compatible) -------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stop: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Chat completion (OpenAI-compatible, sans streaming).

        Args:
            messages: liste de messages `{role, content}` (system/user/assistant).
            model: nom d'un modèle de l'instance (cf. `list_models`).
        """
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if stop:
            payload["stop"] = stop
        return self._request("POST", "chat/completions", json=payload,
                             timeout=120)

    # ---- base documentaire (RAG) --------------------------------------------

    def query(
        self,
        query: Union[str, List[str]],
        *,
        collection: Optional[str] = None,
        n: int = 5,
    ) -> Dict[str, Any]:
        """Extrait les chunks les plus pertinents de la base documentaire
        (recherche sémantique RAG).

        Args:
            query: requête (ou liste de requêtes) en langage naturel.
            collection: collection à interroger (défaut Paradigm =
                `base_collection`).
            n: nombre de chunks par requête (défaut 5).
        """
        payload: Dict[str, Any] = {"query": query, "n": n}
        if collection:
            payload["collection"] = collection
        return self._request("POST", "query", json=payload)

    def list_files(
        self,
        *,
        private_scope: Optional[bool] = None,
        company_scope: Optional[bool] = None,
        workspace_scope: Optional[int] = None,
        page: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Documents accessibles à la clé (paginé).

        Args:
            private_scope: inclure la collection privée de l'utilisateur.
            company_scope: inclure la collection société.
            workspace_scope: inclure les documents du workspace donné (id).
            page: numéro de page.
        """
        params: Dict[str, Any] = {}
        if private_scope is not None:
            params["private_scope"] = private_scope
        if company_scope is not None:
            params["company_scope"] = company_scope
        if workspace_scope is not None:
            params["workspace_scope"] = workspace_scope
        if page is not None:
            params["page"] = page
        return self._request("GET", "files", params=params or None)

    def get_file(self, file_id: int) -> Dict[str, Any]:
        """Fiche d'un document (métadonnées, statut d'ingestion)."""
        return self._request("GET", f"files/{file_id}")

    def upload_file_bytes(
        self,
        data: bytes,
        filename: str,
        *,
        collection_type: Optional[str] = None,
        workspace_id: Optional[int] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload d'un document dans la base documentaire (multipart).

        Args:
            data: contenu binaire du fichier.
            filename: nom de fichier (l'extension détermine le parsing).
            collection_type: `private` (défaut Paradigm), `company` ou
                `workspace`.
            workspace_id: requis si `collection_type='workspace'`.
            title: titre affiché (défaut = filename).
        """
        form: Dict[str, Any] = {}
        if collection_type:
            form["collection_type"] = collection_type
        if workspace_id is not None:
            form["workspace_id"] = str(workspace_id)
        if title:
            form["title"] = title
        return self._request(
            "POST", "files",
            files={"file": (filename, data)},
            data=form or None,
            timeout=180,
        )

    def ask_document(self, file_id: int, question: str) -> Dict[str, Any]:
        """Réponse générée sur le contenu d'UN document (question/réponse)."""
        return self._request(
            "POST", f"files/{file_id}/ask-question",
            json={"question": question},
            timeout=120,
        )

    def delete_file(self, file_id: int) -> None:
        """Supprime un document de la base documentaire."""
        return self._request("DELETE", f"files/{file_id}")
