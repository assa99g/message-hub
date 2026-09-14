"""Read-only Gmail integration with separate credentials and cursor per mailbox."""
import base64
import json
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import quote

SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
BASE = 'https://gmail.googleapis.com/gmail/v1/users/me/'


class GmailError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__('Ошибка Gmail: HTTP %s' % status)


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'br', 'li', 'tr') and not self.hidden:
            self.output.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.output.append(data)


def body_text(part):
    headers = {h['name'].lower(): h['value'] for h in part.get('headers', [])}
    if part.get('filename') or headers.get('content-disposition', '').lower().startswith('attachment'):
        return ''
    mime = part.get('mimeType', '')
    if mime.startswith('multipart/'):
        parts = part.get('parts', [])
        if mime == 'multipart/alternative':
            # Prefer plain text; never concatenate duplicate HTML/plain alternatives.
            for p in sorted(parts, key=lambda p: p.get('mimeType') != 'text/plain'):
                value = body_text(p)
                if value.strip():
                    return value
            return ''
        return '\n'.join(filter(None, (body_text(p) for p in parts)))
    if mime not in ('text/plain', 'text/html'):
        return ''
    data = part.get('body', {}).get('data', '')
    raw = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
    content_type = Message()
    content_type['content-type'] = headers.get('content-type', mime)
    charset = content_type.get_content_charset() or 'utf-8'
    try:
        value = raw.decode(charset, errors='replace')
    except LookupError:
        value = raw.decode('utf-8', errors='replace')
    if mime == 'text/html':
        parser = TextHTML()
        parser.feed(value)
        value = ''.join(parser.output)
    return value.strip()


def parse_message(raw):
    if {'SENT', 'DRAFT'} & set(raw.get('labelIds', [])):
        return None
    payload = raw.get('payload', {})
    headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
    return dict(gmail_id=raw['id'], thread_id=raw.get('threadId', raw['id']),
                received_ms=int(raw['internalDate']), sender=headers.get('from', ''),
                subject=headers.get('subject') or '(Без темы)', body=body_text(payload))


class CredentialsVault:
    def __init__(self, namespace):
        # Explicit macOS backend: no fallback to plaintext keyring plugins.
        from keyring.backends.macOS import Keyring
        self.backend = Keyring()
        self.namespace = namespace

    def save(self, account_id, credentials):
        self.backend.set_password(self.namespace, account_id, credentials.to_json())

    def load(self, account_id):
        from google.oauth2.credentials import Credentials
        value = self.backend.get_password(self.namespace, account_id)
        if not value:
            raise RuntimeError('Нужно повторно подключить этот ящик.')
        return Credentials.from_authorized_user_info(json.loads(value), scopes=[SCOPE])


class GmailClient:
    def __init__(self, credentials, persist=lambda credentials: None):
        from google.auth.transport.requests import AuthorizedSession
        self.session = AuthorizedSession(credentials)
        self.credentials = credentials
        self.persist = persist

    def get(self, path, **params):
        response = self.session.get(BASE + path, params=params, timeout=30)
        if response.status_code != 200:
            raise GmailError(response.status_code)
        self.persist(self.credentials)
        return response.json()

    def close(self):
        self.session.close()


def sync_account(store, account, client):
    count = 0

    def collect(message_id):
        nonlocal count
        try:
            raw = client.get('messages/' + quote(message_id, safe=''), format='full')
        except GmailError as error:
            if error.status == 404:  # Deleted before it could be collected.
                return
            raise
        message = parse_message(raw)
        if message:
            count += store.save_message(account, message)

    if account['history_id']:
        try:
            page = client.get('history', startHistoryId=account['history_id'],
                              historyTypes='messageAdded', maxResults=500)
        except GmailError as error:
            if error.status != 404:
                raise
        else:
            seen = set()
            while True:
                for history in page.get('history', []):
                    for added in history.get('messagesAdded', []):
                        message_id = added['message']['id']
                        if message_id not in seen:
                            collect(message_id)
                            seen.add(message_id)
                if not page.get('nextPageToken'):
                    store.checkpoint(account['id'], page['historyId'])
                    return count
                page = client.get('history', startHistoryId=account['history_id'],
                                  historyTypes='messageAdded', maxResults=500,
                                  pageToken=page['nextPageToken'])

    # First collection or expired history: scan only mail after connection.
    # Take the cursor BEFORE scanning, so arrivals during pagination are replayed.
    baseline = client.get('profile')['historyId']
    params = dict(q='after:%s -in:sent -in:drafts' % (account['connected_ms'] // 1000 - 1),
                  includeSpamTrash='true', maxResults=500)
    while True:
        page = client.get('messages', **params)
        for message in page.get('messages', []):
            collect(message['id'])
        if not page.get('nextPageToken'):
            break
        params['pageToken'] = page['nextPageToken']
    store.checkpoint(account['id'], baseline)
    return count
