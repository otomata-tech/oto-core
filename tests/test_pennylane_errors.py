"""Un refus amont est une exception, jamais une valeur (oto-backend#223, oto-core#77).

Le bug d'origine : `fetch_all_pages` faisait `break` sur un dict d'erreur → rendait
`[]`, indistinguable d'un « 0 résultat ». Sur clé 401,
`find_invoice_by_external_reference` (anti-doublon d'avoir) concluait « aucune
facture d'origine » et pouvait recréer des avoirs en double.

Le correctif d'alors a fermé CE chemin. La règle a ensuite été recopiée une seconde
fois dans `_filter_eq`, parce que le transport ne la portait toujours pas — chaque
appelant rattrapait le dict pour son propre compte, et celui qui n'y pensait pas
lisait un refus comme un résultat.

Depuis oto-core#77, la règle vit dans le transport : `_appel` lève, les deux copies
ont disparu. Ces épreuves l'attaquent donc à sa nouvelle place — au niveau HTTP —
et vérifient que les gardes anti-doublon PROPAGENT au lieu de conclure.
"""

import pytest

from oto.tools.common import UpstreamHTTPError
from oto.tools.pennylane.client import PennylaneClient


class _Reponse:
    """Une réponse HTTP de bureau — ce que la session rend vraiment."""

    def __init__(self, status_code, body=b"{}", json_body=None):
        self.status_code = status_code
        self.content = body
        self.text = body.decode() if isinstance(body, bytes) else str(body)
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("pas de JSON")
        return self._json


def _client_repondant(reponse):
    """Le client, avec sa SESSION remplacée — pas son `fetch`.

    On substitue au niveau du transport HTTP, pas au niveau de la méthode : c'est
    maintenant là que la traduction se fait, et remplacer `fetch` court-circuiterait
    exactement ce qu'on veut éprouver.
    """
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    c.session.request = lambda *a, **kw: reponse
    return c


def test_un_refus_http_leve_au_lieu_de_rendre_une_valeur():
    c = _client_repondant(_Reponse(401, b'{"message":"invalid key"}'))
    with pytest.raises(UpstreamHTTPError) as ei:
        c.fetch("me")
    assert ei.value.status_code == 401
    assert ei.value.is_client_error


def test_les_quatre_verbes_suivent_la_meme_regle():
    """La classe, pas le cas : le défaut d'origine venait d'avoir corrigé un seul
    chemin. Si un verbe échappait au transport unifié, il rendrait le dict."""
    for geste in (lambda c: c.fetch("me"),
                  lambda c: c.post("customer_invoices", {}),
                  lambda c: c.put("customer_invoices/1", {}),
                  lambda c: c.delete("customer_invoices/1")):
        c = _client_repondant(_Reponse(422, b'{"message":"nope"}'))
        with pytest.raises(UpstreamHTTPError):
            geste(c)


def test_une_panne_reseau_leve_aussi():
    """Sans statut HTTP à porter, ça remonte quand même — jamais un dict."""
    c = PennylaneClient(api_key="k", rate_limit_delay=0)

    def _casse(*a, **kw):
        raise OSError("connexion coupée")

    c.session.request = _casse
    with pytest.raises(RuntimeError, match="connexion coupée"):
        c.fetch("me")


def test_une_reponse_sans_json_lisible_leve_en_le_disant():
    """Avant, elle devenait un dict d'erreur ; maintenant elle nomme ce qui est
    arrivé, sinon l'appelant croit à un corps vide."""
    c = _client_repondant(_Reponse(200, b"<html>maintenance</html>"))
    with pytest.raises(RuntimeError, match="sans JSON lisible"):
        c.fetch("me")


def test_un_204_reste_un_succes():
    """Une suppression rend 204 sans corps : ce n'est pas un refus."""
    c = _client_repondant(_Reponse(204, b""))
    assert c.delete("customer_invoices/1") == {"ok": True}


def test_fetch_all_pages_propage_au_lieu_d_avaler_en_liste_vide():
    c = _client_repondant(_Reponse(401, b'{"message":"invalid key"}'))
    with pytest.raises(UpstreamHTTPError):
        c.fetch_all_pages("customer_invoices")


def test_find_invoice_propage_au_lieu_de_conclure_a_rien():
    """Le cœur du risque : sur 401, l'anti-doublon doit LEVER, pas rendre None —
    sinon un avoir est recréé en double."""
    c = _client_repondant(_Reponse(401, b'{"message":"invalid key"}'))
    with pytest.raises(UpstreamHTTPError):
        c.find_invoice_by_external_reference("gocardless-payment-xyz")


# --- Anti-doublon : filtre serveur, plus de scan borné (signal #268) ---------

class _Spy:
    """Capture le dernier (endpoint, params) et rend une réponse fixée."""
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, endpoint, params=None, retries=3):
        self.calls.append((endpoint, params))
        return self.payload


def _client_with(spy):
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    c.fetch = spy
    return c


def test_find_invoice_uses_the_native_server_filter():
    import json
    spy = _Spy({"items": [{"id": 42, "external_reference": "AUT-70943"}]})
    c = _client_with(spy)
    assert c.find_invoice_by_external_reference("AUT-70943")["id"] == 42
    endpoint, params = spy.calls[-1]
    assert endpoint == "customer_invoices"
    assert json.loads(params["filter"]) == [
        {"field": "external_reference", "operator": "eq", "value": "AUT-70943"}]
    assert len(spy.calls) == 1          # un seul appel : plus de scan paginé


def test_find_invoice_returns_none_when_the_filter_matches_nothing():
    c = _client_with(_Spy({"items": []}))
    assert c.find_invoice_by_external_reference("ZZZ") is None


def test_find_customer_shares_the_same_guard():
    """Le chemin customers court-circuitait la règle #223 : sur 401 il rendait None,
    donc « ce client n'existe pas » — puis un create en 422."""
    c = _client_repondant(_Reponse(401, b'{"message":"invalid key"}'))
    with pytest.raises(UpstreamHTTPError):
        c.find_customer_by_external_reference("mm-companyId-7")
