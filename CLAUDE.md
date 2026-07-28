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
- **Cache de token = process-wide keyé par credential** (hash des secrets, jamais un secret en clair comme clé) : le serveur construit un client **par appel MCP**, donc un cache porté par l'instance ne sert jamais → un refresh par appel → rate-limit du provider (Zoho : tous les appels en 400 pendant ~5 min).

## Gotchas

- **Namespace cross-package** : oto-core fournit `oto.tools`/`oto.config`, oto-cli fournit `oto.cli`/`oto.commands`. Les deux installés editable cohabitent dans le même `oto`. Changer le pyproject d'un des deux → **réinstaller editable** (le finder setuptools suit le pyproject).
- **Sur PyPI depuis 1.6.0** (2026-06-13, promesse ADR 0005). Release = bump version pyproject → build+twine depuis `git archive HEAD` (recette meta-repo ; hatch cassé sur cette machine). ⚠️ **Toujours bumper le champ `version` AVEC le tag** : un tag `vX.Y.Z` créé sans bumper `version` (resté en dessous) fait mentir `pip show oto-core` (vu le 2026-06-22 : tag v1.7.0 sur code à `version="1.6.1"` → prod affichait 1.6.1 malgré le bon code → fausse piste « bump non appliqué »). oto-backend pin oto-core par **tag git** (`@vX.Y.Z`) ; bump = nouveau tag + édit du pin backend. ⚠️ Les data files runtime (`sirene/data`, `pdf/templates`) sont déclarés en `package-data` — tout nouveau fichier chargé via `Path(__file__)` doit y être ajouté, sinon la wheel casse. Les installs editable (box, oto-cli local) ne sont PAS affectés par un publish — `git pull` requis.
