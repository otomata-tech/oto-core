"""SOPS provider for oto secret resolution.

Lit un ou plusieurs fichiers YAML chiffrés via SOPS + age. Configurable via
`~/.otomata/config.yaml` :

- `sops_dir`  : répertoire racine (préféré). Tous les `*.yaml` y sont décryptés
  récursivement et mergés dans un seul dict plat. Défaut : `~/.otomata/secrets/`
  (avec auto-détection si la struct contient un sous-dir avec `.sops.yaml`).
- `sops_file` : un seul fichier (legacy / mono-fichier). Si défini, prioritaire
  sur `sops_dir` et seul ce fichier est lu.

Layout multi-fichiers (cf. AlexisLaporte/secrets) :
    secrets/
    ├── secrets.yaml         # transverse Otomata
    ├── tuls.yaml            # host
    ├── legacy.yaml          # orphelins préservés
    ├── missions/*.yaml      # par mission/client
    └── projects/*.yaml      # projets internes

Décrypt délégué au CLI `sops` (déjà installé), résultat parsé en YAML plat,
caché module-level (une seule décryption par process).

Collisions de clés : même valeur ⇒ merge silencieux ; valeurs différentes
(clé générique scopée par fichier, ex. DATABASE_URL par projet) ⇒ la clé est
EXCLUE du namespace flat et listée dans `ambiguous_keys()` — `get_secret`
lève une erreur explicite si on la demande.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional, List

_cache: Optional[Dict[str, str]] = None
# Clés définies avec des VALEURS DIFFÉRENTES dans plusieurs fichiers (clés
# génériques scopées par fichier : DATABASE_URL, DB_USER…). Elles sont exclues
# du namespace flat — les résoudre = lire le fichier scope directement.
_ambiguous: Dict[str, List[str]] = {}


def _candidate_default_dirs() -> List[Path]:
    """Default candidates for the secrets root directory."""
    home = Path.home()
    return [
        home / ".otomata" / "secrets",
    ]


def _autodetect_dir() -> Optional[Path]:
    """Find the first candidate dir that contains a .sops.yaml."""
    for c in _candidate_default_dirs():
        if (c / ".sops.yaml").is_file():
            return c
    return None


def _decrypt_file(path: Path) -> Dict[str, str]:
    try:
        decrypted = subprocess.run(
            ["sops", "--decrypt", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
    except FileNotFoundError as e:
        raise RuntimeError(
            "sops CLI not found. Install it: "
            "https://github.com/getsops/sops/releases"
        ) from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"sops --decrypt failed for {path}. "
            f"Make sure ~/.config/sops/age/keys.txt matches a recipient in .sops.yaml.\n"
            f"stderr: {e.stderr}"
        ) from e

    import yaml
    data = yaml.safe_load(decrypted) or {}
    return {str(k): str(v) for k, v in data.items()}


def _merge(
    target: Dict[str, str],
    origins: Dict[str, str],
    source: Dict[str, str],
    source_path: Path,
) -> None:
    for k, v in source.items():
        if k in _ambiguous:
            _ambiguous[k].append(str(source_path))
            continue
        if k in target and target[k] != v:
            # Valeurs divergentes ⇒ clé scopée par fichier, pas transverse.
            # On la retire du flat ; get_secret lèvera si on la demande.
            _ambiguous[k] = [origins.pop(k), str(source_path)]
            del target[k]
            continue
        target[k] = v
        origins[k] = str(source_path)


def ambiguous_keys() -> Dict[str, List[str]]:
    """Clés exclues du namespace flat (valeurs divergentes), avec leurs fichiers."""
    return _ambiguous


def fetch_secrets(
    path: Optional[str] = None,
    dir_path: Optional[str] = None,
) -> Dict[str, str]:
    """Decrypt all SOPS files and return a flat dict.

    Args:
        path: Legacy single-file mode. If set, only this file is read.
        dir_path: Root directory. All `*.yaml` files (recursive) are decrypted
                  and merged. If None and `path` is None, autodetects.
    """
    global _cache
    if _cache is not None:
        return _cache

    merged: Dict[str, str] = {}

    if path:
        single = Path(path).expanduser()
        if not single.exists():
            raise FileNotFoundError(f"SOPS secrets file not found at {single}.")
        merged = _decrypt_file(single)
    else:
        root: Optional[Path] = Path(dir_path).expanduser() if dir_path else _autodetect_dir()
        if root is None or not root.is_dir():
            raise FileNotFoundError(
                f"SOPS secrets dir not found. Tried: {[str(c) for c in _candidate_default_dirs()]}. "
                f"Clone: git clone git@github.com:AlexisLaporte/secrets ~/.otomata/secrets"
            )
        yaml_files = sorted(p for p in root.rglob("*.yaml") if p.name != ".sops.yaml")
        if not yaml_files:
            raise FileNotFoundError(f"No .yaml files found under {root}.")
        origins: Dict[str, str] = {}
        for f in yaml_files:
            _merge(merged, origins, _decrypt_file(f), f)

    _cache = merged
    return _cache


def invalidate_cache() -> None:
    """Force a re-decrypt on next fetch (utile après `sops edit`)."""
    global _cache
    _cache = None
    _ambiguous.clear()
