"""`cursor_with_limit` (#179) : le limit de l'appel courant prime sur celui figé
dans le cursor Unipile (base64 de {limit, startIndex})."""
import base64
import json

from oto.tools.unipile.client import cursor_with_limit


def _cursor(d: dict) -> str:
    return base64.b64encode(json.dumps(d).encode()).decode()


def test_limit_is_rewritten():
    c = _cursor({"limit": 3, "startIndex": 6})
    out = json.loads(base64.b64decode(cursor_with_limit(c, 100)))
    assert out == {"limit": 100, "startIndex": 6}


def test_unpadded_base64_ok():
    c = _cursor({"limit": 3, "startIndex": 6}).rstrip("=")
    out = json.loads(base64.b64decode(cursor_with_limit(c, 50)))
    assert out["limit"] == 50 and out["startIndex"] == 6


def test_unexpected_shape_passthrough():
    assert cursor_with_limit("not-base64!!", 100) == "not-base64!!"
    scalar = _cursor({"startIndex": 6})  # pas de clé limit → intouché
    assert cursor_with_limit(scalar, 100) == scalar
