"""Comptes Unipile & lien d'auth hébergée.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests

from ..const import _REQUEST_TIMEOUT
from ..errors import UnipileError


class _AccountsMixin:
    """Comptes Unipile & lien d'auth hébergée."""

    def list_accounts(self) -> list[dict]:
        data = self._request("GET", "/accounts")
        if isinstance(data, dict):
            return data.get("data") or data.get("items") or []
        return data or []

    def delete_account(self, account_id: str) -> None:
        """Retire un compte de l'instance Unipile — c'est ce qui LIBÈRE le siège
        facturé (une déconnexion côté oto ne fait que dénouer le binding, le siège
        continue de courir).

        ⚠️ IRRÉVERSIBLE : la session hébergée est détruite. Une reconnexion de la
        même personne repartira d'un `account_id` NEUF — donc l'historique de
        propriété côté appelant (bindings morts) ne rebindera plus ce compte.
        204 attendu ; un id inconnu remonte en `UnipileError` 404."""
        self._request("DELETE", f"/accounts/{quote(account_id, safe='')}")

    def account_id(self) -> str:
        """`account_id` LinkedIn : celui fourni, sinon le 1er compte LinkedIn du compte
        Unipile.

        ⚠️ La casse du provider a CHANGÉ en v2 : un compte porte `provider:"linkedin"`
        (minuscules) et **plus de champ `type`** (champs v2 relevés en live :
        application_id, created_at, id, is_locked, metadata, name, object, provider,
        proxy, status, user_id). L'ancien test `== "LINKEDIN"` ne pouvait donc plus
        JAMAIS être vrai → la découverte automatique tombait toujours dans le « aucun
        compte LinkedIn connecté » alors qu'un compte opérationnel existait : un
        diagnostic qui MENT coûte des heures. Comparaison insensible à la casse, sur
        `provider` (v2) avec repli `type` (v1), et le message d'échec ÉNUMÈRE les
        providers réellement vus."""
        if self._account_id:
            return self._account_id
        seen: list[str] = []
        for acc in self.list_accounts():
            if not isinstance(acc, dict):
                continue
            provider = str(acc.get("provider") or acc.get("type") or "").strip()
            if provider:
                seen.append(provider)
            if provider.lower() == "linkedin" and acc.get("id"):
                self._account_id = str(acc["id"])
                return self._account_id
        inventory = (f" Comptes connectés : {', '.join(sorted(set(seen)))}."
                     if seen else " Aucun compte connecté sur cette clé Unipile.")
        raise UnipileError(
            "Aucun compte LinkedIn connecté sur Unipile "
            "(et UNIPILE_LINKEDIN_ACCOUNT_ID non défini)." + inventory
        )

    def account_alive(self, account_id: str) -> bool:
        """La SESSION du compte est-elle vivante ? `GET /v2/{account_id}/users/me` :
        200 = utilisable, 401 = déconnecté (checkpoint / login avorté / cookie mort).
        Distinct de `status:'running'` du compte, qui peut mentir sur un compte
        mort-né (wizard abandonné). Sert à ne binder qu'un compte RÉELLEMENT
        utilisable (un compte mort-né préféré à l'ancien sain = incident vécu)."""
        try:
            resp = self.session.request(
                "GET", f"{self.base_url}/{quote(account_id, safe='')}/users/me",
                timeout=_REQUEST_TIMEOUT)
        except requests.RequestException:
            return False
        return resp.status_code == 200

    # ---- hosted auth -----------------------------------------------------

    # Produits LinkedIn activables au lien hosted-auth (`config.linkedin.products`).
    # `classic` = la base, toujours incluse. Les deux PREMIUM sont EXCLUSIFS : un
    # compte ne peut en activer qu'UN (contrainte Unipile documentée).
    LINKEDIN_PREMIUM_PRODUCTS = ("recruiter", "sales_navigator")

    def hosted_auth_link(
        self,
        notify_url: Optional[str] = None,
        providers: Optional[list[str]] = None,
        name: Optional[str] = None,
        success_redirect_url: Optional[str] = None,
        failure_redirect_url: Optional[str] = None,
        ttl_minutes: int = 60,
        premium: Optional[str] = None,
        allow_cookies: bool = False,
        reconnect_account: Optional[str] = None,
    ) -> str:
        """URL d'auth hébergée (v2 : `POST /v2/auth/link`, createAuthLink).

        Schéma v2 : `expires_on` (snake) ; `providers` = liste de codes
        **minuscules** (`["linkedin"]`) ou `"*"` (tous) ; **un seul** `redirect_uri`
        (v2 ne sépare plus succès/échec) ; la réponse porte le lien sur **`link`**.
        `name`/`notify_url` restent acceptés (corrélation webhook du hosted-auth #131).

        ⚠️ **C'est à l'app d'activer les produits premium** : sans
        `config.linkedin.products`, Unipile ne connecte que `classic` → les
        endpoints Recruiter/Sales Navigator répondent 403 « out of your scope » et
        le wizard n'offre AUCUNE case premium (confirmé par le support Unipile).
        - `premium` : `"recruiter"` | `"sales_navigator"` | None. **Exclusifs** — un
          compte ne peut activer qu'un seul des deux.
        - `allow_cookies` : ajoute la connexion par cookies aux méthodes du wizard
          (sans lui, seul identifiant/mot de passe est proposé). **Recommandé par
          Unipile pour les produits premium.**
        - `reconnect_account` : `account_id` d'un compte EXISTANT → `type=reconnect`
          (rattache le produit/répare la session SUR ce compte) au lieu de `create`
          (qui ferait un DOUBLON). À utiliser pour activer un premium sur un compte
          déjà connecté."""
        expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        body: dict[str, Any] = {
            "type": "reconnect" if reconnect_account else "create",
            "providers": [str(p).lower() for p in providers] if providers else "*",
            "api_url": f"https://{self.dsn}",
            "expires_on": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        # v2 = un seul redirect_uri (l'échec n'a plus d'URL dédiée) ; on prend le
        # succès, sinon l'échec en repli.
        redirect = success_redirect_url or failure_redirect_url
        if redirect:
            body["redirect_uri"] = redirect
        if reconnect_account:
            body["reconnect_account"] = reconnect_account
        if notify_url:
            body["notify_url"] = notify_url
        if name:
            body["name"] = name
        # config.linkedin : produits à activer + méthodes de connexion offertes.
        # N'est posé que si on demande quelque chose de non-défaut (sinon Unipile
        # garde son comportement d'origine : classic + credentials).
        if premium or allow_cookies:
            if premium and premium not in self.LINKEDIN_PREMIUM_PRODUCTS:
                raise UnipileError(
                    f"premium invalide : {premium!r} (attendu "
                    f"{' ou '.join(map(repr, self.LINKEDIN_PREMIUM_PRODUCTS))}). "
                    "Un compte ne peut activer qu'UN produit premium."
                )
            cfg: dict[str, Any] = {}
            if premium:
                cfg["products"] = ["classic", premium]
            if allow_cookies:
                cfg["allow_methods"] = ["credentials", "cookies"]
            body["config"] = {"linkedin": cfg}
        data = self._request("POST", "/auth/link", json=body)
        return (data or {}).get("link") or (data or {}).get("url", "")

