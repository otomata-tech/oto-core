"""Appels et réunions Leexi — lecture, transcripts, et cycle d'import.

Ce mixin n'est jamais instancié seul : il est composé dans `LeexiClient`, qui
fournit le transport (`_request`, `_list`, `_check_choice`).

⚠️ **Ce que ces méthodes rendent dépend de la *call access scope* de la clé**
(toute l'entreprise / l'accès d'un utilisateur / des règles d'accès). Hors
périmètre, un appel n'est pas listé et répond **404** en direct : un 404 sur
`get_call` ne signifie donc pas « n'existe pas ».
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...common import raise_for_upstream
from ..const import (CALL_DATE_FILTERS, CALL_ORDERS, HTTP_TIMEOUT)


class _CallsMixin:
    """Appels et réunions."""

    def list_calls(self, page: Optional[int] = None, items: Optional[int] = None,
                   order: Optional[str] = None, date_filter: Optional[str] = None,
                   date_from: Optional[str] = None, date_to: Optional[str] = None,
                   source: Optional[str] = None,
                   source_id: Optional[Any] = None,
                   owner_uuid: Optional[Any] = None,
                   participating_user_uuid: Optional[Any] = None,
                   conversation_type_uuid: Optional[str] = None,
                   customer_phone_number: Optional[Any] = None,
                   customer_email_address: Optional[Any] = None,
                   with_simple_transcript: Optional[bool] = None) -> Any:
        """GET /v1/calls — appels et réunions DANS LE PÉRIMÈTRE de la clé. Scope `read_calls`.

        Une liste vide est un réglage possible (clé dont le périmètre ne couvre
        aucun appel), pas une anomalie.

        `date_from`/`date_to` bornent le champ nommé par `date_filter` (défaut
        `created_at`). Ils portent ce nom préfixé parce que `from` est un mot
        réservé de Python ; ils partent bien en `from`/`to` sur le fil.

        `with_simple_transcript=True` joint le transcript à granularité paragraphe
        — la réponse en devient nettement plus lourde, page par page.

        Cinq filtres sont multi-valués (`source_id`, `owner_uuid`,
        `participating_user_uuid`, `customer_phone_number`,
        `customer_email_address`) : passer une liste, le transport pose les
        crochets `[]` attendus par l'amont.
        """
        self._check_choice("order", order, CALL_ORDERS)
        self._check_choice("date_filter", date_filter, CALL_DATE_FILTERS)
        return self._list("/calls", page, items, {
            "order": order, "date_filter": date_filter,
            "from": date_from, "to": date_to,
            "source": source, "source_id": source_id,
            "owner_uuid": owner_uuid,
            "participating_user_uuid": participating_user_uuid,
            "conversation_type_uuid": conversation_type_uuid,
            "customer_phone_number": customer_phone_number,
            "customer_email_address": customer_email_address,
            "with_simple_transcript": with_simple_transcript,
        })

    def get_call(self, uuid: str) -> Any:
        """GET /v1/calls/{uuid} — un appel, **avec ses topics et son transcript**.

        Scope `read_calls`. Mêmes attributs que la liste, plus `simple_transcript`
        (horodatage au paragraphe) et `transcript` (horodatage au MOT).

        ⚠️ **404 = hors périmètre de la clé**, pas nécessairement inexistant.
        """
        return self._request("GET", f"/calls/{uuid}")

    def presign_recording_url(self, extension: str) -> Any:
        """POST /v1/calls/presign_recording_url — URL de téléversement. Scope `write_calls`.

        Premier temps du cycle d'import : rend l'URL et **les en-têtes** à rejouer
        pour un PUT en une seule partie (cf. `upload_recording`), plus la clé de
        stockage à passer ensuite en `recording_s3_key` à `create_call`. Le fichier
        téléversé expire au bout de **3 jours** s'il ne sert à aucun appel.
        """
        return self._request("POST", "/calls/presign_recording_url",
                             json={"extension": extension})

    def upload_recording(self, presigned: Dict[str, Any], data: Any,
                         timeout: Any = None) -> int:
        """PUT du fichier sur l'URL pré-signée. **Hors API Leexi** (stockage objet).

        `presigned` = la réponse de `presign_recording_url` telle quelle. Ses
        en-têtes sont rejoués À L'IDENTIQUE : ils sont signés avec l'URL, donc en
        changer un — ou en ajouter un — invalide la signature et le stockage
        répond 403.

        ⚠️ Requête faite **hors session** (`requests.put`, pas `self.session`) : la
        session porte l'en-tête `Authorization` de Leexi, qui n'a rien à faire chez
        le stockage objet et y casserait justement la signature. Rend le status du PUT.
        """
        if not isinstance(presigned, dict):
            raise ValueError(
                "`presigned` doit être la réponse de `presign_recording_url` "
                f"(un objet), reçu {type(presigned).__name__}.")
        url = presigned.get("url") or presigned.get("presigned_url")
        if not url:
            raise ValueError(
                "réponse de `presign_recording_url` sans `url` : "
                f"clés reçues {sorted(presigned)}")
        headers = presigned.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError("`headers` de la réponse pré-signée doit être un objet.")
        resp = requests.put(url, data=data, headers=headers,
                            timeout=timeout or HTTP_TIMEOUT)
        raise_for_upstream(resp, service="leexi (stockage)")
        return resp.status_code

    def create_call(self, payload: Dict[str, Any]) -> Any:
        """POST /v1/calls — crée un appel **de façon asynchrone**. Scope `write_calls`.

        Requis : `direction`, `external_id`, `performed_at`, `recording_s3_key`,
        `user_uuid`. Optionnels : `customers`, `description`, `emails`, `locale`,
        `participating_user_uuids`, `raw_phone_number`, `tags`, `title`.

        ⚠️ Le téléversement doit être **terminé** avant cet appel (cf.
        `presign_recording_url` puis `upload_recording`). Le traitement prend
        typiquement quelques minutes, et les complétions de prompt (résumé,
        chapitrage) n'arrivent qu'ENSUITE : relire l'appel plus tard, et ne pas
        conclure de leur absence immédiate qu'elles manquent.

        ⚠️ Rate limit propre et bas : **10 requêtes/minute** (contre 50 ailleurs).
        """
        return self._request("POST", "/calls", json=dict(payload))
