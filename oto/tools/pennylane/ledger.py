"""Grand livre Pennylane — écritures, journaux, lettrage de lignes.

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
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Optional


def _somme(lignes: list[dict], champ: str) -> Decimal:
    total = Decimal(0)
    for ligne in lignes:
        brut = str(ligne.get(champ) or "0").strip() or "0"
        try:
            total += Decimal(brut)
        except InvalidOperation:
            raise ValueError(
                f"Montant illisible en `{champ}` : {brut!r}. Pennylane attend une "
                "chaîne décimale (ex. \"120.50\"), pas un nombre ni une expression.")
    return total


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

    # --- écriture ----------------------------------------------------------

    @staticmethod
    def controler_ecriture(ledger_entry_lines: list[dict]) -> dict:
        """Contrôle une écriture SANS l'écrire, et rend son récapitulatif.

        Séparé de `create_ledger_entry` pour qu'un appelant puisse montrer à un
        humain ce qui sera posé, avec ses totaux, AVANT de le poser — le geste
        n'ayant pas de brouillon chez Pennylane. Les deux chemins partagent donc
        la même règle : ce qui est validé ici est exactement ce qui partira.

        Lève sur refus, avec de quoi corriger. Rend `{lignes, total_debit,
        total_credit}` sinon.
        """
        if not ledger_entry_lines:
            raise ValueError("Une écriture comptable exige au moins une ligne.")
        if len(ledger_entry_lines) > 1000:
            raise ValueError(
                f"{len(ledger_entry_lines)} lignes : Pennylane en accepte 1000 au "
                "plus par requête. Découper l'écriture.")
        debits, credits = _somme(ledger_entry_lines, "debit"), _somme(
            ledger_entry_lines, "credit")
        if debits != credits:
            raise ValueError(
                f"Écriture déséquilibrée : {debits} au débit contre {credits} au "
                f"crédit, écart de {debits - credits}. Pennylane la refuserait ; "
                "corriger les lignes avant de rappeler.")
        for i, ligne in enumerate(ledger_entry_lines):
            if not ligne.get("ledger_account_id"):
                raise ValueError(
                    f"Ligne {i} sans `ledger_account_id` : le compte du plan "
                    "comptable est obligatoire (cf. `get_ledger_accounts`).")
        return {"lignes": len(ledger_entry_lines), "total_debit": str(debits),
                "total_credit": str(credits)}

    def create_ledger_entry(self, date: str, label: str, journal_id: int,
                            ledger_entry_lines: list[dict],
                            due_date: Optional[str] = None,
                            currency: Optional[str] = None,
                            piece_number: Optional[str] = None) -> dict:
        """Crée une écriture au grand livre — `POST /ledger_entries`.

        Scope `ledger_entries:all`. Chaque ligne porte `debit`, `credit` (des
        **chaînes** décimales) et `ledger_account_id` ; `label` est optionnel.

        ⚠️ **Pennylane n'a pas de brouillon pour une écriture comptable.** Le
        reste du connecteur est brouillon-d'abord (une facture se crée en
        brouillon, se finalise ensuite) ; ici l'écriture est immédiatement
        posée. L'appelant doit donc annoncer le détail exact AVANT d'appeler, et
        savoir que le défaire passe par `PUT /ledger_entries/{id}`, pas par une
        suppression.

        ⚠️ **L'ordre des lignes rendues n'est pas garanti** : pour retrouver
        l'id d'une ligne, apparier sur son contenu, jamais sur sa position.

        L'équilibre est vérifié ICI plutôt que laissé au 422 de Pennylane :
        l'écart chiffré est ce qui permet de corriger, « not balanced » ne l'est
        pas.
        """
        self.controler_ecriture(ledger_entry_lines)
        body = {"date": date, "label": label, "journal_id": journal_id,
                "ledger_entry_lines": ledger_entry_lines}
        if due_date:
            body["due_date"] = due_date
        if currency:
            body["currency"] = currency
        if piece_number:
            body["piece_number"] = piece_number
        return self.post("ledger_entries", body)

    def update_ledger_entry(self, entry_id: int, **fields) -> dict:
        """Modifie une écriture posée — `PUT /ledger_entries/{id}`.

        Le seul recours quand une écriture est fausse : il n'y a pas de
        suppression d'écriture dans l'API.

        ⚠️ **Ce geste peut DÉTRUIRE des lignes.** `ledger_entry_lines` y prend
        trois sous-objets — `create`, `update`, `delete` — et le `delete`
        supprime des lignes par id. Corriger n'est donc pas plus anodin que
        créer : l'appelant doit soumettre le détail à un humain de la même
        façon.
        """
        return self.put(f"ledger_entries/{entry_id}", fields)

    # --- lettrage de lignes ------------------------------------------------

    def letter_ledger_entry_lines(self, line_ids: list[int],
                                  unbalanced_lettering_strategy: str = "none") -> dict:
        """Lettre des lignes du grand livre entre elles.

        `POST /ledger_entry_lines/lettering` — noter le chemin : le slug de la
        doc dit « letter », l'OpenAPI sert `lettering`. Scope `ledger_entries:all`.

        ⚠️ **Le geste est absorbant** : si une ligne passée est déjà lettrée, le
        lettrage s'étend à ses lignes déjà associées. Demander [A, C] quand A et
        B sont lettrées rend [A, B, C]. Un appelant qui l'ignore élargit un
        lettrage sans le vouloir — d'où `get_lettered_lines` pour constater.

        `unbalanced_lettering_strategy` : `"none"` refuse un lettrage
        déséquilibré, `"partial"` l'accepte. Le défaut refuse, parce qu'un
        déséquilibre non voulu est plus coûteux qu'un appel rejeté.
        """
        return self.post("ledger_entry_lines/lettering",
                         self._corps_lettrage(line_ids, unbalanced_lettering_strategy))

    def unletter_ledger_entry_lines(self, line_ids: list[int],
                                    unbalanced_lettering_strategy: str = "none") -> dict:
        """Défait un lettrage — `DELETE /ledger_entry_lines/lettering`, même chemin.

        C'est ce qui rend le lettrage réversible, et donc sûr à exposer.
        """
        return self.delete("ledger_entry_lines/lettering",
                           self._corps_lettrage(line_ids,
                                                unbalanced_lettering_strategy))

    @staticmethod
    def _corps_lettrage(line_ids: list[int], strategie: str) -> dict:
        if strategie not in ("none", "partial"):
            raise ValueError(
                f"`unbalanced_lettering_strategy` vaut {strategie!r} : Pennylane "
                "n'accepte que 'none' (refuse un lettrage déséquilibré) ou "
                "'partial' (l'accepte).")
        if len(line_ids) < 2:
            raise ValueError(
                f"{len(line_ids)} ligne(s) : le lettrage en associe au moins deux.")
        return {"unbalanced_lettering_strategy": strategie,
                "ledger_entry_lines": [{"id": int(i)} for i in line_ids]}
