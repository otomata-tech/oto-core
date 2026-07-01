"""Leviers de volume + enrichissement messagerie Unipile (oto-backend#76).

- `list_invitations` : pagination `limit`/`cursor` (sans borne l'endpoint
  renvoie tout le backlog) + garde-fou de troncature si l'API ignore `limit`.
- `list_chats(with_attendee_names=True)` : les fils 1-à-1 LinkedIn arrivent
  avec `name: null` et un `attendee_provider_id` opaque → résolution BATCH via
  le carnet `/attendees` (jamais un appel par fil), best-effort.
"""

from oto.tools.unipile.client import UnipileClient


def _client(responses):
    """Client stubé : `responses` = liste de (method, path, params) → payload,
    matché séquentiellement par (method, path). Journalise les appels."""
    c = UnipileClient(api_key="k", account_id="acc")
    calls = []

    def fake_request(method, path, params=None, json=None):
        calls.append({"method": method, "path": path, "params": params or {}})
        for i, (m, p, payload) in enumerate(responses):
            if m == method and p == path:
                responses.pop(i)
                return payload
        raise AssertionError(f"appel inattendu : {method} {path}")

    c._request = fake_request
    c._calls = calls
    return c


# ---- list_invitations : limit / cursor ------------------------------------

def test_invitations_passes_limit_and_cursor():
    c = _client([("GET", "/users/invite/received", {"items": [{"id": "1"}]})])
    out = c.list_invitations("received", limit=10, cursor="CUR")
    params = c._calls[0]["params"]
    assert params["limit"] == 10
    assert params["cursor"] == "CUR"
    assert out["items"] == [{"id": "1"}]


def test_invitations_without_limit_sends_no_pagination_params():
    c = _client([("GET", "/users/invite/sent", {"items": []})])
    c.list_invitations("sent")
    params = c._calls[0]["params"]
    assert "limit" not in params and "cursor" not in params


def test_invitations_truncates_when_api_ignores_limit():
    items = [{"id": str(i)} for i in range(5)]
    c = _client([("GET", "/users/invite/received", {"items": items})])
    out = c.list_invitations("received", limit=2)
    assert len(out["items"]) == 2
    assert out["truncated"] is True


def test_invitations_no_truncation_marker_when_within_limit():
    c = _client([("GET", "/users/invite/received", {"items": [{"id": "1"}]})])
    out = c.list_invitations("received", limit=10)
    assert "truncated" not in out


# ---- list_chats : enrichissement attendee ----------------------------------

CHATS = {
    "items": [
        {"id": "c1", "name": None, "attendee_provider_id": "ACo111"},
        {"id": "c2", "name": "Groupe X", "attendee_provider_id": None},
        {"id": "c3", "name": None, "attendee_provider_id": "ACo333"},
    ]
}

ATTENDEES_PAGE = {
    "items": [
        {"provider_id": "ACo111", "name": "Jane Doe",
         "profile_url": "https://linkedin.com/in/jane",
         "specifics": {"occupation": "CEO at Acme"}},
        {"provider_id": "ACo333", "name": "John Smith", "specifics": {}},
    ],
    "cursor": None,
}


def test_chats_enriched_with_attendee_names():
    c = _client([
        ("GET", "/chats", CHATS),
        ("GET", "/attendees", ATTENDEES_PAGE),
    ])
    out = c.list_chats(limit=20, with_attendee_names=True)
    by_id = {it["id"]: it for it in out["items"]}
    assert by_id["c1"]["attendee_name"] == "Jane Doe"
    assert by_id["c1"]["attendee_headline"] == "CEO at Acme"
    assert by_id["c1"]["attendee_profile_url"] == "https://linkedin.com/in/jane"
    assert by_id["c3"]["attendee_name"] == "John Smith"
    # fil de groupe sans attendee_provider_id : intact
    assert "attendee_name" not in by_id["c2"]
    # le `name` brut n'est PAS réécrit (exposer le brut)
    assert by_id["c1"]["name"] is None


def test_chats_default_is_raw_no_extra_call():
    c = _client([("GET", "/chats", {"items": [
        {"id": "c1", "name": None, "attendee_provider_id": "ACo111"}]})])
    out = c.list_chats(limit=20)
    assert "attendee_name" not in out["items"][0]
    assert len(c._calls) == 1  # pas d'appel /attendees


def test_chats_enrichment_failure_still_returns_list():
    c = _client([("GET", "/chats", {"items": [
        {"id": "c1", "name": None, "attendee_provider_id": "ACo111"}]})])
    # tout appel /attendees lèvera (aucune réponse stubée) → best-effort
    out = c.list_chats(limit=20, with_attendee_names=True)
    assert out["items"][0]["id"] == "c1"
    assert "attendee_name" not in out["items"][0]


def test_resolve_attendee_names_stops_early_when_all_resolved():
    page1 = {"items": [{"provider_id": "A", "name": "Ann"}], "cursor": "NEXT"}
    c = _client([("GET", "/attendees", page1)])
    out = c.resolve_attendee_names({"A"})
    assert out["A"]["name"] == "Ann"
    assert len(c._calls) == 1  # cursor NEXT jamais suivi : tout est résolu


def test_resolve_attendee_names_paginates_until_found():
    page1 = {"items": [{"provider_id": "X", "name": "Xen"}], "cursor": "NEXT"}
    page2 = {"items": [{"provider_id": "B", "name": "Bob"}], "cursor": None}
    c = _client([("GET", "/attendees", page1), ("GET", "/attendees", page2)])
    out = c.resolve_attendee_names({"B"})
    assert out["B"]["name"] == "Bob"
    assert len(c._calls) == 2
    assert c._calls[1]["params"]["cursor"] == "NEXT"
