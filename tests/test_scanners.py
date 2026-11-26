import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from core.adapter import Adapter
from core.dossiers import Dossiers
from core.fleet_store import FleetStore
from core.scan_worker import ScanWorker
from core.scanners import ScannerRegistry, compare_codes
from core.store import Store

F450 = '1FDXF46S12EC12345'
ROVER = 'SALGS2SE0LA123456'
ADDRESS = 'AA:BB:CC:DD:EE:FF'
ACTOR = 'tech'


class ScannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'apex.sqlite3')
        self.addCleanup(self.store.db.close)
        self.dossiers = Dossiers(self.store)
        self.fleet = FleetStore(self.store)
        self.registry = ScannerRegistry(self.store, self.dossiers, self.fleet)
        self.store.scan_linker = self.link_scan
        self.scanner = self.registry.register({'address': ADDRESS, 'transport': 'ble', 'model': 'EVO'})
        self.ford = self.dossiers.save({'vin': F450, 'make': 'Ford', 'model': 'F-450'}, ACTOR)
        self.rover = self.dossiers.save({'vin': ROVER, 'make': 'Land Rover', 'model': 'Range Rover'}, ACTOR)
        self.manual = Adapter(self.store.save_scan, self.store.samples)
        self.manual.connection_address = ADDRESS
        self.manual.state.update(connected=True, ready=True, transport='ble')
        self.worker = ScanWorker(self.registry, self.manual, SimpleNamespace(link_lock=asyncio.Lock()),
                                 SimpleNamespace(task=None))

    def link_scan(self, record):
        self.dossiers.link_scan(record)
        self.registry.link_scan(record)

    def simulate(self, vin, *, fail_after_verify=False):
        self.manual.read_vin = AsyncMock(return_value=vin)

        async def scan(vehicle, *, require_verified_vin, job_id):
            self.assertTrue(require_verified_vin)
            self.assertEqual(vehicle['vin'], vin)
            if fail_after_verify:
                raise ValueError('VIN changed before the diagnostic scan.')
            return self.store.save_scan({'vehicle': vehicle, 'codes': [], 'vinSource': 'obd',
                                         'scanStatus': 'partial', 'scanJobId': job_id,
                                         'scannerEvidence': {'address': ADDRESS, 'transport': 'ble'}})
        self.manual.scan = scan

    async def execute_next(self):
        job = self.registry.next_job()
        self.registry.transition(job['id'], 'running')
        await self.worker.execute(job)
        return self.registry.job(job['id'])

    async def test_moving_a_scanner_requires_confirmation_and_keeps_old_diagnostics(self):
        self.simulate(F450)
        self.registry.enqueue(self.scanner['id'], None, ACTOR, 'verify')
        first = await self.execute_next()
        self.assertEqual(first['status'], 'partial')
        before = self.store.diagnostic_file(first['scanId']).read_bytes()
        self.assertEqual(json.loads(before)['vehicleId'], self.ford['id'])
        self.simulate(ROVER)
        self.registry.enqueue(self.scanner['id'], self.ford['id'], ACTOR, 'scan')
        pending = await self.execute_next()
        self.assertEqual(pending['status'], 'needs_confirmation')
        self.assertEqual(pending['detail']['suggestedVehicle']['id'], self.rover['id'])
        self.assertEqual(self.registry.current(scanner_id=self.scanner['id'])['vehicleId'], self.ford['id'])
        with self.assertRaisesRegex(ValueError, 'matching the detected VIN'):
            self.registry.confirm(pending['id'], self.ford['id'], 'admin')
        self.registry.confirm(pending['id'], self.rover['id'], 'admin')
        second = await self.execute_next()
        self.assertEqual(second['status'], 'partial')
        self.assertEqual(self.store.get(second['scanId'])['scannerId'], self.scanner['id'])
        self.assertEqual(self.store.get(second['scanId'])['vehicleId'], self.rover['id'])
        self.assertEqual(self.store.diagnostic_file(first['scanId']).read_bytes(), before)
        self.assertEqual(self.dossiers.detail(self.ford['id'])['scans'][0]['id'], first['scanId'])
        history = self.registry.history(self.scanner['id'])
        self.assertEqual([item['verifiedVin'] for item in history], [ROVER, F450])
        self.assertTrue(history[-1]['endedAt'])
        self.assertEqual(self.registry.get(self.scanner['id'])['identifier'], 'OBD-001')
        self.registry.release(self.scanner['id'], 'admin')
        self.assertIsNone(self.registry.current(scanner_id=self.scanner['id']))
        self.assertTrue(self.registry.history(self.scanner['id'])[0]['endedAt'])
        self.assertEqual(self.dossiers.detail(self.rover['id'])['scans'][0]['id'], second['scanId'])

    async def test_missing_vin_can_record_assignment_but_cannot_attribute_a_scan(self):
        self.simulate(None)
        self.fleet.register({'address': ADDRESS, 'controller': '/org/bluez/hci0',
                             'name': 'EVO', 'vin': F450})
        self.registry.enqueue(self.scanner['id'], self.ford['id'], ACTOR, 'verify')
        pending = await self.execute_next()
        confirmed = self.registry.confirm(pending['id'], self.ford['id'], 'admin')
        self.assertEqual(confirmed['status'], 'complete')
        current = self.registry.current(scanner_id=self.scanner['id'])
        self.assertIsNone(current['verifiedVin'])
        self.assertFalse(self.fleet.reader(ADDRESS)['enabled'])
        self.assertEqual(self.dossiers.detail(self.ford['id'])['scans'], [])
        self.simulate(F450)
        self.registry.enqueue(self.scanner['id'], self.ford['id'], ACTOR, 'scan')
        verified = await self.execute_next()
        self.assertEqual(verified['status'], 'partial')
        self.assertTrue(self.fleet.reader(ADDRESS)['enabled'])

    async def test_second_vin_check_prevents_stale_attribution(self):
        self.simulate(F450, fail_after_verify=True)
        self.registry.enqueue(self.scanner['id'], None, ACTOR, 'verify')
        failed = await self.execute_next()
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('changed', failed['detail']['message'])
        scan = self.store.get(failed['scanId'])
        self.assertIsNone(scan['vehicleId'])
        self.assertEqual(scan['scannerId'], self.scanner['id'])
        self.assertTrue(self.store.diagnostic_file(scan['id']).exists())

    async def test_an_occupied_vehicle_needs_confirmation_before_displacement(self):
        second = self.registry.register({'transport':'serial', 'address':'COM14'})
        with self.store.lock, self.store.db:
            self.registry.assign(second['id'], self.rover['id'], 'obd_vin', ROVER, ACTOR)
        self.simulate(ROVER)
        self.registry.enqueue(self.scanner['id'], None, ACTOR, 'verify')
        pending = await self.execute_next()
        self.assertEqual(pending['detail']['displacedScannerId'], second['id'])
        self.assertIsNotNone(self.registry.current(scanner_id=second['id']))
        self.registry.confirm(pending['id'], self.rover['id'], 'admin')
        self.assertIsNone(self.registry.current(scanner_id=second['id']))

    async def test_worker_does_not_replay_running_jobs_after_restart(self):
        second = self.registry.register({'transport':'serial', 'address':'COM14'})
        running = self.registry.enqueue(self.scanner['id'], None, ACTOR, 'verify')
        self.registry.transition(running['id'], 'running')
        queued = self.registry.enqueue(second['id'], None, ACTOR, 'verify')
        reopened = ScannerRegistry(self.store, self.dossiers, self.fleet)
        self.assertEqual(reopened.job(running['id'])['status'], 'failed')
        self.assertEqual(reopened.next_job()['id'], queued['id'])

    def test_comparison_only_calls_a_missing_code_absent_for_a_responding_ecu(self):
        first = {'code':'P0300', 'status':'stored', 'ecu':'7E8'}
        second = {'code':'P0301', 'status':'stored', 'ecu':'7E9'}
        new = {'code':'P0400', 'status':'stored', 'ecu':'7E8'}
        before = {'codes':[first, second], 'codeCoverage':{'03':['7E8', '7E9']}}
        after = {'codes':[new], 'codeCoverage':{'03':['7E8']}}
        result = compare_codes(before, after)
        self.assertEqual(result['new'], [new])
        self.assertEqual(result['noLongerReported'], [first])
        self.assertEqual(result['unconfirmed'], [second])
