"""Dépôts GitHub — métadonnées, branches, commits, contenus de fichiers, releases.

Ce mixin n'est jamais instancié seul : il est composé dans `GitHubClient`, qui
fournit le transport (`_request`, `_get`, `_check_choice`, `_check_per_page`).

⚠️ **Le contenu d'un fichier arrive en base64**, pas en clair : `get_content`
rend `{type, encoding: "base64", content, sha, …}`. `read_text_file` fait le
décodage, et refuse nommément les cas où il n'y a rien à décoder (un dossier, un
fichier trop gros servi sans contenu, un binaire).

⚠️ **Écrire un fichier existant EXIGE son `sha`** (celui du blob, rendu par
`get_content`). Sans lui, GitHub répond 409 — c'est son contrôle de concurrence :
il garantit qu'on écrase bien la version qu'on a lue, et pas une modification
arrivée entre-temps.

**Délibérément absent** : `DELETE /repos/{owner}/{repo}`. Supprimer un dépôt est
irréversible et hors du périmètre de ce connecteur.
"""
from __future__ import annotations

import base64
from typing import Any, Dict, Optional

from ..const import (DEFAULT_ACCEPT, ORG_REPO_TYPES, REPO_SORTS, REPO_TYPES,
                     SORT_DIRECTIONS)


