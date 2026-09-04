"""Grand livre Pennylane : écritures, journaux, lettrage de lignes.

Les épreuves visent les trois endroits où ce domaine se trompe en silence :

 * le **chemin du lettrage** — le slug de la doc dit `letter`, l'API sert
   `lettering`. Une lettre de différence, et le geste part sur un 404 ;
 * l'**équilibre** d'une écriture, qu'on vérifie avant l'appel pour rendre
   l'écart chiffré plutôt qu'un « not balanced » qui ne dit pas de combien ;
 * le **délettrage**, qui est un DELETE *avec un corps* — inhabituel, et
   silencieusement inopérant si le transport laisse tomber le corps.
"""
import json

import pytest

from oto.tools.pennylane.client import PennylaneClient


def _client():
    """Un client dont le transport est remplacé par un mouchard."""
    c = PennylaneClient(api_key="k", rate_limit_delay=0)
    c.appels = []

    def _mouchard(verbe):
        def _f(endpoint, data=None, **kw):
            c.appels.append((verbe, endpoint, data))
            return {"id": 1}
        return _f

    c.post = _mouchard("POST")
    c.delete = _mouchard("DELETE")
    c.put = _mouchard("PUT")

    def _pages(endpoint, params=None, max_pages=None, per_page=100):
        c.appels.append(("GET", endpoint, params))
        return []
    c.fetch_all_pages = _pages

    def _un(endpoint, params=None, retries=3):
        c.appels.append(("GET", endpoint, params))
        return {}
    c.fetch = _un
    return c


LIGNES = [{"debit": "120.00", "credit": "0", "ledger_account_id": 11},
          {"debit": "0", "credit": "120.00", "ledger_account_id": 22}]


# --- le chemin du lettrage -------------------------------------------------

def test_le_lettrage_vise_lettering_et_non_le_slug_letter():
    """Le piège de ce domaine : la doc s'appelle « …letter », l'API sert
    « lettering ». Le test cite le chemin en clair pour qu'un renommage se
    voie ici et pas en production."""
    c = _client()
    c.letter_ledger_entry_lines([1, 2])
    verbe, endpoint, _ = c.appels[0]
    assert (verbe, endpoint) == ("POST", "ledger_entry_lines/lettering"), c.appels


def test_le_delettrage_est_un_delete_avec_un_corps_sur_le_meme_chemin():
    """Défaire passe par le MÊME chemin, verbe opposé — et le corps porte les
    lignes. Un transport qui ignorerait le corps enverrait un délettrage vide
    qui ne délettre rien, sans erreur."""
    c = _client()
    c.unletter_ledger_entry_lines([7, 8])
    verbe, endpoint, corps = c.appels[0]
    assert (verbe, endpoint) == ("DELETE", "ledger_entry_lines/lettering")
    assert corps["ledger_entry_lines"] == [{"id": 7}, {"id": 8}], corps


def test_le_corps_du_lettrage_porte_les_deux_champs_requis():
    c = _client()
    c.letter_ledger_entry_lines([3, 4], unbalanced_lettering_strategy="partial")
    _, _, corps = c.appels[0]
    assert corps == {"unbalanced_lettering_strategy": "partial",
                     "ledger_entry_lines": [{"id": 3}, {"id": 4}]}


def test_le_lettrage_refuse_une_seule_ligne():
    """Lettrer, c'est associer : une ligne seule n'est pas un lettrage, et
    Pennylane l'aurait refusé après un aller-retour."""
    with pytest.raises(ValueError, match="au moins deux"):
        _client().letter_ledger_entry_lines([1])


def test_le_lettrage_refuse_une_strategie_inconnue():
    with pytest.raises(ValueError, match="'none'.*'partial'"):
        _client().letter_ledger_entry_lines([1, 2], unbalanced_lettering_strategy="oui")


def test_le_lettrage_refuse_par_defaut_un_desequilibre():
    """Le défaut doit être le choix prudent : `none` refuse un lettrage
    déséquilibré. Un défaut permissif passerait inaperçu."""
    c = _client()
    c.letter_ledger_entry_lines([1, 2])
    assert c.appels[0][2]["unbalanced_lettering_strategy"] == "none"


# --- l'équilibre d'une écriture --------------------------------------------

def test_une_ecriture_desequilibree_ne_part_pas_et_chiffre_l_ecart():
    c = _client()
    lignes = [{"debit": "120.00", "credit": "0", "ledger_account_id": 11},
              {"debit": "0", "credit": "100.00", "ledger_account_id": 22}]
    with pytest.raises(ValueError, match="écart de 20.00"):
        c.create_ledger_entry("2026-09-04", "OD", 5, lignes)
    assert c.appels == [], "rien ne doit partir sur le réseau"


