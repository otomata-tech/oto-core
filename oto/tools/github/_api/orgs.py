"""Organisations GitHub — membres, équipes, collaborateurs de dépôt.

Ce mixin n'est jamais instancié seul : il est composé dans `GitHubClient`, qui
fournit le transport (`_request`, `_get`, `_check_choice`).

⚠️ **Trois notions d'appartenance se ressemblent et ne sont pas la même chose :**

- **membre d'une ORGANISATION** (`/orgs/{org}/members`) — appartenance globale,
  qui peut être publique ou privée ;
- **membre d'une ÉQUIPE** (`/orgs/{org}/teams/{slug}/members`) — sous-ensemble,
  qui porte des droits sur les dépôts de l'équipe ;
- **collaborateur d'un DÉPÔT** (`/repos/{owner}/{repo}/collaborators`) — accès à
  un dépôt précis, sans appartenance à l'organisation.

Retirer quelqu'un de l'un ne le retire pas des autres, et c'est la source
d'erreur la plus fréquente ici — d'où trois familles de méthodes nommées d'après
la portée, jamais un `remove_member` générique.

⚠️ **`list_members` ne montre par défaut que ce que le jeton a le droit de
voir.** Un jeton sans le scope d'organisation ne verra que les membres *publics*
— une liste plus courte, sans erreur. Elle n'est donc pas un recensement.

**Délibérément absent** : la suppression d'une organisation, et la gestion des
GitHub Apps installées.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ...common import raise_for_upstream
from ..const import (COLLABORATOR_PERMISSIONS, MEMBER_FILTERS,
                     MEMBERSHIP_ROLES, TEAM_ROLES)


class _OrgsMixin:
    """Organisations, équipes, membres, collaborateurs."""

    # --- identité --------------------------------------------------------------

    def me(self) -> Any:
        """GET /user — le compte porteur du jeton.

        **Aucun scope particulier requis** : c'est la sonde d'authentification du
        connecteur. Un 401 dit que le jeton est mauvais ou révoqué ; une réponse
        dit qui il est, sans rien prouver de ses droits.
        """
        return self._request("GET", "/user")

    def rate_limit(self) -> Any:
        """GET /rate_limit — l'état des quotas, **sans les consommer**.

        Le seul endpoint qui ne compte pas dans la limite primaire : utile pour
        expliquer un 403 sans aggraver la situation.
        """
        return self._request("GET", "/rate_limit")

    def list_my_orgs(self, per_page: Optional[int] = None,
                     page: Optional[int] = None) -> Any:
        """GET /user/orgs — organisations du porteur du jeton.

        ⚠️ Un jeton classique sans le scope `read:org` rend une liste **vide**
        plutôt qu'une erreur : l'absence n'y prouve pas la non-appartenance.
        """
        return self._get("/user/orgs", None, per_page, page)

    # --- organisations ----------------------------------------------------------

    def get_org(self, org: str) -> Any:
        """GET /orgs/{org} — la fiche d'une organisation."""
        return self._request("GET", f"/orgs/{org}")

    def list_org_members(self, org: str, filter: Optional[str] = None,
                         role: Optional[str] = None,
                         per_page: Optional[int] = None,
                         page: Optional[int] = None) -> Any:
        """GET /orgs/{org}/members — membres de l'organisation.

        `role` restreint aux `admin` (propriétaires) ou aux `member`.
        `filter="2fa_disabled"` liste ceux sans double authentification —
        réservé aux propriétaires.

        ⚠️ Sans droits suffisants, seuls les membres **publics** sont rendus.
        """
        self._check_choice("filter", filter, MEMBER_FILTERS)
        self._check_choice("role", role, MEMBERSHIP_ROLES)
        return self._get(f"/orgs/{org}/members", {"filter": filter,
                                                  "role": role},
                         per_page, page)

    def check_org_membership(self, org: str, username: str) -> bool:
        """GET /orgs/{org}/members/{username} — cette personne est-elle membre ?

        ⚠️ Endpoint sans corps : **204 si membre, 404 sinon** (et 302 si le jeton
        n'a pas le droit de savoir). Le 404 est une RÉPONSE, pas une erreur —
        d'où ce booléen.
        """
        resp = self._request("GET", f"/orgs/{org}/members/{username}", raw=True)
        if resp.status_code == 204:
            return True
        if resp.status_code in (404, 302):
            return False
        raise_for_upstream(resp, service="github")
        return False

    def get_org_membership(self, org: str, username: str) -> Any:
        """GET /orgs/{org}/memberships/{username} — l'appartenance détaillée.

        Contrairement à `check_org_membership`, rend le rôle et l'état
        (`active` / `pending` — une invitation non acceptée).
        """
        return self._request("GET", f"/orgs/{org}/memberships/{username}")

    def set_org_membership(self, org: str, username: str,
                           role: Optional[str] = None) -> Any:
        """PUT /orgs/{org}/memberships/{username} — invite ou change un rôle.

        ⚠️ **Envoie une invitation par email** si la personne n'est pas déjà
        membre, et son état reste `pending` tant qu'elle n'a pas accepté. Sur un
        membre existant, change son rôle (`admin` = propriétaire de
        l'organisation, un droit très large).
        """
        self._check_choice("role", role, MEMBERSHIP_ROLES)
        body = {"role": role} if role else None
        return self._request("PUT", f"/orgs/{org}/memberships/{username}",
                             json=body)

    def remove_org_member(self, org: str, username: str) -> Any:
        """DELETE /orgs/{org}/members/{username} — **retire de l'organisation**.

        ⚠️ Retire la personne de l'organisation ET de toutes ses équipes, et lui
        fait perdre l'accès aux dépôts privés. Ne supprime pas ses contributions.
        Ceci ne la retire PAS des dépôts où elle est collaboratrice à titre
        individuel : voir `remove_collaborator`.
        """
        return self._request("DELETE", f"/orgs/{org}/members/{username}")

    # --- équipes ------------------------------------------------------------------

    def list_teams(self, org: str, per_page: Optional[int] = None,
                   page: Optional[int] = None) -> Any:
        """GET /orgs/{org}/teams — équipes visibles de l'organisation."""
        return self._get(f"/orgs/{org}/teams", None, per_page, page)

    def get_team(self, org: str, team_slug: str) -> Any:
        """GET /orgs/{org}/teams/{team_slug} — une équipe.

        ⚠️ La clé est le **slug** (dans l'URL), pas le nom affiché.
        """
        return self._request("GET", f"/orgs/{org}/teams/{team_slug}")

    def list_team_members(self, org: str, team_slug: str,
                          role: Optional[str] = None,
                          per_page: Optional[int] = None,
                          page: Optional[int] = None) -> Any:
        """GET /orgs/{org}/teams/{team_slug}/members — membres d'une équipe.

        `role` : `member` ou `maintainer`.
        """
        self._check_choice("role", role, TEAM_ROLES)
        return self._get(f"/orgs/{org}/teams/{team_slug}/members",
                         {"role": role}, per_page, page)

    def list_team_repos(self, org: str, team_slug: str,
                        per_page: Optional[int] = None,
                        page: Optional[int] = None) -> Any:
        """GET /orgs/{org}/teams/{team_slug}/repos — dépôts gérés par l'équipe."""
        return self._get(f"/orgs/{org}/teams/{team_slug}/repos", None,
                         per_page, page)

    def add_team_member(self, org: str, team_slug: str, username: str,
                        role: Optional[str] = None) -> Any:
        """PUT /orgs/{org}/teams/{team_slug}/memberships/{username} — ajoute à l'équipe.

        ⚠️ La personne doit **déjà être membre de l'organisation** ; sinon, cet
        appel lui envoie une invitation à la rejoindre, et l'appartenance reste
        `pending`.
        """
        self._check_choice("role", role, TEAM_ROLES)
        body = {"role": role} if role else None
        return self._request(
            "PUT", f"/orgs/{org}/teams/{team_slug}/memberships/{username}",
            json=body)

    def remove_team_member(self, org: str, team_slug: str,
                           username: str) -> Any:
        """DELETE /orgs/{org}/teams/{team_slug}/memberships/{username} — retire de l'ÉQUIPE.

        ⚠️ Ne retire PAS de l'organisation : la personne garde son appartenance
        globale et les accès qui en découlent.
        """
        return self._request(
            "DELETE", f"/orgs/{org}/teams/{team_slug}/memberships/{username}")

    # --- collaborateurs de dépôt ---------------------------------------------------

    def list_collaborators(self, owner: str, repo: str,
                           affiliation: Optional[str] = None,
                           permission: Optional[str] = None,
                           per_page: Optional[int] = None,
                           page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/collaborators — qui a accès à ce dépôt.

        `affiliation` : `outside`, `direct` ou `all` (défaut) — « direct » exclut
        les accès hérités d'une équipe, ce qui est souvent la vraie question.
        """
        self._check_choice("permission", permission, COLLABORATOR_PERMISSIONS)
        return self._get(f"/repos/{owner}/{repo}/collaborators",
                         {"affiliation": affiliation, "permission": permission},
                         per_page, page)

    def check_collaborator(self, owner: str, repo: str, username: str) -> bool:
        """GET /repos/{owner}/{repo}/collaborators/{username} — a-t-elle accès ?

        ⚠️ Endpoint sans corps : **204 si oui, 404 si non**. Le 404 est une
        réponse, pas une erreur.
        """
        resp = self._request(
            "GET", f"/repos/{owner}/{repo}/collaborators/{username}", raw=True)
        if resp.status_code == 204:
            return True
        if resp.status_code == 404:
            return False
        raise_for_upstream(resp, service="github")
        return False

    def get_collaborator_permission(self, owner: str, repo: str,
                                    username: str) -> Any:
        """GET /repos/{owner}/{repo}/collaborators/{username}/permission — son niveau.

        Rend le niveau EFFECTIF, héritages d'équipe compris — ce que
        `list_collaborators(affiliation="direct")` ne dirait pas.
        """
        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/collaborators/{username}/permission")

    def add_collaborator(self, owner: str, repo: str, username: str,
                         permission: Optional[str] = None) -> Any:
        """PUT /repos/{owner}/{repo}/collaborators/{username} — invite au dépôt.

        ⚠️ **Envoie une invitation** : l'accès n'est effectif qu'une fois
        acceptée (la réponse porte alors l'invitation, pas un accès actif).
        `permission` : `pull`, `triage`, `push`, `maintain`, `admin`.
        """
        self._check_choice("permission", permission, COLLABORATOR_PERMISSIONS)
        body = {"permission": permission} if permission else None
        return self._request(
            "PUT", f"/repos/{owner}/{repo}/collaborators/{username}", json=body)

    def remove_collaborator(self, owner: str, repo: str,
                            username: str) -> Any:
        """DELETE /repos/{owner}/{repo}/collaborators/{username} — retire du DÉPÔT.

        ⚠️ Ne retire pas de l'organisation, et **ne retire pas un accès hérité
        d'une équipe** : si la personne a le dépôt par son équipe, elle le garde.
        Vérifier avec `get_collaborator_permission` après coup.
        """
        return self._request(
            "DELETE", f"/repos/{owner}/{repo}/collaborators/{username}")
