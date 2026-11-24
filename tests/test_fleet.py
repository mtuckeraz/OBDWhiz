import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from core.adapter import Adapter
from core.bluetooth import Bluetooth
from core.fleet import Fleet
from core.fleet_store import FleetStore
from core.store import Store


RADIO = '/org/bluez/hci0'


def spec(number=0):
    return {'address': f'AA:BB:CC:DD:EE:{number:02X}', 'name': 'Carista EVO', 'label': f'Fleet vehicle {number}', 'controller': RADIO}


class DiagnosticArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fleet.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def scan(self):
        return self.store.save_scan({'vehicle': {'make': 'BMW', 'model': 'Test'}, 'vin': 'WBA12345678901234',
                                    'codes': [{'code': 'P0300', 'ecu': '7E8', 'status': 'stored'}], 'trace': [{'response': 'evidence'}]})

    def test_dated_snapshot_is_private_and_does_not_change_when_vehicle_is_edited(self):
        scan = self.scan()
        file = self.store.diagnostic_file(scan['id'])
        self.assertRegex(file.name, r'\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{6}Z_')
        original = file.read_bytes()
        self.assertEqual(json.loads(original)['id'], scan['id'])
        self.store.update_vehicle(scan['vin'], {'make': 'BMW', 'model': 'Corrected name'})
        self.assertEqual(file.read_bytes(), original)
        self.assertEqual(self.store.get(scan['id'])['vehicle']['model'], 'Corrected name')

    def test_failed_file_write_preserves_sqlite_record_and_recovers_after_restart(self):
        with patch('core.store.os.fsync', side_effect=OSError('Test disk write failure')):
            scan = self.scan()
        self.assertIn('Test disk write failure', scan['diagnosticFileError'])
        self.assertEqual(self.store.get(scan['id'])['codes'][0]['code'], 'P0300')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertTrue(self.store.diagnostic_file(scan['id']).exists())
        self.assertNotIn('diagnosticFileError', self.store.get(scan['id']))

    def test_existing_different_file_is_never_silently_overwritten(self):
        scan = self.scan()
        file = self.store.diagnostic_file(scan['id'])
        file.write_text('externally changed diagnostic evidence')
        self.store.write_diagnostic_file(scan['id'])
        self.assertEqual(file.read_text(), 'externally changed diagnostic evidence')
        self.assertIn('preserved', self.store.get(scan['id'])['diagnosticFileError'])


class FleetStoreTests(unittest.TestCase):
    tearDown = DiagnosticArchiveTests.tearDown
    scan = DiagnosticArchiveTests.scan

    def setUp(self):
        DiagnosticArchiveTests.setUp(self)
        self.fleet = FleetStore(self.store)

    def test_ten_reader_capacity_and_registration_survive_restart(self):
        for number in range(10):
            self.fleet.register(spec(number))
        self.fleet.register({**spec(0), 'label': 'Renamed fleet vehicle'})
        with self.assertRaisesRegex(ValueError, 'up to 10'):
            self.fleet.register(spec(10))
        self.assertEqual(len(FleetStore(self.store).readers()), 10)
        self.fleet.remove(spec(0)['address'])
        self.fleet.register(spec(10))
        self.assertEqual(len(self.fleet.readers()), 10)

    def test_code_absence_requires_positive_response_from_the_same_ecu_and_mode(self):
        address = self.fleet.register(spec())['address']
        first = self.scan()
        first['codeCoverage'] = {'03': ['7E8'], '07': [], '0A': []}
        self.fleet.track_codes(address, first)
        self.fleet.track_codes(address, {**first, 'codes': [], 'codeCoverage': {'03': ['7E9']}})
        events = self.fleet.events()['events']
        self.assertEqual(sum(e['type'] == 'code_first_observed' for e in events), 1)
        self.assertFalse(any(e['type'] == 'code_no_longer_reported' for e in events))
        self.fleet.track_codes(address, {**first, 'codes': [], 'codeCoverage': {'03': ['7E8']}})
        self.assertEqual(sum(e['type'] == 'code_no_longer_reported' for e in self.fleet.events()['events']), 1)

    def test_event_cursors_are_incremental_and_removing_reader_keeps_history(self):
        reader = self.fleet.register(spec())
        first = self.fleet.events(limit=1)
        self.fleet.event(reader['address'], 'test_event')
        next_page = self.fleet.events(after=first['nextCursor'])
        self.assertEqual(next_page['events'][0]['type'], 'test_event')
        self.fleet.remove(reader['address'])
        self.assertTrue(any(e['type'] == 'test_event' for e in self.fleet.events()['events']))


