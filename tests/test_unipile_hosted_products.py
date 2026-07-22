"""Produits LinkedIn au lien hosted-auth (`config.linkedin`).

Sans `config.linkedin.products`, Unipile ne connecte que `classic` → Recruiter /
Sales Navigator répondent 403 « out of your scope » et le wizard n'offre aucune
case premium. C'est à l'APP de les demander (confirmé par le support Unipile).
Les deux premium sont EXCLUSIFS (un seul par compte)."""
import pytest

from oto.tools.unipile import UnipileClient
from oto.tools.unipile.client import UnipileError


def _client(rec):
    c = UnipileClient(api_key="k", account_id="acc")

    def fake(method, path, params=None, json=None, timeout=None):
        rec.append({"path": path, "json": json})
        return {"link": "https://auth.unipile.com/?token=x"}

    c._request = fake  # type: ignore[method-assign]
    return c


def test_no_config_when_nothing_asked():
    # défaut inchangé : pas de bloc config → comportement Unipile d'origine
    rec = []
    _client(rec).hosted_auth_link(providers=["LINKEDIN"])
    assert "config" not in rec[0]["json"]


def test_recruiter_sets_products_with_classic():
    rec = []
    _client(rec).hosted_auth_link(providers=["LINKEDIN"], premium="recruiter")
    assert rec[0]["json"]["config"]["linkedin"]["products"] == ["classic", "recruiter"]


def test_sales_navigator_sets_products():
    rec = []
    _client(rec).hosted_auth_link(providers=["LINKEDIN"], premium="sales_navigator")
    assert rec[0]["json"]["config"]["linkedin"]["products"] == ["classic", "sales_navigator"]


def test_allow_cookies_adds_methods():
    rec = []
    _client(rec).hosted_auth_link(providers=["LINKEDIN"], premium="recruiter",
                                  allow_cookies=True)
    cfg = rec[0]["json"]["config"]["linkedin"]
    assert cfg["allow_methods"] == ["credentials", "cookies"]


def test_invalid_premium_raises():
    with pytest.raises(UnipileError) as e:
        _client([]).hosted_auth_link(providers=["LINKEDIN"], premium="premium")
    assert "recruiter" in str(e.value) and "sales_navigator" in str(e.value)
