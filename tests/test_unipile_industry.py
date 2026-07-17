"""Filtre `industry` de la recherche Unipile.

L'API LinkedIn **classic** n'accepte qu'une liste de secteurs à INCLURE : elle n'a
pas d'exclusion. On lève une erreur actionnable au lieu de concaténer `exclude`
dans `include` — sinon « cherche dans la Tech mais PAS la Banque » renvoyait aussi
les banquiers, sans aucune erreur (faux en silence)."""
import pytest

from oto.tools.unipile import UnipileClient
from oto.tools.unipile.client import UnipileError


def _client(recorder=None):
    c = UnipileClient(api_key="k", account_id="acc")

    def fake(method, path, params=None, json=None):
        if recorder is not None:
            recorder.append({"path": path, "json": json})
        return {"data": []}

    c._request = fake  # type: ignore[method-assign]
    # facettes : nom -> id, sans réseau
    c.resolve_facet = lambda ft, kw, limit=100: [{"id": f"id-{kw}", "title": kw}]  # type: ignore
    return c


def test_classic_exclude_raises_instead_of_silently_including():
    c = _client()
    with pytest.raises(UnipileError) as e:
        c.search(keywords="cto", industry={"include": ["Tech"], "exclude": ["Banque"]})
    msg = str(e.value)
    assert "exclude" in msg and "classic" in msg
    assert "sales_navigator" in msg  # oriente vers l'API qui le supporte


def test_classic_include_only_sends_flat_ids():
    rec = []
    c = _client(rec)
    c.search(keywords="cto", industry={"include": ["Tech"]})
    assert rec[0]["json"]["industry"] == ["id-Tech"]


def test_classic_without_industry_omits_the_field():
    rec = []
    c = _client(rec)
    c.search(keywords="cto")
    assert "industry" not in rec[0]["json"]


def test_exclude_allowed_on_sales_navigator(monkeypatch):
    # la garde ne vise QUE classic : SN/recruiter supportent l'exclusion
    rec = []
    c = _client(rec)
    c.search(keywords="cto", api="sales_navigator",
             industry={"include": ["Tech"], "exclude": ["Banque"]})
    assert rec[0]["json"]["industry"]  # pas d'exception
