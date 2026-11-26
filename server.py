"""OBDWhiz Pi workbench with optional authenticated local-network access."""
import argparse
import asyncio
import concurrent.futures
import importlib.util
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse
import webbrowser

ROOT = Path(__file__).resolve().parent
INSTALL_HINT = 'Run bash install-pi.sh on the Pi.' if sys.platform == 'linux' else 'Run Start-Apex.ps1 on Windows.'
for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines() if (ROOT / '.env').exists() else []:
    if '=' in line and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

from core import obd
from core.adapter import Adapter
from core.access import BrowserAccess
from core.bluetooth import ADAPTER, DEVICE, Bluetooth
from core.fleet import Fleet
from core.dossiers import Dossiers, now
from core.media import CATEGORIES, MAX_BYTES, Media
from core.notifications import Notifications
from core.scan_worker import ScanWorker
from core.scanners import ScannerRegistry
from core.research import Research
from core.store import VEHICLE_FIELDS, Store, definition_source_batches, read_definition_csv


def demo():
    return {'vehicle': {'make': 'BMW', 'model': 'Demonstration vehicle', 'year': 2025, 'engine': 'Illustrative data', 'symptoms': 'Demo only'},
            'demo': True, 'source': 'Built-in demonstration', 'protocol': '6',
            'scope': 'Simulated standard OBD-II scan. No vehicle connected.',
            'codes': [{'code': 'P0300', 'ecu': '7E8', 'status': 'stored', 'definition': 'Illustrative random/multiple cylinder misfire code'},
                      {'code': 'P0171', 'ecu': '7E8', 'status': 'pending', 'definition': 'Illustrative lean mixture code'}],
            'readings': [{'pid': '0C', 'ecu': '7E8', 'label': 'Engine speed', 'unit': 'rpm', 'value': 812},
                         {'pid': '05', 'ecu': '7E8', 'label': 'Coolant temperature', 'unit': '°C', 'value': 86},
                         {'pid': '42', 'ecu': '7E8', 'label': 'Control module voltage', 'unit': 'V', 'value': 13.8}],
            'warnings': ['Demonstration data; no diagnosis has been performed.'], 'trace': [],
            'research': {'status': 'demo', 'text': 'This is a sample workspace. Connect the adapter and scan a real vehicle to research its codes. A code describes a detected condition; it does not establish which component caused it.'}}


