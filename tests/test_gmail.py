import base64
import tempfile
import unittest
from pathlib import Path

from message_hub.gmail import GmailError, body_text, parse_message, sync_account
from message_hub.store import Store


def part(text, mime='text/plain', **kwargs):
    return dict(mimeType=mime, body={'data': base64.urlsafe_b64encode(text.encode()).decode()}, **kwargs)


def message(message_id='new', timestamp=10000, labels=None):
    return dict(id=message_id, threadId='thread', internalDate=str(timestamp),
                labelIds=labels or ['INBOX'], payload=part('Текст письма'))


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        expected, response = next(self.responses)
        if expected != path:
            raise AssertionError((expected, path))
        if isinstance(response, Exception):
            raise response
        return response


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / 'test.sqlite3')
        self.store.connect('one@example.test', lambda account_id: None)
        with self.store.db() as db:
            db.execute('UPDATE accounts SET connected_ms=5000')
        self.account = self.store.accounts()[0]

    def test_first_sync_cutoff_and_duplicate_retry(self):
        responses = [('profile', {'historyId':'h0'}),
                     ('messages', {'messages':[{'id':'old'}, {'id':'new'}]}),
                     ('messages/old', message('old', 4999)),
                     ('messages/new', message())]
        client = FakeClient(responses)
        self.assertEqual(sync_account(self.store, self.account, client), 1)
        self.assertEqual(client.calls[1][1]['q'], 'after:4 -in:sent -in:drafts')
        self.assertEqual(client.calls[1][1]['includeSpamTrash'], 'true')
        self.assertEqual(sync_account(self.store, self.account, FakeClient(responses)), 0)
        self.assertEqual(self.store.stats()['messages'], 1)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='collected'").fetchone()[0], 1)

    def test_same_gmail_id_in_two_accounts(self):
        self.store.connect('two@example.test', lambda account_id: None)
        with self.store.db() as db:
            db.execute('UPDATE accounts SET connected_ms=5000')
        for account in self.store.accounts():
            self.store.save_message(account, parse_message(message()))
        self.assertEqual(self.store.stats()['messages'], 2)

    def test_reauthorization_preserves_history_and_cutoff(self):
        self.store.checkpoint(self.account['id'], 'old-cursor')
        self.store.connect('ONE@example.test', lambda account_id: self.assertEqual(account_id, self.account['id']))
        account = self.store.accounts()[0]
        self.assertEqual(account['history_id'], 'old-cursor')
        self.assertEqual(account['connected_ms'], 5000)
        self.assertEqual(len(self.store.accounts()), 1)

    def test_expired_history_uses_connection_cutoff_and_snapshot_before_scan(self):
        self.account['history_id'] = 'expired'
        client = FakeClient([('history', GmailError(404)), ('profile', {'historyId':'fresh'}),
                             ('messages', {'messages':[{'id':'new'}], 'nextPageToken':'next'}),
                             ('messages/new', message()), ('messages', {})])
        self.assertEqual(sync_account(self.store, self.account, client), 1)
        self.assertEqual(self.store.accounts()[0]['history_id'], 'fresh')
        self.assertEqual(client.calls[-1][1]['pageToken'], 'next')

    def test_failure_does_not_advance_cursor_and_retry_is_idempotent(self):
        self.store.checkpoint(self.account['id'], 'before')
        self.account = self.store.accounts()[0]
        page = {'history':[{'messagesAdded':[{'message':{'id':'new'}}]}], 'historyId':'after', 'nextPageToken':'p2'}
        client = FakeClient([('history', page), ('messages/new', message()), ('history', GmailError(503))])
        with self.assertRaises(GmailError):
            sync_account(self.store, self.account, client)
        self.assertEqual(self.store.accounts()[0]['history_id'], 'before')
        client = FakeClient([('history', page), ('messages/new', message()), ('history', {'historyId':'after'})])
        self.assertEqual(sync_account(self.store, self.account, client), 0)
        self.assertEqual(self.store.accounts()[0]['history_id'], 'after')

    def test_deleted_message_does_not_block_history(self):
        self.account['history_id'] = 'before'
        page = {'history':[{'messagesAdded':[{'message':{'id':'gone'}}]}], 'historyId':'after'}
        self.assertEqual(sync_account(self.store, self.account, FakeClient([
            ('history', page), ('messages/gone', GmailError(404))])), 0)
        self.assertEqual(self.store.accounts()[0]['history_id'], 'after')

    def test_no_update_checkpoint(self):
        self.account['history_id'] = 'before'
        sync_account(self.store, self.account, FakeClient([('history', {'historyId':'after'})]))
        self.assertEqual(self.store.accounts()[0]['history_id'], 'after')

    def test_html_plain_alternatives_and_attachment_exclusion(self):
        payload = dict(mimeType='multipart/mixed', parts=[
            dict(mimeType='multipart/alternative', parts=[part('<p>HTML</p>', 'text/html'),part('Plain')]),
            part('Secret attachment', filename='notes.txt'),
            part('Another attachment', headers=[{'name':'Content-Disposition','value':'attachment'}])])
        self.assertEqual(body_text(payload), 'Plain')
        self.assertEqual(body_text(part('<style>hidden</style><p>Hello &amp; hi</p><script>bad()</script>', 'text/html')), 'Hello & hi')
        self.assertIsNone(parse_message(message(labels=['SENT'])))
        self.assertIsNone(parse_message(message(labels=['DRAFT'])))

    def test_openings_count_distinct_messages_and_keep_events(self):
        self.store.save_message(self.account, parse_message(message()))
        message_id = self.store.messages()[0]['id']
        self.store.open_message(message_id)
        self.store.open_message(message_id)
        self.assertEqual(self.store.stats(), {'messages':1, 'opened':1, 'unread':0})
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='opened'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
