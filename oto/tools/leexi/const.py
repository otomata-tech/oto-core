"""Constantes du connecteur Leexi — bornes, énumérations, réglages de transport.

Domicile unique : `client.py` les réexporte via son `__all__`, et les mixins de
`_api/` les importent d'ici. Un tag oto-core fige ce chemin d'import pour le
backend, qui n'épingle que `oto.tools.leexi.client`.
"""
from __future__ import annotations

# (connexion, lecture) — aucune attente illimitée.
HTTP_TIMEOUT = (10, 60)

# Bornes de pagination imposées par l'API (doc « Pagination »).
MIN_ITEMS, MAX_ITEMS = 1, 100
DEFAULT_ITEMS = 10

# Les paramètres que l'amont attend en `nom[]=…`, répétés (Rails). Sans le
# suffixe, seule la DERNIÈRE valeur est lue et le filtre ment — cf. l'en-tête de
# `client.py`. Les six sont écrits avec leurs crochets dans la doc éditeur.
ARRAY_PARAMS = frozenset({
    "source_id", "owner_uuid", "participating_user_uuid",
    "customer_phone_number", "customer_email_address", "roles",
})

# Statuts retentés : rate limit (50/min, 10/min sur la création d'appel) et
# indisponibilités passagères. Un 4xx de validation n'est jamais retenté.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3

# Ordres et filtres acceptés, relevés dans l'OpenAPI de chaque page de liste.
# Refuser localement une valeur hors liste évite un 400 dont le message ne dit
# pas lesquelles sont valides.
CALL_ORDERS = ("created_at desc", "created_at asc", "performed_at desc",
               "performed_at asc", "updated_at desc", "updated_at asc")
CALL_DATE_FILTERS = ("created_at", "performed_at", "updated_at")

MEETING_ORDERS = ("created_at desc", "created_at asc", "start_time desc",
                  "start_time asc", "end_time desc", "end_time asc")
MEETING_DATE_FILTERS = ("start_time", "end_time")
MEETING_ORIGINS = ("calendar", "manual", "api")

__all__ = [
    "HTTP_TIMEOUT", "MIN_ITEMS", "MAX_ITEMS", "DEFAULT_ITEMS",
    "ARRAY_PARAMS", "RETRY_STATUSES", "MAX_ATTEMPTS",
    "CALL_ORDERS", "CALL_DATE_FILTERS",
    "MEETING_ORDERS", "MEETING_DATE_FILTERS", "MEETING_ORIGINS",
]
