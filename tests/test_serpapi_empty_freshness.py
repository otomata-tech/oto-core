"""Un résultat VIDE ne doit jamais sortir d'un cache — il doit être constaté frais.

Signal d'usage #456 (mission Audiens, 2026-08-27), qui clôt et corrige #454 et #455 :
la même requête rendait 0 offre puis 1 offre avec `no_cache=True`, à quelques
secondes d'intervalle. Un zéro s'était installé en cache. Or ce connecteur sert
d'INDICATEUR D'ACTIVITÉ (« une maison qui recrute se développe ») : un zéro
mémorisé devient une absence fausse et PERSISTANTE, indiscernable d'une vraie
absence puisque c'est exactement ce que le champ est censé pouvoir dire. Sur les
8 900 lignes d'une campagne traitées une seule fois chacune, rien ne la rattrape.

Le cache n'est PAS le nôtre : il n'y a aucun cache dans oto-core ni dans le
backend. Mesuré le 2026-08-27 sur l'API réelle, il y en a même DEUX en série —
le cache de recherche de SerpApi (même `search_metadata.id`, même `created_at`,
réponse en 0,0 s) et le cache d'arête Cloudflare devant lui (`cf-cache-status:
HIT`, `cache-control: max-age=3600, public`, en-tête `Age`). Les deux tiennent
une heure, et les deux mémorisent le payload d'un vide, qui porte pourtant
`error: "Google hasn't returned any results for this query."` et
`search_information.jobs_results_state: "Fully empty"`.

On ne peut donc pas « ne pas mettre le vide en cache » : on ne possède pas le
stockage. Ce qu'on possède, c'est la LECTURE — d'où la règle vérifiée ici : un
vide n'est acceptable que s'il vient d'être constaté ; sinon on le refait une
fois, en forçant `no_cache`. Un résultat NON vide, lui, garde le droit au cache
(une offre publiée il y a quarante minutes reste un signal d'activité valide, et
c'est là que les 0,0 s gagnent leur place).

Le faux amont ci-dessous n'affirme aucune intention : il REPRODUIT le
comportement mesuré (un cache d'une heure, contourné par `no_cache=true`), et
les tests portent sur ce que l'appelant reçoit.
"""
from __future__ import annotations

import datetime as dt

import pytest

from oto.tools.serpapi.client import SerpAPIClient


def _payload(items: list, age_seconds: float, results_key: str = "jobs_results",
             horodate: bool = True) -> dict:
    """Un payload SerpApi tel que mesuré, daté de `age_seconds` dans le passé.

    `created_at` est l'horodatage que SerpApi appose au moment où il interroge
    réellement Google ; resservi par un cache, il reste figé à ce moment-là.
    C'est le seul témoin d'âge présent dans le corps de la réponse — les caches
    l'ont d'ailleurs recopié à l'identique lors de la mesure.
    """
    created = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds)
    metadata: dict = {"id": f"id-{age_seconds}", "status": "Success"}
    if horodate:
        metadata["created_at"] = created.strftime("%Y-%m-%d %H:%M:%S UTC")
    out: dict = {"search_metadata": metadata, results_key: list(items)}
    if not items:
        out["error"] = "Google hasn't returned any results for this query."
        out["search_information"] = {"jobs_results_state": "Fully empty"}
    return out


class _FauxSerpApi:
    """L'amont mesuré : un cache d'une heure, que `no_cache=true` contourne."""

    def __init__(self, en_cache: dict | None, frais: dict):
        self._en_cache = en_cache
        self._frais = frais
        self.appels: list[dict] = []

    def __call__(self, params: dict) -> dict:
        self.appels.append(dict(params))
        if params.get("no_cache") == "true" or self._en_cache is None:
            return self._frais
        return self._en_cache


@pytest.fixture
def amont(monkeypatch):
    """Branche un faux amont sur le seam de transport et rend le brancheur."""
    def brancher(en_cache: dict | None, frais: dict) -> _FauxSerpApi:
        faux = _FauxSerpApi(en_cache, frais)
        monkeypatch.setattr(SerpAPIClient, "_request",
                            lambda self, params: faux(params))
        return faux
    return brancher


def _client() -> SerpAPIClient:
    return SerpAPIClient(api_key="k")


# --- le défaut du signal #456 -------------------------------------------------

def test_un_vide_resservi_par_le_cache_ne_sort_pas_du_client(amont):
    """LE défaut : le zéro mémorisé une demi-heure plus tôt masquait une offre
    réelle. Le client doit refaire l'appel et rendre ce que l'amont contient
    VRAIMENT — c'est le cas exact du signal (0 offre, puis 1 avec `no_cache`)."""
    faux = amont(en_cache=_payload([], age_seconds=1800),
                 frais=_payload([{"title": "Responsable stratégie événementielle"}],
                                age_seconds=0))

    r = _client().search_jobs(query="Editis")

    assert [j["title"] for j in r["jobs_results"]] == [
        "Responsable stratégie événementielle"]
    assert faux.appels[-1]["no_cache"] == "true"


