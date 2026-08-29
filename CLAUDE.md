# oto-core

**Lib de connecteurs Oto** — clients API pour agents IA, **sans CLI**. Repo **public** (`otomata-tech/oto-core`), **open source**. Split d'oto-cli le 2026-06-11 (otomata#13).

Namespace package `oto` (PEP 420, **pas d'`oto/__init__.py`**) :
- `oto.tools.*` — les clients (serper, attio, hunter, google, linkedin via o-browser, pennylane, reddit, slack, gocardless, sirene/inpi/bodacc/boamp/dvf/culture via france-opendata…). Messagerie (WhatsApp/LinkedIn) = Unipile côté backend ; le bridge WhatsApp Baileys (Node) a été retiré le 2026-07-22 (fallback archivé, deps npm vulnérables).
- `oto.config` — résolution de secrets 3-tier (env → SOPS/file/scaleway → défaut). `config.get_secret` orchestre ; les providers vivent dans le package `oto.secrets` (`sops`/`scaleway`/`file`), sélectionnés par la factory `oto.secrets.make_provider`.

## Place dans l'écosystème

**Source unique des clients connecteurs**, consommée par :
- **oto-cli** — façade Typer (`oto <cmd>`) qui dépend d'oto-core. Surtout fallback local LinkedIn browser (basse priorité, cf. oto-cli/CLAUDE.md).
- **oto-mcp** — serveur MCP : importe `oto.tools.*` **directement** (plus aucune dép à la CLI). C'est le produit central déployable (SaaS/on-premise).

Donc : un connecteur = un client ici, plusieurs faces (CLI, MCP). [[meta otomata/docs/architecture.md]].

## Stack

- Python ≥3.10, setuptools (namespace package). Version dans `pyproject.toml`.
- Deps cœur : requests, france-opendata, python-dotenv, pyyaml. **Pas de typer** (c'est la façade oto-cli).
- Extras : `google`, `browser` (o-browser), `vivatech`, `anthropic`, `stock`. `all`.

## Architecture

```
oto/                      # namespace (PAS d'__init__.py)
├── tools/                # 1 dossier/connecteur : <svc>/client.py (+ lib/ pour google)
│                         # gros connecteur → familles d'appels en <svc>/_api/*.py (cf. Conventions)
├── config.py             # get_secret/require_secret (orchestrateur : env → provider → fallback fichier → défaut)
└── secrets/              # providers de secrets + factory
    ├── __init__.py       # make_provider(name, cfg) — factory + registre
    ├── base.py           # protocole SecretProvider + sentinelles (MISSING/STORE_ABSENT) + AmbiguousSecretError
    ├── sops.py           # provider SOPS+age (SopsProvider)
    ├── scaleway.py       # provider Secret Manager (ScalewayProvider)
    └── file.py           # provider fichier .otomata/secrets.env (FileProvider)
```

Ajouter un provider = un module exposant `lookup(name)` + une ligne au registre
`oto/secrets/__init__.py` — zéro branche `if provider ==` dans `oto.config`.

## Conventions

- **Clients purs, sans typer ni I/O CLI** — `print`/Typer vivent dans oto-cli. Un client retourne des objets/dicts.
- Imports lazy des deps optionnelles (google, o-browser) pour ne pas casser si l'extra manque.
- ⚠️ **Pas d'`oto/__init__.py`** (namespace) → ne jamais faire `from oto import __version__` ; utiliser `importlib.metadata.version("oto-core")`.
- Connecteur **client-sensible** (auth reverse-engineerée, infra client) → jamais ici (repo public) : package privé + bridge (cf. ADR 0003 du meta-repo).
- **Auth d'une FAMILLE de connecteurs = un module partagé**, jamais recopiée par client — ex. `oto/tools/zoho/auth.py` (refresh OAuth + cache, source unique CRM/Desk/Analytics). Tant que les trois dupliquaient ce bloc, un correctif n'en couvrait qu'un tiers (le cache de token #233 n'avait atterri que sur Analytics).
- ⚠️ **Un secret ne part JAMAIS en `params=`** (query string) : il entre dans l'URL, donc dans le message de toute exception `requests` — remonté à l'agent, journalisé, envoyé en breadcrumb Sentry — et dans les access logs du serveur distant. Toujours **`data=`** (corps, RFC 6749 §2.3.1 pour OAuth), et pas de `raise_for_status()` sur un endpoint token (son message porte l'URL). Fuite vécue #284 ; garde-fou AST dans **oto-backend** (test « no secrets in query string »).
- **Un refus d'un client ne prescrit JAMAIS un outil MCP** (2026-08-29, oto-backend#632) :
  la lib ne connaît pas le jeu d'outils servi à l'appelant (une CLI, un endpoint publié qui
  sert une liste à l'inclusion…). Un message dit le FAIT (« la source est close à
  l'extraction ») et au plus une condition (« si tu as un compte connecté, c'est par lui »)
  — jamais un nom d'outil ni une famille `xxx_*`. Vécu : `SerperClient._NEVER_SCRAPABLE`
  nommait `unipile_*`, que l'appelant n'avait pas — et qui n'existe même plus sous ce nom
  (`linkedin_unipile_*` depuis l'ADR 0010). Garde : `tests/test_serper_scrape_guard.py`.
- **Cache de token = process-wide keyé par credential** (hash des secrets, jamais un secret en clair comme clé) : le serveur construit un client **par appel MCP**, donc un cache porté par l'instance ne sert jamais → un refresh par appel → rate-limit du provider (Zoho : tous les appels en 400 pendant ~5 min).
- **Fichier de code < 500 lignes — un gros connecteur se découpe SANS bouger son chemin d'import.** Le point d'entrée reste `<svc>/client.py` (ou `<svc>/lib/<svc>_client.py` côté google) : il porte la construction et le transport, et **compose des mixins par famille d'appels** rangés dans `<svc>/_api/*.py` (un module = un domaine de l'API amont). Les constantes, les types d'erreur et le parsing lourd sortent en modules frères (`const.py`, `errors.py`, `feed.py`), et `client.py` les **réexporte** via `__all__` — le backend et oto-cli épinglent oto-core **par tag** : un symbole qui déménage ne casse pas ici, il casse **au bump du pin**, ailleurs, plus tard. Fait le 2026-08-27 sur unipile (1 702 L → 13 modules) et google/slides (1 516 L → 9 modules) ; le contrat est verrouillé par `tests/test_unipile_surface_frozen.py` et `tests/test_slides_surface_frozen.py`, qui figent membres + signatures et refusent tout module ≥ 500 lignes dans ces deux packages.

## Gotchas

- **Namespace cross-package** : oto-core fournit `oto.tools`/`oto.config`, oto-cli fournit `oto.cli`/`oto.commands`. Les deux installés editable cohabitent dans le même `oto`. Changer le pyproject d'un des deux → **réinstaller editable** (le finder setuptools suit le pyproject).
- ⚠️ **La CI doit installer tout extra dont un TEST importe la dépendance.** Elle
  installait `-e ".[anonymize]"` seul : `tests/test_gmail_headers_and_draft.py`
  importe le client Gmail, donc `google-api-python-client` (extra `google`) →
  `ModuleNotFoundError` **à la collecte**, pytest s'arrête avant le premier test et
  le job échoue sans rien avoir vérifié. `main` est restée rouge du 12 au 18/08/2026,
  et **aucune PR ne pouvait devenir verte** — la garde version-skew du backend
  renvoyait alors des PR saines en échec. Un extra ajouté ici se répercute dans
  `.github/workflows/ci.yml`.
- **Sur PyPI depuis 1.6.0** (2026-06-13, promesse ADR 0005). Release = bump version pyproject → build+twine depuis `git archive HEAD` (recette meta-repo ; hatch cassé sur cette machine). ⚠️ **Toujours bumper le champ `version` AVEC le tag** : un tag `vX.Y.Z` créé sans bumper `version` (resté en dessous) fait mentir `pip show oto-core` (vu le 2026-06-22 : tag v1.7.0 sur code à `version="1.6.1"` → prod affichait 1.6.1 malgré le bon code → fausse piste « bump non appliqué »). oto-backend pin oto-core par **tag git** (`@vX.Y.Z`) ; bump = nouveau tag + édit du pin backend. ⚠️ Les data files runtime (`sirene/data`, `pdf/templates`) sont déclarés en `package-data` — tout nouveau fichier chargé via `Path(__file__)` doit y être ajouté, sinon la wheel casse. Les installs editable (box, oto-cli local) ne sont PAS affectés par un publish — `git pull` requis.
