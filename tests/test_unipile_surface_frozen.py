"""La surface de `UnipileClient` est un CONTRAT, pas un détail.

oto-backend et oto-cli épinglent oto-core par tag et importent
`oto.tools.unipile[.client]` : renommer une méthode, changer un défaut ou
déplacer un symbole hors de `client.py` casse un consommateur **au bump du
pin**, loin d'ici. Ce test fige donc la liste des membres AVEC leurs signatures,
et la liste des noms importables depuis `oto.tools.unipile.client`.

Il a été posé avec le découpage du client en modules par domaine (2026-08-27,
1 702 lignes → `client.py` + `const.py`/`errors.py`/`feed.py` + `_api/*`) : sa
raison d'être est qu'un découpage ultérieur soit **prouvé** sans surface perdue,
pas seulement annoncé.

⚠️ Ce test échoue si tu ajoutes une méthode : c'est voulu — mets à jour la
constante EN CONNAISSANCE DE CAUSE (nouveau verbe = nouvelle surface publique à
assumer côté backend), jamais par réflexe pour faire passer le vert.
"""
import inspect

import oto.tools.unipile as pkg
import oto.tools.unipile.client as mod

# {nom de membre: signature str, ou None pour un attribut de classe}
EXPECTED_MEMBERS = {
    'LINKEDIN_PREMIUM_PRODUCTS': None,
    '_CHAT_ACTION_FIELD': None,
    '_acct': "(self, sub_path: 'str') -> 'str'",
    '_annotate_chat_attendees': "(self, data: 'Any') -> 'None'",
    '_as_facet_ids': "(self, facet_type: 'str', values: 'Optional[list[str]]') -> 'list[str]'",
    '_by_shape': "(self, inbox_call, plain_call, what: 'str') -> 'Any'",
    '_facet_field': "(self, facet_type: 'str', value, api: 'str', dict_input: 'bool' = False)",
    '_get_company_raw': "(self, identifier: 'str') -> 'dict'",
    '_identity_ok': "(requested: 'str', resp: 'dict', expect_object: 'str') -> 'bool'",
    '_member_id': "(self, identifier: 'str') -> 'str'",
    '_norm': "(data: 'Any') -> 'Any'",
    '_request': "(self, method: 'str', path: 'str', params: 'Optional[dict]' = None, json: 'Optional[dict]' = None, timeout: 'Optional[tuple]' = None) -> 'Any'",
    '_resolve_company_slugs': "(self, name: 'str', limit: 'int' = 5) -> 'list[str]'",
    '_sanitize': "(self, msg: 'str') -> 'str'",
    'account_alive': "(self, account_id: 'str') -> 'bool'",
    'account_id': "(self) -> 'str'",
    'cancel_invitation': "(self, invitation_id: 'str') -> 'dict'",
    'comment_post': "(self, post_id: 'str', text: 'str') -> 'dict'",
    'create_post': "(self, text: 'str') -> 'dict'",
    'delete_account': "(self, account_id: 'str') -> 'None'",
    'endorse_profile': "(self, profile_id: 'str', skill_endorsement_id: 'int') -> 'dict'",
    'get_company': "(self, identifier: 'str', resolve: 'bool' = True) -> 'dict'",
    'get_feed': "(self, count: 'int' = 20, cursor: 'Optional[str]' = None, raw: 'bool' = False, sort_order: 'str' = 'MEMBER_SETTING') -> 'dict'",
    'get_job_applicant': "(self, job_id: 'str', applicant_id: 'str') -> 'dict'",
    'get_job_posting': "(self, job_id: 'str') -> 'dict'",
    'get_own_profile': "(self) -> 'dict'",
    'get_post': "(self, post_id: 'str') -> 'dict'",
    'get_profile': "(self, identifier: 'str', sections: 'str' = '*') -> 'dict'",
    'handle_invitation': "(self, invitation_id: 'str', shared_secret: 'str', action: 'str' = 'accept') -> 'dict'",
    'hosted_auth_link': "(self, notify_url: 'Optional[str]' = None, providers: 'Optional[list[str]]' = None, name: 'Optional[str]' = None, success_redirect_url: 'Optional[str]' = None, failure_redirect_url: 'Optional[str]' = None, ttl_minutes: 'int' = 60, premium: 'Optional[str]' = None, allow_cookies: 'bool' = False, reconnect_account: 'Optional[str]' = None) -> 'str'",
    'inmail_balance': "(self) -> 'dict'",
    'linkedin_raw': "(self, request_url: 'str', method: 'str' = 'GET', body: 'Optional[dict]' = None, headers: 'Optional[dict]' = None, encoding: 'bool' = False, force_api: 'bool' = False) -> 'dict'",
    'list_accounts': "(self) -> 'list[dict]'",
    'list_attendees': "(self, cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_chat_attendees': "(self, chat_id: 'str') -> 'dict'",
    'list_chats': "(self, limit: 'int' = 20, cursor: 'Optional[str]' = None, with_attendee_names: 'bool' = False, inbox: 'str' = 'CLASSIC_PRIMARY') -> 'dict'",
    'list_comments': "(self, post_id: 'str', cursor: 'Optional[str]' = None) -> 'dict'",
    'list_contracts': "(self) -> 'dict'",
    'list_followers': "(self, user_id: 'Optional[str]' = None, cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_following': "(self, user_id: 'Optional[str]' = None, cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_hiring_projects': "(self, cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_inboxes': "(self) -> 'dict'",
    'list_invitations': "(self, direction: 'str' = 'received', limit: 'Optional[int]' = None, cursor: 'Optional[str]' = None) -> 'dict'",
    'list_job_applicants': "(self, job_id: 'str', cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_job_postings': "(self, cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_member_comments': "(self, identifier: 'str', cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_member_posts': "(self, identifier: 'str', cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_member_reactions': "(self, identifier: 'str', cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'list_messages': "(self, chat_id: 'str', limit: 'int' = 50) -> 'dict'",
    'list_reactions': "(self, post_id: 'str', cursor: 'Optional[str]' = None) -> 'dict'",
    'list_relations': "(self, cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict'",
    'member_action': "(self, user_id: 'str', api: 'str', action: 'str', hiring_project_id: 'Optional[str]' = None, stage: 'Optional[str]' = None, list_id: 'Optional[str]' = None) -> 'dict'",
    'patch_chat': "(self, chat_id: 'str', action: 'str', value: 'Any' = None) -> 'dict'",
    'react_message': "(self, message_id: 'str', reaction: 'str', chat_id: 'Optional[str]' = None) -> 'dict'",
    'react_post': "(self, post_id: 'str', value: 'str' = 'LIKE') -> 'dict'",
    'resolve_attendee_names': "(self, provider_ids, max_pages: 'int' = 10, page_limit: 'int' = 100) -> 'dict'",
    'resolve_facet': "(self, facet_type: 'str', keywords: 'str', limit: 'int' = 100) -> 'list[dict]'",
    'search': "(self, keywords: 'Optional[str]' = None, category: 'str' = 'people', company: 'Optional[list[str]]' = None, location: 'Optional[list[str]]' = None, cursor: 'Optional[str]' = None, api: 'str' = 'classic', network_distance: 'Optional[list[int]]' = None, url: 'Optional[str]' = None, advanced_keywords: 'Optional[dict]' = None, industry: 'Optional[dict]' = None, skills: 'Optional[list]' = None) -> 'dict'",
    'select_contract': "(self, contract_id: 'str') -> 'dict'",
    'send_invitation': "(self, provider_id: 'str', message: 'Optional[str]' = None) -> 'dict'",
    'send_message': "(self, text: 'str', chat_id: 'Optional[str]' = None, attendee_id: 'Optional[str]' = None, inbox: 'str' = 'CLASSIC_PRIMARY') -> 'dict'",
    'uses_inboxes': "(self) -> 'bool'",
}

# Noms importables depuis `oto.tools.unipile.client`. Les `_`-préfixés y sont
# parce qu'ils sont importés AILLEURS (tests du feed, du 429), pas parce qu'ils
# seraient publics.
EXPECTED_MODULE_NAMES = {
    'DEFAULT_DSN',
    'FEED_QUERY_ID',
    'UnipileClient',
    'UnipileError',
    'UnipileRateLimited',
    '_API_PREFIX',
    '_CAMEL_SPLIT',
    '_CONTENT_ALIASES',
    '_CONTENT_LABEL_KEYS',
    '_CONTENT_LABEL_MAX',
    '_CONTENT_PRIORITY',
    '_DEFAULT_PROVIDER',
    '_INBOX_PROVIDERS',
    '_REQUEST_TIMEOUT',
    '_RETRY_RE',
    '_SCRAPE_TIMEOUT',
    '_URL_SEARCH_TIMEOUT',
    '_activity_urn_from',
    '_annotated_entity',
    '_comment_authors',
    '_content_facets',
    '_content_key_to_type',
    '_content_label',
    '_deep_get',
    '_extract_activity',
    '_feed_context',
    '_is_promo',
    '_map_feed_item',
    '_parse_retry_after',
    '_posted_at_from_activity',
    '_sections_param',
    '_slug_from_company_url',
    '_social_counts',
    '_text_of',
    '_unpack_cursor',
    'cursor_with_limit',
    'parse_feed',
}


def _members(cls):
    out = {}
    for name, member in inspect.getmembers(cls):
        if name.startswith("__"):
            continue
        try:
            out[name] = str(inspect.signature(member))
        except (TypeError, ValueError):
            out[name] = None
    return out


def test_membres_et_signatures_inchanges():
    got = _members(mod.UnipileClient)
    assert set(got) == set(EXPECTED_MEMBERS), (
        f"membres ajoutés: {sorted(set(got) - set(EXPECTED_MEMBERS))} / "
        f"disparus: {sorted(set(EXPECTED_MEMBERS) - set(got))}")
    for name, sig in sorted(EXPECTED_MEMBERS.items()):
        assert got[name] == sig, f"signature changée pour {name}: {got[name]}"


def test_noms_importables_depuis_client():
    for name in EXPECTED_MODULE_NAMES:
        assert hasattr(mod, name), (
            f"{name} n'est plus importable depuis oto.tools.unipile.client")
    assert set(mod.__all__) == EXPECTED_MODULE_NAMES


def test_facade_du_package_inchangee():
    """Le backend importe `UnipileClient`/`make_unipile_client` depuis le PACKAGE
    (et les monkeypatche par ce chemin) : il ne bouge pas non plus."""
    assert pkg.UnipileClient is mod.UnipileClient
    assert pkg.UnipileError is mod.UnipileError
    assert pkg.parse_feed is mod.parse_feed
    assert str(inspect.signature(pkg.make_unipile_client)) == (
        "(api_key=None, dsn=None, account_id=None, provider=None)")


def test_hierarchie_des_erreurs():
    assert issubclass(mod.UnipileRateLimited, mod.UnipileError)
    assert issubclass(mod.UnipileError, RuntimeError)
    assert mod.UnipileError("x", status_code=404).status_code == 404
    assert mod.UnipileRateLimited("x", retry_after=60).retry_after == 60


def test_aucun_module_du_connecteur_ne_depasse_500_lignes():
    """Le seuil qui a motivé le découpage se garde tout seul (audit 27/08)."""
    import pathlib
    pkg_dir = pathlib.Path(mod.__file__).parent
    trop_gros = {
        p.name: len(p.read_text(encoding="utf-8").splitlines())
        for p in sorted(pkg_dir.rglob("*.py"))
        if len(p.read_text(encoding="utf-8").splitlines()) >= 500
    }
    assert not trop_gros, trop_gros
