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
- Deps cœur : requests, france-opendata, python-dotenv, pyyaml, defusedxml. **Pas de typer** (c'est la façade oto-cli).
- Extras : `google`, `browser` (o-browser), `vivatech`, `anthropic`, `stock`. `all`.
- **`uv.lock` est commité et ne gouverne AUCUNE install.** Les deps sont déclarées en
  **plancher** (`>=`) : le graphe de dépendances GitHub ne peut alors attribuer aucune
  version aux paquets du `pyproject.toml`, donc **aucune alerte de sécurité ne peut se
  déclencher** — zéro depuis la création du dépôt, alors que des libs qu'il consomme
  portent des avis publiés. Le lock donne au graphe des versions exactes, rien de plus :
  il est **absent de la wheel ET du sdist** (donc invisible à `pip install oto-core`),
  et un consommateur `uv` **ignore le lock de sa dépendance** (mesuré le 2026-09-07,
  y compris sur une dép `path`) — oto-backend et oto-cli continuent de résoudre oto-core
  depuis son `pyproject.toml`, à l'identique. Le régénérer par `uv lock` quand une
  dépendance bouge. ⚠️ **Monter un plancher reste un geste séparé** : ce fichier rend le
  dépôt observable, il ne le répare pas.
- **`MANIFEST.in` borne l'ARCHIVE SOURCE (sdist).** `[tool.setuptools.packages.find]`
  ne gouverne que la **roue** ; le sdist, lui, est composé par les défauts de
  setuptools, qui y versaient `tests/` **en entier** — 419 fichiers publiés contre
  334 dans la roue, et six fixtures y nommaient un tiers. **Une fixture est une
  surface publiée** tant que le sdist n'est pas borné (mesuré et corrigé le
  2026-09-10, à partir du 1.122.0).
  Toute reprise du packaging se vérifie sur le tarball, jamais sur le pyproject :
  `python -m build --sdist` puis `tar tzf dist/*.tar.gz` — il ne doit en sortir que
  `oto/`, ses `package-data` et les métadonnées.

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
- Connecteur **client-sensible** → jamais ici (repo public) : package privé + bridge (cf. ADR 0003 du meta-repo). ⚠️ **Client-sensible veut dire « le back-office PROPRE d'un client »** — son outil interne chez lui, son infra, ses accès. Un **produit commercial utilisé par des milliers d'entreprises** n'en est pas un, même si son API est fermée et qu'on a dû l'établir nous-mêmes : Planity, Pennylane, Zoho vivent ici, comme les autres. La règle se lit sur le PROPRIÉTAIRE de l'accès, jamais sur la difficulté d'y accéder — trois lecteurs l'ont appliquée à tort à un SaaS le 2026-09-09, et ont failli en sortir un connecteur parfaitement ordinaire.
- **Certains annuaires et sites professionnels interdisent le moissonnage dans leurs conditions d'usage.** Vérifier avant d'ouvrir un accès ou d'écrire un connecteur qui les viserait.
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
- **Un client est SYNCHRONE, sauf quand l'amont ne le permet pas.** L'exception
  est `planity`, dont le transport n'a pas d'équivalent synchrone : tout le package
  est `async`, d'où l'extra **`planity`** (`httpx` + `websockets`) — deux libs pour
  un seul connecteur, ce n'est pas au socle. Ce n'est pas un précédent à imiter :
  c'est ce que l'amont impose, et le module concerné le dit à l'endroit où
  quelqu'un aurait envie de « simplifier ».
- ⚠️ **Un extra manquant se retraduit À L'ORIGINE, jamais chez le consommateur.**
  `oto/tools/planity/__init__.py` rattrape l'`ImportError` de `httpx`/`websockets`
  et rend « installe `oto-core[planity]` » — parce que « No module named 'httpx' »
  est vrai, inutile, et devient chez oto-backend une ligne de journal sur laquelle
  on cherche un bug d'import. Les consommateurs sont plusieurs ; une règle posée
  chez l'un ne protège pas les autres. Elle ne s'applique QU'aux modules de
  l'extra : un `ImportError` interne remonte tel quel.
