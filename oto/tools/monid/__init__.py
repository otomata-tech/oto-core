"""Monid — passerelle payante vers ~2 000 endpoints de données (~70 fournisseurs),
facturés à l'appel sur un portefeuille prépayé : trouver, inspecter, lancer, suivre."""

from .client import (
    DEFAULT_BASE_URL,
    RUN_READ_TIMEOUT,
    RUN_STATUSES,
    SERVICE,
    TERMINAL_STATUSES,
    MonidClient,
    MonidHTTPError,
    MonidProtocolError,
    is_terminal,
    run_cost_usd,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "RUN_READ_TIMEOUT",
    "RUN_STATUSES",
    "SERVICE",
    "TERMINAL_STATUSES",
    "MonidClient",
    "MonidHTTPError",
    "MonidProtocolError",
    "is_terminal",
    "run_cost_usd",
]
