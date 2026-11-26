"""Fair, sequential automatic scans for up to ten explicitly enrolled BLE readers."""
import asyncio
import contextlib
import datetime as dt
from functools import partial
import sys
import shutil
import time

from .adapter import Adapter
from .bluetooth import ADAPTER, DEVICE
from .fleet_store import FleetStore, utcnow


def age(timestamp, now=None):
    if not timestamp:
        return None
    return max(0, ((now or dt.datetime.now(dt.timezone.utc)) - dt.datetime.fromisoformat(timestamp)).total_seconds())


class Fleet:
    def __init__(self, store, bluetooth, manual, adapter_factory=Adapter):
        self.store = store
        self.data = FleetStore(store)
        self.bluetooth = bluetooth
        self.manual = manual
        self.adapter_factory = adapter_factory
        self.link_lock = asyncio.Lock()
        self.task = self.watch_task = self.manual_task = None
        self.scanners = {}
        self.scanner_retry = {}
        self.readers = {}
        self.seen = {}
        self.devices = {}
        self.pending_seen = {}
        self.session = None
        self.state = {'running': False, 'busyReader': None, 'lastError': None, 'radioErrors': {}}

    def status(self):
        readers = self.data.readers()
        for reader in readers:
            elapsed = age(reader.get('lastScanAt'))
            reader['lastScanAgeSeconds'] = round(elapsed, 1) if elapsed is not None else None
            reader['scanFresh'] = bool(reader['presence'] == 'present' and reader['lastScanStatus'] in ('complete', 'partial')
                                       and elapsed is not None and elapsed <= 60 and not reader.get('lastError'))
        return {'capacity': 10, 'settings': self.data.settings(), 'readers': readers, **self.state,
                'timingNote': 'Fault times are first observations. Bluetooth signal loss is only an inferred departure.',
                'freshnessNote': 'scanFresh means a scan within 60 seconds, not that a vehicle is safe or cleared for departure.'}

    def configure(self, changes):
        previous = self.data.settings()
        settings = self.data.configure(changes)
        if settings['enabled'] != previous['enabled']:
            for reader in self.data.readers():
                reader.update(presence='unknown', visitId=None)
                self.data.put(reader)
            self.seen.clear()
            self.pending_seen.clear()
            self.data.event(None, 'monitor_resumed' if settings['enabled'] else 'monitor_paused',
                            {'observation': 'Paused presence gaps are not reported as vehicle departures.'})
        return settings

    async def start(self):
        if self.task or sys.platform != 'linux':
            return
        for reader in self.data.readers():
            if reader['lastScanStatus'] == 'scanning':
                self.archive_failure(reader, 'Scan interrupted by service restart; no complete result.',
                                     'service_restart', reader.get('lastAttemptAt'), [])
                reader = self.data.reader(reader['address'])
            reader['presence'] = 'unknown'
            self.data.put(reader)
        self.data.event(None, 'monitor_started', {'observation': 'Presence is re-established after restart; offline departures are not invented.'})
        self.state['running'] = True
        self.task = asyncio.create_task(self.run())
        self.watch_task = asyncio.create_task(self.watch())

    async def close(self):
        self.state['running'] = False
        for task in (self.watch_task, self.task, self.manual_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for scanner in list(self.scanners.values()):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(scanner.stop(), timeout=5)
        self.scanners.clear()

    def detect(self, controller, device, advertisement):
        address = device.address.upper()
        reader = self.readers.get(address)
        if not reader or not reader['enabled'] or reader['controller'] != controller:
            return
        self.devices[(address, controller)] = device
        self.seen[address] = time.monotonic()
        self.pending_seen[address] = utcnow()

    def advertisement_callback(self, controller):
        # Bleak validates an exact two-parameter callable, including defaults.
        return partial(self.detect, controller)

    async def watch(self):
        from bleak import BleakScanner
        try:
            while self.state['running']:
                settings = self.data.settings()
                self.readers = {r['address']: r for r in self.data.readers() if r['enabled']}
                if self.readers and settings['enabled']:
                    try:
                        async with self.bluetooth.lock:
                            await self.bluetooth.refresh()
                        radios = {v[ADAPTER].get('Address'): p for p, v in self.bluetooth.objects.items() if ADAPTER in v}
                        for reader in self.readers.values():
                            resolved = radios.get(reader.get('controllerAddress'))
                            if resolved and resolved != reader['controller']:
                                reader['controller'] = resolved
                                self.data.put(reader)
                    except Exception as exc:
                        self.state['lastError'] = f'Bluetooth presence unavailable: {exc}'
                        await asyncio.sleep(5)
                        continue
                wanted = {r['controller'] for r in self.readers.values()} if settings['enabled'] else set()
                for controller in list(self.scanners):
                    if controller not in wanted:
                        scanner = self.scanners.pop(controller)
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(scanner.stop(), timeout=5)
                for controller in wanted - self.scanners.keys():
                    if time.monotonic() < self.scanner_retry.get(controller, 0):
                        continue
                    scanner = None
                    try:
                        async with self.bluetooth.lock:
                            await self.bluetooth.refresh()
                            if ADAPTER not in self.bluetooth.objects.get(controller, {}):
                                raise ValueError('Registered Pi Bluetooth radio is unavailable.')
                            await self.bluetooth.set_property(controller, ADAPTER, 'Powered', 'b', True)
                        scanner = BleakScanner(detection_callback=self.advertisement_callback(controller),
                                               bluez={'adapter': controller.rsplit('/', 1)[-1],
                                                      'filters': {'DuplicateData': True}})
                        await asyncio.wait_for(scanner.start(), timeout=20)
                        self.scanners[controller] = scanner
                        self.state['radioErrors'].pop(controller, None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if scanner:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(scanner.stop(), timeout=5)
                        self.state['radioErrors'][controller] = str(exc)
                        self.scanner_retry[controller] = time.monotonic() + 30
                if settings['enabled']:
                    self.presence_tick()
                await asyncio.sleep(2)
        finally:
            for scanner in list(self.scanners.values()):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(scanner.stop(), timeout=5)
            self.scanners.clear()

    def presence_tick(self):
        now = time.monotonic()
        settings = self.data.settings()
        for session in (self.manual, self.session):
            if session and session.state['connected'] and session.connection_address:
                address = session.connection_address.upper()
                self.seen[address] = now
                self.pending_seen[address] = utcnow()
        pending, self.pending_seen = self.pending_seen, {}
        for address, timestamp in pending.items():
            self.data.observe(address, timestamp, return_after=settings['returnAfterSeconds'])
        for reader in self.data.readers():
            if reader['presence'] == 'present' and now - self.seen.get(reader['address'], now) > min(
                    settings['presenceTimeoutSeconds'], settings['returnAfterSeconds']):
                self.data.mark_absent(reader)

    def due_reader(self):
        settings = self.data.settings()
        if not settings['enabled']:
            return None
        candidates = []
        for reader in self.data.readers():
            if not reader['enabled'] or reader['presence'] != 'present' or reader['lastScanStatus'] == 'scanning':
                continue
            last_attempt = reader.get('lastAttemptAt')
            return_due = reader.get('returnScanDueAt')
            since_scan = age(reader.get('lastScanAt'))
            if not return_due and since_scan is not None and since_scan < settings['scanIntervalSeconds']:
                continue
            new_return = return_due and (not last_attempt or return_due > last_attempt)
            if reader['lastScanStatus'] == 'error' and last_attempt and not new_return and age(last_attempt) < settings['retrySeconds']:
                continue
            trigger = 'return_after_absence' if return_due else 'return_or_first_contact' if since_scan is None else 'continuous'
            candidates.append({**reader, 'scanTrigger': trigger})
        return min(candidates, key=lambda r: r.get('lastAttemptAt') or '') if candidates else None

    async def run(self):
        while self.state['running']:
            try:
                if self.watch_task and self.watch_task.done():
                    error = self.watch_task.exception()
                    self.state['lastError'] = f'Presence monitor restarting: {error}'
                    self.watch_task = asyncio.create_task(self.watch())
                    await asyncio.sleep(5)
                if self.data.settings()['enabled']:
                    if shutil.disk_usage(self.store.directory).free < 256 * 1024 * 1024:
                        self.state['lastError'] = 'Automatic scans paused: less than 256 MB free disk space. Existing files are preserved.'
                        await asyncio.sleep(15)
                        continue
                    self.presence_tick()
                    reader = self.due_reader()
                    if reader and not (self.manual_task and not self.manual_task.done()) and not (self.bluetooth.task and not self.bluetooth.task.done()):
                        await self.scan_one(reader, reader['scanTrigger'])
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state['lastError'] = str(exc)
                await asyncio.sleep(5)

    async def manual_connected(self, spec):
        if not self.data.settings()['enabled']:
            return
        address = spec['id'].upper() if spec.get('transport') == 'ble' else spec['id']
        reader = self.data.reader(address)
        if reader and not reader['enabled']:
            return
        if spec.get('transport') == 'ble' and not reader:
            device = self.manual.device_cache.get(spec['id'])
            details = getattr(device, 'details', None)
            # Only BlueZ supplies a D-Bus path. Windows supplies RawAdvData.
            path = details.get('path') if isinstance(details, dict) else None
            controller = None
            if isinstance(path, str):
                controller = path.rsplit('/', 1)[0] if '/dev_' in path else self.bluetooth.controller
            if controller:
                try:
                    reader = self.data.register({'address': address, 'name': spec.get('name') or address, 'controller': controller,
                        'controllerAddress': self.bluetooth.objects.get(controller, {}).get(ADAPTER, {}).get('Address')})
                except ValueError:
                    pass  # Manual connections still get a file when the ten-reader registry is full.
        if reader:
            self.seen[address] = time.monotonic()
            reader = self.data.observe(address) or reader
        else:
            reader = {'address': address, 'name': spec.get('name') or address, 'label': spec.get('name') or address,
                      'vehicle': {}, 'vin': None, 'visitId': None, 'controller': None}
        connection_id = self.manual.connection_id
        if self.manual_task and not self.manual_task.done():
            self.manual_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.manual_task
        self.manual_task = asyncio.create_task(self.scan_one(reader, 'manual_connection', connection_id=connection_id))

    def archive_failure(self, reader, error, trigger, started, trace):
        failed = self.store.save_scan({'vehicle': dict(reader.get('vehicle') or {}), 'codes': [],
            'source': 'Automatic fleet scan attempt', 'scanStatus': 'failed',
            'scope': 'Attempt failed; no all-clear result and no verified vehicle identity.',
            'warnings': [str(error)], 'trace': list(trace),
            'scannerEvidence': {'transport': reader.get('transport', 'ble'), 'address': reader['address'],
                                'name': reader['label']} if reader.get('transport') or reader.get('controller') else None,
            'reader': {'address': reader['address'], 'label': reader['label']},
            'automation': {'trigger': trigger, 'visitId': reader.get('visitId'), 'startedAt': started,
                           'expectedVin': reader.get('vin'), 'kind': 'failed_attempt'}})
        self.data.scan_failed(reader['address'], error, trigger)
        self.data.event(reader['address'], 'failed_attempt_saved', {'scanId': failed['id'], 'diagnosticFile': failed.get('diagnosticFile')})
        return failed

    async def scan_one(self, reader, trigger, connection_id=None):
        address = reader['address']
        async with self.link_lock:
            if self.bluetooth.task and not self.bluetooth.task.done():
                return
            manual = bool(self.manual.state['connected'] and self.manual.connection_address == address)
            if connection_id and (not manual or self.manual.connection_id != connection_id):
                return
            # Never race a newly queued manual-connect scan with a periodic job.
            if not connection_id and self.manual_task and not self.manual_task.done():
                return
            registered = self.data.reader(address)
            if not connection_id and (not registered or not registered['enabled'] or registered['presence'] != 'present'
                                      or not self.data.settings()['enabled']):
                return
            reader = registered or reader
            self.state.update(busyReader=address, lastError=None)
            self.data.scan_started(address, trigger)
            started = utcnow()

            def save(record):
                record['reader'] = {'address': address, 'label': reader['label'], 'controller': reader.get('controller')}
                record['automation'] = {'trigger': trigger, 'visitId': reader.get('visitId'), 'startedAt': started,
                                        'kind': 'code_scan', 'connectionId': session.connection_id}
                return self.store.save_scan(record)

            session = self.manual if manual else self.adapter_factory(save, self.store.samples)
            original_save = session.save_scan
            if manual:
                session.save_scan = save
            self.session = session
            try:
                async def execute():
                    if not manual:
                        device = self.devices.get((address, reader['controller']))
                        if not device:
                            raise ValueError('Waiting for a fresh advertisement from this registered receiver.')
                        session.device_cache[address] = device
                        await session.connect({'id': address, 'name': reader['name'], 'transport': 'ble'})
                    self.data.event(address, 'reader_connected', {'visitId': reader.get('visitId'), 'trigger': trigger,
                                                               'connectionId': session.connection_id})
                    if not session.state['ready']:
                        await session.initialize({}, discover_pids=False)
                    vehicle = dict(reader.get('vehicle') or {})
                    vehicle.setdefault('make', 'Unspecified')
                    vehicle.setdefault('model', 'Connected vehicle')
                    if reader.get('vin'):
                        vehicle['vin'] = reader['vin']
                    check_vin = getattr(self, 'require_verified_vin', None)
                    if check_vin and check_vin(address):
                        return await session.scan(vehicle, codes_only=True, require_verified_vin=True)
                    return await session.scan(vehicle, codes_only=True)

                scan = await asyncio.wait_for(execute(), timeout=120)
                self.data.scan_saved(address, scan, trigger)
            except asyncio.CancelledError:
                self.archive_failure(reader, 'Automatic scan interrupted; no complete result.', trigger, started, session.trace)
                raise
            except Exception as exc:
                self.archive_failure(reader, exc, trigger, started, session.trace)
            finally:
                session.save_scan = original_save
                if not manual:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(session.disconnect(), timeout=10)
                self.session = None
                self.state['busyReader'] = None
