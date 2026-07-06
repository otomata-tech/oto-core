"""Tests du client Unipile **API v2** (`client_v2.UnipileClientV2`).

Verrouille : (1) les paths/params/corps v2 (account_id en path, endpoints
réorganisés), (2) la normalisation d'enveloppe `data/next_cursor → items/cursor`,
(3) la **garde anti-mismatch** identifier↔réponse (feedback #153), (4) le mapping
d'erreurs réseau propre + caviardage de l'account_id (#177/#178), (5) la factory.

Aucun réseau : `_request` est monkeypatché pour capturer (method, path, params,
json) et rendre une réponse canned.
"""

import pytest
import requests

from oto.tools.unipile import (
    UnipileClient,
    UnipileClientV2,
    make_unipile_client,
)
from oto.tools.unipile.client import UnipileError


def _client(canned=None, recorder=None):
    """Client v2 avec `_request` stubé. `canned` = valeur rendue (ou callable
    `(method, path, params, json) -> value`). `recorder` = liste qui reçoit les
    tuples d'appel."""
    c = UnipileClientV2(api_key="k", account_id="acc")

    def fake(method, path, params=None, json=None):
        if recorder is not None:
            recorder.append((method, path, params, json))
        if callable(canned):
            return canned(method, path, params, json)
        return canned

    c._request = fake  # type: ignore[method-assign]
    return c


# ---- factory -------------------------------------------------------------

def test_factory_selects_version():
    assert isinstance(make_unipile_client(api_key="k", api_version="v2"),
                      UnipileClientV2)
    assert isinstance(make_unipile_client(api_key="k", api_version="2"),
                      UnipileClientV2)
    assert isinstance(make_unipile_client(api_key="k"), UnipileClient)
    assert isinstance(make_unipile_client(api_key="k", api_version="v1"),
                      UnipileClient)


# ---- account_id dans le path --------------------------------------------

def test_account_id_in_path_and_norm():
    rec = []
    c = _client(canned={"data": [{"id": 1}], "next_cursor": "N", "total_count": 3},
                recorder=rec)
    out = c.list_chats(limit=5, cursor="C")
    method, path, params, json = rec[0]
    assert method == "GET"
    assert path == "/acc/chats"          # account_id en path
    assert params == {"limit": 5, "cursor": "C"}
    # normalisation : items/cursor ajoutés, natifs conservés
    assert out["items"] == [{"id": 1}]
    assert out["cursor"] == "N"
    assert out["data"] == [{"id": 1}] and out["next_cursor"] == "N"


# ---- profils : garde anti-mismatch (#153) -------------------------------

def test_get_profile_ok_when_identity_matches():
    c = _client(canned={"object": "UserProfile", "public_identifier": "john-doe",
                        "id": "ACoAAA"})
    out = c.get_profile("john-doe")
    assert out["public_identifier"] == "john-doe"


def test_get_profile_matches_on_provider_id():
    c = _client(canned={"object": "UserProfile", "public_identifier": "john-doe",
                        "id": "ACoAAA"})
    assert c.get_profile("ACoAAA")["id"] == "ACoAAA"  # demandé par provider id


def test_get_profile_rejects_wrong_member(recwarn=None):
    # Sous concurrence l'API a rendu un AUTRE membre → on rejette (#144-149).
    c = _client(canned={"object": "UserProfile", "public_identifier": "someone-else",
                        "id": "ACoBBB"})
    with pytest.raises(UnipileError) as e:
        c.get_profile("john-doe")
    assert "identity_mismatch" in str(e.value)


def test_get_profile_rejects_company_object():
    # Reçu un CompanyProfile à la place d'un UserProfile (#148/#149).
    c = _client(canned={"object": "CompanyProfile", "public_identifier": "john-doe"})
    with pytest.raises(UnipileError):
        c.get_profile("john-doe")


def test_get_company_rejects_user_object():
    c = _client(canned={"object": "UserProfile", "public_identifier": "acme"})
    with pytest.raises(UnipileError):
        c.get_company("acme")


# ---- résolution de slug tolérante (#176) --------------------------------

def _company_stub(known_slug, search_hits):
    """Stub `_request` : GET company connu → 200 ; GET inconnu → 404 ;
    POST search/companies → `search_hits`."""
    def fake(method, path, params=None, json=None):
        if method == "POST" and path.endswith("/linkedin/search/companies"):
            return {"data": search_hits}
        if method == "GET" and path.endswith(f"/linkedin/company/{known_slug}"):
            return {"object": "CompanyProfile", "public_identifier": known_slug}
        raise UnipileError("Unipile 404: Company not found", status_code=404)
    return fake


def test_get_company_falls_back_to_canonical_slug():
    # "mooniz" (marque) en 404 → search → public_identifier "mooniz1" → 200 (#176).
    c = _client()
    c._request = _company_stub("mooniz1", [{"public_identifier": "mooniz1"}])
    out = c.get_company("mooniz")
    assert out["public_identifier"] == "mooniz1"