def create_app(data_path=None, *, lan=False, authenticated=False, access_key=None, media_path=None, tls=False, public_host=None):
    hostname = socket.gethostname().lower()
    data_path = Path(data_path) if data_path else ROOT / '.local' / 'apex.sqlite3'
    bluetooth = Bluetooth(data_path.parent / 'receiver.json')
    store = Store(data_path)
    store.load_dtc_catalog()
    dossiers = Dossiers(store)
    media = Media(dossiers, media_path or store.directory / 'media')
    if media.root == ROOT / 'web' or ROOT / 'web' in media.root.parents:
        raise ValueError('Choose private media storage outside the web directory.')
    access = BrowserAccess(access_key, dossiers.authenticate) if lan or authenticated or access_key else None
    research = Research(store)
    notifications = Notifications(store, research.notification_suggestions)
    loop = asyncio.new_event_loop()
    def run_loop():
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=run_loop, daemon=True, name='obdwhiz-bluetooth')
    thread.start()
    adapter = Adapter(store.save_scan, store.samples)
    adapter.bluetooth = bluetooth
    fleet = Fleet(store, bluetooth, adapter)
    adapter.fleet = fleet
    scanners = ScannerRegistry(store, dossiers, fleet.data)
    worker = ScanWorker(scanners, adapter, fleet, bluetooth)
    adapter.scan_worker = worker
    adapter.notifications = notifications

    def link_scan(scan):
        dossiers.link_scan(scan)
        scanners.link_scan(scan)
        notifications.record_scan(scan)

    store.scan_linker = link_scan

    def require_fleet_vin(address):
        scanner = scanners.find('ble', address)
        return bool(scanner and scanner['assignment'] and scanner['assignment']['verifiedVin'])

    fleet.require_verified_vin = require_fleet_vin

    async def bluetooth_status():
        status = await bluetooth.snapshot()
        if status['available']:
            bluetooth.cache_devices(adapter)
        return status

    async def discover(spec):
        return await bluetooth.discover(adapter, spec.get('controller')) if sys.platform == 'linux' else await adapter.devices()

    async def connect(spec):
        async with fleet.link_lock:
            if bluetooth.task and not bluetooth.task.done():
                raise ValueError('Finish or cancel pairing before connecting.')
            result = await adapter.connect(spec)
            with store.lock, store.db:
                scanner = scanners.register({'transport': spec['transport'], 'address': adapter.connection_address,
                                             'name': str(result.get('device') or adapter.connection_address)[:120]}, commit=False)
            scanners.seen(scanner['id'], lastConnectedAt=now(), lastDetectedAt=now())
            bluetooth.state.update(status='idle', error=None, prompt=None, device=None)
            bluetooth.remember(spec)
            await fleet.manual_connected(spec)
            return result

    async def register_reader(spec):
        async with bluetooth.lock:
            await bluetooth.refresh()
            address = str(spec.get('address', '')).upper()
            controller = spec.get('controller') or bluetooth.controller
            props = next((v[DEVICE] for v in bluetooth.objects.values() if DEVICE in v
                          and v[DEVICE].get('Address') == address and v[DEVICE].get('Adapter') == controller), None)
            if not props or ADAPTER not in bluetooth.objects.get(controller, {}):
                raise ValueError('Discover this receiver on the selected Pi radio before adding it to the fleet.')
            checked = {**spec, 'address': address, 'controller': controller,
                       'controllerAddress': bluetooth.objects[controller][ADAPTER].get('Address'),
                       'name': props.get('Name') or props.get('Alias') or address}
            scanner = scanners.find('ble', address)
            assignment = scanner['assignment'] if scanner else None
            if assignment and assignment['method'] != 'legacy_fleet':
                if not assignment['verifiedVin']:
                    raise ValueError('Verify the scanner VIN before enabling this fleet reader.')
                checked.update(vin=assignment['verifiedVin'], vehicle={key: value for key, value in
                               assignment['vehicle'].items() if key in VEHICLE_FIELDS})
            reader = fleet.data.register(checked)
            with store.lock, store.db:
                scanners.register({'transport': 'ble', 'address': address, 'name': reader['name'],
                                   'controller': reader['controller']}, commit=False)
            return reader

    async def confirm_job(job_id, spec, actor):
        async with fleet.link_lock:
            job = scanners.confirm(job_id, spec.get('vehicleId'), actor, cancel=spec.get('cancel') is True)
        await worker.notify()
        return job

    async def release_scanner(scanner_id, actor):
        async with fleet.link_lock:
            return scanners.release(scanner_id, actor)

    async def manual_disconnect():
        async with fleet.link_lock:
            return await adapter.disconnect()

    async def initialize(spec):
        async with fleet.link_lock:
            state = await adapter.initialize(spec)
            scanner = scanners.find(state['transport'], adapter.connection_address)
            if scanner:
                scanners.capabilities(scanner['id'], state)
            return state

    async def manual_scan(vehicle):
        async with fleet.link_lock:
            return await adapter.scan(vehicle)

    async def configure_fleet(changes):
        return fleet.configure(changes)

    async def pair_reader(address):
        async with fleet.link_lock:
            return await bluetooth.start_pair(address)

    def run(coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=240)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ValueError('Operation timed out. Disconnect and reconnect the adapter.')

    def auto(scan):
        if research.auto and research.key and scan['codes'] and not scan.get('demo'):
            try:
                research.start(scan['id'])
            except ValueError as exc:
                scan['research'] = {'status': 'error', 'error': str(exc)}
                store.update(scan)
        result = store.get(scan['id'])
        result['definitionMatches'] = store.matches(result)
        return result

    class Handler(BaseHTTPRequestHandler):
        server_version = 'OBDWhiz/0.6'
        timeout = 60

        def log_message(self, fmt, *args):
            # Avoid logging imported data or provider keys.
            if args and str(args[0]).startswith(('POST', 'GET')):
                return

        def allowed(self, write=False):
            expected = {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}
            if lan:
                short = hostname.split('.')[0]
                names = {hostname, short, short + '.local', hostname if hostname.endswith('.local') else hostname + '.local', self.connection.getsockname()[0]}
                expected.update(f'{name}:{self.server.server_port}' for name in names)
            if public_host:
                expected.add(public_host.lower())
            host = self.headers.get('Host', '').lower()
            if host not in expected:
                self.json({'error': 'Use this Pi hostname or IP address to open OBDWhiz.'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin != f'{"https" if tls else "http"}://{host}':
                self.json({'error': 'Cross-origin access is blocked.'}, 403)
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' or (write and self.headers.get('X-Apex-Client') != '1'):
                self.json({'error': 'Use the OBDWhiz web interface.'}, 403)
                return False
            path = urlparse(self.path).path
            self.user = access.identity(self.headers.get('Cookie')) if access else None
            repository = path.startswith(('/api/dossiers', '/api/media/', '/api/staff', '/api/adapters', '/api/scan-jobs'))
            requires_login = repository or (access and path.startswith('/api/') and path not in ('/api/login', '/api/session'))
            if requires_login and not self.user:
                self.json({'error': 'Sign in to OBDWhiz.', 'authenticationRequired': True}, 401)
                return False
            if self.user and self.user.get('mustChangePassword') and path.startswith('/api/') and path not in (
                    '/api/session', '/api/logout', '/api/password'):
                self.json({'error': 'Change your initial password to continue.', 'passwordChangeRequired': True}, 403)
                return False
            if self.user and self.user['role'] != 'admin' and (
                    path.startswith(('/api/staff', '/api/notifications', '/api/smtp')) or
                    (write and path in ('/api/settings', '/api/dossiers/policy'))):
                self.json({'error': 'An administrator account is required.'}, 403)
                return False
            if self.user and self.user['role'] == 'user' and write and (
                    path.startswith(('/api/vehicles/', '/api/definitions/')) or path in ('/api/fleet/settings', '/api/fleet/remove',
                        '/api/fleet/readers', '/api/bluetooth/power', '/api/definitions')):
                self.json({'error': 'An administrator account is required.'}, 403)
                return False
            return True

        def send_headers(self, status, content_type, length, cookie=None, filename=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            if filename:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename, safe=''))
            self.end_headers()

        def json(self, data, status=200, cookie=None):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_headers(status, 'application/json; charset=utf-8', len(body), cookie)
            self.wfile.write(body)

        def session_cookie(self, token):
            secure = '; Secure' if tls else ''
            return f'obdwhiz_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={access.lifetime}{secure}'

        def do_GET(self):
            if not self.allowed():
                return
            path = urlparse(self.path).path
            try:
                if path == '/api/session':
                    return self.json({'required': bool(access), 'authenticated': not access or bool(self.user), 'user': self.user})
                if path == '/api/notifications':
                    return self.json(notifications.overview())
                if path == '/api/smtp':
                    return self.json(notifications.smtp_config())
                if path == '/api/dossiers':
                    query = parse_qs(urlparse(self.path).query)
                    return self.json(dossiers.search(query.get('q', [''])[0]))
                if path == '/api/dossiers/policy':
                    return self.json({'flagMissingGps': dossiers.flag_missing_gps(), 'categories': CATEGORIES})
                if path == '/api/staff':
                    return self.json(dossiers.users())
                if path == '/api/staff/storage':
                    return self.json(media.usage())
                if path == '/api/adapters':
                    return self.json(scanners.list())
                match = re.fullmatch(r'/api/adapters/([0-9a-f-]{36})(/qr)?', path)
                if match:
                    scanner = scanners.get(match[1])
                    if match[2]:
                        import qrcode
                        origin = f'{"https" if tls else "http"}://{self.headers["Host"]}'
                        output = BytesIO()
                        qrcode.make(f'{origin}/#scanners/{scanner["id"]}').save(output, format='PNG')
                        payload = output.getvalue()
                        self.send_headers(200, 'image/png', len(payload))
                        return self.wfile.write(payload)
                    scanner.update(history=scanners.history(scanner['id']), jobs=scanners.jobs(scanner_id=scanner['id']))
                    return self.json(scanner)
                match = re.fullmatch(r'/api/scan-jobs/([0-9a-f-]{36})', path)
                if match:
                    return self.json(scanners.job(match[1]))
                match = re.fullmatch(r'/api/dossiers/([0-9a-f-]{36})/(diagnostics|compare)', path)
                if match:
                    if match[2] == 'diagnostics':
                        vehicle = dossiers.detail(match[1])
                        return self.json({'assignment': scanners.current(vehicle_id=vehicle['id']),
                                          'jobs': scanners.jobs(vehicle_id=vehicle['id']), 'scans': vehicle['scans']})
                    query = parse_qs(urlparse(self.path).query)
                    return self.json(scanners.compare(match[1], query.get('before', [''])[0],
                                                      query.get('after', [''])[0]))
                match = re.fullmatch(r'/api/dossiers/([0-9a-f-]{36})', path)
                if match:
                    vehicle = dossiers.detail(match[1])
                    vehicle['media'] = media.list(match[1])
                    return self.json(vehicle)
                match = re.fullmatch(r'/api/media/([0-9a-f-]{36})/(original|thumb.jpg|preview.jpg)', path)
                if match:
                    item = media.get(match[1])
                    file = media.path(item, match[2])
                    with file.open('rb') as stream:
                        original = match[2] == 'original'
                        self.send_headers(200, 'application/octet-stream' if original else 'image/jpeg',
                                          os.fstat(stream.fileno()).st_size,
                                          filename=item['filename'] if original else None)
                        shutil.copyfileobj(stream, self.wfile)
                    return
                if path == '/api/bluetooth':
                    return self.json(run(bluetooth_status()))
                if path == '/api/fleet':
                    return self.json(fleet.status())
                if path == '/api/fleet/events':
                    query = parse_qs(urlparse(self.path).query)
                    return self.json(fleet.data.events(int(query['after'][0]) if 'after' in query else None, int(query.get('limit', ['100'])[0])))
                if path == '/api/status':
                    dependencies = {name: importlib.util.find_spec(name) is not None for name in ('bleak', 'serial')}
                    return self.json({'adapter': dict(adapter.state), 'ai': research.config(), 'dependencies': dependencies,
                                      'catalog': obd.catalog(), 'localOnly': not lan,
                                      'platform': sys.platform, 'installHint': INSTALL_HINT,
                                      'automaticScan': {'busyReader': fleet.state['busyReader'],
                                                        'manualPending': bool(fleet.manual_task and not fleet.manual_task.done())}})
                if path == '/api/scans':
                    return self.json(store.list())
                if path == '/api/vehicles':
                    return self.json(store.vehicles())
                if path == '/api/definitions':
                    return self.json(store.definitions())
                if path == '/api/definitions/catalog':
                    query = parse_qs(urlparse(self.path).query)
                    return self.json(store.definition_page(query.get('q', [''])[0], int(query.get('page', ['0'])[0])))
                if path == '/api/adapter/trace':
                    return self.json({'adapter': dict(adapter.state), 'trace': list(adapter.trace)})
                match = re.fullmatch(r'/api/scans/([0-9a-f-]+)(/samples|/export|/diagnostic)?', path)
                if match:
                    scan = store.get(match[1])
                    if match[2] == '/diagnostic':
                        file = store.diagnostic_file(match[1])
                        payload = file.read_bytes()
                        self.send_headers(200, 'application/json; charset=utf-8', len(payload), filename=file.name)
                        return self.wfile.write(payload)
                    if match[2] == '/samples':
                        return self.json(store.samples(match[1]))
                    if match[2] == '/export':
                        scan['samples'] = store.samples(match[1])
                    scan['definitionMatches'] = store.matches(scan)
                    return self.json(scan)
                files = {'/': 'index.html', '/app.js': 'app.js', '/dossiers.js': 'dossiers.js',
                         '/scanners.js': 'scanners.js',
                         '/styles.css': 'styles.css', '/logo.svg': 'logo.svg'}
                if path in files:
                    file = ROOT / 'web' / files[path]
                    data = file.read_bytes()
                    content_type = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml'}[file.suffix]
                    self.send_headers(200, content_type + '; charset=utf-8', len(data))
                    return self.wfile.write(data)
                self.json({'error': 'Not found.'}, 404)
            except LookupError as exc:
                self.json({'error': str(exc)}, 404)
            except ValueError as exc:
                self.json({'error': str(exc)}, 400)
            except Exception as exc:
                self.json({'error': str(exc)}, 500)

        def do_POST(self):
            if not self.allowed(write=True):
                return
            try:
                path = urlparse(self.path).path
                length = int(self.headers.get('Content-Length', '0'))
                match = re.fullmatch(r'/api/dossiers/([0-9a-f-]{36})/media', path)
                if match:
                    if not 0 < length <= MAX_BYTES:
                        return self.json({'error': 'Each upload must contain 1 byte to 50 MB.'}, 413)
                    options = json.loads(unquote(self.headers.get('X-Upload-Metadata', '{}')))
                    if not isinstance(options, dict):
                        raise ValueError('Invalid upload metadata.')
                    self.connection.settimeout(60)
                    return self.json(media.upload(match[1], self.rfile, length, options, self.user['id']), 201)
                if not 0 <= length <= 5_000_000:
                    return self.json({'error': 'Upload limit is 5 MB.'}, 413)
                payload = self.rfile.read(length)
                if path == '/api/definitions':
                    if self.headers.get_content_type() == 'text/csv':
                        definitions = read_definition_csv(payload.decode('utf-8-sig'))
                    else:
                        definitions = json.loads(payload or b'{}')
                        if isinstance(definitions, list):
                            definitions = {'definitions': definitions}
                    return self.json(store.import_definitions(definitions))
                body = json.loads(payload or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('Expected a JSON object.')
                if path == '/api/definitions/extract':
                    return self.json(research.extract_definitions(body))
                if path == '/api/definitions/prepare':
                    return self.json({'batches': definition_source_batches(body)})
                if path == '/api/definitions/approve':
                    return self.json(store.import_definitions(body, source_import=True))
                if path == '/api/smtp':
                    return self.json(notifications.configure_smtp(body))
                if path == '/api/smtp/test':
                    return self.json(notifications.test_mail(body.get('recipient')))
                if path == '/api/notifications':
                    return self.json(notifications.save_rule(body))
                match = re.fullmatch(r'/api/notifications/([0-9a-f-]{36})/remove', path)
                if match:
                    return self.json(notifications.remove_rule(match[1]))
                path = urlparse(self.path).path
                if path == '/api/login':
                    if not access:
                        return self.json({'authenticated': True})
                    password = body.get('password', body.get('key'))
                    token = access.login(password, self.client_address[0], body.get('username'))
                    if not token:
                        return self.json({'error': 'The sign-in credentials are incorrect.'}, 401)
                    return self.json({'authenticated': True}, cookie=self.session_cookie(token))
                if path == '/api/password':
                    if not access or not self.user:
                        return self.json({'error': 'Sign in first.'}, 401)
                    if body.get('password') != body.get('confirmation'):
                        raise ValueError('The passwords do not match.')
                    token = access.complete_password_change(self.headers.get('Cookie'), body.get('password'),
                                                             dossiers.change_initial_password)
                    return self.json({'authenticated': True}, cookie=self.session_cookie(token))
                if path == '/api/logout':
                    if access:
                        access.logout(self.headers.get('Cookie'))
                    secure = '; Secure' if tls else ''
                    return self.json({'authenticated': False}, cookie='obdwhiz_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0' + secure)
                if path == '/api/dossiers':
                    return self.json(dossiers.save(body, self.user['id']), 201)
                if path == '/api/dossiers/policy':
                    return self.json({'flagMissingGps': dossiers.flag_missing_gps(body['flagMissingGps'], self.user['id'])})
                if path == '/api/staff':
                    return self.json(dossiers.create_user(body, self.user['id']), 201)
                if path == '/api/adapters':
                    if self.user['role'] != 'admin':
                        return self.json({'error': 'Administrator access required to register scanners.'}, 403)
                    return self.json(scanners.register(body), 201)
                match = re.fullmatch(r'/api/adapters/([0-9a-f-]{36})/release', path)
                if match:
                    if self.user['role'] != 'admin':
                        return self.json({'error': 'Administrator access required to release scanners.'}, 403)
                    return self.json(run(release_scanner(match[1], self.user['id'])))
                if path == '/api/scan-jobs':
                    job = scanners.enqueue(body.get('scannerId'), body.get('vehicleId'), self.user['id'],
                                           body.get('type', 'scan'))
                    run(worker.notify())
                    return self.json(job, 202)
                match = re.fullmatch(r'/api/scan-jobs/([0-9a-f-]{36})/confirm', path)
                if match:
                    if self.user['role'] != 'admin':
                        return self.json({'error': 'Administrator access required to confirm scanner assignments.'}, 403)
                    return self.json(run(confirm_job(match[1], body, self.user['id'])))
                match = re.fullmatch(r'/api/dossiers/([0-9a-f-]{36})(/note|/visit)?', path)
                if match:
                    if match[2]:
                        return self.json(dossiers.add_entry(match[1], self.user['id'], match[2][1:], body.get('value')), 201)
                    if self.user['role'] == 'user':
                        return self.json({'error': 'An administrator can edit an existing vehicle.'}, 403)
                    return self.json(dossiers.save(body, self.user['id'], match[1]))
                match = re.fullmatch(r'/api/media/([0-9a-f-]{36})/metadata', path)
                if match:
                    if self.user['role'] == 'user' and media.get(match[1])['uploader'] != self.user['id']:
                        return self.json({'error': 'Only the uploader or an administrator can edit this media metadata.'}, 403)
                    return self.json(media.revise(match[1], body, self.user['id']))
                if path == '/api/fleet/readers':
                    return self.json(run(register_reader(body)))
                if path == '/api/fleet/remove':
                    if fleet.state['busyReader'] == body.get('address'):
                        raise ValueError('Wait for this reader scan to finish before removing it.')
                    return self.json(fleet.data.remove(body.get('address')))
                if path == '/api/fleet/settings':
                    return self.json(run(configure_fleet(body)))
                if path == '/api/bluetooth/power':
                    if body.get('powered') is False and fleet.data.settings()['enabled'] and fleet.data.readers():
                        raise ValueError('Pause automatic fleet scanning before turning the radio off.')
                    if adapter.state['connected']:
                        raise ValueError('Disconnect the receiver before changing the Pi Bluetooth radio.')
                    return self.json(run(bluetooth.power(body.get('controller'), body.get('powered'))))
                if path == '/api/bluetooth/pair':
                    if adapter.state['connected']:
                        raise ValueError('Disconnect the receiver before pairing.')
                    return self.json(run(pair_reader(body.get('id'))))
                if path == '/api/bluetooth/answer':
                    return self.json(run(bluetooth.answer(body.get('promptId'), body.get('value'))))
                if path == '/api/bluetooth/cancel':
                    return self.json(run(bluetooth.cancel()))
                if path == '/api/discover':
                    if adapter.state['connected']:
                        raise ValueError('Disconnect the receiver before discovering adapters.')
                    return self.json(run(discover(body)))
                if path == '/api/connect':
                    return self.json(run(connect(body)))
                if path == '/api/initialize':
                    if fleet.manual_task and not fleet.manual_task.done():
                        raise ValueError('The connection scan is running automatically. Wait for it to finish.')
                    return self.json(run(initialize(body)))
                if path == '/api/disconnect':
                    return self.json(run(manual_disconnect()))
                if path == '/api/poll':
                    return self.json(run(adapter.polling(body)))
                if path == '/api/scan':
                    return self.json(auto(run(manual_scan(body.get('vehicle', {})))))
                if path == '/api/import':
                    # Preserve OEM module names, status, raw evidence and definitions as supplied.
                    body['source'] = 'Imported scan'
                    for field in ('scannerEvidence', 'scannerId', 'vehicleId', 'scanJobId'):
                        body.pop(field, None)
                    body.setdefault('scope', 'Imported data; module coverage depends on the source tool.')
                    return self.json(auto(store.save_scan(body)))
                if path == '/api/demo':
                    return self.json(store.save_scan(demo()))
                vehicle_match = re.fullmatch(r'/api/vehicles/([A-HJ-NPR-Z0-9]{17})', path)
                if vehicle_match:
                    vehicle = store.update_vehicle(vehicle_match[1], body.get('vehicle', {}))
                    for reader in fleet.data.readers():
                        if reader.get('vin') == vehicle_match[1]:
                            reader['vehicle'] = dict(vehicle['vehicle'])
                            fleet.data.put(reader)
                    return self.json(vehicle)
                if path == '/api/settings':
                    model = body.get('model', research.model)
                    if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,100}', model):
                        raise ValueError('Invalid model name.')
                    key = body.get('apiKey')
                    if key:
                        if not isinstance(key, str) or len(key) > 1000 or '\n' in key or '\r' in key:
                            raise ValueError('Invalid API key.')
                        research.key = key.strip()
                    if body.get('clearKey'):
                        research.key = ''
                    research.model = model
                    research.auto = bool(body.get('autoResearch', research.auto))
                    return self.json(research.config())
                match = re.fullmatch(r'/api/scans/([0-9a-f-]+)/research', path)
                if match:
                    return self.json(research.start(match[1]))
                self.json({'error': 'Not found.'}, 404)
            except (ValueError, LookupError, KeyError, TypeError) as exc:
                self.json({'error': str(exc)}, 400)
            except ModuleNotFoundError:
                self.json({'error': f'Bluetooth libraries are missing. {INSTALL_HINT}'}, 503)
            except Exception as exc:
                self.json({'error': str(exc) or type(exc).__name__}, 500)

    return Handler, adapter, run, loop, store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--lan', action='store_true', help='Allow signed-in browsers on the local network')
    args = parser.parse_args()
    data = Path(os.environ.get('OBDWHIZ_DATA_DIR') or ROOT / '.local').resolve()
    cert = os.environ.get('OBDWHIZ_TLS_CERT')
    private_key = os.environ.get('OBDWHIZ_TLS_KEY')
    if bool(cert) != bool(private_key):
        sys.exit('Configure both OBDWHIZ_TLS_CERT and OBDWHIZ_TLS_KEY for HTTPS.')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if cert else None
    if context:
        context.load_cert_chain(cert, private_key)
    handler, adapter, run, loop, store = create_app(data / 'apex.sqlite3', lan=args.lan, authenticated=True,
        media_path=os.environ.get('OBDWHIZ_MEDIA_DIR'), tls=bool(context), public_host=os.environ.get('OBDWHIZ_PUBLIC_HOST'))
    try:
        httpd = ThreadingHTTPServer(('0.0.0.0' if args.lan else '127.0.0.1', args.port), handler)
    except OSError as exc:
        sys.exit(f'Cannot start OBDWhiz: {exc}. Use --port to select an unused port.')
    if context:
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    run(adapter.fleet.start())
    run(adapter.scan_worker.start())
    threading.Thread(target=adapter.notifications.run, daemon=True, name='obdwhiz-notifications').start()
    print(f'OBDWhiz ready on port {args.port} ({"LAN" if args.lan else "localhost"}; account sign-in required)', flush=True)
    if args.open:
        webbrowser.open(f'{"https" if context else "http"}://localhost:{args.port}')
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        adapter.notifications.stopped.set()
        run(adapter.scan_worker.close())
        run(adapter.fleet.close())
        run(adapter.bluetooth.close())
        run(adapter.disconnect())
        httpd.server_close()
        loop.call_soon_threadsafe(loop.stop)


if __name__ == '__main__':
    main()
