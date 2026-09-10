"""Réseau & outreach : relations et invitations.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from ..const import cursor_with_limit
from ..errors import UnipileError


# Curseur SYNTHÉTIQUE des invitations : `off:<offset>`.
#
# `relation-requests` est paginé par OFFSET côté Unipile, pas par curseur — il
# ne rend donc JAMAIS de `next_cursor`. Pour garder au tool son contrat
# (« rappelle-moi avec le cursor rendu »), on FABRIQUE ce jeton et on le
# redécode à l'entrée : il n'est JAMAIS transmis en amont. Le préfixe le rend
# lisible en log et empêche toute collision si Unipile finissait par en rendre
# un vrai (auquel cas l'amont gagne, cf. `list_invitations`).
_INV_CURSOR = "off:"

# Plafond OBSERVÉ (2026-09-10) de `limit` sur relation-requests : 100 passe,
# 101 et 200 rendent `Unipile 400: Invalid querystring` — un message qui ne
# nomme ni le param fautif ni la borne. Unipile ne le documente pas (l'OpenAPI
# v2 dit `default: 20, minimum: 1`, sans maximum : « depends on the
# provider »). On trie donc ICI, pour rendre une erreur qui se lit.
_INV_LIMIT_MAX = 100


def _invitations_offset(cursor: Optional[str]) -> int:
    """Décode un curseur d'invitations FABRIQUÉ par nous → offset.

    Tout autre curseur est refusé ICI plutôt que transmis : passé en amont il
    déclenchait le 400 « Unexpected parameters: type » (cf.
    `list_invitations`), illisible pour l'appelant."""
    if not cursor:
        return 0
    if cursor.startswith(_INV_CURSOR):
        raw = cursor[len(_INV_CURSOR):]
        if raw.isdigit():
            return int(raw)
    raise UnipileError(
        "list_invitations : curseur invalide. Cet endpoint est paginé par "
        "offset — ne repasse QUE le `cursor` rendu par l'appel précédent "
        f"(forme `{_INV_CURSOR}<n>`), ou `offset=` directement."
    )


class _NetworkMixin:
    """Réseau & outreach : relations et invitations."""

    def list_relations(self, cursor: Optional[str] = None,
                       limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            # Le limit de l'appel prime sur celui figé dans le cursor (#179).
            params["cursor"] = cursor_with_limit(cursor, limit) if limit else cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/users/me/relations"), params=params
        ))

    def list_invitations(self, direction: str = "received",
                         limit: Optional[int] = None,
                         cursor: Optional[str] = None,
                         offset: Optional[int] = None) -> dict:
        """Invitations — v2 : `GET /v2/{account}/users/me/relation-requests`,
        `type=sent|received` (param REQUIS côté Unipile).

        ⚠️ Cet endpoint est paginé par `offset`, PAS par curseur. Unipile :
        « Pagination for this endpoint works with the `offset` parameter. » Il
        ne rend donc jamais de `next_cursor`, et il REFUSE tout param autre que
        `limit`/`meta_only` à côté d'un `cursor` :

            Unipile 400: When cursor is provided, only "limit" and "meta_only"
            are allowed alongside it. Unexpected parameters: type.

        `type` étant OBLIGATOIRE, envoyer un `cursor` était une impasse : la
        page 1 passait, toute page suivante 400ait — la pagination des
        invitations était morte au-delà du premier écran, sans que rien ne le
        signale côté schéma (le tool annonçait « Paginé »). On pagine donc par
        `offset` et on FABRIQUE le curseur rendu (`off:<n>`, cf.
        `_invitations_offset`) pour garder au tool son contrat.

        Avance de `limit` par page — contrat Unipile : « increment the offset
        by the limit » — et s'arrête quand `data` est VIDE, pas sur une page
        courte : le provider peut filtrer des items DANS la fenêtre, et
        avancer de `len(data)` re-servirait alors les mêmes. Pour un export
        exhaustif, déduplique quand même par `id`."""
        if offset is None:
            offset = _invitations_offset(cursor)
        if limit is not None and not 1 <= limit <= _INV_LIMIT_MAX:
            raise UnipileError(
                f"list_invitations : limit doit être entre 1 et "
                f"{_INV_LIMIT_MAX} (reçu {limit}). Au-delà, Unipile rend un "
                "« Invalid querystring » qui ne nomme pas la borne."
            )
        params: dict[str, Any] = {
            "type": "sent" if direction == "sent" else "received"
        }
        if limit:
            params["limit"] = limit
        if offset:
            params["offset"] = offset
        out = self._norm(self._request(
            "GET", self._acct("/users/me/relation-requests"), params=params
        ))
        # Curseur fabriqué UNIQUEMENT si l'amont n'en rend pas (aujourd'hui il
        # n'en rend jamais) : le jour où Unipile en rend un vrai, il gagne.
        # Page vide = fin de liste → pas de curseur, l'appelant s'arrête.
        if isinstance(out, dict) and not out.get("next_cursor"):
            page = out.get("data")
            if isinstance(page, list) and page:
                nxt = f"{_INV_CURSOR}{offset + (limit or len(page))}"
                out["next_cursor"] = nxt
                out["cursor"] = nxt
        return out

    def send_invitation(self, provider_id: str,
                        message: Optional[str] = None) -> dict:
        """v2 : `POST /users/me/relation-requests`, corps `{user_id, message}`."""
        body: dict[str, Any] = {"user_id": provider_id}
        if message:
            body["message"] = message
        return self._request(
            "POST", self._acct("/users/me/relation-requests"), json=body
        )

    def handle_invitation(
        self, invitation_id: str, shared_secret: str, action: str = "accept"
    ) -> dict:
        """Accepte/refuse une invitation REÇUE. v2 : `request_id` suffit (plus de
        `shared_secret`, gardé dans la signature pour compat appelant). accept →
        `/accept` ; decline → `/cancel`."""
        if action not in ("accept", "decline"):
            raise UnipileError("handle_invitation : action = 'accept' ou 'decline'.")
        verb = "accept" if action == "accept" else "cancel"
        return self._request(
            "POST",
            self._acct(
                f"/users/me/relation-requests/{quote(invitation_id, safe='')}/{verb}"
            ),
        )

    def cancel_invitation(self, invitation_id: str) -> dict:
        """Annule une invitation ENVOYÉE. v2 : `/relation-requests/{id}/cancel`."""
        return self._request(
            "POST",
            self._acct(
                f"/users/me/relation-requests/{quote(invitation_id, safe='')}/cancel"
            ),
        )

