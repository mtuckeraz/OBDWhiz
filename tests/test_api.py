import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from server import create_app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        handler, cls.adapter, cls.run_async, cls.loop, cls.store = create_app(Path(cls.temp.name) / 'api.sqlite3')
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.run_async(cls.adapter.disconnect())
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.store.db.close()
        cls.temp.cleanup()

    def request(self, path, body=None, headers=None, method=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        outgoing = {'X-Apex-Client': '1', 'Content-Type': 'application/json'}
        outgoing.update(headers or {})
        connection.request(method or ('POST' if body is not None else 'GET'), path, json.dumps(body) if body is not None else None, outgoing)
        response = connection.getresponse()
        result = response.status, response.getheaders(), response.read()
        connection.close()
        return result

    def test_static_app_and_security_headers(self):
        status, headers, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'Diagnostic workbench', body)
        self.assertIn(b'OBDWhiz', body)
        self.assertIn(b'Raspberry Pi', body)
        self.assertIn('frame-ancestors', dict(headers)['Content-Security-Policy'])

    def test_dns_rebinding_and_cross_origin_blocked(self):
        self.assertEqual(self.request('/api/status', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/demo', {}, {'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/demo', {}, {'X-Apex-Client': ''})[0], 403)
        self.assertEqual(self.request('/api/status', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_no_credential_echo_and_no_mutation_get_route(self):
        fake_key = 'test-key-never-sent-to-provider'
        result = self.request('/api/settings', {'apiKey': fake_key, 'autoResearch': False})
        self.assertEqual(result[0], 200)
        self.assertNotIn(fake_key.encode(), result[2])
        status = json.loads(self.request('/api/status')[2])
        self.assertTrue(status['ai']['configured'])
        self.assertNotIn(fake_key, json.dumps(status))
        self.assertEqual(self.request('/api/disconnect')[0], 404)
        self.request('/api/settings', {'clearKey': True})

    def test_import_preserves_unknown_codes_and_exports(self):
        status, _, body = self.request('/api/import', {'vehicle': {'make': 'Ferrari', 'model': 'Test', 'year': 2026}, 'codes': [{'code': 'A1B2C3', 'ecu': 'ECU2', 'definition': None}], 'trace': [{'response': 'unpublished raw evidence'}]})
        self.assertEqual(status, 200)
        scan = json.loads(body)
        export = json.loads(self.request('/api/scans/' + scan['id'] + '/export')[2])
        self.assertIsNone(export['codes'][0]['definition'])
        self.assertEqual(export['trace'][0]['response'], 'unpublished raw evidence')
        self.assertEqual(export['source'], 'Imported scan')
        self.assertEqual(export['samples'], [])

    def test_demo_excluded_from_paid_research(self):
        scan = json.loads(self.request('/api/demo', {})[2])
        status, _, body = self.request('/api/scans/' + scan['id'] + '/research', {})
        self.assertEqual(status, 400)
        self.assertIn(b'Demo', body)

    def test_uninitialized_scan_returns_actionable_error(self):
        status, _, body = self.request('/api/scan', {'vehicle': {'make': 'BMW', 'model': 'Test', 'year': 2026}})
        self.assertEqual(status, 400)
        self.assertIn(b'Initialize', body)

    def test_missing_scan_and_bad_schema(self):
        self.assertEqual(self.request('/api/scans/00000000-0000-0000-0000-000000000000')[0], 404)
        self.assertEqual(self.request('/api/import', {'vehicle': {}, 'codes': ['bad']})[0], 400)
        self.assertEqual(self.request('/api/settings', {'model': 'invalid model\r\n'})[0], 400)

    def test_vehicle_endpoint_preserves_vin_and_updates_matching_scans(self):
        vin = 'WBA12345678901234'
        scan = json.loads(self.request('/api/import', {'vehicle': {'make': 'Unspecified', 'model': 'Connected vehicle'}, 'vin': vin, 'codes': []})[2])
        context = {'make': 'Hyundai', 'model': 'IONIQ 5', 'year': 2023, 'trim': 'SEL', 'drivetrain': '2WD'}
        status, _, body = self.request('/api/vehicles/' + vin, {'vehicle': context})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['vehicle']['model'], 'IONIQ 5')
        groups = json.loads(self.request('/api/vehicles')[2])
        self.assertEqual(next(g for g in groups if g['vin'] == vin)['vehicle']['trim'], 'SEL')
        saved = json.loads(self.request('/api/scans/' + scan['id'])[2])
        self.assertEqual(saved['vin'], vin)
        self.assertEqual(saved['vehicle']['drivetrain'], '2WD')

    def test_invalid_vin_import_is_rejected(self):
        self.assertEqual(self.request('/api/import', {'vehicle': {}, 'vin': 'invalid', 'codes': []})[0], 400)

    def test_diagnostic_file_download_is_dated_and_ignores_imported_file_paths(self):
        status, _, body = self.request('/api/import', {'vehicle': {'make': 'BMW'}, 'codes': [], 'diagnosticFile': '../../.env'})
        self.assertEqual(status, 200)
        scan = json.loads(body)
        self.assertNotIn('..', scan['diagnosticFile'])
        status, headers, payload = self.request('/api/scans/' + scan['id'] + '/diagnostic')
        self.assertEqual(status, 200)
        self.assertIn('attachment;', dict(headers)['Content-Disposition'])
        self.assertEqual(json.loads(payload)['id'], scan['id'])

    def test_fleet_api_settings_and_event_cursor_validation(self):
        status, _, body = self.request('/api/fleet')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['capacity'], 10)
        self.assertEqual(self.request('/api/fleet/settings', {'scanIntervalSeconds': 1})[0], 400)
        self.assertEqual(self.request('/api/fleet/events?after=-1')[0], 400)
        self.assertEqual(self.request('/api/fleet/events?after=0&limit=2')[0], 200)


if __name__ == '__main__':
    unittest.main()
