"""Gmail API client using OAuth2 user credentials."""

import base64
import mimetypes
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']


def _markdown_to_html_fragment(text: str) -> str:
    """Render markdown to an HTML fragment suitable for Gmail's text/html part.

    No <html>/<body> wrapping — Gmail accepts the fragment directly inside a
    multipart/alternative message. Warns (no auto-fix) on markdown pitfalls
    such as a list glued to the preceding paragraph; supports tables, fenced
    code, inline attributes.
    """
    import markdown as _md
    from oto.tools.markdown_lint import warn_markdown
    warn_markdown(text, source='corps du mail')
    return _md.markdown(
        text,
        extensions=['tables', 'fenced_code', 'sane_lists', 'attr_list'],
        output_format='html',
    )


def _recipients(header: str, exclude: str = "") -> str:
    """Les adresses d'un en-tête, en liste, moins `exclude`.

    ⚠️ Ne PAS utiliser `parseaddr` ici : il ne lit qu'une adresse et rend
    `('', '')` sur `To: a@x, b@y` — un fil à plusieurs destinataires devenait
    alors « Cannot determine reply recipient ».
    """
    # `if "@" in a` : sur un en-tête sans adresse réelle (« Nom Sans Adresse »),
    # getaddresses rend [('', 'Nom')] — le laisser passer enverrait vers « Nom »
    # au lieu de lever le garde-fou d'appel.
    addrs = [a for _, a in getaddresses([header]) if a and "@" in a]
    if exclude:
        addrs = [a for a in addrs if a.lower() != exclude.lower()]
    return ", ".join(addrs)


class GmailClientError(Exception):
    """Gmail API error."""


