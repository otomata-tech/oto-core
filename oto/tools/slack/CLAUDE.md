# Connecteur Slack (`oto.tools.slack`)

Client Slack Web API multi-workspace. Source : `client.py` (`SlackClient`). Exposé en
CLI (`oto slack …`) et en MCP (`slack_*`). Gestion d'erreur : les rejets logiques de
Slack (`{"ok": false, "error": "<code>"}` en HTTP 200) sont traduits en erreur typée
portant `.status` (4xx amont = input rejeté, 5xx = incident Slack) — cf. `_SLACK_ERROR_STATUS`.
Sur un `missing_scope`, `SlackError` porte aussi **`needed`/`provided`** : Slack NOMME
lui-même le droit qui manque, l'aval le relaie au lieu de le deviner (v1.100.0).

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
- **Lecture** (channels, history, **replies**, `channel_info`, find-user) → bot token suffit.
- **Post** → bot (`xoxb-`, l'app poste sous son identité) **ou** user (`xoxp-`, poste « comme toi »).
- **`join_channel`** → bot (rejoindre est un acte de l'app).

⚠️ **`history()` ne rend que le PREMIER NIVEAU.** Sur un message parent il annonce
`reply_count`/`reply_users`/`latest_reply` mais **jamais un corps de réponse** : les
réponses d'un fil s'obtiennent par **`replies()`** (`conversations.replies`). Le contrat
sondé — le paramètre s'appelle `ts`, le parent revient en `messages[0]` et se répète à
chaque page, `limit` borne les réponses sans compter le parent, `oldest`/`latest` sont
exclusives — est écrit dans la docstring de la méthode et vérifié par
`tests/test_slack_thread_replies.py`. ⚠️ Appelé avec le `ts` d'une **réponse**, Slack rend
ce seul message en `ok:true` : un « fil vide » qui n'en est pas un, à détecter en aval
(`messages[0].thread_ts != .ts`).

⚠️ **`join_channel()` ne vaut que pour les canaux PUBLICS.** Un canal privé ne se rejoint
par **aucune** API Slack — il faut qu'un humain déjà membre y invite l'app. `channel_info()`
existe pour trancher public/privé **avant** de tenter quoi que ce soit ; il répond sur un
canal public non rejoint, mais rend `channel_not_found` sur un canal privé où l'app n'est
pas — indiscernable d'un ID faux, donc l'aval doit dire les deux.

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
   - **lire l'historique ET les fils** : un scope `<surface>:history` par surface —
     `channels:history` (public), `groups:history` (privé), `im:history` (DM),
     `mpim:history` (DM de groupe). `conversations.replies` exige le même que
     `conversations.history` (constaté : `needed=groups:history` sur un canal privé)
   - **rejoindre un canal public** : `channels:join` (bot) — `channels:write` en user token
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
