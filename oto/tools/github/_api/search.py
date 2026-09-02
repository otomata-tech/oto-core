"""Recherche GitHub — dépôts, code, issues/PR, commits, comptes.

Ce mixin n'est jamais instancié seul : il est composé dans `GitHubClient`, qui
fournit le transport (`_request`, `_get`, `_check_choice`).

Quatre choses distinguent la recherche du reste de l'API, et chacune se paie
comptant si on l'ignore :

- ⚠️ **1 000 résultats maximum, quoi qu'annonce `total_count`.** Au-delà de la
  page qui atteint 1 000, GitHub répond **422**. `total_count` est une estimation
  du corpus, PAS le nombre de lignes récupérables : lire « 12 000 résultats » et
  boucler jusqu'au bout est le piège classique.

- ⚠️ **Toutes les réponses sont des OBJETS** `{total_count, incomplete_results,
  items: [...]}`. `incomplete_results: true` veut dire que GitHub a **abandonné
  la recherche en cours de route** (délai dépassé) : la réponse est partielle et
  ne le dit pas autrement.

- ⚠️ **Limite d'usage propre et basse** : 30 requêtes/minute avec un jeton (10
  sans). C'est un ordre de grandeur en dessous du reste de l'API — une boucle de
  recherche épuise le quota en quelques secondes.

- **La recherche de CODE a ses propres règles** : elle n'indexe que la branche
  par défaut, ignore les fichiers de plus de 384 Ko, exige au moins un terme de
  recherche (une qualification seule comme `repo:x` ne suffit pas), et — sur les
  dépôts privés — demande un jeton qui y a accès.

La syntaxe des qualificateurs (`repo:`, `org:`, `language:`, `is:`, `state:`…)
appartient à GitHub et n'est pas réécrite ici : `q` part tel quel.
"""
from __future__ import annotations

from typing import Any, Optional

from ..const import (SEARCH_CODE_SORTS, SEARCH_ISSUE_SORTS, SEARCH_MAX_RESULTS,
                     SEARCH_REPO_SORTS, SORT_DIRECTIONS)


class _SearchMixin:
    """Recherche."""

    @staticmethod
    def _check_query(q: str) -> None:
        """Une recherche sans terme est refusée ICI.

        GitHub rendrait un 422 dont le message ne dit pas que le problème est
        l'absence de `q` — et un `q` vide est presque toujours un bug d'appelant
        (variable non substituée), pas une intention.
        """
        if not q or not str(q).strip():
            raise ValueError(
                "`q` requis : une recherche GitHub sans terme est refusée "
                "(422). Les qualificateurs seuls — `repo:`, `org:`… — ne "
                "suffisent pas pour la recherche de code.")

    def _search(self, path: str, q: str, sort: Optional[str],
                order: Optional[str], per_page: Optional[int],
                page: Optional[int], extra: Optional[dict] = None) -> Any:
        self._check_query(q)
        self._check_choice("order", order, SORT_DIRECTIONS)
        params = {"q": q, "sort": sort, "order": order}
        params.update(extra or {})
        return self._get(path, params, per_page, page)

    def search_repositories(self, q: str, sort: Optional[str] = None,
                            order: Optional[str] = None,
                            per_page: Optional[int] = None,
                            page: Optional[int] = None) -> Any:
        """GET /search/repositories — cherche des dépôts.

        `sort` : `stars`, `forks`, `help-wanted-issues`, `updated`. Sans `sort`,
        GitHub classe par pertinence.

        ⚠️ Plafond de 1 000 résultats (cf. en-tête de module).
        """
        self._check_choice("sort", sort, SEARCH_REPO_SORTS)
        return self._search("/search/repositories", q, sort, order,
                            per_page, page)

    def search_code(self, q: str, sort: Optional[str] = None,
                    order: Optional[str] = None,
                    per_page: Optional[int] = None,
                    page: Optional[int] = None) -> Any:
        """GET /search/code — cherche DANS le code.

        ⚠️ Trois limites propres à cet index, qui expliquent la plupart des
        « pourquoi ne trouve-t-il pas ? » :
        seule la **branche par défaut** est indexée ; les fichiers de plus de
        **384 Ko** ne le sont pas ; et il faut au moins un terme réel, pas
        seulement des qualificateurs.

        ⚠️ La réponse ne porte PAS le contenu du fichier — seulement son chemin,
        son dépôt et des extraits. Lire le fichier ensuite avec
        `read_text_file`.
        """
        self._check_choice("sort", sort, SEARCH_CODE_SORTS)
        return self._search("/search/code", q, sort, order, per_page, page)

    def search_issues(self, q: str, sort: Optional[str] = None,
                      order: Optional[str] = None,
                      per_page: Optional[int] = None,
                      page: Optional[int] = None) -> Any:
        """GET /search/issues — cherche des issues ET des pull requests.

        Les deux partagent cet index : filtrer avec `is:issue` ou `is:pr` dans
        `q`. C'est d'ailleurs le moyen le plus simple de compter les issues d'un
        dépôt sans se faire piéger par les PR.
        """
        self._check_choice("sort", sort, SEARCH_ISSUE_SORTS)
        return self._search("/search/issues", q, sort, order, per_page, page)

    def search_users(self, q: str, sort: Optional[str] = None,
                     order: Optional[str] = None,
                     per_page: Optional[int] = None,
                     page: Optional[int] = None) -> Any:
        """GET /search/users — cherche des comptes et des organisations.

        `type:user` / `type:org` dans `q` pour trancher entre les deux.
        """
        return self._search("/search/users", q, sort, order, per_page, page)

    def search_commits(self, q: str, sort: Optional[str] = None,
                       order: Optional[str] = None,
                       per_page: Optional[int] = None,
                       page: Optional[int] = None) -> Any:
        """GET /search/commits — cherche des commits."""
        return self._search("/search/commits", q, sort, order, per_page, page)

    @staticmethod
    def search_is_truncated(payload: Any) -> bool:
        """La réponse de recherche est-elle incomplète ou tronquée ?

        Vrai si GitHub a abandonné en cours de route (`incomplete_results`) **ou**
        si le corpus dépasse le plafond de 1 000 résultats récupérables. Écrit ici
        pour que « j'ai tout » ne se déduise jamais d'un `total_count` lu de
        travers — les deux causes sont invisibles sans cette lecture.
        """
        if not isinstance(payload, dict):
            return False
        if payload.get("incomplete_results"):
            return True
        total = payload.get("total_count")
        return isinstance(total, int) and total > SEARCH_MAX_RESULTS
