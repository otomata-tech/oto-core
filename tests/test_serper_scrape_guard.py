"""Refuser d'emblée un scrape que l'hôte ne servira jamais.

Serper met ~45 s — son propre timeout — pour renoncer sur un site qui exige une
session. Sur quatre jours de journal : six échecs entre 45 et 48 s, dont QUATRE sur
des profils LinkedIn, que l'agent réessayait faute de savoir que la source était
close. Le garde rend l'échec immédiat et nomme l'outil qui sait lire la source.

Ce qu'il ne doit PAS devenir : une liste noire. Un site qui bloque parfois mérite
sa tentative — d'où les tests d'inclusion ET d'exclusion ci-dessous.
"""
from __future__ import annotations

import pytest

from oto.tools.serper.client import SerperClient


@pytest.mark.parametrize("url", [
    "https://fr.linkedin.com/in/florentletoullec",
    "https://uk.linkedin.com/in/charlotte-atyeo-6a9b7a19",
    "https://www.linkedin.com/company/otomata",
    "https://linkedin.com/in/x",
    "https://www.instagram.com/editions_du_parmelan/",
])
def test_a_closed_source_is_refused_immediately(url):
    assert SerperClient._refuses_scraping(url) is not None


def test_the_refusal_names_the_right_tool():
    why = SerperClient._refuses_scraping("https://fr.linkedin.com/in/qqun")
    assert "unipile" in why.lower()


@pytest.mark.parametrize("url", [
    "https://www.bouchard-mathieux.com/contact",
    "https://www.pagesjaunes.fr/pros/61376942",
    "https://www.numilog.com/static/CgsReadzis.html",
    "https://otomata.tech",
])
def test_an_ordinary_site_still_gets_its_attempt(url):
    """Ceux-là ont échoué eux aussi — mais par anti-bot ponctuel, pas par mur de
    connexion. Les bannir transformerait un garde-fou de latence en censure."""
    assert SerperClient._refuses_scraping(url) is None


def test_a_lookalike_domain_is_not_caught():
    """`linkedin.com.exemple.fr` n'est pas LinkedIn — la comparaison porte sur le
    domaine enregistrable, pas sur une sous-chaîne."""
    assert SerperClient._refuses_scraping("https://linkedin.com.exemple.fr/x") is None


def test_a_malformed_url_does_not_raise():
    assert SerperClient._refuses_scraping("pas une url") is None
