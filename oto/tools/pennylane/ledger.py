"""Grand livre Pennylane — lecture : écritures, journaux, lignes, lettrage.

Second module du client (cf. `brevo` pour le même découpage) : `PennylaneClient`
en hérite et lui fournit le transport (`fetch`, `fetch_all_pages`, `post`,
`delete`). Séparé parce que le grand livre est un domaine à lui, et que
`client.py` avait déjà dépassé la taille où on relit un fichier.

**Trois scopes distincts, pas un.** Pennylane a éclaté l'ancien scope `ledger` :
lire les journaux demande `journals:*`, lire le plan comptable
`ledger_accounts:*`, lire ou écrire les écritures `ledger_entries:*`. Une clé
qui lit les écritures ne lit donc pas forcément les journaux — et le périmètre
est propre à qui a posé la clé. Les droits effectifs se lisent sur `GET /me`,
champ `scopes`.

**Le lettrage d'ici n'est pas le rapprochement bancaire.** Le mot « lettrage »
recouvre deux gestes : associer une transaction bancaire à une facture
(`match_transaction`, plus haut dans le client), et associer entre elles des
lignes du grand livre (ici). Objets différents, endpoints différents.

Ce module est en LECTURE seule à ce stade : poser une écriture et lettrer des
lignes arrivent ensuite (oto-backend#872, pièces C et D). La lecture vient
d'abord parce que c'est elle qui permet de contrôler ce que les écritures font.
"""
from __future__ import annotations

import json
from typing import Optional


class LedgerMixin:
    """Les gestes du grand livre. Attend le transport de `PennylaneClient`."""

    # --- lecture -----------------------------------------------------------

    @staticmethod
    def filtre(clauses: list[dict]) -> str:
        """Encode des clauses au format que Pennylane attend en query.

        Le paramètre `filter` est une CHAÎNE JSON, pas un objet : une liste de
        `{"field": …, "operator": …, "value": …}`. Exemple servi par la doc :
        `[{"field": "date", "operator": "gteq", "value": "2026-01-01"}]`.

        Champs filtrables sur les écritures : `id`, `date`, `journal_id`.
        Opérateurs : `lt`, `lteq`, `gt`, `gteq`, `eq`, `not_eq`, plus `in` et
        `not_in` sur `id` et `journal_id`.
        """
        return json.dumps(clauses, separators=(",", ":"))

    def get_journals(self, max_pages: Optional[int] = None) -> list:
        """Les journaux de la société — `{id, code, label, type}`.

        Prérequis de `create_ledger_entry`, qui exige un `journal_id`. Ces ids
        sont **propres à la société** : les résoudre à chaque fois, jamais les
        coder en dur. Scope `journals:readonly` ou `journals:all`.
        """
        return self.fetch_all_pages("journals", max_pages=max_pages)

    def get_ledger_accounts(self) -> list:
        """Le plan comptable — les `ledger_account_id` qu'exige chaque ligne
        d'écriture. Scope `ledger_accounts:readonly` ou `ledger_accounts:all`."""
        return self.fetch_all_pages("ledger_accounts")

    def get_ledger_entries(self, max_pages: Optional[int] = None,
                           clauses: Optional[list[dict]] = None) -> list:
        """Les écritures du grand livre.

        ⚠️ Sans `clauses`, TOUT l'historique remonte — sur une comptabilité
        réelle, des milliers d'écritures. Filtrer à la source : `clauses` est
        une liste de `{"field", "operator", "value"}` (cf. `filtre`), le seul
        moyen de retrouver une écriture sans tout paginer.
        """
        params = {"filter": self.filtre(clauses)} if clauses else None
        return self.fetch_all_pages("ledger_entries", params=params,
                                    max_pages=max_pages)

    def get_ledger_entry(self, entry_id: int) -> dict:
        """UNE écriture par son id — la relecture de ce qu'on vient de poser.

        Scope `ledger_entries:readonly` ou `ledger_entries:all`.
        """
        return self.fetch(f"ledger_entries/{entry_id}")

    def get_ledger_entry_lines(self, entry_id: int,
                               max_pages: Optional[int] = None) -> list:
        """Les lignes d'une écriture, avec leur `id` — ce que consomme le lettrage."""
        return self.fetch_all_pages(f"ledger_entries/{entry_id}/ledger_entry_lines",
                                    max_pages=max_pages)

    def get_lettered_lines(self, line_id: int,
                           max_pages: Optional[int] = None) -> list:
        """Les lignes lettrées AVEC une ligne donnée.

        La seule façon de constater ce qu'un lettrage a réellement embarqué : le
        geste est absorbant (cf. `letter_ledger_entry_lines`), donc son résultat
        n'est pas toujours ce qu'on a demandé.
        """
        return self.fetch_all_pages(
            f"ledger_entry_lines/{line_id}/lettered_ledger_entry_lines",
            max_pages=max_pages)
