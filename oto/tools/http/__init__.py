"""Connecteur `http` générique — nœud HTTP lecture seule multi-auth (ADR 0037).

Consommé par l'adaptateur `oto_mcp/tools/http.py` (connecteur `http` d'oto-backend) ;
réutilisable par oto-cli. Pur (`requests` seul). La protection SSRF est un contrôle
d'egress réseau au niveau plateforme, pas du code ici."""
from .client import (
    AUTH_MODES,
    ApiKeyHeader,
    ApiKeyQuery,
    BasicAuth,
    HttpConnectorClient,
    NoAuth,
    OAuth2ClientCredentials,
    StaticBearer,
    UpstreamAuth,
    build_auth,
)

__all__ = [
    "AUTH_MODES",
    "ApiKeyHeader",
    "ApiKeyQuery",
    "BasicAuth",
    "HttpConnectorClient",
    "NoAuth",
    "OAuth2ClientCredentials",
    "StaticBearer",
    "UpstreamAuth",
    "build_auth",
]
