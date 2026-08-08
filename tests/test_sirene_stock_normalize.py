"""Le stock SIRENE porte des sentinelles textuelles, pas que des nombres (signal #358).

Un établissement non diffusible sort `[ND]` dans les colonnes de géolocalisation.
Le `float()` nu qui les lisait faisait mourir le scan ENTIER sur la première ligne
concernée — un `--all` sur un NAF (~10 000 établissements) ne rendait rien.

Une coordonnée absente est une donnée manquante ordinaire : le reste de la fiche
(adresse, NAF, effectifs) est valide et doit sortir.
"""
from __future__ import annotations

import pytest

from oto.tools.sirene.stock import SireneStock

_BASE = {"siren": "123456789", "siret": "12345678900012", "code_postal": "13001",
         "libelle_commune": "MARSEILLE", "naf": "58.11Z", "etat": "A"}


@pytest.mark.parametrize("x,y", [("[ND]", "[ND]"), ("[ND]", "6247890.1"), ("", ""), (None, None)])
def test_undisclosed_coordinates_do_not_kill_the_record(x, y):
    out = SireneStock._normalize({**_BASE, "lambert_x": x, "lambert_y": y})
    assert "lambert_x" not in out          # coordonnée absente, pas une exception
    assert out["naf"] == "58.11Z"          # …et la fiche sort quand même
    assert out["city"] == "MARSEILLE"


def test_real_coordinates_are_still_floats():
    out = SireneStock._normalize({**_BASE, "lambert_x": "912345.6", "lambert_y": "6247890.1"})
    assert (out["lambert_x"], out["lambert_y"]) == (912345.6, 6247890.1)


def test_half_disclosed_coordinates_keep_what_exists():
    out = SireneStock._normalize({**_BASE, "lambert_x": "912345.6", "lambert_y": "[ND]"})
    assert out["lambert_x"] == 912345.6 and out["lambert_y"] is None
