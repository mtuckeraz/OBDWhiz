"""OBDWhiz Pi workbench with optional authenticated local-network access."""
import argparse
import asyncio
import concurrent.futures
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import webbrowser

ROOT = Path(__file__).resolve().parent
INSTALL_HINT = 'Run bash install-pi.sh on the Pi.' if sys.platform == 'linux' else 'Run Start-Apex.ps1 on Windows.'
for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines() if (ROOT / '.env').exists() else []:
    if '=' in line and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

from core import obd
from core.adapter import Adapter
from core.access import BrowserAccess, load_access_key
from core.bluetooth import ADAPTER, DEVICE, Bluetooth
from core.fleet import Fleet
from core.research import Research
from core.store import Store


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


def create_app(data_path=None, *, lan=False, access_key=None):
    if lan and not access_key:
        raise ValueError('LAN mode requires an access key.')
    access = BrowserAccess(access_key) if lan else None
    hostname = socket.gethostname().lower()
    bluetooth = Bluetooth((Path(data_path).parent if data_path else ROOT / '.local') / 'receiver.json')
    store = Store(data_path or ROOT / '.local' / 'apex.sqlite3')
    research = Research(store)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name='obdwhiz-bluetooth')
    thread.start()
    adapter = Adapter(store.save_scan, store.samples)
    adapter.bluetooth = bluetooth
    fleet = Fleet(store, bluetooth, adapter)
    adapter.fleet = fleet

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
            return fleet.data.register(checked)

    async def manual_disconnect():
        async with fleet.link_lock:
            return await adapter.disconnect()

    async def initialize(spec):
        async with fleet.link_lock:
            return await adapter.initialize(spec)

    async def manual_scan(vehicle):
        async with fleet.link_lock:
            return await adapter.scan(vehicle)

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
        server_version = 'OBDWhiz/0.4'

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
            if self.headers.get('Host', '').lower() not in expected:
                self.json({'error': 'Use this Pi hostname or IP address to open OBDWhiz.'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://{host}' for host in expected}:
                self.json({'error': 'Cross-origin access is blocked.'}, 403)
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' or (write and self.headers.get('X-Apex-Client') != '1'):
                self.json({'error': 'Use the OBDWhiz web interface.'}, 403)
                return False
            path = urlparse(self.path).path
            if access and path.startswith('/api/') and path not in ('/api/login', '/api/session') and not access.allowed(self.headers.get('Cookie')):
                self.json({'error': 'Sign in with the Pi access key.', 'authenticationRequired': True}, 401)
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
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()

        def json(self, data, status=200, cookie=None):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_headers(status, 'application/json; charset=utf-8', len(body), cookie)
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return
            path = urlparse(self.path).path
            try:
                if path == '/api/session':
                    return self.json({'required': lan, 'authenticated': not access or access.allowed(self.headers.get('Cookie'))})
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
                files = {'/': 'index.html', '/app.js': 'app.js', '/styles.css': 'styles.css', '/logo.svg': 'logo.svg'}
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
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 5_000_000:
                    return self.json({'error': 'Upload limit is 5 MB.'}, 413)
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('Expected a JSON object.')
                path = urlparse(self.path).path
                if path == '/api/login':
                    if not access:
                        return self.json({'authenticated': True})
                    token = access.login(body.get('key'), self.client_address[0])
                    if not token:
                        return self.json({'error': 'The access key is incorrect.'}, 401)
                    return self.json({'authenticated': True}, cookie=f'obdwhiz_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={access.lifetime}')
                if path == '/api/logout':
                    if access:
                        access.logout(self.headers.get('Cookie'))
                    return self.json({'authenticated': False}, cookie='obdwhiz_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if path == '/api/fleet/readers':
                    return self.json(run(register_reader(body)))
                if path == '/api/fleet/remove':
                    if fleet.state['busyReader'] == body.get('address'):
                        raise ValueError('Wait for this reader scan to finish before removing it.')
                    return self.json(fleet.data.remove(body.get('address')))
                if path == '/api/fleet/settings':
                    result = fleet.configure(body)
                    return self.json(result)
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
                    body.setdefault('scope', 'Imported data; module coverage depends on the source tool.')
                    return self.json(auto(store.save_scan(body)))
                if path == '/api/demo':
                    return self.json(store.save_scan(demo()))
                if path == '/api/definitions':
                    return self.json(store.import_definitions(body))
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
    parser.add_argument('--lan', action='store_true', help='Allow local-network browsers using a private access key')
    args = parser.parse_args()
    key = load_access_key(ROOT / '.local/access.key') if args.lan else None
    handler, adapter, run, loop, store = create_app(lan=args.lan, access_key=key)
    try:
        httpd = ThreadingHTTPServer(('0.0.0.0' if args.lan else '127.0.0.1', args.port), handler)
    except OSError as exc:
        sys.exit(f'Cannot start OBDWhiz: {exc}. Use --port to select an unused port.')
    run(adapter.fleet.start())
    print(f'OBDWhiz ready on port {args.port} ({"LAN; access key required" if args.lan else "localhost only"})', flush=True)
    if args.open:
        webbrowser.open(f'http://localhost:{args.port}')
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        run(adapter.fleet.close())
        run(adapter.bluetooth.close())
        run(adapter.disconnect())
        httpd.server_close()
        loop.call_soon_threadsafe(loop.stop)


if __name__ == '__main__':
    main()