def test_get_company_fallback_via_profile_url():
    # Candidat sans public_identifier → slug dérivé de l'URL /company/<slug>.
    c = _client()
    c._request = _company_stub(
        "mooniz1",
        [{"public_profile_url": "https://www.linkedin.com/company/mooniz1/"}],
    )
    assert c.get_company("mooniz")["public_identifier"] == "mooniz1"


def test_get_company_404_lists_candidates_when_retry_fails():
    # Search rend des candidats mais aucun ne résout → 404 propre + candidats.
    c = _client()

    def fake(method, path, params=None, json=None):
        if method == "POST" and path.endswith("/linkedin/search/companies"):
            return {"data": [{"public_identifier": "other-co"}]}
        raise UnipileError("Unipile 404: Company not found", status_code=404)

    c._request = fake
    with pytest.raises(UnipileError) as e:
        c.get_company("mooniz")
    assert e.value.status_code == 404
    assert "other-co" in str(e.value)


def test_get_company_numeric_id_no_fallback():
    # Un id numérique introuvable ne déclenche PAS de recherche par nom.
    calls = []
    c = _client()

    def fake(method, path, params=None, json=None):
        calls.append((method, path))
        raise UnipileError("Unipile 404: Company not found", status_code=404)

    c._request = fake
    with pytest.raises(UnipileError):
        c.get_company("93154483")
    assert not any("search" in p for _, p in calls)  # pas de fallback recherche


def test_get_company_resolve_false_disables_fallback():
    calls = []
    c = _client()

    def fake(method, path, params=None, json=None):
        calls.append(path)
        raise UnipileError("Unipile 404: Company not found", status_code=404)

    c._request = fake
    with pytest.raises(UnipileError):
        c.get_company("mooniz", resolve=False)
    assert not any("search" in p for p in calls)


def test_get_profile_with_sections_mapping():
    rec = []
    c = _client(canned={"object": "UserProfile", "public_identifier": "x", "id": "x"},
                recorder=rec)
    c.get_profile("x", sections="experience,education")
    _, path, params, _ = rec[0]
    assert path == "/acc/users/x"
    assert params["with_sections"] == ["linkedin_experience", "linkedin_education"]

    rec.clear()
    c.get_profile("x", sections="*")   # tout → pas de param
    assert "with_sections" not in (rec[0][2] or {})


def test_get_own_profile_skips_guard():
    rec = []
    c = _client(canned={"object": "UserProfile", "public_identifier": "me-real"},
                recorder=rec)
    c.get_own_profile()   # pas de mismatch même si l'id ≠ "me"
    assert rec[0][1] == "/acc/users/me"


# ---- invitations / relations --------------------------------------------

def test_list_invitations_type_param():
    rec = []
    c = _client(canned={"data": []}, recorder=rec)
    c.list_invitations(direction="sent", limit=10)
    _, path, params, _ = rec[0]
    assert path == "/acc/users/me/relation-requests"
    assert params["type"] == "sent" and params["limit"] == 10


def test_send_invitation_body():
    rec = []
    c = _client(canned={}, recorder=rec)
    c.send_invitation("ACoAAA", message="hi")
    _, path, _, json = rec[0]
    assert path == "/acc/users/me/relation-requests"
    assert json == {"user_id": "ACoAAA", "message": "hi"}


def test_handle_invitation_accept_cancel():
    rec = []
    c = _client(canned={}, recorder=rec)
    c.handle_invitation("REQ1", "secret", action="accept")
    assert rec[0][1] == "/acc/users/me/relation-requests/REQ1/accept"
    c.handle_invitation("REQ2", "secret", action="decline")
    assert rec[1][1] == "/acc/users/me/relation-requests/REQ2/cancel"


# ---- messagerie ----------------------------------------------------------

def test_send_message_existing_vs_new_chat():
    rec = []
    c = _client(canned={}, recorder=rec)
    c.send_message("hello", chat_id="CH1")
    assert rec[0][1] == "/acc/chats/CH1/messages/send"
    assert rec[0][3] == {"text": "hello"}
    c.send_message("hi", attendee_id="ACoAAA")
    assert rec[1][1] == "/acc/chats/send"
    assert rec[1][3] == {"users_ids": ["ACoAAA"], "text": "hi"}


def test_react_message_requires_chat_id_in_v2():
    c = _client(canned={})
    with pytest.raises(UnipileError):
        c.react_message("MSG1", "👍")   # pas de chat_id → erreur claire


def test_react_message_path_with_chat_id():
    rec = []
    c = _client(canned={}, recorder=rec)
    c.react_message("MSG1", "👍", chat_id="CH1")
    assert rec[0][1] == "/acc/chats/CH1/messages/MSG1/reactions"


# ---- inmail (#178) + contrats -------------------------------------------

