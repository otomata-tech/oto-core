"""Familles d'appels GitHub, composées dans `GitHubClient`.

Un module par domaine d'API. Détail du contrat : `../client.py`.
"""

from .actions import _ActionsMixin
from .issues import _IssuesMixin
from .orgs import _OrgsMixin
from .pulls import _PullsMixin
from .repos import _ReposMixin
from .search import _SearchMixin

__all__ = [
    "_ActionsMixin",
    "_IssuesMixin",
    "_OrgsMixin",
    "_PullsMixin",
    "_ReposMixin",
    "_SearchMixin",
]
