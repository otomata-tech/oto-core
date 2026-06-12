# oto-core

**Lib de connecteurs Oto** — clients API pour agents IA, **sans CLI**. Repo **public** (`otomata-tech/oto-core`), **open source**. Split d'oto-cli le 2026-06-11 (otomata#13).

Namespace package `oto` (PEP 420, **pas d'`oto/__init__.py`**) :
- `oto.tools.*` — les clients (serper, attio, hunter, google, linkedin via o-browser, pennylane, reddit, slack, whatsapp, gocardless, sirene/inpi/bodacc/boamp/dvf/culture via france-opendata…).
- `oto.config` — résolution de secrets 3-tier (env → SOPS/file/scaleway → défaut) ; `oto.sops_secrets`, `oto.scaleway_secrets`.

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
├── config.py             # get_secret/require_secret (env → provider → défaut)
├── sops_secrets.py       # provider SOPS+age
└── scaleway_secrets.py   # provider Secret Manager
```

## Conventions

- **Clients purs, sans typer ni I/O CLI** — `print`/Typer vivent dans oto-cli. Un client retourne des objets/dicts.
- Imports lazy des deps optionnelles (google, o-browser) pour ne pas casser si l'extra manque.
- ⚠️ **Pas d'`oto/__init__.py`** (namespace) → ne jamais faire `from oto import __version__` ; utiliser `importlib.metadata.version("oto-core")`.
- Connecteur **client-sensible** (auth reverse-engineerée, infra client) → jamais ici (repo public) : package privé + bridge (cf. ADR 0003 du meta-repo).

## Gotchas

- **Namespace cross-package** : oto-core fournit `oto.tools`/`oto.config`, oto-cli fournit `oto.cli`/`oto.commands`. Les deux installés editable cohabitent dans le même `oto`. Changer le pyproject d'un des deux → **réinstaller editable** (le finder setuptools suit le pyproject).
- **Sur PyPI depuis 1.6.0** (2026-06-13, promesse ADR 0005). Release = bump version pyproject → build+twine depuis `git archive HEAD` (recette meta-repo ; hatch cassé sur cette machine). ⚠️ Les data files runtime (`sirene/data`, `pdf/templates`, `whatsapp/node/{package*.json,*.mjs}`) sont déclarés en `package-data` — tout nouveau fichier chargé via `Path(__file__)` doit y être ajouté, sinon la wheel casse. Les installs editable (box, oto-cli local) ne sont PAS affectés par un publish — `git pull` requis.
