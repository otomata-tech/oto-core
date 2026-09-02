"""Issues GitHub — tickets, commentaires, étiquettes, jalons, assignations.

Ce mixin n'est jamais instancié seul : il est composé dans `GitHubClient`, qui
fournit le transport (`_request`, `_get`, `_check_choice`).

⚠️ **Chez GitHub, une pull request EST une issue.** `GET /repos/…/issues` rend
donc AUSSI les PR, chacune portant une clé `pull_request`. C'est le piège le plus
courant de cette API : compter les issues d'un dépôt sans filtrer donne un nombre
faux, souvent de beaucoup. `list_issues(include_pull_requests=False)` — le
défaut — écarte les PR côté client, puisque l'amont n'offre aucun filtre pour ça.

Conséquence symétrique, et utile : les endpoints de commentaire, d'étiquette et
d'assignation d'ISSUE fonctionnent tels quels sur une PR, en passant son numéro.
C'est voulu côté GitHub, et c'est pourquoi `pulls.py` ne les redéclare pas.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..const import ISSUE_SORTS, ISSUE_STATE_WRITES, ISSUE_STATES, SORT_DIRECTIONS


class _IssuesMixin:
    """Issues, commentaires, étiquettes, jalons."""

    # --- issues -------------------------------------------------------------

    def list_issues(self, owner: str, repo: str, state: Optional[str] = None,
                    labels: Optional[Any] = None,
                    assignee: Optional[str] = None,
                    creator: Optional[str] = None,
                    mentioned: Optional[str] = None,
                    milestone: Optional[Any] = None,
                    since: Optional[str] = None, sort: Optional[str] = None,
                    direction: Optional[str] = None,
                    include_pull_requests: bool = False,
                    per_page: Optional[int] = None,
                    page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/issues — issues du dépôt.

        ⚠️ **L'amont rend AUSSI les pull requests** (chez GitHub, une PR est une
        issue). `include_pull_requests=False` (le défaut) les écarte ICI, faute
        de filtre côté serveur. Le mettre à `True` rend la réponse brute de
        l'API — utile pour compter « tickets + PR » comme le fait l'interface.

        ⚠️ Ce filtrage est fait **après pagination** : une page de 30 lignes dont
        12 sont des PR en rend 18. C'est inévitable sans filtre amont, et c'est
        la raison de plus de boucler avec `iterate` plutôt que de lire une page.

        `labels` accepte une liste (jointe par des virgules). `state` vaut
        `open` (défaut GitHub), `closed` ou `all`.
        """
        self._check_choice("state", state, ISSUE_STATES)
        self._check_choice("sort", sort, ISSUE_SORTS)
        self._check_choice("direction", direction, SORT_DIRECTIONS)
        payload = self._get(f"/repos/{owner}/{repo}/issues", {
            "state": state, "labels": labels, "assignee": assignee,
            "creator": creator, "mentioned": mentioned, "milestone": milestone,
            "since": since, "sort": sort, "direction": direction},
            per_page, page)
        if include_pull_requests or not isinstance(payload, list):
            return payload
        return [row for row in payload
                if not (isinstance(row, dict) and row.get("pull_request"))]

    def get_issue(self, owner: str, repo: str, number: Any) -> Any:
        """GET /repos/{owner}/{repo}/issues/{number} — une issue.

        Rend aussi une **pull request** si le numéro en désigne une : les deux
        partagent la même numérotation dans un dépôt.
        """
        return self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")

    def create_issue(self, owner: str, repo: str,
                     payload: Dict[str, Any]) -> Any:
        """POST /repos/{owner}/{repo}/issues — crée une issue.

        Requis : `title`. Optionnels : `body`, `assignees`, `labels`,
        `milestone`.

        ⚠️ **Notifie** : les personnes assignées, les abonnés au dépôt et toute
        personne mentionnée dans `body` reçoivent une notification. Ce n'est pas
        un brouillon — GitHub n'en a pas pour les issues.
        """
        if not payload.get("title"):
            raise ValueError("`title` requis pour créer une issue.")
        return self._request("POST", f"/repos/{owner}/{repo}/issues",
                             json=dict(payload))

    def update_issue(self, owner: str, repo: str, number: Any,
                     payload: Dict[str, Any]) -> Any:
        """PATCH /repos/{owner}/{repo}/issues/{number} — met à jour une issue.

        Champs : `title`, `body`, `state` (`open`/`closed`), `state_reason`
        (`completed`/`not_planned`/`reopened`), `assignees`, `labels`,
        `milestone`.

        ⚠️ `labels` et `assignees` **REMPLACENT** les listes existantes, ils ne
        les enrichissent pas. Pour ajouter sans écraser : `add_labels` /
        `add_assignees`.
        """
        self._check_choice("state", payload.get("state"), ISSUE_STATE_WRITES)
        return self._request("PATCH",
                             f"/repos/{owner}/{repo}/issues/{number}",
                             json=dict(payload))

    def lock_issue(self, owner: str, repo: str, number: Any,
                   lock_reason: Optional[str] = None) -> Any:
        """PUT /repos/{owner}/{repo}/issues/{number}/lock — verrouille la conversation.

        `lock_reason` : `off-topic`, `too heated`, `resolved`, `spam`.
        """
        body = {"lock_reason": lock_reason} if lock_reason else None
        return self._request("PUT",
                             f"/repos/{owner}/{repo}/issues/{number}/lock",
                             json=body)

    def unlock_issue(self, owner: str, repo: str, number: Any) -> Any:
        """DELETE /repos/{owner}/{repo}/issues/{number}/lock — déverrouille."""
        return self._request("DELETE",
                             f"/repos/{owner}/{repo}/issues/{number}/lock")

    # --- commentaires --------------------------------------------------------

    def list_issue_comments(self, owner: str, repo: str, number: Any,
                            since: Optional[str] = None,
                            per_page: Optional[int] = None,
                            page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/issues/{number}/comments — commentaires.

        Marche aussi sur une pull request (même numérotation) : ce sont les
        commentaires du FIL, distincts des commentaires de revue ligne à ligne
        (`list_review_comments`).
        """
        return self._get(f"/repos/{owner}/{repo}/issues/{number}/comments",
                         {"since": since}, per_page, page)

    def create_issue_comment(self, owner: str, repo: str, number: Any,
                             body: str) -> Any:
        """POST /repos/{owner}/{repo}/issues/{number}/comments — commente.

        ⚠️ **Notifie** les personnes abonnées au fil. Marche aussi sur une PR.
        """
        if not body:
            raise ValueError("`body` requis : un commentaire vide est refusé.")
        return self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body})

    def update_issue_comment(self, owner: str, repo: str, comment_id: Any,
                             body: str) -> Any:
        """PATCH /repos/{owner}/{repo}/issues/comments/{id} — édite un commentaire.

        ⚠️ Le chemin porte l'id du COMMENTAIRE, pas le numéro de l'issue.
        """
        return self._request(
            "PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body})

    def delete_issue_comment(self, owner: str, repo: str,
                             comment_id: Any) -> Any:
        """DELETE /repos/{owner}/{repo}/issues/comments/{id} — supprime un commentaire.

        ⚠️ Définitif, sans corbeille.
        """
        return self._request(
            "DELETE", f"/repos/{owner}/{repo}/issues/comments/{comment_id}")

    # --- étiquettes -----------------------------------------------------------

    def list_labels(self, owner: str, repo: str,
                    per_page: Optional[int] = None,
                    page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/labels — étiquettes définies dans le dépôt."""
        return self._get(f"/repos/{owner}/{repo}/labels", None, per_page, page)

    def create_label(self, owner: str, repo: str, name: str, color: str,
                     description: Optional[str] = None) -> Any:
        """POST /repos/{owner}/{repo}/labels — crée une étiquette.

        `color` est un hexadécimal **sans `#`** (ex. `"d73a4a"`) : GitHub refuse
        le croisillon.
        """
        if color.startswith("#"):
            raise ValueError(
                "`color` s'écrit sans `#` (ex. 'd73a4a') — GitHub refuse le "
                "croisillon.")
        body: Dict[str, Any] = {"name": name, "color": color}
        if description is not None:
            body["description"] = description
        return self._request("POST", f"/repos/{owner}/{repo}/labels", json=body)

    def add_labels(self, owner: str, repo: str, number: Any,
                   labels: List[str]) -> Any:
        """POST /repos/{owner}/{repo}/issues/{number}/labels — AJOUTE des étiquettes.

        Contrairement à `update_issue(labels=…)`, qui remplace la liste.
        """
        if not labels:
            raise ValueError("`labels` requis : au moins une étiquette.")
        return self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/labels",
            json={"labels": list(labels)})

    def set_labels(self, owner: str, repo: str, number: Any,
                   labels: List[str]) -> Any:
        """PUT /repos/{owner}/{repo}/issues/{number}/labels — REMPLACE les étiquettes.

        Une liste vide les retire toutes.
        """
        return self._request(
            "PUT", f"/repos/{owner}/{repo}/issues/{number}/labels",
            json={"labels": list(labels)})

    def remove_label(self, owner: str, repo: str, number: Any,
                     label: str) -> Any:
        """DELETE /repos/{owner}/{repo}/issues/{number}/labels/{label} — en retire une."""
        return self._request(
            "DELETE", f"/repos/{owner}/{repo}/issues/{number}/labels/{label}")

    # --- assignation -----------------------------------------------------------

    def add_assignees(self, owner: str, repo: str, number: Any,
                      assignees: List[str]) -> Any:
        """POST /repos/{owner}/{repo}/issues/{number}/assignees — assigne.

        ⚠️ GitHub **ignore en silence** un compte qui n'a pas accès en écriture
        au dépôt : la réponse revient 201 sans l'avoir assigné. Comparer la liste
        rendue à celle demandée pour le voir.
        """
        return self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/assignees",
            json={"assignees": list(assignees)})

    def remove_assignees(self, owner: str, repo: str, number: Any,
                         assignees: List[str]) -> Any:
        """DELETE /repos/{owner}/{repo}/issues/{number}/assignees — désassigne."""
        return self._request(
            "DELETE", f"/repos/{owner}/{repo}/issues/{number}/assignees",
            json={"assignees": list(assignees)})

    # --- jalons ----------------------------------------------------------------

    def list_milestones(self, owner: str, repo: str,
                        state: Optional[str] = None,
                        per_page: Optional[int] = None,
                        page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/milestones — jalons du dépôt."""
        self._check_choice("state", state, ISSUE_STATES)
        return self._get(f"/repos/{owner}/{repo}/milestones", {"state": state},
                         per_page, page)

    def create_milestone(self, owner: str, repo: str, title: str,
                         payload: Optional[Dict[str, Any]] = None) -> Any:
        """POST /repos/{owner}/{repo}/milestones — crée un jalon.

        Optionnels : `state`, `description`, `due_on`.
        """
        body: Dict[str, Any] = {"title": title}
        body.update(payload or {})
        return self._request("POST", f"/repos/{owner}/{repo}/milestones",
                             json=body)