- ⚠️ **Nommer ce qu'on appelle est le métier d'un client ; raconter comment on l'a
  trouvé ne l'est pas.** Hôtes, fonction de shard, endpoints, conventions d'appel :
  c'est du CODE, un client ne peut pas appeler sans nommer, et ça reste ici comme
  pour n'importe quel connecteur. Ce qui n'a pas sa place dans un dépôt publié, ni
  en README ni en docstring ni en commentaire, tient en deux choses : le **récit de
  la reconstitution** (« lu dans le bundle », « capturé dans le trafic », « pas
  encore résolu, à retrouver »), et le **diagnostic sur le tiers** (ce que son
  produit vérifie ou pas, ce qu'il répond quand on se trompe). Ni l'un ni l'autre
  ne sert un lecteur du code, et les deux nous engagent. Ce qui reste est ce qui
  **justifie une décision d'implémentation et ne se lit pas dans le code**, écrit à
  l'endroit qu'il justifie. ⚠️ Ça se vérifie à la relecture d'un fichier ENTIER,
  jamais ligne à ligne : chaque phrase se défend seule, et c'est leur somme qui
  redonne le récit.
- ⚠️ **Aucune COORDONNÉE d'un tiers en dur — même publique.** Les trois constantes
  de Planity (clé d'API Firebase, App ID, racine des lambdas) ont vécu dans
  `oto/tools/planity/config.py` jusqu'au 2026-09-09, où GitHub les a signalées sur
  le dépôt public. Elles sont publiques par conception — tout navigateur qui ouvre
  `pro.planity.com` les reçoit — donc les retirer n'était pas un geste de sécurité,
  c'en était un de **généricité** : un client publié ici décrit un PROTOCOLE, il ne
  se présente pas comme l'intégration officielle d'une entreprise dont il embarque
  les coordonnées. Elles se passent désormais par `PlanityEndpoints`, **sans valeur
  par défaut** — un défaut les aurait remises ici sous un autre nom, et personne
  n'aurait vu la différence. Cliquet : `tests/test_planity_client.py`
  (`test_aucune_valeur_de_planity_ne_subsiste_dans_le_paquet`), qui refuse dans le
  paquet les trois empreintes de ces constantes — il les porte, on ne les recopie
  pas ici. ⚠️ La règle vaut pour le prochain connecteur du même genre, pas seulement
  pour celui-ci.
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
- **Sur PyPI depuis 1.6.0** (2026-06-13, promesse ADR 0005). Release = bump version pyproject → tag `vX.Y.Z` → **publication PyPI automatique** par `.github/workflows/publish.yml` (trusted publishing OIDC, aucun jeton stocké ; décision du 2026-09-12). Tag poussé depuis un poste = déclenché par le push ; tag posé par `tag-release.yml` = dispatché par lui (un tag du `GITHUB_TOKEN` ne déclenche aucun `push`). Rejouer une version : `gh workflow run publish.yml --ref main -f tag=vX.Y.Z`. Le job refuse un tag dont la version ne se retrouve pas dans le pyproject, le sdist et la roue. ⚠️ PyPI reconnaît le workflow par son **nom de fichier** : le renommer coupe la publication. Jamais de twine à la main. ⚠️ **Toujours bumper le champ `version` AVEC le tag** : un tag `vX.Y.Z` créé sans bumper `version` (resté en dessous) fait mentir `pip show oto-core` (vu le 2026-06-22 : tag v1.7.0 sur code à `version="1.6.1"` → prod affichait 1.6.1 malgré le bon code → fausse piste « bump non appliqué »). oto-backend pin oto-core par **tag git** (`@vX.Y.Z`) ; bump = nouveau tag + édit du pin backend. ⚠️ Les data files runtime (`sirene/data`, `pdf/templates`) sont déclarés en `package-data` — tout nouveau fichier chargé via `Path(__file__)` doit y être ajouté, sinon la wheel casse. Les installs editable (box, oto-cli local) ne sont PAS affectés par un publish — `git pull` requis.
