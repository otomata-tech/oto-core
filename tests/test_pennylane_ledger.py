"""Grand livre Pennylane, lecture — écritures, journaux, lignes, lettrage.

La lecture vient avant l'écriture dans ce lot (oto-backend#872) : c'est elle qui
permettra de contrôler ce que les écritures posent. Les épreuves visent les deux
endroits où une lecture se trompe en silence :

 * le **format du filtre**, qui est une chaîne JSON et non un objet — passer la
   liste telle quelle produit une query que l'API ne comprend pas, et rend une
   page de résultats non filtrés qui ressemble à un succès ;
 * les **chemins**, cités en clair pour qu'un renommage amont se voie ici.
"""
import json

from oto.tools.pennylane.client import PennylaneClient


def _client():
    """Un client dont le transport est remplacé par un mouchard."""
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    c.appels = []

    def _pages(endpoint, params=None, max_pages=None, per_page=100):
        c.appels.append((endpoint, params))
        return []

    def _un(endpoint, params=None, retries=3):
        c.appels.append((endpoint, params))
        return {}

    c.fetch_all_pages = _pages
    c.fetch = _un
    return c


def test_le_filtre_part_en_chaine_json_dans_le_parametre_filter():
    """Pennylane attend une CHAÎNE JSON, pas un objet."""
    c = _client()
    clauses = [{"field": "date", "operator": "gteq", "value": "2026-01-01"}]
    c.get_ledger_entries(clauses=clauses)
    endpoint, params = c.appels[0]
    assert endpoint == "ledger_entries"
    assert isinstance(params["filter"], str), "un objet ici serait mal sérialisé"
    assert json.loads(params["filter"]) == clauses


def test_sans_clause_aucun_filtre_n_est_envoye():
    """Un `filter` vide n'est pas la même chose qu'aucun `filter`."""
    c = _client()
    c.get_ledger_entries()
    assert c.appels[0][1] is None


def test_les_lectures_visent_les_chemins_de_l_api():
    c = _client()
    c.get_journals()
    c.get_ledger_accounts()
    c.get_ledger_entry(42)
    c.get_ledger_entry_lines(42)
    c.get_lettered_lines(7)
    assert [a[0] for a in c.appels] == [
        "journals",
        "ledger_accounts",
        "ledger_entries/42",
        "ledger_entries/42/ledger_entry_lines",
        "ledger_entry_lines/7/lettered_ledger_entry_lines",
    ]


def test_le_plan_comptable_reste_servi_apres_le_deplacement():
    """`get_ledger_accounts` a changé de fichier (client.py → le mixin) : ses
    appelants — dont `fetch_complete_data` et le tool `pennylane_ref` — passent
    par la classe, que l'héritage doit continuer de servir."""
    assert PennylaneClient.get_ledger_accounts is not None
    c = _client()
    c.get_ledger_accounts()
    assert c.appels == [("ledger_accounts", None)]


def test_max_pages_traverse_jusqu_au_transport():
    """La borne de volume est le seul garde-fou sur un grand livre réel."""
    vus = {}
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    c.fetch_all_pages = lambda endpoint, params=None, max_pages=None, per_page=100: (
        vus.update(max_pages=max_pages) or [])
    c.get_ledger_entries(max_pages=3)
    assert vus["max_pages"] == 3
