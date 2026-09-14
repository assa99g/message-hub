import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from message_hub.server import Application, handler_for


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Application(Path(self.directory.name), 60)

    def test_one_mailbox_failure_does_not_stop_others(self):
        for email in ['one@example.test', 'two@example.test']:
            self.app.store.connect(email, lambda account_id: None)
        with patch.object(self.app, 'vault') as vault, patch('message_hub.server.GmailClient'), patch('message_hub.server.sync_account') as sync:
            sync.side_effect = [RuntimeError('network failure'), 0]
            self.app.sync()
        self.assertEqual(sync.call_count, 2)
        self.assertIsNotNone(self.app.store.accounts()[0]['error'])
        self.assertFalse(self.app.sync_lock.locked())

    def test_client_config_rejects_web_and_normalizes_endpoints(self):
        with self.assertRaises(ValueError):
            self.app.configure({'web':{'client_id':'test.apps.googleusercontent.com'}})
        self.app.configure({'installed':{'client_id':'test.apps.googleusercontent.com', 'client_secret':'test', 'token_uri':'https://evil.test'}})
        self.assertEqual(json.loads(self.app.client_path.read_text())['installed']['token_uri'], 'https://oauth2.googleapis.com/token')
        self.assertEqual(self.app.client_path.stat().st_mode & 0o777, 0o600)

    def test_http_session_host_guards_and_no_arbitrary_static_files(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.app))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def request(path, headers=None):
                connection = HTTPConnection('127.0.0.1', server.server_port)
                connection.request('GET', path, headers=headers or {})
                response = connection.getresponse()
                result = response.status, response.read()
                connection.close()
                return result
            self.assertEqual(request('/api/status')[0], 401)
            self.assertEqual(request('/api/status', {'X-Message-Hub-Session':self.app.session})[0], 200)
            self.assertEqual(request('/', {'Host':'evil.test'})[0], 403)
            self.assertEqual(request('/../message_hub/server.py')[0], 404)
            self.assertEqual(request('/')[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
