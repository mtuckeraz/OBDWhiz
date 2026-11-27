"""Run queued dossier jobs alongside the existing scan and fleet connection paths."""
import asyncio
from contextlib import suppress
import sys

from .adapter import Adapter
from .dossiers import now
from .scanners import SPECIAL_FUNCTIONS
from .store import VEHICLE_FIELDS, normalize_vin


class ScanWorker:
    def __init__(self, registry, adapter, fleet, bluetooth, adapter_factory=Adapter):
        self.registry = registry
        self.adapter, self.fleet, self.bluetooth = adapter, fleet, bluetooth
        self.adapter_factory = adapter_factory
        self.wake = asyncio.Event()
        self.task = None

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def notify(self):
        await self.start()
        self.wake.set()

    async def run(self):
        while True:
            self.wake.clear()
            job = self.registry.next_job()
            if not job:
                await self.wake.wait()
                continue
            if self.bluetooth.task and not self.bluetooth.task.done():
                await asyncio.sleep(1)
                continue
            async with self.fleet.link_lock:
                if self.bluetooth.task and not self.bluetooth.task.done():
                    continue
                self.registry.transition(job['id'], 'running')
                try:
                    await asyncio.wait_for(self.execute(job), timeout=240)
                except asyncio.CancelledError:
                    self.registry.transition(job['id'], 'failed', {**job['detail'], 'message': 'Interrupted by service shutdown.'})
                    raise
                except Exception as exc:
                    self.registry.transition(job['id'], 'failed',
                                             {**job['detail'], 'message': str(exc) or 'The scanner did not respond in time.'})

    async def session_for(self, session, scanner):
        address, transport = scanner['address'], scanner['transport']
        if transport == 'ble':
            from bleak import BleakScanner
            options = {}
            controller = (self.registry.fleet.reader(address) or {}).get('controller') or scanner.get('controller')
            if sys.platform == 'linux' and controller:
                options['bluez'] = {'adapter': controller.rsplit('/', 1)[-1]}
            device = await BleakScanner.find_device_by_address(address, timeout=10, **options)
            if not device:
                raise ValueError('Scanner did not advertise. Check ignition, range and other connected apps.')
            session.device_cache[address] = device
        await session.connect({'transport': transport, 'id': address, 'name': scanner['name']})

    async def execute(self, job):
        special = job['type'] in SPECIAL_FUNCTIONS
        if special:
            target = self.registry.special_target(job['scannerId'], job['vehicleId'], job['type'])
            if target['id'] != job['detail']['assignmentId'] or target['verifiedVin'] != job['detail']['expectedVin']:
                raise ValueError('The scanner assignment changed. Select the vehicle and submit a new request.')
        scanner = self.registry.get(job['scannerId'])
        same_link = (self.adapter.state['connected'] and self.adapter.connection_address == scanner['address']
                     and self.adapter.state['transport'] == scanner['transport'])
        session = self.adapter if same_link else self.adapter_factory(self.registry.store.save_scan, self.registry.store.samples)
        trace_start = len(session.trace) if same_link and session.state['ready'] else 0
        try:
            if not same_link:
                await self.session_for(session, scanner)
            self.registry.seen(scanner['id'], lastConnectedAt=now(), lastDetectedAt=now())
            if not session.state['ready']:
                chars = session.state.get('characteristics', [])
                pairs = [(tx, rx) for tx in chars for rx in chars
                         if tx['service'] == rx['service'] and tx['handle'] != rx['handle']
                         and set(tx['properties']) & {'write', 'write-without-response'}
                         and set(rx['properties']) & {'notify', 'indicate'}]
                spec = {'tx': pairs[0][0]['handle'], 'rx': pairs[0][1]['handle']} if len(pairs) == 1 and not session.state.get('profile') else {}
                if session.client and not session.state.get('profile') and not spec:
                    raise ValueError('Open Adapter & vehicle, initialize using the scanner’s documented UART endpoints, then retry.')
                await session.initialize(spec)
            self.registry.capabilities(scanner['id'], session.state)
            vin = normalize_vin(await session.read_vin())
            if special:
                if not vin or vin != job['detail']['expectedVin']:
                    raise ValueError('The vehicle did not return the VIN recorded for this scanner.')
                self.registry.transition(job['id'], 'complete', {**job['detail'], 'confirmedVin': vin,
                    'message': 'Vehicle VIN confirmed. Complete the service operation using the listed OEM tool.'})
                return
            target = self.registry.inspect_vin(job, scanner, vin)
            if not target:
                return
            vehicle = {key: target[key] for key in VEHICLE_FIELDS if target.get(key)}
            vehicle['vin'] = vin
            await session.scan(vehicle, require_verified_vin=True, job_id=job['id'])
        except Exception as exc:
            if special:
                raise
            message = str(exc) or 'The scanner did not respond in time.'
            self.registry.store.save_scan({
                'vehicle': {}, 'codes': [], 'scanStatus': 'failed', 'source': 'Scanner job',
                'trigger': 'scanner_job', 'scanJobId': job['id'], 'startedAt': self.registry.job(job['id'])['startedAt'],
                'scannerEvidence': {'transport': scanner['transport'], 'address': scanner['address']},
                'warnings': [message[:5000]], 'error': message[:5000], 'trace': session.trace[trace_start:]
            })
        finally:
            if not same_link:
                with suppress(Exception):
                    await asyncio.wait_for(session.disconnect(), timeout=10)