def test_un_vide_perime_reste_vide_si_l_amont_est_vraiment_vide(amont):
    """La reprise ne fabrique rien : quand l'absence est réelle, elle est rendue
    — mais CONSTATÉE, ce qui est toute la différence pour l'appelant."""
    faux = amont(en_cache=_payload([], age_seconds=1800),
                 frais=_payload([], age_seconds=0))

    r = _client().search_jobs(query="maison sans offre")

    assert r["jobs_results"] == []
    assert len(faux.appels) == 2, "une seule reprise, jamais de boucle"


def test_un_vide_sans_horodatage_est_traite_comme_perime(amont):
    """Un payload sans `created_at` ne peut pas certifier sa fraîcheur. On penche
    du côté de la réponse juste, jamais du côté de la réponse rapide."""
    faux = amont(en_cache=_payload([], age_seconds=1800, horodate=False),
                 frais=_payload([{"title": "x"}], age_seconds=0))

    assert _client().search_jobs(query="q")["jobs_results"] == [{"title": "x"}]
    assert faux.appels[-1]["no_cache"] == "true"


# --- ce que la règle ne doit PAS coûter ---------------------------------------

def test_un_vide_frais_est_rendu_tel_quel_sans_second_appel(amont):
    """86 % du fichier client rend zéro offre (signal #454) : refaire l'appel sur
    TOUT vide doublerait le coût du cas dominant, en temps comme en crédits. Un
    vide qu'on vient de constater est déjà la vérité — il n'y a rien à refaire."""
    faux = amont(en_cache=None, frais=_payload([], age_seconds=3))

    assert _client().search_jobs(query="petite maison")["jobs_results"] == []
    assert len(faux.appels) == 1


def test_un_resultat_non_vide_garde_le_droit_au_cache(amont):
    """Le cache n'est pas le coupable, son CONTENU vide l'est (#456). Une liste
    d'offres vieille de quarante minutes reste un indicateur d'activité valide,
    et c'est elle qui paie les 0,0 s au lieu des 5 à 20 s."""
    faux = amont(en_cache=_payload([{"title": "en cache"}], age_seconds=2400),
                 frais=_payload([{"title": "frais"}], age_seconds=0))

    assert _client().search_jobs(query="Editis")["jobs_results"] == [
        {"title": "en cache"}]
    assert len(faux.appels) == 1


# --- portée : là où le tableau de résultats est nommé -------------------------

def test_la_garantie_vaut_pour_le_search_generique_quand_le_tableau_est_nomme(amont):
    """Le défaut n'a rien de propre à Google Jobs : tout moteur voit ses vides
    mémorisés une heure. La garantie suit `results_key`, seul endroit où le
    client sait ce que « vide » veut dire."""
    faux = amont(
        en_cache=_payload([], age_seconds=1800, results_key="organic_results"),
        frais=_payload([{"link": "https://x"}], age_seconds=0,
                       results_key="organic_results"))

    r = _client().search("google", params={"q": "oto"},
                         results_key="organic_results")

    assert r["organic_results"] == [{"link": "https://x"}]
    assert faux.appels[-1]["no_cache"] == "true"


def test_sans_results_key_le_client_ne_peut_pas_juger_du_vide(amont):
    """Aucune heuristique sur le brut : sans tableau nommé, on ne devine pas
    lequel porte la réponse — le payload passe, inchangé, en un seul appel."""
    faux = amont(en_cache=_payload([], age_seconds=1800), frais=_payload([], 0))

    _client().search("google_trends", params={"q": "oto"})

    assert len(faux.appels) == 1


def test_la_pagination_travaille_sur_le_resultat_repris(amont):
    """La reprise passe AVANT la pagination : sinon on paginerait le vide périmé
    et le `next_page_token` du cache mènerait la suite de la lecture."""
    frais = _payload([{"title": f"o{i}"} for i in range(5)], age_seconds=0)
    faux = amont(en_cache=_payload([], age_seconds=1800), frais=frais)

    r = _client().search_jobs(query="q", max_results=3)

    assert [j["title"] for j in r["jobs_results"]] == ["o0", "o1", "o2"]


# --- ce que la réponse dit d'elle-même ---------------------------------------

def test_la_reponse_dit_son_age_et_si_elle_a_ete_refaite(amont):
    """Demande 2 du signal #456 : l'appelant ne pouvait pas savoir si son zéro
    venait du cache. Il le sait désormais sans avoir à faire d'arithmétique sur
    `created_at`, et sait aussi quand on a repris l'appel pour lui."""
    amont(en_cache=_payload([], age_seconds=1800),
          frais=_payload([{"title": "x"}], age_seconds=0))
    r = _client().search_jobs(query="Editis")
    assert r["oto_freshness"]["refetched"] is True
    assert r["oto_freshness"]["age_seconds"] < 60

    amont(en_cache=_payload([{"title": "en cache"}], age_seconds=2400),
          frais=_payload([], age_seconds=0))
    r = _client().search_jobs(query="Editis")
    assert r["oto_freshness"]["refetched"] is False
    assert r["oto_freshness"]["age_seconds"] == pytest.approx(2400, abs=30)
