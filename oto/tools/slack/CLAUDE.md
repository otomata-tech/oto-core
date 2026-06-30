# Connecteur Slack (`oto.tools.slack`)

Client Slack Web API multi-workspace. Source : `client.py` (`SlackClient`). Exposé en
CLI (`oto slack …`) et en MCP (`slack_*`). Gestion d'erreur : les rejets logiques de
Slack (`{"ok": false, "error": "<code>"}` en HTTP 200) sont traduits en erreur typée
portant `.status` (4xx amont = input rejeté, 5xx = incident Slack) — cf. `_SLACK_ERROR_STATUS`.

## 1. Modèle multi-workspace + résolution de tokens

Un `SlackClient` cible **un workspace** (`workspace="<slug>"`, défaut `otomata`). Le client
résout ses tokens depuis les secrets par **convention de nommage** :

| | clé secret | usage |
|---|---|---|
| bot token (`xoxb-`) | `SLACK_<SLUG>_BOT_TOKEN` | lecture + post « au nom de l'app » |
| user token (`xoxp-`) | `SLACK_<SLUG>_USER_TOKEN` | post « au nom de l'utilisateur » (`as_user=True`) |

`<SLUG>` = `workspace.upper()`. Pour le workspace par défaut (`otomata`), les clés **plates
legacy** `SLACK_BOT_TOKEN` / `SLACK_USER_TOKEN` sont acceptées en fallback.

**Choix du token par appel** : `post_message`/`update_message`/`open_dm`/`add_reaction`
acceptent `as_user=True|False`. Si omis → `default_as_user` du client (défaut `False` = bot).
- **Lecture** (channels, history, find-user) → bot token suffit.
- **Post** → bot (`xoxb-`, l'app poste sous son identité) **ou** user (`xoxp-`, poste « comme toi »).

Erreur si aucun token résolu : `No Slack token for workspace '<slug>'…` → poser la clé.

### Ajouter un 2ᵉ workspace
Poser `SLACK_<NOUVEAU_SLUG>_BOT_TOKEN` (et `_USER_TOKEN` si post-as-user voulu) dans le
vault, puis instancier `SlackClient(workspace="<nouveau_slug>")`. Aucune autre config.

## 2. Gotcha — `find_user_by_email` dépend de l'email **réel** du compte Slack

`slack_find_user_by_email` / `oto slack find-user` appelle `users.lookupByEmail` : l'email
passé doit être **celui du compte Slack de la personne**, pas forcément son email pro. Ex.
vécu : `alexis@otomata.tech` échoue (`users_not_found`), seul `alexis.laporte@gmail.com`
(email Slack réel) marche. Si lookup KO, vérifier l'email d'inscription Slack de la cible.

> Pour savoir sur **quel workspace** et sous **quelle identité** on agit, il faut aujourd'hui
> taper l'API `auth.test` directement (pas de méthode exposée). Enhancement optionnel #25 :
> un `slack_whoami` / `oto slack whoami` (wrap `auth.test`). Non implémenté.

## 4. Onboarding d'un nouveau user

1. **App Slack** : créer une app sur https://api.slack.com/apps (ou installer une app
   partagée) sur le workspace cible.
2. **Scopes** (OAuth & Permissions) selon l'usage :
   - lecture / post bot : `chat:write`, `channels:read`, `users:read.email`, `im:write`
   - recherche : `search:read` (⚠️ scope **user token** uniquement)
   - post « as user » : installer aussi un **user token** (`xoxp-`) avec les scopes user voulus.
3. **Installer** l'app sur le workspace → récupérer `Bot User OAuth Token` (`xoxb-`) et, si
   besoin, le `User OAuth Token` (`xoxp-`).
4. **Poser les tokens** dans le vault per-user (`~/.otomata/secrets/`, lu par `oto.config`)
   sous `SLACK_<SLUG>_BOT_TOKEN` / `SLACK_<SLUG>_USER_TOKEN` (mapping slug → workspace).

## 3. Décision ouverte (NON tranchée — #25)

Le seul workspace configuré aujourd'hui est **« Otomata Community »** (`zen-otomata.slack.com`),
un espace **communautaire** (membres/audience) où Alexis figure sous son email perso — donc
**inapproprié** pour router de la donnée privée (compta, business). À trancher :
- **(a)** créer un workspace Slack **interne** dédié (admin/notifs) avec son slug + tokens, ou
- **(b)** assumer que les notifs perso passent par WhatsApp et garder Slack = communauté.

(Pour la routine Pennylane → notif, on est partis sur WhatsApp.)
