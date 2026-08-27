"""Lire une FENÊTRE d'issues Linear : bornes de date et ordre déclaré.

Deux signaux d'usage sur `linear_issue op=list` :
- **#561** — aucune borne de date (`updatedAt`/`createdAt`), donc toute lecture
  fenêtrée doit paginer puis filtrer côté client : l'appelant ne peut pas borner
  ce qu'il rapatrie.
- **#568** — et l'ordre de tri n'est annoncé nulle part. Relevé le 24/08/2026
  contre l'org 196 : la liste revient triée par identifiant décroissant. La page
  une ne contenait rien du jour visé alors qu'OTO-43, créée le 26 juillet, portait
  un `updatedAt` du 21 août plus bas ; un run qui fenêtre côté client doit donc
  parcourir TOUT le workspace au lieu de s'arrêter à la première page sans
  élément dans la fenêtre — ce que la consigne d'alors laissait croire.

Contrat amont (SDL publié dans `@linear/sdk` 92.0.0, lu le 27/08/2026) :
- `Query.issues(filter: IssueFilter, orderBy: PaginationOrderBy, first, after, …)` ;
- `IssueFilter.updatedAt` et `IssueFilter.createdAt` sont des `DateComparator`
  (`eq`/`gt`/`gte`/`lt`/`lte`/`neq`/`in`/`nin`) — la fenêtre existe bel et bien
  côté serveur, on ne l'exposait simplement pas ;
- `PaginationOrderBy` = `createdAt` | `updatedAt`, défaut `createdAt`
  (guide de pagination Linear : « By default results are ordered by `createdAt`
  field »), décroissant — cohérent avec le relevé du 24/08.

⚠️ En GraphQL un champ de filtre inconnu est une erreur DURE de validation, pas
un paramètre avalé : le risque « filtre ignoré en silence » d'une API REST ne se
pose pas ici. Ce qui se vérifie donc, c'est la construction de la requête —
et surtout le piège maison déjà documenté dans le client : une variable DÉCLARÉE
mais jamais référencée fait échouer l'opération entière.
"""
from __future__ import annotations

import pytest

from oto.tools.linear import client as lc


class _AnyKeyDict(dict):
    def __getitem__(self, key):
        return self.get(key, _AnyKeyDict())


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, body):
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {}

    def fake_post(self, url, **kwargs):
        seen.update(url=url, **kwargs)
        return _Resp({"data": _AnyKeyDict()})

    monkeypatch.setattr(lc.requests.Session, "post", fake_post)
    return seen


def _client():
    return lc.LinearClient(api_key="lin-test-token")


def _body(capture):
    return capture["json"]


def test_updated_after_borne_la_fenetre_cote_serveur(capture):
    _client().list_issues(updated_after="2026-08-24T00:00:00Z")
    b = _body(capture)
    assert "updatedAt: { gte: $updatedAfter }" in b["query"]
    assert "$updatedAfter: DateTimeOrDuration" in b["query"]
    assert b["variables"]["updatedAfter"] == "2026-08-24T00:00:00Z"


def test_les_deux_bornes_tiennent_dans_un_seul_comparateur(capture):
    """`gte` et `lte` sont deux clés du MÊME `DateComparator` — les émettre en
    deux clauses `updatedAt:` séparées serait un objet d'entrée invalide."""
    _client().list_issues(updated_after="2026-08-24T00:00:00Z",
                          updated_before="2026-08-25T00:00:00Z")
    q = _body(capture)["query"]
    assert "updatedAt: { gte: $updatedAfter, lte: $updatedBefore }" in q
    assert q.count("updatedAt:") == 1


def test_createdAt_et_updatedAt_cohabitent_avec_les_filtres_d_id(capture):
    _client().list_issues(team_id="t1", created_after="2026-08-01T00:00:00Z",
                          updated_before="2026-08-27T00:00:00Z")
    b = _body(capture)
    assert "team: { id: { eq: $teamId } }" in b["query"]
    assert "createdAt: { gte: $createdAfter }" in b["query"]
    assert "updatedAt: { lte: $updatedBefore }" in b["query"]
    assert b["variables"] == {
        "first": 50, "teamId": "t1",
        "createdAfter": "2026-08-01T00:00:00Z",
        "updatedBefore": "2026-08-27T00:00:00Z",
    }


def test_aucune_variable_declaree_sans_etre_referencee(capture):
    """Le piège maison du client (confirmé en live le 21/08) : une variable
    déclarée et jamais utilisée fait échouer TOUTE l'opération. Les bornes de
    date se déclarent donc en lockstep, comme les filtres d'id."""
    _client().list_issues(updated_after="2026-08-24T00:00:00Z")
    q = _body(capture)["query"]
    for var in ("$createdAfter", "$createdBefore", "$updatedBefore"):
        assert var not in q, f"{var} déclarée sans être référencée"


def test_order_by_est_transmis_et_type(capture):
    _client().list_issues(order_by="updatedAt")
    b = _body(capture)
    assert "$orderBy: PaginationOrderBy" in b["query"]
    assert "orderBy: $orderBy" in b["query"]
    assert b["variables"]["orderBy"] == "updatedAt"


def test_order_by_hors_enum_est_refuse_en_le_nommant():
    """`PaginationOrderBy` ne connaît que `createdAt` et `updatedAt` : tout
    autre valeur ferait échouer la requête côté Linear, avec un message qui ne
    dit pas à l'appelant ce qui était permis."""
    with pytest.raises(ValueError, match="order_by"):
        _client().list_issues(order_by="identifier")


def test_sans_borne_ni_ordre_la_requete_ne_change_pas(capture):
    """Non-régression : le chemin nu reste exactement celui d'avant — ni
    `filter:`, ni `orderBy:` inventés."""
    _client().list_issues()
    q = _body(capture)["query"]
    assert "filter:" not in q
    assert "orderBy" not in q