class GmailClient:
    """Gmail API client.

    Args:
        credentials: OAuth2 user credentials. If None, uses get_user_credentials().
        account: Named account to use (None = auto-detect if single account).
    """

    def __init__(self, credentials: Optional[Credentials] = None, account: Optional[str] = None):
        if credentials is None:
            from oto.tools.google.credentials import get_user_credentials
            credentials = get_user_credentials(SCOPES, account=account)
        self.service = build('gmail', 'v1', credentials=credentials)

    def list_messages(
        self,
        query: Optional[str] = None,
        label_ids: Optional[list[str]] = None,
        max_results: int = 20,
    ) -> list[dict]:
        """List messages with metadata (id, snippet, from, subject, date)."""
        kwargs = {'userId': 'me', 'maxResults': max_results}
        if query:
            kwargs['q'] = query
        if label_ids:
            kwargs['labelIds'] = label_ids

        resp = self.service.users().messages().list(**kwargs).execute()
        messages = resp.get('messages', [])

        results = []
        for msg in messages:
            meta = self.service.users().messages().get(
                userId='me', id=msg['id'], format='metadata',
                metadataHeaders=['From', 'Subject', 'Date'],
            ).execute()
            headers = {h['name']: h['value'] for h in meta.get('payload', {}).get('headers', [])}
            results.append({
                'id': meta['id'],
                'threadId': meta['threadId'],
                'snippet': meta.get('snippet', ''),
                'from': headers.get('From', ''),
                'subject': headers.get('Subject', ''),
                'date': headers.get('Date', ''),
                'labelIds': meta.get('labelIds', []),
            })

        return results

    def get_message(self, message_id: str) -> dict:
        """Get full message content with attachment metadata."""
        msg = self.service.users().messages().get(
            userId='me', id=message_id, format='full',
        ).execute()

        # Lookup case-insensitive (cf. `reply`) : l'API rend les noms d'en-tête TELS
        # QU'ÉCRITS par l'émetteur. Un `To:`/`Cc:`/`Subject:` cherché à la lettre près
        # revenait vide sur tout message écrit en minuscules — dont les nôtres avant
        # ce correctif : relire un brouillon ne montrait ni destinataire ni copie,
        # donc impossible de le vérifier avant envoi (signal #342). Vaut aussi pour
        # l'existant, que la capitalisation à l'écriture ne rattrape pas.
        headers = {h['name'].lower(): h['value'] for h in msg.get('payload', {}).get('headers', [])}
        body = self._extract_body(msg.get('payload', {}))
        attachments = self._list_attachments(msg.get('payload', {}))

        result = {
            'id': msg['id'],
            'threadId': msg['threadId'],
            'subject': headers.get('subject', ''),
            'from': headers.get('from', ''),
            'to': headers.get('to', ''),
            'cc': headers.get('cc', ''),
            'date': headers.get('date', ''),
            'body': body,
            # `body` est le text/plain — donc le markdown SOURCE quand le message
            # porte aussi une partie HTML. Sans ce drapeau, relire un mail bien rendu
            # donne à voir des `**gras**` et fait conclure « le rendu est cassé »
            # (signal #341). Booléen plutôt que le HTML lui-même : lever le doute
            # sans verser une page de balises dans le contexte de l'agent.
            'has_html': self._find_part(msg.get('payload', {}).get('parts', []), 'text/html') is not None,
            'labelIds': msg.get('labelIds', []),
        }
        if attachments:
            result['attachments'] = attachments
        return result

    def download_attachments(self, message_id: str, output_dir: str) -> list[dict]:
        """Download all attachments from a message.

        Returns list of {filename, path, size_bytes}.
        """
        msg = self.service.users().messages().get(
            userId='me', id=message_id, format='full',
        ).execute()

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        downloaded = []

        for part in self._iter_parts(msg.get('payload', {})):
            filename = part.get('filename')
            att_id = part.get('body', {}).get('attachmentId')
            if not filename or not att_id:
                continue

            att = self.service.users().messages().attachments().get(
                userId='me', messageId=message_id, id=att_id,
            ).execute()
            data = base64.urlsafe_b64decode(att['data'])
            path = self._unique_path(out, filename)
            path.write_bytes(data)
            downloaded.append({
                'filename': path.name,
                'path': str(path),
                'size_bytes': len(data),
            })

        return downloaded

    @staticmethod
    def _unique_path(out: Path, filename: str) -> Path:
        """Return a path under `out` that doesn't collide with an existing file.

        Outlook inline images all share names like `image.png`, so without
        disambiguation the second attachment would silently overwrite the
        first. Suffix an index before the extension on collision:
        `image.png`, `image_1.png`, `image_2.png`, …
        """
        candidate = out / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        i = 1
        while True:
            candidate = out / f'{stem}_{i}{suffix}'
            if not candidate.exists():
                return candidate
            i += 1

    def get_signature(self) -> str:
        """Get the primary Gmail signature (HTML)."""
        result = self.service.users().settings().sendAs().list(userId='me').execute()
        for alias in result.get('sendAs', []):
            if alias.get('isPrimary'):
                return alias.get('signature', '')
        return ''

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
    ) -> dict:
        """Send an email. Returns the sent message metadata.

        If `html` is not provided and `markdown=True` (default), the body is
        rendered from markdown to an HTML fragment so Gmail wraps paragraphs
        naturally instead of dumping hard line breaks in text/plain mode.
        """
        if html is None and markdown:
            html = _markdown_to_html_fragment(body)
        message = self._build_message(to, subject, body, html, cc, bcc, attachments, from_name=from_name)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self.service.users().messages().send(
            userId='me', body={'raw': raw},
        ).execute()
        return {'id': sent['id'], 'threadId': sent.get('threadId', '')}

    def reply(
        self,
        message_id: str,
        body: str,
        html: Optional[str] = None,
        cc: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
    ) -> dict:
        """Reply to a message. Preserves thread, subject, and headers.

        If `html` is not provided and `markdown=True` (default), the body is
        rendered from markdown to an HTML fragment.
        """
        if html is None and markdown:
            html = _markdown_to_html_fragment(body)
        original = self.service.users().messages().get(
            userId='me', id=message_id, format='full',
        ).execute()
        # Lookup case-insensitive : la casse des headers n'est pas normalisée
        # ("Message-ID" vs "Message-Id" selon l'expéditeur).
        headers = {h['name'].lower(): h['value'] for h in original['payload']['headers']}
        thread_id = original['threadId']

        # Determine recipient: reply to sender (unless we sent it, then reply to To)
        from_addr = headers.get('from', '')
        to_addr = headers.get('to', '')
        reply_to_header = headers.get('reply-to', '')
        profile = self.service.users().getProfile(userId='me').execute()
        my_email = profile['emailAddress']

        if reply_to_header:
            reply_to = _recipients(reply_to_header)
        elif my_email.lower() in from_addr.lower():
            # notre propre message : on relance le fil vers TOUS ses destinataires
            reply_to = _recipients(to_addr)
        else:
            reply_to = _recipients(from_addr)

        if not reply_to:
            raise GmailClientError(f"Cannot determine reply recipient (from={from_addr!r}, to={to_addr!r})")

        subject = headers.get('subject', '')
        if not subject.lower().startswith('re:'):
            subject = f"Re: {subject}"

        message = self._build_message(reply_to, subject, body, html, cc, None, attachments, from_name=from_name)
        orig_msg_id = headers.get('message-id', '')
        if orig_msg_id:
            message['In-Reply-To'] = orig_msg_id
            message['References'] = orig_msg_id

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self.service.users().messages().send(
            userId='me', body={'raw': raw, 'threadId': thread_id},
        ).execute()
        return {'id': sent['id'], 'threadId': sent.get('threadId', ''), 'to': reply_to}

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        markdown: bool = True,
    ) -> dict:
        """Create a draft email. Pass thread_id + in_reply_to for threaded replies.

        If `html` is not provided and `markdown=True` (default), the body is
        rendered from markdown to an HTML fragment — **même contrat que `send` et
        `reply`**, qui le faisaient déjà. Seul le brouillon ne le faisait pas : un
        agent composant en markdown obtenait un corps texte où `**gras**` et les
        puces restaient littéraux, visibles tels quels du destinataire (#341/#343).
        """
        if html is None and markdown:
            html = _markdown_to_html_fragment(body)
        message = self._build_message(to, subject, body, html, cc, bcc, attachments)
        if in_reply_to:
            message['In-Reply-To'] = in_reply_to
            message['References'] = in_reply_to
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        msg_body: dict = {'raw': raw}
        if thread_id:
            msg_body['threadId'] = thread_id
        draft = self.service.users().drafts().create(
            userId='me', body={'message': msg_body},
        ).execute()
        msg = draft['message']
        return {'id': draft['id'], 'message_id': msg['id'], 'threadId': msg.get('threadId', '')}

    def list_drafts(self, max_results: int = 20) -> list[dict]:
        """List drafts with metadata (id, message_id, to, subject, snippet)."""
        resp = self.service.users().drafts().list(userId='me', maxResults=max_results).execute()
        drafts = resp.get('drafts', [])
        out = []
        for d in drafts:
            msg_id = d['message']['id']
            msg = self.service.users().messages().get(
                userId='me', id=msg_id, format='metadata',
                metadataHeaders=['To', 'Subject', 'Date'],
            ).execute()
            headers = {h['name'].lower(): h['value'] for h in msg.get('payload', {}).get('headers', [])}
            out.append({
                'id': d['id'],
                'message_id': msg_id,
                'to': headers.get('to', ''),
                'subject': headers.get('subject', ''),
                'date': headers.get('date', ''),
                'snippet': msg.get('snippet', ''),
            })
        return out

    def delete_draft(self, draft_id: str) -> dict:
        """Delete a draft by its draft ID."""
        self.service.users().drafts().delete(userId='me', id=draft_id).execute()
        return {'deleted': draft_id}

    def create_draft_reply(
        self,
        message_id: str,
        body: str,
        html: Optional[str] = None,
        cc: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        markdown: bool = True,
    ) -> dict:
        """Create a draft reply to a message. Preserves thread, subject, and headers.

        If `html` is not provided and `markdown=True` (default), the body is
        rendered from markdown to an HTML fragment — same contract as `reply`.
        """
        if html is None and markdown:
            html = _markdown_to_html_fragment(body)
        original = self.service.users().messages().get(
            userId='me', id=message_id, format='full',
        ).execute()
        # Lookup case-insensitive (cf. reply) : "Message-ID" vs "Message-Id".
        headers = {h['name'].lower(): h['value'] for h in original['payload']['headers']}
        thread_id = original['threadId']

        from_addr = headers.get('from', '')
        to_addr = headers.get('to', '')
        reply_to_header = headers.get('reply-to', '')
        profile = self.service.users().getProfile(userId='me').execute()
        my_email = profile['emailAddress']

        if reply_to_header:
            reply_to = _recipients(reply_to_header)
        elif my_email.lower() in from_addr.lower():
            # notre propre message : on relance le fil vers TOUS ses destinataires
            reply_to = _recipients(to_addr)
        else:
            reply_to = _recipients(from_addr)

        if not reply_to:
            raise GmailClientError(f"Cannot determine reply recipient (from={from_addr!r}, to={to_addr!r})")

        subject = headers.get('subject', '')
        if not subject.lower().startswith('re:'):
            subject = f"Re: {subject}"

        orig_msg_id = headers.get('message-id', '')
        return self.create_draft(
            to=reply_to, subject=subject, body=body, html=html,
            cc=cc, attachments=attachments,
            thread_id=thread_id, in_reply_to=orig_msg_id or None,
            # Le rendu a déjà eu lieu ci-dessus : `markdown` est propagé pour qu'un
            # appel `markdown=False` ne se fasse pas re-rendre par le défaut de
            # `create_draft` (html reste None dans ce cas — c'est voulu).
            markdown=markdown,
        )

    def _build_message(self, to, subject, body, html=None, cc=None, bcc=None, attachments=None, from_name=None):
        """Build a MIME message."""
        has_attachments = attachments and len(attachments) > 0

        if has_attachments:
            message = MIMEMultipart('mixed')
            if html:
                text_part = MIMEMultipart('alternative')
                text_part.attach(MIMEText(body, 'plain'))
                text_part.attach(MIMEText(html, 'html'))
                message.attach(text_part)
            else:
                message.attach(MIMEText(body, 'plain'))
            for filepath in attachments:
                message.attach(self._make_attachment(filepath))
        elif html:
            message = MIMEMultipart('alternative')
            message.attach(MIMEText(body, 'plain'))
            message.attach(MIMEText(html, 'html'))
        else:
            message = MIMEText(body)

        # En-têtes en forme CANONIQUE (RFC 5322) : l'API Gmail rend les noms TELS
        # QU'ÉCRITS — un `cc:` minuscule reste `cc` dans `payload.headers`, quand
        # ceux que Gmail pose lui-même arrivent capitalisés (`From`, `Date`). Tout
        # lecteur qui cherche `Cc` (le nôtre compris, cf. `get_message`) voyait donc
        # nos propres messages sans destinataire ni copie — d'où le diagnostic
        # « le cc n'est pas appliqué » alors qu'il l'était (signaux #340/#342).
        message['To'] = to
        message['Subject'] = subject
        if cc:
            message['Cc'] = cc
        if bcc:
            message['Bcc'] = bcc
        if from_name:
            profile = self.service.users().getProfile(userId='me').execute()
            message['From'] = f'{from_name} <{profile["emailAddress"]}>'
        return message

    @staticmethod
    def _make_attachment(filepath: str) -> MIMEBase:
        """Create a MIME attachment from a file path."""
        path = Path(filepath)
        content_type, _ = mimetypes.guess_type(str(path))
        if content_type is None:
            content_type = 'application/octet-stream'
        main_type, sub_type = content_type.split('/', 1)

        part = MIMEBase(main_type, sub_type)
        part.set_payload(path.read_bytes())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=path.name)
        return part

    def search(self, query: str, max_results: int = 20) -> list[dict]:
        """Search messages using Gmail query syntax."""
        return self.list_messages(query=query, max_results=max_results)

    def archive_messages(self, message_ids: list[str]) -> list[dict]:
        """Archive messages (remove INBOX label). Returns list of results."""
        results = []
        for mid in message_ids:
            res = self.service.users().messages().modify(
                userId='me', id=mid,
                body={'removeLabelIds': ['INBOX']},
            ).execute()
            results.append({'id': res['id'], 'labelIds': res.get('labelIds', [])})
        return results

    def trash_message(self, message_id: str) -> dict:
        """Move a message to trash."""
        return self.service.users().messages().trash(
            userId='me', id=message_id,
        ).execute()

    def _list_attachments(self, payload: dict) -> list[dict]:
        """List attachment metadata from message payload.

        Renvoie {filename, mimeType, size}. **Pas d'`attachmentId`** : Gmail le
        régénère à chaque `messages.get` (vérifié — deux lectures successives du
        même message donnent des ids différents) → ce n'est pas un handle stable
        entre appels. Le contenu se récupère par `get_attachment(message_id,
        filename)`, qui résout l'id frais dans un seul `messages.get`.
        """
        attachments = []
        for part in self._iter_parts(payload):
            filename = part.get('filename')
            att_id = part.get('body', {}).get('attachmentId')
            if filename and att_id:
                attachments.append({
                    'filename': filename,
                    'mimeType': part.get('mimeType', ''),
                    'size': part.get('body', {}).get('size', 0),
                })
        return attachments

    def get_attachment(self, message_id: str, filename: str, index: int = 0) -> dict:
        """Récupère le CONTENU d'une pièce jointe par son NOM de fichier.

        Retourne {filename, mimeType, size, data: bytes}. Le **filename** est le
        handle (stable) — pas l'attachmentId Gmail, volatile entre appels. La
        résolution nom→id et le téléchargement se font dans le MÊME `messages.get`
        (l'id est donc toujours frais). `index` (défaut 0) départage si plusieurs
        PJ portent le même nom (ex. `image.png` inline multiples). Client pur :
        renvoie les octets, n'écrit rien sur disque (≠ `download_attachments`,
        réservé à la CLI). Lève `GmailClientError` si introuvable.
        """
        msg = self.service.users().messages().get(
            userId='me', id=message_id, format='full',
        ).execute()
        matches = []
        for part in self._iter_parts(msg.get('payload', {})):
            att_id = part.get('body', {}).get('attachmentId')
            if att_id and part.get('filename') == filename:
                matches.append((att_id, part.get('mimeType', '')))
        if not matches:
            raise GmailClientError(
                f"Pièce jointe {filename!r} introuvable dans le message {message_id!r}."
            )
        if index < 0 or index >= len(matches):
            raise GmailClientError(
                f"index {index} hors bornes ({len(matches)} PJ nommées {filename!r})."
            )
        att_id, mime = matches[index]
        att = self.service.users().messages().attachments().get(
            userId='me', messageId=message_id, id=att_id,
        ).execute()
        data = base64.urlsafe_b64decode(att['data'])
        return {
            'filename': filename,
            'mimeType': mime or 'application/octet-stream',
            'size': len(data),
            'data': data,
        }

    def _iter_parts(self, payload: dict):
        """Recursively yield all parts from a message payload."""
        parts = payload.get('parts', [])
        for part in parts:
            yield part
            yield from self._iter_parts(part)

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from message payload."""
        # Simple single-part message
        if payload.get('body', {}).get('data'):
            return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')

        # Multipart: look for text/plain first, then text/html
        parts = payload.get('parts', [])
        for mime_type in ('text/plain', 'text/html'):
            text = self._find_part(parts, mime_type)
            if text:
                return text

        return ''

    def _find_part(self, parts: list, mime_type: str) -> Optional[str]:
        """Recursively find a part by MIME type."""
        for part in parts:
            if part.get('mimeType') == mime_type and part.get('body', {}).get('data'):
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')
            # Recurse into nested parts
            nested = part.get('parts', [])
            if nested:
                result = self._find_part(nested, mime_type)
                if result:
                    return result
        return None
