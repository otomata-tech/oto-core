"""Constantes du connecteur GitHub — en-têtes, bornes, énumérations.

Domicile unique : `client.py` les réexporte via son `__all__`, et les mixins de
`_api/` les importent d'ici. Le backend épingle oto-core par tag et n'importe que
`oto.tools.github.client`.
"""
from __future__ import annotations

# (connexion, lecture) — aucune attente illimitée. La lecture est large : un
# `compare_commits` sur un gros dépôt, ou un téléchargement de logs de job,
# prennent leur temps.
HTTP_TIMEOUT = (10, 90)

DEFAULT_BASE_URL = "https://api.github.com"

#: Type de média attendu par la quasi-totalité des endpoints REST.
DEFAULT_ACCEPT = "application/vnd.github+json"

#: Version d'API envoyée à CHAQUE requête. Épinglée ici, jamais recopiée sur un
#: site d'appel : GitHub date ses versions et en retirera d'anciennes, et il doit
#: y avoir UN endroit à changer.
DEFAULT_API_VERSION = "2022-11-28"

# ⚠️ `per_page` PLAFONNE À 100, et GitHub **rabote en silence** au-delà : pas
# d'erreur, juste moins de lignes que demandé. C'est le piège nº 1 de cette API —
# un appelant qui demande 500 croit tout avoir et n'a que les 100 premiers. D'où
# un refus LOCAL, qui nomme la borne au lieu de la subir.
MIN_PER_PAGE, MAX_PER_PAGE = 1, 100
DEFAULT_PER_PAGE = 30

#: La recherche a son propre plafond, plus bas, et un total borné à 1 000
#: résultats quelle que soit la pagination.
SEARCH_MAX_RESULTS = 1000

# Statuts retentés. GitHub a DEUX limites : la primaire (en-têtes
# `x-ratelimit-*`, 403 ou 429) et une limite « secondaire » anti-abus, qui
# répond aussi 403/429 et porte souvent `Retry-After`. Les deux se retentent.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3

# --- énumérations -----------------------------------------------------------

ISSUE_STATES = ("open", "closed", "all")
ISSUE_STATE_WRITES = ("open", "closed")
SORT_DIRECTIONS = ("asc", "desc")

ISSUE_SORTS = ("created", "updated", "comments")
PULL_STATES = ("open", "closed", "all")
PULL_SORTS = ("created", "updated", "popularity", "long-running")

#: ⚠️ `merge` fabrique un commit de fusion, `squash` écrase l'historique de la
#: branche en un seul commit, `rebase` réécrit les commits. Les trois modifient
#: la branche cible de façon différente, et aucun n'est annulable d'un clic.
MERGE_METHODS = ("merge", "squash", "rebase")

REVIEW_EVENTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")

REPO_TYPES = ("all", "owner", "public", "private", "member")
REPO_SORTS = ("created", "updated", "pushed", "full_name")
ORG_REPO_TYPES = ("all", "public", "private", "forks", "sources", "member")

RUN_STATUSES = (
    "completed", "action_required", "cancelled", "failure", "neutral",
    "skipped", "stale", "success", "timed_out", "in_progress", "queued",
    "requested", "waiting", "pending",
)

MEMBERSHIP_ROLES = ("admin", "member")
MEMBER_FILTERS = ("2fa_disabled", "all")
TEAM_ROLES = ("member", "maintainer")

#: Permissions posables sur un collaborateur de dépôt.
COLLABORATOR_PERMISSIONS = ("pull", "triage", "push", "maintain", "admin")

SEARCH_CODE_SORTS = ("indexed",)
SEARCH_REPO_SORTS = ("stars", "forks", "help-wanted-issues", "updated")
SEARCH_ISSUE_SORTS = (
    "comments", "reactions", "author-date", "committer-date", "updated",
    "created",
)

__all__ = [
    "HTTP_TIMEOUT", "DEFAULT_BASE_URL", "DEFAULT_ACCEPT", "DEFAULT_API_VERSION",
    "MIN_PER_PAGE", "MAX_PER_PAGE", "DEFAULT_PER_PAGE", "SEARCH_MAX_RESULTS",
    "RETRY_STATUSES", "MAX_ATTEMPTS",
    "ISSUE_STATES", "ISSUE_STATE_WRITES", "SORT_DIRECTIONS", "ISSUE_SORTS",
    "PULL_STATES", "PULL_SORTS", "MERGE_METHODS", "REVIEW_EVENTS",
    "REPO_TYPES", "REPO_SORTS", "ORG_REPO_TYPES", "RUN_STATUSES",
    "MEMBERSHIP_ROLES", "MEMBER_FILTERS", "TEAM_ROLES",
    "COLLABORATOR_PERMISSIONS",
    "SEARCH_CODE_SORTS", "SEARCH_REPO_SORTS", "SEARCH_ISSUE_SORTS",
]
