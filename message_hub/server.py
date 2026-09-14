"""Loopback-only UI. Start with python -m message_hub.server."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .gmail import CredentialsVault, GmailClient, GmailError, SCOPE, sync_account
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / 'prototype' / 'dist'


class Application:
    def __init__(self, data_dir, interval):
        self.data_dir = data_dir
        self.store = Store(data_dir / 'message-hub.sqlite3')
        self.client_path = data_dir / 'secrets' / 'google-client.json'
        self.interval = interval
        self.session = secrets.token_urlsafe(32)
        self.sync_lock = threading.Lock()
        self.auth_lock = threading.Lock()
        self.stop = threading.Event()
        self.auth_status = 'idle'
        self.auth_error = ''

    def vault(self):
        namespace = hashlib.sha256(str(self.data_dir.resolve()).encode()).hexdigest()[:16]
        return CredentialsVault('Message Hub Gmail ' + namespace)

    def configure(self, config):
        installed = config.get('installed', {})
        client_id = installed.get('client_id', '')
        if not isinstance(client_id, str) or not client_id.endswith('.apps.googleusercontent.com'):
            raise ValueError('Нужен JSON OAuth-клиента типа Desktop app из Google Cloud.')
        if not isinstance(installed.get('client_secret'), str):
            raise ValueError('В файле отсутствует client_secret.')
        if self.store.accounts():
            raise ValueError('Приложение уже настроено и имеет подключённые ящики.')
        normalized = {'installed': {
            'client_id': client_id, 'client_secret': installed['client_secret'],
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': ['http://localhost']}}
        self.client_path.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = self.client_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(normalized), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(self.client_path)

    def authorize(self):
        if not self.client_path.exists():
            raise ValueError('Сначала добавьте файл настройки Google.')
        if not self.auth_lock.acquire(blocking=False):
            raise ValueError('Завершите вход в текущий ящик, затем добавьте следующий.')
        self.auth_status, self.auth_error = 'waiting', ''

        def work():
            client = None
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_path), [SCOPE], autogenerate_code_verifier=True)
                credentials = flow.run_local_server(
                    host='127.0.0.1', port=0, open_browser=True, timeout_seconds=300,
                    authorization_prompt_message='',
                    success_message='Вход завершён. Вернитесь в Message Hub.',
                    access_type='offline', prompt='consent select_account')
                if not credentials.refresh_token or not credentials.has_scopes([SCOPE]):
                    raise ValueError('Google не предоставил необходимый доступ.')
                client = GmailClient(credentials)
                email = client.get('profile')['emailAddress']
                self.store.connect(email, lambda account_id: self.vault().save(account_id, credentials))
                self.auth_status = 'connected'
            except Exception:
                # Never send OAuth responses or credentials to browser/logs.
                self.auth_status = 'error'
                self.auth_error = ('Подключение не завершено. Проверьте настройку Google, '
                                   'разрешение на чтение и доступ к Связке ключей. Попробуйте снова.')
            finally:
                if client:
                    client.close()
                self.auth_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def sync(self):
        if not self.sync_lock.acquire(blocking=False):
            return
        try:
            for account in self.store.accounts():
                client = None
                try:
                    vault = self.vault()
                    credentials = vault.load(account['id'])
                    client = GmailClient(credentials, lambda c, aid=account['id']: vault.save(aid, c))
                    sync_account(self.store, account, client)
                except GmailError as error:
                    detail = 'Gmail временно недоступен. Проверка повторится.'
                    if error.status in (401, 403):
                        detail = 'Проверьте доступ Gmail API или подключите ящик повторно.'
                    self.store.set_error(account['id'], detail)
                except Exception:
                    self.store.set_error(account['id'],
                        'Не удалось проверить ящик. Проверьте сеть и доступ к Связке ключей; при необходимости повторите вход.')
                finally:
                    if client:
                        client.close()
        finally:
            self.sync_lock.release()

    def schedule(self):
        while not self.stop.is_set():
            self.sync()
            self.stop.wait(self.interval * 60)

    def status(self):
        return dict(configured=self.client_path.exists(), accounts=self.store.accounts(),
                    stats=self.store.stats(), syncing=self.sync_lock.locked(),
                    auth_status=self.auth_status, auth_error=self.auth_error, interval=self.interval)


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, payload, mime='application/json; charset=utf-8'):
            data = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, (dict, list)) else payload
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def permitted(self):
            # Guard DNS rebinding as well as cross-origin requests.
            host = '127.0.0.1:%s' % self.server.server_port
            if self.headers.get('Host') != host:
                self.respond(403, {'error': 'Недопустимый адрес.'})
                return False
            if urlsplit(self.path).path.startswith('/api/'):
                supplied = self.headers.get('X-Message-Hub-Session', '')
                if not secrets.compare_digest(supplied, app.session):
                    self.respond(401, {'error': 'Откройте окно Message Hub через запуск программы.'})
                    return False
            return True

        def do_GET(self):
            if not self.permitted():
                return
            url = urlsplit(self.path)
            if url.path == '/api/status':
                self.respond(200, app.status())
            elif url.path == '/api/messages':
                query = parse_qs(url.query)
                try:
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                except ValueError:
                    self.respond(400, {'error': 'Неверная страница.'})
                    return
                self.respond(200, app.store.messages(query.get('account', [''])[0], offset))
            else:
                files = {'/': ('live.html', 'text/html; charset=utf-8'),
                         '/live.html': ('live.html', 'text/html; charset=utf-8'),
                         '/live.js': ('live.js', 'text/javascript; charset=utf-8'),
                         '/styles.css': ('styles.css', 'text/css; charset=utf-8')}
                if url.path not in files:
                    self.respond(404, {'error': 'Страница не найдена.'})
                    return
                filename, mime = files[url.path]
                self.respond(200, (STATIC / filename).read_bytes(), mime)

        def do_POST(self):
            if not self.permitted():
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 32768:
                    raise ValueError('Слишком большой запрос.')
                body = json.loads(self.rfile.read(length) or b'{}')
                path = urlsplit(self.path).path
                if path == '/api/config':
                    if not isinstance(body, dict):
                        raise ValueError('Неверный файл настройки.')
                    app.configure(body)
                    self.respond(200, {'ok': True})
                elif path == '/api/connect':
                    app.authorize()
                    self.respond(202, {'ok': True})
                elif path == '/api/sync':
                    threading.Thread(target=app.sync, daemon=True).start()
                    self.respond(202, {'ok': True})
                elif path == '/api/open':
                    result = app.store.open_message(int(body['id']))
                    self.respond(200 if result else 404, result or {'error': 'Письмо не найдено.'})
                else:
                    self.respond(404, {'error': 'Действие не найдено.'})
            except (ValueError, KeyError, TypeError, AttributeError):
                self.respond(400, {'error': 'Не удалось выполнить действие. Проверьте файл настройки и завершите предыдущий вход.'})
            except Exception:
                self.respond(500, {'error': 'Не удалось выполнить действие. Проверьте локальное хранилище.'})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path.home() / 'Library/Application Support/Message Hub')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('--interval должен быть не менее одной минуты')
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = Application(args.data_dir.resolve(), args.interval)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(app))
    server.daemon_threads = True
    url = 'http://127.0.0.1:%s/#session=%s' % (server.server_port, app.session)
    print('Message Hub: ' + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    threading.Thread(target=app.schedule, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