class FakeReader(Adapter):
    connected_count = 0
    maximum = 0

    async def connect(self, request):
        FakeReader.connected_count += 1
        FakeReader.maximum = max(FakeReader.maximum, FakeReader.connected_count)
        self.connection_address = request['id']
        self.connection_id = str(uuid.uuid4())
        self.state.update(connected=True, ready=False)
        return dict(self.state)

    async def initialize(self, request, **kwargs):
        self.state['ready'] = True
        return dict(self.state)

    async def scan(self, vehicle, **kwargs):
        await asyncio.sleep(0)
        return self.save_scan({'vehicle': dict(vehicle), 'codes': [{'code': 'P0300', 'ecu': '7E8', 'status': 'stored'}],
                               'codeCoverage': {'03': ['7E8'], '07': ['7E8'], '0A': ['7E8']},
                               'vinSource': 'unavailable', 'scanStatus': 'complete', 'trace': [{'response': 'synthetic evidence'}]})

    async def disconnect(self):
        if self.state['connected']:
            FakeReader.connected_count -= 1
        self.state.update(connected=False, ready=False)
        return dict(self.state)


class FleetEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'fleet.sqlite3')
        self.manual = FakeReader(self.store.save_scan, self.store.samples)
        self.bluetooth = Bluetooth()
        self.bluetooth.controller = RADIO
        self.fleet = Fleet(self.store, self.bluetooth, self.manual, adapter_factory=FakeReader)
        FakeReader.connected_count = FakeReader.maximum = 0

    async def asyncTearDown(self):
        await self.fleet.close()
        await self.manual.disconnect()
        self.store.db.close()
        self.temp.cleanup()

    def enroll(self, number=0):
        reader = self.fleet.data.register(spec(number))
        reader = self.fleet.data.observe(reader['address'])
        self.fleet.seen[reader['address']] = time.monotonic()
        self.fleet.devices[(reader['address'], RADIO)] = SimpleNamespace(address=reader['address'])
        return reader

    async def test_advertisement_callback_passes_the_real_bleak_validator_and_binds_its_radio(self):
        from bleak.backends.scanner import BaseBleakScanner
        reader = self.enroll()
        self.fleet.readers = {reader['address']: reader}
        self.fleet.seen.clear()
        self.fleet.pending_seen.clear()
        device = SimpleNamespace(address=reader['address'])
        for radio in ('/org/bluez/hci1', RADIO):
            native = SimpleNamespace(_ad_callbacks={})
            BaseBleakScanner.register_detection_callback(native, self.fleet.advertisement_callback(radio))
            for callback in native._ad_callbacks.values():
                callback(device, SimpleNamespace())
            self.assertEqual(reader['address'] in self.fleet.pending_seen, radio == RADIO)

    async def test_scanner_lifecycle_uses_real_callback_validation_and_updates_presence(self):
        from bleak.backends.scanner import BaseBleakScanner
        reader = self.fleet.data.register(spec())
        manager = self.bluetooth
        manager.objects = {RADIO: {'org.bluez.Adapter1': {'Address': '00:11:22:33:44:55', 'Powered': True}}}
        manager.refresh = AsyncMock()
        manager.set_property = AsyncMock()
        scanners = []

        class Scanner:
            def __init__(scanner, detection_callback, **kwargs):
                scanner._ad_callbacks = {}
                scanner.options = kwargs
                scanner.stopped = False
                BaseBleakScanner.register_detection_callback(scanner, detection_callback)
                scanners.append(scanner)

            async def start(scanner):
                for callback in scanner._ad_callbacks.values():
                    callback(SimpleNamespace(address=reader['address']), SimpleNamespace())

            async def stop(scanner):
                scanner.stopped = True

        self.fleet.state['running'] = True
        with patch('bleak.BleakScanner', Scanner):
            task = asyncio.create_task(self.fleet.watch())
            try:
                async def wait_for_presence():
                    while self.fleet.data.reader(reader['address'])['presence'] != 'present':
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_presence(), timeout=2)
                self.assertTrue(scanners[0].options['bluez']['filters']['DuplicateData'])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(scanners[0].stopped)

    async def test_a_live_scan_connection_keeps_presence_when_reader_stops_advertising(self):
        reader = self.enroll()
        self.fleet.seen[reader['address']] = time.monotonic() - 91
        self.manual.state['connected'] = True
        self.manual.connection_address = reader['address']
        self.fleet.presence_tick()
        self.assertEqual(self.fleet.data.reader(reader['address'])['presence'], 'present')
        self.manual.state['connected'] = False

    async def test_ten_readers_take_turns_without_overlapping_connections(self):
        readers = [self.enroll(n) for n in range(10)]
        await asyncio.gather(*(self.fleet.scan_one(reader, 'continuous') for reader in readers))
        self.assertEqual(FakeReader.maximum, 1)
        self.assertEqual(len(self.store.list()), 10)
        self.assertEqual(len(list(Path(self.temp.name).rglob('*.json'))), 10)
        self.assertTrue(all(r['lastScanStatus'] == 'complete' for r in self.fleet.data.readers()))

    async def test_manual_connection_automatically_scans_without_a_browser_scan_click(self):
        address = spec()['address']
        self.manual.device_cache[address] = SimpleNamespace(details={'path': RADIO + '/dev_AA_BB_CC_DD_EE_00'})
        request = {'id': address, 'name': 'Carista EVO', 'transport': 'ble'}
        await self.manual.connect(request)
        await self.fleet.manual_connected(request)
        await self.fleet.manual_task
        scans = self.store.list()
        self.assertEqual(len(scans), 1)
        record = self.store.get(scans[0]['id'])
        self.assertEqual(record['automation']['trigger'], 'manual_connection')
        self.assertTrue(self.store.diagnostic_file(record['id']).exists())
        self.assertTrue(self.manual.state['connected'])

    async def test_signal_loss_debounces_once_and_return_gets_a_new_visit(self):
        reader = self.enroll()
        original_visit = reader['visitId']
        self.fleet.seen[reader['address']] = time.monotonic() - 91
        self.fleet.presence_tick()
        self.fleet.presence_tick()
        events = self.fleet.data.events()['events']
        departures = [e for e in events if e['type'] == 'departure_inferred']
        self.assertEqual(len(departures), 1)
        self.assertFalse(departures[0]['freshScanAtDeparture'])
        returned = self.fleet.data.observe(reader['address'])
        self.assertNotEqual(returned['visitId'], original_visit)
        self.assertIsNone(returned['lastAttemptAt'])

    async def test_oldest_due_reader_wins_and_failed_reader_does_not_starve_others(self):
        first, second = self.enroll(0), self.enroll(1)
        self.assertEqual(self.fleet.due_reader()['address'], first['address'])
        self.fleet.data.scan_started(first['address'], 'continuous')
        self.fleet.data.scan_failed(first['address'], 'ECU asleep', 'continuous')
        self.assertEqual(self.fleet.due_reader()['address'], second['address'])

    async def test_failed_attempt_gets_a_file_and_never_becomes_an_all_clear_vehicle(self):
        reader = self.enroll()
        with patch.object(FakeReader, 'initialize', side_effect=ValueError('VIN mismatch or ECU unavailable')):
            await self.fleet.scan_one(reader, 'continuous')
        self.assertEqual(self.fleet.data.reader(reader['address'])['lastScanStatus'], 'error')
        attempt = self.store.get(self.store.list()[0]['id'])
        self.assertEqual(attempt['scanStatus'], 'failed')
        self.assertTrue(self.store.diagnostic_file(attempt['id']).exists())
        self.assertEqual(self.store.vehicles(), [])
        self.assertEqual(FakeReader.connected_count, 0)

    async def test_cancelling_an_inflight_scan_saves_an_interrupted_attempt_and_releases_link(self):
        reader = self.enroll()
        entered = asyncio.Event()
        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(FakeReader, 'scan', new=waiting):
            task = asyncio.create_task(self.fleet.scan_one(reader, 'continuous'))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(FakeReader.connected_count, 0)
        self.assertFalse(self.fleet.link_lock.locked())
        attempts = self.store.list()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['scanStatus'], 'failed')
        self.assertTrue(self.store.diagnostic_file(attempts[0]['id']).exists())

    async def test_pause_and_resume_do_not_invent_departures(self):
        reader = self.enroll()
        self.fleet.configure({'enabled': False})
        self.fleet.configure({'enabled': True})
        self.fleet.presence_tick()
        self.assertEqual(self.fleet.data.reader(reader['address'])['presence'], 'unknown')
        self.assertFalse(any(e['type'] == 'departure_inferred' for e in self.fleet.data.events()['events']))

    async def test_interrupted_attempt_is_archived_when_service_restarts(self):
        reader = self.enroll()
        self.fleet.data.scan_started(reader['address'], 'continuous')
        with patch('core.fleet.sys.platform', 'linux'), \
                patch.object(self.fleet, 'run', new=AsyncMock()), \
                patch.object(self.fleet, 'watch', new=AsyncMock()):
            await self.fleet.start()
            await asyncio.sleep(0)
        record = self.store.get(self.store.list()[0]['id'])
        self.assertEqual(record['scanStatus'], 'failed')
        self.assertEqual(record['automation']['trigger'], 'service_restart')
        self.assertTrue(self.store.diagnostic_file(record['id']).exists())