class _ReposMixin:
    """Dépôts, branches, commits, contenus, releases."""

    # --- dépôts -------------------------------------------------------------

    def list_my_repos(self, visibility: Optional[str] = None,
                      affiliation: Optional[str] = None,
                      type: Optional[str] = None, sort: Optional[str] = None,
                      direction: Optional[str] = None,
                      per_page: Optional[int] = None,
                      page: Optional[int] = None) -> Any:
        """GET /user/repos — dépôts du porteur du jeton.

        ⚠️ `type` et `visibility`/`affiliation` sont **exclusifs** côté GitHub :
        passer les deux rend un 422.
        """
        self._check_choice("type", type, REPO_TYPES)
        self._check_choice("sort", sort, REPO_SORTS)
        self._check_choice("direction", direction, SORT_DIRECTIONS)
        if type and (visibility or affiliation):
            raise ValueError(
                "`type` est exclusif de `visibility`/`affiliation` (422 côté "
                "GitHub) — choisir l'un ou l'autre.")
        return self._get("/user/repos", {
            "visibility": visibility, "affiliation": affiliation, "type": type,
            "sort": sort, "direction": direction}, per_page, page)

    def list_org_repos(self, org: str, type: Optional[str] = None,
                       sort: Optional[str] = None,
                       direction: Optional[str] = None,
                       per_page: Optional[int] = None,
                       page: Optional[int] = None) -> Any:
        """GET /orgs/{org}/repos — dépôts d'une organisation.

        ⚠️ Un jeton sans accès aux dépôts privés ne les verra simplement pas
        listés : la liste est silencieusement plus courte, elle n'échoue pas.
        """
        self._check_choice("type", type, ORG_REPO_TYPES)
        self._check_choice("sort", sort, REPO_SORTS)
        self._check_choice("direction", direction, SORT_DIRECTIONS)
        return self._get(f"/orgs/{org}/repos", {
            "type": type, "sort": sort, "direction": direction},
            per_page, page)

    def list_user_repos(self, username: str, type: Optional[str] = None,
                        sort: Optional[str] = None,
                        direction: Optional[str] = None,
                        per_page: Optional[int] = None,
                        page: Optional[int] = None) -> Any:
        """GET /users/{username}/repos — dépôts PUBLICS d'un compte."""
        self._check_choice("sort", sort, REPO_SORTS)
        self._check_choice("direction", direction, SORT_DIRECTIONS)
        return self._get(f"/users/{username}/repos", {
            "type": type, "sort": sort, "direction": direction},
            per_page, page)

    def get_repo(self, owner: str, repo: str) -> Any:
        """GET /repos/{owner}/{repo} — la fiche d'un dépôt.

        ⚠️ **404 sur un dépôt privé = jeton sans le droit**, le plus souvent, et
        non « n'existe pas » : GitHub masque l'existence exprès.
        """
        return self._request("GET", f"/repos/{owner}/{repo}")

    def list_languages(self, owner: str, repo: str) -> Any:
        """GET /repos/{owner}/{repo}/languages — octets par langage.

        ⚠️ Rend un OBJET `{langage: octets}`, pas une liste : ne pas le passer à
        `iterate`.
        """
        return self._request("GET", f"/repos/{owner}/{repo}/languages")

    def list_contributors(self, owner: str, repo: str,
                          per_page: Optional[int] = None,
                          page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/contributors — contributeurs et leur volume."""
        return self._get(f"/repos/{owner}/{repo}/contributors", None,
                         per_page, page)

    def list_topics(self, owner: str, repo: str) -> Any:
        """GET /repos/{owner}/{repo}/topics — les « topics » du dépôt."""
        return self._request("GET", f"/repos/{owner}/{repo}/topics")

    # --- branches et commits -------------------------------------------------

    def list_branches(self, owner: str, repo: str,
                      protected: Optional[bool] = None,
                      per_page: Optional[int] = None,
                      page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/branches — branches du dépôt."""
        return self._get(f"/repos/{owner}/{repo}/branches",
                         {"protected": protected}, per_page, page)

    def get_branch(self, owner: str, repo: str, branch: str) -> Any:
        """GET /repos/{owner}/{repo}/branches/{branch} — une branche et son HEAD."""
        return self._request("GET", f"/repos/{owner}/{repo}/branches/{branch}")

    def list_commits(self, owner: str, repo: str, sha: Optional[str] = None,
                     path: Optional[str] = None, author: Optional[str] = None,
                     since: Optional[str] = None, until: Optional[str] = None,
                     per_page: Optional[int] = None,
                     page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/commits — historique.

        `sha` est le point de départ (branche, tag ou SHA), `path` restreint aux
        commits qui touchent un chemin. `since`/`until` sont des dates ISO 8601.
        """
        return self._get(f"/repos/{owner}/{repo}/commits", {
            "sha": sha, "path": path, "author": author,
            "since": since, "until": until}, per_page, page)

    def get_commit(self, owner: str, repo: str, ref: str) -> Any:
        """GET /repos/{owner}/{repo}/commits/{ref} — un commit ET son diff.

        ⚠️ La réponse embarque `files[]` : sur un gros commit, c'est lourd. GitHub
        plafonne d'ailleurs à 300 fichiers, et tronque au-delà **sans le dire
        dans les fichiers eux-mêmes** — comparer `files` à `stats` pour le voir.
        """
        return self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}")

    def compare_commits(self, owner: str, repo: str, base: str, head: str,
                        per_page: Optional[int] = None,
                        page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/compare/{base}...{head} — l'écart entre deux refs.

        `base` et `head` acceptent branches, tags et SHA. Pour comparer entre
        deux forks, préfixer d'un propriétaire (`autre:branche`).
        """
        return self._get(f"/repos/{owner}/{repo}/compare/{base}...{head}",
                         None, per_page, page)

    def list_tags(self, owner: str, repo: str, per_page: Optional[int] = None,
                  page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/tags — tags du dépôt."""
        return self._get(f"/repos/{owner}/{repo}/tags", None, per_page, page)

    # --- contenus ------------------------------------------------------------

    def get_content(self, owner: str, repo: str, path: str,
                    ref: Optional[str] = None) -> Any:
        """GET /repos/{owner}/{repo}/contents/{path} — fichier OU dossier.

        Rend un OBJET pour un fichier (avec `content` en **base64** et le `sha`
        du blob), une LISTE pour un dossier. `ref` choisit branche, tag ou SHA.

        ⚠️ Au-delà de 1 Mo, GitHub rend la métadonnée **sans `content`** ; au-delà
        de 100 Mo, il refuse. `read_text_file` nomme ces cas au lieu de rendre
        une chaîne vide.
        """
        return self._request("GET", f"/repos/{owner}/{repo}/contents/{path}",
                             params={"ref": ref})

    def read_text_file(self, owner: str, repo: str, path: str,
                       ref: Optional[str] = None,
                       encoding: str = "utf-8") -> str:
        """Le CONTENU TEXTE d'un fichier, décodé — confort par-dessus `get_content`.

        Écrit ici pour que le décodage base64 n'existe qu'une fois, et surtout
        pour que les trois cas où il n'y a rien à décoder soient **nommés** :
        un dossier, un fichier trop gros servi sans contenu, un binaire non
        décodable dans l'encodage demandé. Chacun rendrait sinon une chaîne vide
        ou une exception opaque loin du site d'appel.
        """
        payload = self.get_content(owner, repo, path, ref)
        if isinstance(payload, list):
            raise ValueError(
                f"`{path}` est un DOSSIER ({len(payload)} entrées), pas un "
                "fichier — utiliser `get_content` pour le lister.")
        if not isinstance(payload, dict):
            raise ValueError(
                f"réponse inattendue pour `{path}` : {type(payload).__name__}")
        content = payload.get("content")
        if not content:
            taille = payload.get("size")
            raise ValueError(
                f"`{path}` est servi SANS contenu (taille {taille} octets). "
                "GitHub omet `content` au-delà de 1 Mo — passer par l'API Git "
                "blobs, ou lire le fichier depuis une archive du dépôt.")
        if payload.get("encoding") != "base64":
            raise ValueError(
                f"encodage inattendu pour `{path}` : "
                f"{payload.get('encoding')!r} (attendu base64).")
        brut = base64.b64decode(content)
        try:
            return brut.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"`{path}` n'est pas du texte {encoding} ({exc}) — c'est "
                "probablement un binaire.") from exc

    def create_or_update_file(self, owner: str, repo: str, path: str,
                              message: str, content: Any,
                              sha: Optional[str] = None,
                              branch: Optional[str] = None,
                              committer: Optional[Dict[str, Any]] = None,
                              author: Optional[Dict[str, Any]] = None) -> Any:
        """PUT /repos/{owner}/{repo}/contents/{path} — écrit un fichier (commit).

        `content` accepte du texte, des octets, ou du base64 déjà encodé ; il est
        encodé ici si besoin. `message` est le message de commit.

        ⚠️ **`sha` est REQUIS pour écraser un fichier existant** — c'est celui du
        blob, rendu par `get_content`. Sans lui, GitHub répond **409** : c'est son
        contrôle de concurrence, qui garantit qu'on remplace bien la version lue
        et non une modification arrivée entre-temps. L'omettre sur une création
        est normal.
        """
        if isinstance(content, bytes):
            encoded = base64.b64encode(content).decode("ascii")
        else:
            encoded = base64.b64encode(str(content).encode("utf-8")).decode("ascii")
        body: Dict[str, Any] = {"message": message, "content": encoded}
        for key, value in (("sha", sha), ("branch", branch),
                           ("committer", committer), ("author", author)):
            if value is not None:
                body[key] = value
        return self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}",
                             json=body)

    def delete_file(self, owner: str, repo: str, path: str, message: str,
                    sha: str, branch: Optional[str] = None) -> Any:
        """DELETE /repos/{owner}/{repo}/contents/{path} — supprime un fichier (commit).

        `sha` (celui du blob) est **obligatoire** ici, sans exception : il n'y a
        pas de suppression « à l'aveugle ».
        """
        if not sha:
            raise ValueError(
                "`sha` du blob requis pour supprimer un fichier (le lire avec "
                "`get_content`) — GitHub refuse une suppression sans lui.")
        body: Dict[str, Any] = {"message": message, "sha": sha}
        if branch is not None:
            body["branch"] = branch
        return self._request("DELETE", f"/repos/{owner}/{repo}/contents/{path}",
                             json=body)

    def get_readme(self, owner: str, repo: str,
                   ref: Optional[str] = None) -> Any:
        """GET /repos/{owner}/{repo}/readme — le README, quel que soit son nom.

        Même forme que `get_content` (contenu en base64).
        """
        return self._request("GET", f"/repos/{owner}/{repo}/readme",
                             params={"ref": ref})

    # --- releases ------------------------------------------------------------

    def list_releases(self, owner: str, repo: str,
                      per_page: Optional[int] = None,
                      page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/releases — releases du dépôt."""
        return self._get(f"/repos/{owner}/{repo}/releases", None, per_page, page)

    def get_release(self, owner: str, repo: str, release_id: Any) -> Any:
        """GET /repos/{owner}/{repo}/releases/{id} — une release."""
        return self._request("GET",
                             f"/repos/{owner}/{repo}/releases/{release_id}")

    def get_latest_release(self, owner: str, repo: str) -> Any:
        """GET /repos/{owner}/{repo}/releases/latest — la dernière release publiée.

        ⚠️ Ignore les brouillons ET les préversions : « latest » n'est pas le
        dernier tag créé.
        """
        return self._request("GET", f"/repos/{owner}/{repo}/releases/latest")

    def create_release(self, owner: str, repo: str,
                       payload: Dict[str, Any]) -> Any:
        """POST /repos/{owner}/{repo}/releases — crée une release.

        Requis : `tag_name`. Optionnels : `name`, `body`, `draft`, `prerelease`,
        `target_commitish`, `generate_release_notes`.

        ⚠️ Une release **publiée** (`draft` absent ou faux) est visible
        immédiatement, et notifie les personnes abonnées au dépôt. `draft=True`
        pour préparer sans publier.
        """
        if not payload.get("tag_name"):
            raise ValueError("`tag_name` requis pour créer une release.")
        return self._request("POST", f"/repos/{owner}/{repo}/releases",
                             json=dict(payload))

    def update_release(self, owner: str, repo: str, release_id: Any,
                       payload: Dict[str, Any]) -> Any:
        """PATCH /repos/{owner}/{repo}/releases/{id} — met à jour une release."""
        return self._request("PATCH",
                             f"/repos/{owner}/{repo}/releases/{release_id}",
                             json=dict(payload))

    def download_tarball(self, owner: str, repo: str, ref: str) -> Any:
        """GET /repos/{owner}/{repo}/tarball/{ref} — archive du dépôt à une ref.

        Rend la **réponse brute** (redirection suivie non comprise) : le corps est
        un binaire, pas du JSON. Utile pour lire des fichiers trop gros pour
        `get_content`.
        """
        return self._request("GET", f"/repos/{owner}/{repo}/tarball/{ref}",
                             accept=DEFAULT_ACCEPT, raw=True)