def test_une_ecriture_equilibree_part_avec_les_quatre_champs_requis():
    c = _client()
    c.create_ledger_entry("2026-09-04", "OD prélèvement échoué", 5, LIGNES)
    verbe, endpoint, corps = c.appels[0]
    assert (verbe, endpoint) == ("POST", "ledger_entries")
    assert set(corps) == {"date", "label", "journal_id", "ledger_entry_lines"}
    assert corps["journal_id"] == 5


def test_les_optionnels_ne_sont_envoyes_que_fournis():
    """Envoyer `due_date: null` n'est pas la même chose que ne pas l'envoyer."""
    c = _client()
    c.create_ledger_entry("2026-09-04", "OD", 5, LIGNES, piece_number="OD-42")
    corps = c.appels[0][2]
    assert corps["piece_number"] == "OD-42"
    assert "due_date" not in corps and "currency" not in corps


def test_une_ecriture_sans_ligne_est_refusee():
    with pytest.raises(ValueError, match="au moins une ligne"):
        _client().create_ledger_entry("2026-09-04", "OD", 5, [])


def test_une_ligne_sans_compte_est_refusee_en_nommant_son_rang():
    c = _client()
    lignes = [{"debit": "10", "credit": "0"},
              {"debit": "0", "credit": "10", "ledger_account_id": 22}]
    with pytest.raises(ValueError, match="Ligne 0"):
        c.create_ledger_entry("2026-09-04", "OD", 5, lignes)


def test_un_montant_illisible_nomme_le_champ_fautif():
    c = _client()
    lignes = [{"debit": "12,50", "credit": "0", "ledger_account_id": 11},
              {"debit": "0", "credit": "12.50", "ledger_account_id": 22}]
    with pytest.raises(ValueError, match="debit"):
        c.create_ledger_entry("2026-09-04", "OD", 5, lignes)


def test_les_centimes_ne_sont_pas_arrondis_en_flottant():
    """0.1 + 0.2 ≠ 0.3 en flottant : une écriture juste serait refusée à tort.
    Le calcul passe par Decimal, ce test tomberait si on repassait au float."""
    c = _client()
    lignes = [{"debit": "0.10", "credit": "0", "ledger_account_id": 11},
              {"debit": "0.20", "credit": "0", "ledger_account_id": 11},
              {"debit": "0", "credit": "0.30", "ledger_account_id": 22}]
    c.create_ledger_entry("2026-09-04", "OD", 5, lignes)
    assert c.appels, "une écriture équilibrée au centime doit partir"


# --- lecture ----------------------------------------------------------------

def test_le_filtre_part_en_chaine_json_dans_le_parametre_filter():
    """Pennylane attend une CHAÎNE JSON, pas un objet : passer la liste telle
    quelle produirait une query que l'API ne comprend pas."""
    c = _client()
    clauses = [{"field": "date", "operator": "gteq", "value": "2026-01-01"}]
    c.get_ledger_entries(clauses=clauses)
    _, endpoint, params = c.appels[0]
    assert endpoint == "ledger_entries"
    assert isinstance(params["filter"], str)
    assert json.loads(params["filter"]) == clauses


def test_sans_clause_aucun_filtre_n_est_envoye():
    c = _client()
    c.get_ledger_entries()
    assert c.appels[0][2] is None


def test_les_lectures_visent_les_chemins_de_l_api():
    c = _client()
    c.get_journals()
    c.get_ledger_entry(42)
    c.get_ledger_entry_lines(42)
    c.get_lettered_lines(7)
    assert [a[1] for a in c.appels] == [
        "journals", "ledger_entries/42", "ledger_entries/42/ledger_entry_lines",
        "ledger_entry_lines/7/lettered_ledger_entry_lines"]


# --- le contrôle joué SANS écrire ------------------------------------------

def test_controler_ecriture_rend_les_totaux_sans_rien_appeler():
    """C'est ce que l'appelant montrera à un humain avant de poser l'écriture :
    il doit donc valoir contrôle, et ne toucher à rien."""
    c = _client()
    recap = c.controler_ecriture(LIGNES)
    assert recap == {"lignes": 2, "total_debit": "120.00", "total_credit": "120.00"}
    assert c.appels == []


def test_controler_ecriture_applique_la_MEME_regle_que_la_creation():
    """Les deux chemins doivent refuser exactement la même chose : si le
    contrôle validait ce que la création refuse, l'humain approuverait un détail
    qui ne partira jamais — ou pire, l'inverse."""
    c = _client()
    faux = [{"debit": "120.00", "credit": "0", "ledger_account_id": 11},
            {"debit": "0", "credit": "100.00", "ledger_account_id": 22}]
    with pytest.raises(ValueError, match="écart de 20.00"):
        c.controler_ecriture(faux)
    with pytest.raises(ValueError, match="écart de 20.00"):
        c.create_ledger_entry("2026-09-04", "OD", 5, faux)
    assert c.appels == []
