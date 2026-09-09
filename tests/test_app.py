import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.cookiejar import CookieJar

from app import Config, SourceAdapter, create_server, device_summary, mask_ip, redact_path


class AppTests(unittest.TestCase):
    def test_redacts_query_strings_and_fragments(self):
        self.assertEqual(redact_path('GET /private?token=secret#fragment HTTP/1.1'), '/private')

    def test_masks_ipv4_and_normalizes_device(self):
        self.assertEqual(mask_ip('203.0.113.42'), '203.0.***.***')
        self.assertEqual(device_summary('Mozilla/5.0 (Windows NT 10.0) Chrome/1.0'), 'Chrome on Windows')

    def test_sources_normalize_and_sort_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'users.json').write_text(json.dumps([{'id': 'z'}, {'userId': 'a'}, {'id': 'z'}]), encoding='utf-8')
            (root / 'logins.json').write_text(json.dumps([{'userId': 'a', 'timestamp': '2099-01-02T09:00:00Z', 'ip': '203.0.113.1'}]), encoding='utf-8')
            (root / 'access.log').write_text('203.0.113.1 - - [02/Jan/2099:10:00:00 +0000] "GET /x?secret=1 HTTP/1.1" 200 10 "-" "Mozilla/5.0 Chrome/1.0 (Windows NT 10.0)"\n', encoding='utf-8')
            config = Config('127.0.0.1', 0, 'admin', 'password', 'x' * 32, root / 'users.json', root / 'logins.json', root / 'access.log', True, 24 * 365, 50, False, 20000)
            adapter = SourceAdapter(config)
            self.assertEqual([item['userId'] for item in adapter.users()], ['a', 'z'])
            self.assertEqual(adapter.logins()[0]['userId'], 'a')
            self.assertEqual(adapter.access()[0]['path'], '/x')

    def test_production_metadata_adapters_do_not_read_mailbox_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mailbox = root / 'mail' / 'mail' / 'talk.example'
            (mailbox / 'alice@talk.example').mkdir(parents=True)
            (mailbox / 'bob@talk.example').mkdir()
            log = root / 'chatmail.json.log'
            log.write_text(json.dumps({'log': 'imap-login: Login: user=<alice@talk.example>, method=PLAIN, rip=203.0.113.9, lip=172.23.0.2, TLS', 'time': '2099-01-02T09:30:00Z'}) + '\n', encoding='utf-8')
            config = Config('127.0.0.1', 0, 'admin', 'password', 'x' * 32, f'maildir:{root / "mail"}', f'docker-json:{log}', root / 'missing-access.log', True, 24 * 365, 50, False, 20000)
            adapter = SourceAdapter(config)
            self.assertEqual([item['userId'] for item in adapter.users()], ['alice@talk.example', 'bob@talk.example'])
            self.assertEqual(adapter.logins()[0]['ip'], '203.0.113.9')

    def test_http_authentication_and_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'users.json').write_text('[{"id":"a"}]', encoding='utf-8')
            (root / 'logins.json').write_text('[]', encoding='utf-8')
            (root / 'access.log').write_text('', encoding='utf-8')
            config = Config('127.0.0.1', 0, 'admin', 'password', 'x' * 32, root / 'users.json', root / 'logins.json', root / 'access.log', True, 24 * 365, 50, False, 20000)
            server = create_server(config)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            opener = build_opener(HTTPCookieProcessor(CookieJar()))
            base = f'http://127.0.0.1:{server.server_port}'
            with self.assertRaises(HTTPError) as unauthenticated:
                opener.open(f'{base}/api/summary')
            self.assertEqual(unauthenticated.exception.code, 401)
            login_page = opener.open(f'{base}/login').read().decode()
            csrf = login_page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
            request = Request(f'{base}/auth/login', data=urlencode({'csrf': csrf, 'username': 'admin', 'password': 'password'}).encode(), method='POST')
            response = opener.open(request)
            self.assertEqual(response.geturl(), f'{base}/')
            summary = json.loads(opener.open(f'{base}/api/summary').read())
            self.assertEqual(summary['userCount'], 1)
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
