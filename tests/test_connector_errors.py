"""Erreurs connecteur typées : contrat `status_code` (routage « erreur gérée »).

Le backend (sentry_setup) classe un refus amont par la présence d'un
`status_code` 4xx sur l'exception (chaîne `__cause__` incluse) — même contrat
que `UpstreamHTTPError`. Ces tests figent que UnipileError et ZohoAuthError
le portent.
"""
import pytest

from oto.tools.unipile.client import UnipileError
from oto.tools.zoho import ZohoAuthError
from oto.tools.salesforce import SalesforceAuthError


def test_unipile_error_carries_http_status():
    e = UnipileError("Unipile 422: recipient invalide", status_code=422)
    assert e.status_code == 422
    assert isinstance(e, RuntimeError)
    assert "422" in str(e)


def test_unipile_error_without_status_defaults_none():
    e = UnipileError("Unipile: erreur réseau (ConnectTimeout).")
    assert e.status_code is None


def test_zoho_auth_error_is_a_401_valueerror():
    e = ZohoAuthError("Zoho OAuth error: invalid_client")
    assert e.status_code == 401
    # sous-classe ValueError : les `except ValueError` existants tiennent
    assert isinstance(e, ValueError)
    with pytest.raises(ValueError):
        raise ZohoAuthError("Zoho OAuth error: invalid_code")


def test_zoho_auth_error_shared_by_desk_and_analytics():
    from oto.tools.zohodesk import client as desk
    from oto.tools.zohoanalytics import client as analytics
    assert desk.ZohoAuthError is ZohoAuthError
    assert analytics.ZohoAuthError is ZohoAuthError


def test_salesforce_auth_error_is_a_401_valueerror():
    e = SalesforceAuthError("Salesforce OAuth error: invalid_grant")
    assert e.status_code == 401
    assert isinstance(e, ValueError)
    with pytest.raises(ValueError):
        raise SalesforceAuthError("Salesforce OAuth error: invalid_client")
