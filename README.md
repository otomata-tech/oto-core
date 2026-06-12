# oto-core

Connector library for [Oto](https://oto.ninja) — pure Python API clients for AI agents and automation. No CLI, no server: clients return plain dicts.

```bash
pip install oto-core              # core (requests, france-opendata)
pip install "oto-core[google]"    # + Google Workspace (Drive, Docs, Sheets, Gmail, Calendar, Tasks)
pip install "oto-core[browser]"   # + browser-based scraping (LinkedIn, via o-browser)
pip install "oto-core[stock]"     # + SIRENE stock queries (DuckDB/parquet)
```

## What's inside

- `oto.tools.*` — one client per service: French company data (SIRENE, INPI, BODACC, BOAMP, DVF via [france-opendata](https://pypi.org/project/france-opendata/)), web search (Serper), email finding (Hunter), CRM (Attio, Folk), outreach (Lemlist, Kaspr, Fullenrich), Google Workspace, Slack, WhatsApp, Reddit, Pennylane, and more.
- `oto.config` — three-tier secret resolution: environment variables → secret provider (SOPS/age, file, Scaleway Secret Manager) → defaults.

## Ecosystem

| Package | Role |
|---|---|
| **oto-core** (this) | the clients — single source of truth |
| [oto-cli](https://github.com/otomata-tech/oto-cli) | `oto` command-line façade |
| oto-backend | hosted platform ([mcp.oto.ninja](https://oto.ninja) — MCP + REST, credential vault, orgs) |

```python
from oto.tools.sirene import SireneClient

client = SireneClient()          # reads SIRENE_API_KEY via oto.config
company = client.get_company("130025265")
```

Conventions: clients are import-lazy per optional dependency, raise on error (no silent fallbacks), and stay free of CLI/printing concerns.

MIT — © Otomata.
