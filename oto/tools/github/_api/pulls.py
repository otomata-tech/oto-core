"""Pull requests GitHub — propositions, revues, fusion.

Ce mixin n'est jamais instancié seul : il est composé dans `GitHubClient`, qui
fournit le transport (`_request`, `_get`, `_check_choice`).

⚠️ **Une PR est aussi une issue** : ses commentaires de FIL, ses étiquettes, ses
jalons et ses assignations passent par les méthodes d'`issues.py`, avec le numéro
de la PR. Ce module ne porte que ce qui lui est propre — le diff, les revues, les
commentaires de revue (ligne à ligne), les relecteurs demandés, et la fusion.

⚠️ **`merge_pull` modifie la branche cible, et n'est pas annulable d'un clic.**
Les trois méthodes n'ont pas le même effet sur l'historique : `merge` ajoute un
commit de fusion, `squash` écrase la branche en un seul commit, `rebase` réécrit
les commits. Le choix appartient à l'appelant, et le connecteur ne le devine pas.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...common import raise_for_upstream
from ..const import (MERGE_METHODS, PULL_SORTS, PULL_STATES, REVIEW_EVENTS,
                     SORT_DIRECTIONS)


class _PullsMixin:
    """Pull requests, revues, fusion."""

    # --- pull requests --------------------------------------------------------

    def list_pulls(self, owner: str, repo: str, state: Optional[str] = None,
                   head: Optional[str] = None, base: Optional[str] = None,
                   sort: Optional[str] = None, direction: Optional[str] = None,
                   per_page: Optional[int] = None,
                   page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/pulls — pull requests du dépôt.

        `state` vaut `open` (défaut GitHub), `closed` ou `all`. `head` filtre par
        branche source (`utilisateur:branche`), `base` par branche cible.

        ⚠️ Une PR **fusionnée** est `closed` : il n'existe pas d'état `merged`.
        Pour les distinguer, lire `merged_at` (nul = fermée sans fusion).
        """
        self._check_choice("state", state, PULL_STATES)
        self._check_choice("sort", sort, PULL_SORTS)
        self._check_choice("direction", direction, SORT_DIRECTIONS)
        return self._get(f"/repos/{owner}/{repo}/pulls", {
            "state": state, "head": head, "base": base,
            "sort": sort, "direction": direction}, per_page, page)

    def get_pull(self, owner: str, repo: str, number: Any) -> Any:
        """GET /repos/{owner}/{repo}/pulls/{number} — une PR, avec ses compteurs.

        Cette forme (contrairement à la liste) porte `mergeable`, `merged`,
        `additions`, `deletions`, `changed_files`.

        ⚠️ **`mergeable` peut valoir `null`** : GitHub calcule la fusionnabilité
        en tâche de fond au premier appel. `null` veut dire « pas encore su » —
        redemander, et surtout ne pas le lire comme « non fusionnable ».
        """
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def create_pull(self, owner: str, repo: str,
                    payload: Dict[str, Any]) -> Any:
        """POST /repos/{owner}/{repo}/pulls — ouvre une pull request.

        Requis : `title` (ou `issue`), `head`, `base`. Optionnels : `body`,
        `draft`, `maintainer_can_modify`.

        `head` est la branche source (`utilisateur:branche` depuis un fork),
        `base` la branche cible. `draft=True` ouvre un brouillon, qui ne demande
        pas de revue tant qu'il n'est pas marqué prêt.

        ⚠️ **Notifie** les propriétaires de code (CODEOWNERS) et les abonnés du
        dépôt, sauf en brouillon.
        """
        for champ in ("head", "base"):
            if not payload.get(champ):
                raise ValueError(f"`{champ}` requis pour ouvrir une PR.")
        if not payload.get("title") and not payload.get("issue"):
            raise ValueError(
                "`title` requis (ou `issue`, pour convertir une issue "
                "existante en PR).")
        return self._request("POST", f"/repos/{owner}/{repo}/pulls",
                             json=dict(payload))

    def update_pull(self, owner: str, repo: str, number: Any,
                    payload: Dict[str, Any]) -> Any:
        """PATCH /repos/{owner}/{repo}/pulls/{number} — met à jour une PR.

        Champs : `title`, `body`, `state` (`open`/`closed`), `base`,
        `maintainer_can_modify`.

        ⚠️ On ne FUSIONNE pas par ici : `state="closed"` ferme sans fusionner.
        La fusion est `merge_pull`.
        """
        return self._request("PATCH", f"/repos/{owner}/{repo}/pulls/{number}",
                             json=dict(payload))

    def list_pull_files(self, owner: str, repo: str, number: Any,
                        per_page: Optional[int] = None,
                        page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/pulls/{number}/files — fichiers modifiés + patch.

        ⚠️ **Plafonné à 3 000 fichiers**, et le `patch` de chaque fichier est
        omis au-delà d'une certaine taille. Une PR massive est donc rendue
        incomplète, sans erreur.
        """
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}/files", None,
                         per_page, page)

    def list_pull_commits(self, owner: str, repo: str, number: Any,
                          per_page: Optional[int] = None,
                          page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/pulls/{number}/commits — commits de la PR.

        ⚠️ Plafonné à 250 commits ; au-delà, passer par l'API des commits du dépôt.
        """
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}/commits", None,
                         per_page, page)

    def check_pull_merged(self, owner: str, repo: str, number: Any) -> bool:
        """GET /repos/{owner}/{repo}/pulls/{number}/merge — la PR est-elle fusionnée ?

        ⚠️ Endpoint sans corps : GitHub répond **204 si fusionnée, 404 sinon**.
        Le 404 est ici une RÉPONSE, pas une erreur — d'où ce booléen, plutôt que
        de laisser un `UpstreamHTTPError` remonter pour dire « non ».
        """
        resp = self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}/merge",
                             raw=True)
        if resp.status_code == 204:
            return True
        if resp.status_code == 404:
            return False
        raise_for_upstream(resp, service="github")
        return False

    def merge_pull(self, owner: str, repo: str, number: Any,
                   commit_title: Optional[str] = None,
                   commit_message: Optional[str] = None,
                   sha: Optional[str] = None,
                   merge_method: Optional[str] = None) -> Any:
        """PUT /repos/{owner}/{repo}/pulls/{number}/merge — **FUSIONNE la PR**.

        ⚠️ Écriture sur la branche cible, non annulable d'un clic. Les trois
        méthodes ne font pas la même chose à l'historique :
        `merge` ajoute un commit de fusion, `squash` écrase la branche en un seul
        commit, `rebase` réécrit les commits sur la cible.

        `sha` est une **protection contre la course** : si la tête de la PR a
        bougé depuis la lecture, GitHub refuse (409) au lieu de fusionner autre
        chose que ce qui a été relu. Le passer quand la décision de fusion
        s'appuie sur un diff déjà lu.

        Refus courants : **405** (non fusionnable — conflits, contrôles en
        échec), **409** (la tête a bougé, ou `sha` obsolète).
        """
        self._check_choice("merge_method", merge_method, MERGE_METHODS)
        body: Dict[str, Any] = {}
        for key, value in (("commit_title", commit_title),
                           ("commit_message", commit_message),
                           ("sha", sha), ("merge_method", merge_method)):
            if value is not None:
                body[key] = value
        return self._request("PUT",
                             f"/repos/{owner}/{repo}/pulls/{number}/merge",
                             json=body or None)

    def update_pull_branch(self, owner: str, repo: str, number: Any,
                           expected_head_sha: Optional[str] = None) -> Any:
        """PUT /repos/{owner}/{repo}/pulls/{number}/update-branch — rebase la cible dedans.

        Met à jour la branche de la PR avec les derniers commits de sa base. Rend
        **202** (accepté, traité en tâche de fond) : le travail n'est pas fini
        quand la réponse arrive.
        """
        body = ({"expected_head_sha": expected_head_sha}
                if expected_head_sha else None)
        return self._request(
            "PUT", f"/repos/{owner}/{repo}/pulls/{number}/update-branch",
            json=body)

    # --- revues ---------------------------------------------------------------

    def list_reviews(self, owner: str, repo: str, number: Any,
                     per_page: Optional[int] = None,
                     page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/pulls/{number}/reviews — revues déposées."""
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}/reviews", None,
                         per_page, page)

    def create_review(self, owner: str, repo: str, number: Any,
                      payload: Dict[str, Any]) -> Any:
        """POST /repos/{owner}/{repo}/pulls/{number}/reviews — dépose une revue.

        Champs : `body`, `event` (`APPROVE` | `REQUEST_CHANGES` | `COMMENT`),
        `commit_id`, `comments` (commentaires ligne à ligne).

        ⚠️ **`event` absent laisse la revue en ATTENTE** (`PENDING`) : rien n'est
        publié, personne n'est notifié, et elle reste visible de son seul auteur.
        C'est utile pour préparer, et c'est un piège quand on croyait approuver.
        ⚠️ `APPROVE` peut débloquer une fusion protégée : c'est un acte de
        gouvernance, pas un commentaire.
        """
        self._check_choice("event", payload.get("event"), REVIEW_EVENTS)
        return self._request(
            "POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json=dict(payload))

    def submit_review(self, owner: str, repo: str, number: Any,
                      review_id: Any, event: str,
                      body: Optional[str] = None) -> Any:
        """POST /…/pulls/{number}/reviews/{id}/events — publie une revue en attente.

        C'est le geste qui sort une revue `PENDING` de l'ombre. `event` est requis.
        """
        self._check_choice("event", event, REVIEW_EVENTS)
        payload: Dict[str, Any] = {"event": event}
        if body is not None:
            payload["body"] = body
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/events",
            json=payload)

    def list_review_comments(self, owner: str, repo: str, number: Any,
                             since: Optional[str] = None,
                             per_page: Optional[int] = None,
                             page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/pulls/{number}/comments — commentaires LIGNE À LIGNE.

        Distincts des commentaires du fil, qui sont ceux de l'issue
        (`list_issue_comments`).
        """
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}/comments",
                         {"since": since}, per_page, page)

    def create_review_comment(self, owner: str, repo: str, number: Any,
                              payload: Dict[str, Any]) -> Any:
        """POST /repos/{owner}/{repo}/pulls/{number}/comments — commente une LIGNE.

        Requis : `body`, `commit_id`, `path`, et la position — `line` (+ `side`),
        ou `start_line`/`line` pour une plage. `in_reply_to` répond à un fil
        existant, auquel cas la position est inutile.

        ⚠️ `commit_id` doit être un commit **de la PR** : un SHA étranger rend
        un 422.
        """
        if not payload.get("body"):
            raise ValueError("`body` requis.")
        if not payload.get("in_reply_to"):
            for champ in ("commit_id", "path"):
                if not payload.get(champ):
                    raise ValueError(
                        f"`{champ}` requis pour un commentaire de revue neuf "
                        "(sauf en réponse, avec `in_reply_to`).")
        return self._request(
            "POST", f"/repos/{owner}/{repo}/pulls/{number}/comments",
            json=dict(payload))

    # --- relecteurs ------------------------------------------------------------

    def list_requested_reviewers(self, owner: str, repo: str,
                                 number: Any) -> Any:
        """GET /repos/{owner}/{repo}/pulls/{number}/requested_reviewers — demandes en cours.

        ⚠️ Rend un OBJET `{users: [...], teams: [...]}`, pas une liste.
        """
        return self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers")

    def request_reviewers(self, owner: str, repo: str, number: Any,
                          reviewers: Optional[List[str]] = None,
                          team_reviewers: Optional[List[str]] = None) -> Any:
        """POST /…/pulls/{number}/requested_reviewers — demande une revue.

        ⚠️ **Notifie** les personnes et équipes désignées. `reviewers` sont des
        identifiants de compte, `team_reviewers` des *slugs* d'équipe.
        ⚠️ Demander une revue à l'AUTEUR de la PR rend un 422.
        """
        if not reviewers and not team_reviewers:
            raise ValueError(
                "passer `reviewers` (comptes) et/ou `team_reviewers` (slugs "
                "d'équipe).")
        body: Dict[str, Any] = {}
        if reviewers:
            body["reviewers"] = list(reviewers)
        if team_reviewers:
            body["team_reviewers"] = list(team_reviewers)
        return self._request(
            "POST", f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers",
            json=body)

    def remove_requested_reviewers(self, owner: str, repo: str, number: Any,
                                   reviewers: Optional[List[str]] = None,
                                   team_reviewers: Optional[List[str]] = None) -> Any:
        """DELETE /…/pulls/{number}/requested_reviewers — retire une demande de revue."""
        body: Dict[str, Any] = {}
        if reviewers:
            body["reviewers"] = list(reviewers)
        if team_reviewers:
            body["team_reviewers"] = list(team_reviewers)
        if not body:
            raise ValueError("passer `reviewers` et/ou `team_reviewers`.")
        return self._request(
            "DELETE",
            f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers",
            json=body)
