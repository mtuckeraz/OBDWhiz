import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock
from urllib.parse import quote

from PIL import Image, TiffImagePlugin
from pillow_heif import register_heif_opener

from core.dossiers import Dossiers
from core.media import Media
from core.store import Store
from server import create_app
from tools.backup_dossiers import backup, restore, verify


def photo(format, metadata=True):
    output = io.BytesIO()
    image = Image.new('RGB', (100, 50), 'blue')
    exif = Image.Exif()
    if metadata:
        ratio = TiffImagePlugin.IFDRational
        exif[34665] = {36867: '2026:11:23 16:22:10', 36881: '-07:00'}
        exif[34853] = {1: 'N', 2: (ratio(33), ratio(30), ratio(0)),
                       3: 'W', 4: (ratio(111), ratio(54), ratio(0))}
    image.save(output, format, exif=exif.tobytes())
    return output.getvalue()


class DossierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        db = self.data / 'apex.sqlite3'
        legacy = Store(db)
        self.legacy = legacy.save_scan({'vin': 'WBA12345678901234', 'vehicle': {'make': 'BMW'}, 'codes': []})
        legacy.db.close()
        handler, self.adapter, self.run_async, self.loop, self.store = create_app(
            db, lan=True, media_path=self.root / 'private-media')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop)
        self.admin = self.change_password(self.login('admin', '123456'), 'example-admin-passphrase')

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.run_async(self.adapter.scan_worker.close())
        self.run_async(self.adapter.disconnect())
        stopped = threading.Event()
        def stop_loop():
            self.loop.stop()
            stopped.set()
        self.loop.call_soon_threadsafe(stop_loop)
        stopped.wait(2)
        self.store.db.close()

    def request(self, path, body=None, cookie=None, raw=None, metadata=None, method=None):
        headers = {'X-Apex-Client': '1'}
        if cookie:
            headers['Cookie'] = cookie
        data = raw if raw is not None else json.dumps(body) if body is not None else None
        if metadata:
            headers['X-Upload-Metadata'] = quote(json.dumps(metadata), safe='')
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        try:
            conn.request(method or ('POST' if data is not None else 'GET'), path, body=data, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def login(self, username, password):
        status, headers, _ = self.request('/api/login', {'username': username, 'password': password})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0]

    def change_password(self, session, new_password):
        data = {'password': new_password, 'confirmation': new_password}
        status, headers, body = self.request('/api/password', data, session)
        self.assertEqual(status, 200, body)
        return headers['Set-Cookie'].split(';')[0]

    def api(self, path, body=None, code=200, cookie=None):
        status, _, payload = self.request(path, body, cookie or self.admin)
        self.assertEqual(status, code, payload)
        return json.loads(payload)

    def vehicle(self, **overrides):
        return self.api('/api/dossiers', {
            'vin': 'SALGS2SE0LA123456', 'make': 'Land Rover', 'model': 'Range Rover',
            'year': 2020, 'plate': 'AZ 123-ABC', 'owner': 'Alice Example', **overrides}, 201)

    def upload(self, vehicle, original, name='truck.jpg', cookie=None, expected=201, **options):
        status, _, body = self.request('/api/dossiers/' + vehicle['id'] + '/media',
            cookie=cookie or self.admin, raw=original, metadata={'name': name, 'kind': 'photo', **options})
        self.assertEqual(status, expected, body)
        return json.loads(body)

    def test_migration_search_and_dated_owner_history(self):
        legacy = self.api('/api/dossiers?q=901234')[0]
        self.assertEqual(self.api('/api/dossiers/' + legacy['id'])['scans'][0]['id'], self.legacy['id'])
        self.assertEqual(Dossiers(self.store).search('BMW')[0]['id'], legacy['id'])
        draft1 = self.vehicle(vin='')
        draft2 = self.vehicle(vin='')
        self.assertNotEqual(draft1['id'], draft2['id'])
        vehicle = self.vehicle(vin='salgs2se0la123456')
        for query in ('az123abc', 'AZ-123 ABC', 'alice example', 'range 2020', 'a123456'):
            self.assertIn(vehicle['id'], [v['id'] for v in self.api('/api/dossiers?q=' + quote(query))])
        self.api('/api/dossiers/' + vehicle['id'], {'owner': 'Bob Example'}, 200)
        owners = self.api('/api/dossiers/' + vehicle['id'])['owners']
        self.assertEqual([o['name'] for o in owners], ['Bob Example', 'Alice Example'])
        self.assertIsNotNone(owners[1]['endedAt'])
        self.api('/api/dossiers', {'vin': vehicle['vin']}, 400)

    def test_garaging_location_and_profile_notes_persist_independently_of_dated_notes(self):
        vehicle = self.vehicle()
        path = '/api/dossiers/' + vehicle['id']
        profile = {'garagingLocation': 'North garage · Bay 4', 'profileNotes': 'Fleet unit 12\nKeep key at reception.'}
        self.api(path, profile, 200)
        self.api(path + '/note', {'value': 'Arrival documented.'}, 201)
        self.api(path, {'color': 'Blue'}, 200)
        detail = self.api(path)
        for field, value in profile.items():
            self.assertEqual(detail[field], value)
        self.assertEqual([note['body'] for note in detail['notes']], ['Arrival documented.'])
        self.api(path, {'garagingLocation': '', 'profileNotes': ''}, 200)
        detail = self.api(path)
        self.assertEqual(detail['garagingLocation'], '')
        self.assertEqual(detail['profileNotes'], '')
        self.assertEqual(len(detail['notes']), 1)

    def test_role_permissions_and_protected_media(self):
        self.api('/api/staff', {'username': 'tech', 'name': 'Technician 03',
                               'password': 'example-only-passphrase', 'role': 'user'}, 201)
        tech = self.change_password(self.login('tech', 'example-only-passphrase'), 'example-personal-passphrase')
        v = self.vehicle()
        self.api('/api/storage', code=403, cookie=tech)
        self.api('/api/storage', {'drive': 'system'}, 403, tech)
        self.api('/api/staff', code=403, cookie=tech)
        self.api('/api/dossiers/' + v['id'], {'owner': 'Different'}, 403, tech)
        for path in ('/api/dossiers/' + v['id'], '/api/scans/' + self.legacy['id']):
            self.assertEqual(self.request(path, method='DELETE')[0], 401)
            self.assertEqual(self.request(path, cookie=tech, method='DELETE')[0], 403)
        image = self.upload(v, photo('JPEG'), cookie=tech)
        self.assertEqual(image['uploaderName'], 'Technician 03')
        path = '/api/media/' + image['id']
        for variant in ('/original', '/thumb.jpg', '/preview.jpg'):
            self.assertEqual(self.request(path + variant)[0], 401)
            self.assertEqual(self.request(path + variant, cookie=tech)[0], 200)
        self.api(path + '/metadata', {'caption': 'Edited by uploader'}, 200, tech)

    def test_delete_scan_removes_its_artifacts_but_preserves_vehicle_and_other_scans(self):
        vehicle = self.api('/api/dossiers?q=901234')[0]
        scan = self.legacy
        archive = self.store.diagnostic_file(scan['id'])
        self.store.samples(scan['id'], [{'time': scan['createdAt'], 'value': 14.0}])
        other = self.store.save_scan({'vehicle': {}, 'vin': scan['vin'], 'codes': []})
        path = '/api/scans/' + scan['id']
        self.assertEqual(self.request(path, cookie=self.admin, method='DELETE')[0], 200)
        self.assertEqual(self.request(path, cookie=self.admin, method='DELETE')[0], 404)
        self.api(path, code=404)
        self.api(path + '/diagnostic', code=404)
        self.assertFalse(archive.exists())
        self.assertEqual(self.store.samples(scan['id']), [])
        detail = self.api('/api/dossiers/' + vehicle['id'])
        self.assertEqual([record['id'] for record in detail['scans']], [other['id']])

    def test_delete_vehicle_removes_its_files_and_notes_but_preserves_the_scan(self):
        vehicle = self.api('/api/dossiers?q=901234')[0]
        path = '/api/dossiers/' + vehicle['id']
        registry = self.adapter.scan_worker.registry
        address = 'AA:BB:CC:DD:EE:FF'
        scanner = registry.register({'address': address, 'transport': 'ble'})
        registry.fleet.register({'address': address, 'controller': '/org/bluez/hci0', 'vin': vehicle['vin']})
        with self.store.lock, self.store.db:
            registry.assign(scanner['id'], vehicle['id'], 'obd_vin', vehicle['vin'], 'test')
        scan = self.store.save_scan({'vehicle': {}, 'vin': vehicle['vin'], 'codes': [],
                                     'scannerEvidence': {'address': address, 'transport': 'ble'}})
        self.assertEqual(scan['vehicleId'], vehicle['id'])
        media = self.upload(vehicle, photo('JPEG'))
        image_dir = self.root / 'private-media' / vehicle['id'] / media['id']
        self.assertTrue(image_dir.is_dir())
        self.api(path + '/note', {'value': 'Parking instructions'}, 201)
        self.api(path + '/visit', {'value': 'Arrival'}, 201)
        archive = self.store.diagnostic_file(self.legacy['id'])
        self.assertEqual(self.request(path, cookie=self.admin, method='DELETE')[0], 200)
        self.api(path, code=404)
        self.assertEqual(self.api('/api/dossiers?q=901234'), [])
        self.api('/api/media/' + media['id'] + '/original', code=404)
        self.assertFalse(image_dir.exists())
        self.assertTrue(archive.is_file())
        self.api('/api/scans/' + self.legacy['id'])
        self.assertIsNone(registry.current(scanner_id=scanner['id']))
        self.assertFalse(registry.fleet.reader(address)['enabled'])
        self.assertNotIn('vehicleId', self.api('/api/scans/' + scan['id']))

    def test_default_credentials_require_change_and_old_sessions_are_revoked(self):
        pending = self.login('user', '123456')
        other_pending = self.login('user', '123456')
        self.assertTrue(self.api('/api/session', cookie=pending)['user']['mustChangePassword'])
        for route in ('/api/dossiers', '/api/scans', '/api/staff'):
            self.assertTrue(self.api(route, code=403, cookie=pending)['passwordChangeRequired'])
        self.api('/api/password', {'password': '123456', 'confirmation': '123456'}, 400, pending)
        self.api('/api/password', {'password': 'changed-user-passphrase', 'confirmation': 'mismatch'}, 400, pending)
        active = self.change_password(pending, 'changed-user-passphrase')
        self.api('/api/dossiers', code=401, cookie=other_pending)
        self.assertFalse(self.api('/api/session', cookie=active)['user']['mustChangePassword'])
        self.api('/api/staff', code=403, cookie=active)
        self.api('/api/fleet/settings', {'enabled': False}, 403, active)
        self.api('/api/dossiers', cookie=active)
        self.assertEqual(self.request('/api/login', {'username':'user', 'password':'123456'})[0], 401)
        self.assertEqual(self.request('/api/login', {'key':'example-phase-one-key'})[0], 401)
        reopened = Dossiers(self.store)
        self.assertIsNone(reopened.authenticate('user', '123456'))
        self.assertEqual(reopened.authenticate('user', 'changed-user-passphrase')['role'], 'user')

    def test_scanner_qr_profile_is_protected_and_starts_a_durable_job(self):
        self.adapter.scan_worker.notify = AsyncMock()
        scanner = self.api('/api/adapters', {'transport': 'ble', 'address': 'AA:BB:CC:DD:EE:FF'}, 201)
        endpoint = '/api/adapters/' + scanner['id']
        self.assertEqual(self.request(endpoint)[0], 401)
        self.assertEqual(self.request(endpoint + '/qr')[0], 401)
        status, headers, payload = self.request(endpoint + '/qr', cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/png')
        with Image.open(io.BytesIO(payload)) as qr:
            self.assertEqual(qr.format, 'PNG')
        job = self.api('/api/scan-jobs', {'type':'verify', 'scannerId':scanner['id']}, 202)
        self.assertEqual(self.api('/api/scan-jobs/' + job['id'])['status'], 'queued')
        self.assertEqual(self.api(endpoint)['jobs'][0]['id'], job['id'])

    def test_twenty_uploads_keep_exif_and_browser_location_distinct_and_restore(self):
        register_heif_opener()
        v = self.vehicle()
        visit = self.api('/api/dossiers/' + v['id'] + '/visit', {'value':'Arrival'}, 201)
        photos = []
        for index in range(20):
            extension, original = ('heic', photo('HEIF')) if index == 0 else ('jpg', photo('JPEG'))
            item = self.upload(v, original, f'café-{index}.{extension}', visitId=visit['id'],
                               uploadLocation={'latitude': 40, 'longitude': -70, 'accuracy': 9})
            self.assertEqual(item['capturedAt'], '2026-11-23T16:22:10-07:00')
            self.assertNotEqual(item['uploadedAt'], item['capturedAt'])
            self.assertAlmostEqual(item['captureLocation']['latitude'], 33.5)
            self.assertEqual(item['uploadLocation']['latitude'], 40)
            self.assertEqual(item['sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(self.request('/api/media/' + item['id'] + '/original', cookie=self.admin)[2], original)
            preview = self.request('/api/media/' + item['id'] + '/thumb.jpg', cookie=self.admin)[2]
            self.assertFalse(Image.open(io.BytesIO(preview)).getexif())
            photos.append(item)
        self.assertTrue(photos[-1]['duplicate'])
        self.upload(v, b'Example document', 'service.txt', kind='document')
        self.upload(v, b'not an image', expected=400)
        self.upload(v, photo('PNG', metadata=False), 'plain.png', visitId='wrong-vehicle', expected=400)
        untagged = self.upload(v, photo('PNG', metadata=False), 'plain.png')
        self.assertTrue(untagged['captureGpsMissing'])
        self.assertIsNone(untagged['capturedAt'])
        self.assertEqual(len(self.api('/api/dossiers/' + v['id'])['media']), 22)
        snapshot = backup(self.data, self.root / 'private-media', self.root / 'backups')
        verify(snapshot)
        recovered = restore(snapshot, self.root / 'recovered')
        database = sqlite3.connect(recovered / 'proactive.sqlite3')
        try:
            self.assertEqual(database.execute('SELECT count(*) FROM dossier_media').fetchone()[0], 22)
            self.assertEqual(database.execute('SELECT count(*) FROM dossier_visits').fetchone()[0], 1)
        finally:
            database.close()
        source = photos[0]
        reopened = Store(recovered / 'proactive.sqlite3')
        try:
            restored_dossiers = Dossiers(reopened)
            archive = Media(restored_dossiers, recovered / 'media')
            self.assertEqual(restored_dossiers.get(v['id'])['vin'], v['vin'])
            self.assertEqual(len(archive.list(v['id'])), 22)
            recovered_original = archive.path(archive.get(source['id']), 'original')
            self.assertEqual(hashlib.sha256(recovered_original.read_bytes()).hexdigest(), source['sha256'])
        finally:
            reopened.db.close()
