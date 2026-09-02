"""Constantes du connecteur Productlane — bornes, énumérations, transport.

Domicile unique : `client.py` les réexporte via son `__all__`, et les mixins de
`_api/` les importent d'ici. Le backend épingle oto-core par tag et n'importe que
`oto.tools.productlane.client`.

⚠️ **Les énumérations sont SCOPÉES À LEUR ENDPOINT**, et c'est délibéré : le
schéma OpenAPI amont réutilise les mêmes noms de paramètre pour des jeux de
valeurs différents. `status` vaut `open|snoozed|done` sur un fil et
`draft|open|accepted|rejected|superseded` sur un brouillon de doc ; `type` vaut
`EMAIL|DOMAIN` sur un expéditeur bloqué et `email|slack|chat|live_chat|feedback`
sur un message. Une constante « globale » par nom de paramètre accepterait donc
des valeurs que l'amont refuse, et refuserait des valeurs qu'il accepte — d'où un
nom par usage, jamais par paramètre.
"""
from __future__ import annotations

# (connexion, lecture) — aucune attente illimitée.
HTTP_TIMEOUT = (10, 60)

# Pagination par CURSEUR, uniforme sur toutes les listes v2 : ni `page`, ni
# `offset`, ni `skip` nulle part. Tri figé côté serveur (`created_at DESC, id
# DESC`), sans paramètre pour en changer.
DEFAULT_LIMIT = 50
MIN_LIMIT, MAX_LIMIT = 1, 200

# Statuts retentés. Limites amont PAR CLÉ : 1000 GET/minute, 60 écritures/minute,
# avec un burst de 2× sur 10 s. Le 429 porte `Retry-After`.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3

# --- énumérations, par endroit où elles s'appliquent ------------------------

THREAD_STATUSES = ("open", "snoozed", "done")
THREAD_TABS = ("open", "new", "needs-response", "my", "snoozed", "done")
PAIN_LEVELS = ("UNKNOWN", "LOW", "MEDIUM", "HIGH")
THREAD_ORIGINS = (
    "in_app", "portal", "support_portal", "email", "slack", "slack_connect",
    "intercom", "intercom_attachment", "zendesk", "zendesk_attachment",
    "front_attachment", "zapier", "hubspot", "plain", "api", "live_chat",
    "ai_chat", "calendar", "widget", "teams", "linear", "upvote",
)

# `expand` sur GET /threads/{id} : liste séparée par des virgules. L'amont
# IGNORE une valeur inconnue (il ne refuse pas) — donc une faute de frappe se
# solderait par une réponse sans les données demandées, sans un mot. On refuse
# localement pour que l'écart se voie.
THREAD_EXPANDS = ("messages", "comments")

MESSAGE_ORDERS = ("asc", "desc")
MESSAGE_TYPES = ("email", "slack", "chat", "live_chat", "feedback")
MESSAGE_DIRECTIONS = ("inbound", "outbound")

BLOCKED_SENDER_TYPES = ("EMAIL", "DOMAIN")

PROJECT_STATES = ("backlog", "planned", "started", "completed", "canceled")
ROADMAP_SORTS = ("created_at", "total_score")

DOC_VISIBILITIES = ("public", "agent", "internal", "unlisted")
#: `all` n'existe qu'en FILTRE de liste, jamais en écriture — un article ne peut
#: pas « être » de visibilité `all`.
DOC_VISIBILITY_FILTERS = DOC_VISIBILITIES + ("all",)
DOC_KINDS = ("doc", "link")
DOC_KIND_FILTERS = DOC_KINDS + ("all",)

DRAFT_KINDS = ("edit", "create", "delete")
DRAFT_STATUSES = ("draft", "open", "accepted", "rejected", "superseded")

#: Priorités Linear, telles que Linear les numérote. Ce ne sont PAS des niveaux
#: croissants d'urgence : `0` = aucune priorité, `1` = la plus haute.
ISSUE_PRIORITIES = (0, 1, 2, 3, 4)

__all__ = [
    "HTTP_TIMEOUT", "DEFAULT_LIMIT", "MIN_LIMIT", "MAX_LIMIT",
    "RETRY_STATUSES", "MAX_ATTEMPTS",
    "THREAD_STATUSES", "THREAD_TABS", "PAIN_LEVELS", "THREAD_ORIGINS",
    "THREAD_EXPANDS", "MESSAGE_ORDERS", "MESSAGE_TYPES", "MESSAGE_DIRECTIONS",
    "BLOCKED_SENDER_TYPES", "PROJECT_STATES", "ROADMAP_SORTS",
    "DOC_VISIBILITIES", "DOC_VISIBILITY_FILTERS", "DOC_KINDS", "DOC_KIND_FILTERS",
    "DRAFT_KINDS", "DRAFT_STATUSES", "ISSUE_PRIORITIES",
]
