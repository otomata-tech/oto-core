"""
Cognism Search API — allow-lists for closed-set filter fields.

Ces valeurs sont copiées verbatim de la doc Cognism (developers.cognism.com,
endpoint Search Contacts) — PAS dérivées d'un endpoint Filter API dynamique
(celles-là — regions/countries/states/industries/sic/isic/naics/technologies/
skills/companySizes — restent volontairement absentes d'ici : elles sont
longues et évoluent côté Cognism, donc consommées en live via
`CognismClient.filter_values(kind)`, jamais figées dans ce module).

But : transformer un enum typo (le mode d'échec le plus probable et le plus
sournois avec une DSL à ~150 champs — l'API répond 200 avec une page vide,
pas une erreur) en `ValueError` explicite AVANT l'appel réseau, plutôt que de
laisser filer une requête qui « marche » mais ne matche jamais rien.

⚠️ Piège de nesting : les champs côté "société" (types/fundingEvent/
hiringEvent/accountSearchOptions) vivent sous `account.*` dans le body de
`search_contacts` (le contact est la racine, la société est imbriquée), mais
à la RACINE (sans préfixe `account.`) dans le body de `search_accounts` (la
société EST la racine, là). Même noms de champs, profondeur différente selon
l'endpoint → deux tables de chemins (`_CONTACT_ENUM_FIELDS` /
`_ACCOUNT_ENUM_FIELDS`), pas une seule, pour ne pas valider au mauvais niveau
et laisser filer une valeur invalide côté `search_accounts`.
"""
from __future__ import annotations

from typing import Any, Dict

SENIORITY = {"Manager", "Director", "Partner", "CXO", "Owner", "VP"}

JOB_FUNCTIONS = {
    "Oversight", "Technology", "Operations", "Sales", "Marketing",
    "Client Success", "HR", "Accounting", "Business", "Production",
}

MANAGEMENT_LEVELS = {
    "Entry-Level", "Team-Lead", "Experienced Staff", "Executive-Level",
    "Senior Leadership", "Middle-Management", "CxO",
}

ACCOUNT_TYPES = {
    "Public Company", "Educational", "Educational Institution",
    "Government Agency", "Partnership", "Privately Held",
    "Self-Employed", "non profit",
}

FUNDING_TYPES = {
    "venture", "seed", "grant", "private_equity", "angel",
    "debt_financing", "corporate_round", "convertible note",
    "equity_crowfunding",
}

FUNDING_SERIES = {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"}

HIRING_EVENT_DEPARTMENTS = {
    "legal", "it", "administration", "marketing", "sales", "R&D",
    "customer", "operations", "finance",
}

SORT_FIELDS = {
    "LastConfirmedContactDESC", "LastConfirmedContactASC",
    "EmailQualityDESC", "EmailQualityASC",
    "ProfileScoreDESC", "ProfileScoreASC",
}

EXISTS_MISSING = {"exists", "missing"}
LOCATION_TYPE = {"ALL", "HQ"}
AND_OR = {"AND", "OR"}

# Champs "société" — mêmes noms, préfixés `account.` dans search_contacts,
# à la racine dans search_accounts. Générés une seule fois pour éviter la
# dérive entre les deux tables.
_ACCOUNT_SIDE_FIELDS: Dict[str, set] = {
    "types": ACCOUNT_TYPES,
    "fundingEvent.fundingType": FUNDING_TYPES,
    "fundingEvent.series": FUNDING_SERIES,
    "hiringEvent.department": HIRING_EVENT_DEPARTMENTS,
    "accountSearchOptions.filter_email": EXISTS_MISSING,
    "accountSearchOptions.filter_domain": EXISTS_MISSING,
    "accountSearchOptions.location_type": LOCATION_TYPE,
    "accountSearchOptions.events_operator": AND_OR,
    "accountSearchOptions.operators.technologies": AND_OR,
    "accountSearchOptions.operators.excludedTechnologies": AND_OR,
}

# search_contacts : champs contact à la racine + champs société sous `account.`
# + les 3 champs fermés dupliqués sous `previousAccounts.*` (sociétés passées).
_CONTACT_ENUM_FIELDS: Dict[str, set] = {
    "seniority": SENIORITY,
    "jobFunctions": JOB_FUNCTIONS,
    "managementLevel": MANAGEMENT_LEVELS,
    "searchOptions.sort_fields": SORT_FIELDS,
    "previousAccounts.seniority": SENIORITY,
    "previousAccounts.jobFunction": JOB_FUNCTIONS,
    "previousAccounts.managementLevel": MANAGEMENT_LEVELS,
    **{f"account.{k}": v for k, v in _ACCOUNT_SIDE_FIELDS.items()},
}

# search_accounts : les champs société sont à la racine (pas de préfixe
# `account.` — l'objet racine EST déjà le filtre société).
_ACCOUNT_ENUM_FIELDS: Dict[str, set] = dict(_ACCOUNT_SIDE_FIELDS)

_MISSING = object()


def _dig(obj: Any, path: list[str]):
    """Descend un dot-path dans un dict imbriqué. Renvoie _MISSING si un
    segment n'existe pas (dict absent ou pas un dict)."""
    cur = obj
    for seg in path:
        if not isinstance(cur, dict) or seg not in cur:
            return _MISSING
        cur = cur[seg]
    return cur


def validate_enum_filters(filters: Dict[str, Any] | None, *, scope: str = "contact") -> None:
    """Valide les champs à valeurs fermées d'un dict de filtres Cognism.
    Lève `ValueError` avec le champ, la valeur fautive et les valeurs
    autorisées si une valeur hors liste est trouvée. Ne valide PAS les
    champs absents (tous optionnels) ni les listes dynamiques
    (regions/countries/.../technologies) — voir docstring module.

    Args:
        scope: `"contact"` pour un body `search_contacts` (fields société
            sous `account.*`), `"account"` pour un body `search_accounts`
            (fields société à la racine, pas de préfixe).
    """
    if not filters:
        return
    fields = _CONTACT_ENUM_FIELDS if scope == "contact" else _ACCOUNT_ENUM_FIELDS
    for dotted, allowed in fields.items():
        value = _dig(filters, dotted.split("."))
        if value is _MISSING:
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        bad = [v for v in values if v not in allowed]
        if bad:
            raise ValueError(
                f"Cognism filter `{dotted}`: invalid value(s) {bad!r}. "
                f"Allowed: {sorted(allowed)!r}"
            )