def test_inmail_balance_uses_v2_route():
    rec = []
    c = _client(canned={"object": "InmailCredits", "credits": 5}, recorder=rec)
    out = c.inmail_balance()
    assert rec[0][1] == "/acc/linkedin/inmail-credits"   # ≠ v1 inmail/balance
    assert out["credits"] == 5


# ---- recherche -----------------------------------------------------------

def test_search_people_body_and_path():
    rec = []
    c = _client(canned={"data": []}, recorder=rec)
    # ids numériques → pas de résolution de facette (pas d'appel réseau)
    c.search(keywords="CTO", company=["123"], location=["456"],
             network_distance=[1])
    method, path, _, json = rec[0]
    assert method == "POST" and path == "/acc/linkedin/search/people"
    assert json["keywords"] == "CTO"
    assert json["current_company"] == ["123"]   # v2 : current_company
    assert json["location"] == ["456"]
    assert json["network_distance"] == [1]


def test_search_companies_path():
    rec = []
    c = _client(canned={"data": []}, recorder=rec)
    c.search(category="companies", keywords="ESN", location=["456"])
    assert rec[0][1] == "/acc/linkedin/search/companies"


# ---- erreurs propres (#177/#178) ----------------------------------------

def test_network_error_mapped_and_account_sanitized():
    c = UnipileClientV2(api_key="k", account_id="acc-SECRET")

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("net::ERR_NAME_NOT_RESOLVED")

    c.session.request = boom  # type: ignore[method-assign]
    with pytest.raises(UnipileError) as e:
        c.list_relations()
    assert "erreur réseau" in str(e.value)
    assert "ERR_NAME_NOT_RESOLVED" not in str(e.value)   # pas de fuite scraper


def test_http_error_sanitizes_account_id():
    c = UnipileClientV2(api_key="k", account_id="acc-SECRET")

    class Resp:
        status_code = 404
        reason = "Not Found"
        text = "Cannot GET /v2/acc-SECRET/linkedin/inmail-credits"

        def json(self):
            return {"message": "Cannot GET /v2/acc-SECRET/linkedin/inmail-credits"}

    c.session.request = lambda *a, **k: Resp()  # type: ignore[method-assign]
    with pytest.raises(UnipileError) as e:
        c.inmail_balance()
    assert "acc-SECRET" not in str(e.value)      # account_id caviardé (#178)
    assert "<account>" in str(e.value)


# ---- hosted-auth v2 : body réécrit + réponse sur `link` (deltas live 2026-07-06) --

def test_hosted_auth_link_v2_body_and_link_key():
    rec = []
    c = _client(canned={"object": "HostedAuthLink", "link": "https://auth.unipile.com/x"},
                recorder=rec)
    url = c.hosted_auth_link(providers=["LINKEDIN"], name="nonce1",
                             notify_url="https://x/webhook",
                             success_redirect_url="https://x/ok",
                             failure_redirect_url="https://x/ko")
    assert url == "https://auth.unipile.com/x"        # lu sur `link`, pas `url`
    method, path, _, body = rec[0]
    assert method == "POST" and path == "/auth/link"
    assert body["providers"] == ["linkedin"]          # minuscule (v1 passait LINKEDIN)
    assert "expires_on" in body and "expiresOn" not in body
    assert body["redirect_uri"] == "https://x/ok"     # un seul redirect
    assert "success_redirect_url" not in body and "failure_redirect_url" not in body
    assert body["name"] == "nonce1" and body["notify_url"] == "https://x/webhook"


def test_hosted_auth_link_providers_wildcard_when_none():
    rec = []
    c = _client(canned={"link": "u"}, recorder=rec)
    c.hosted_auth_link()
    assert rec[0][3]["providers"] == "*"              # aucun provider → tous


# ---- posts/comments/reactions : slug public → provider_id URN (delta v2) --------

def _member_stub(recorder):
    def fake(method, path, params=None, json=None):
        recorder.append((method, path, params, json))
        if path.endswith("/users/john-doe"):         # get_profile(slug)
            return {"object": "UserProfile", "public_identifier": "john-doe",
                    "id": "ACoAAB123"}
        return {"data": []}
    return fake


def test_member_posts_resolves_slug_to_urn():
    rec = []
    c = UnipileClientV2(api_key="k", account_id="acc")
    c._request = _member_stub(rec)  # type: ignore[method-assign]
    c.list_member_posts("john-doe")
    paths = [p for _, p, _, _ in rec]
    assert "/acc/users/john-doe" in paths             # a résolu le profil
    assert "/acc/users/ACoAAB123/posts" in paths      # puis posts sur l'URN


def test_member_reactions_urn_skips_profile_lookup():
    rec = []
    c = UnipileClientV2(api_key="k", account_id="acc")
    c._request = _member_stub(rec)  # type: ignore[method-assign]
    c.list_member_reactions("ACoAAB999")              # déjà un URN
    paths = [p for _, p, _, _ in rec]
    assert paths == ["/acc/users/ACoAAB999/reactions"]  # aucune résolution de profil
