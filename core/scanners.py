"""Scanner registry, dated assignments and durable diagnostic requests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3

from .dossiers import clean, now, uid
from .store import normalize_vin

SCHEMA = """
CREATE TABLE IF NOT EXISTS scanners(
    number INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, transport TEXT NOT NULL,
    address TEXT NOT NULL, profile TEXT NOT NULL, UNIQUE(transport,address));
CREATE TABLE IF NOT EXISTS scanner_assignments(
    id TEXT PRIMARY KEY, scanner_id TEXT NOT NULL REFERENCES scanners(id),
    vehicle_id TEXT NOT NULL REFERENCES dossiers(id), started TEXT NOT NULL, ended TEXT,
    method TEXT NOT NULL, verified_vin TEXT, actor TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_scanner_assignment ON scanner_assignments(scanner_id) WHERE ended IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS active_vehicle_scanner ON scanner_assignments(vehicle_id) WHERE ended IS NULL;
CREATE TABLE IF NOT EXISTS scanner_scans(
    scan_id TEXT PRIMARY KEY REFERENCES scans(id), scanner_id TEXT NOT NULL REFERENCES scanners(id));
CREATE INDEX IF NOT EXISTS scanner_scan_history ON scanner_scans(scanner_id);
CREATE INDEX IF NOT EXISTS scanner_fleet_connections ON fleet_events(address,type,seq);
CREATE TABLE IF NOT EXISTS scan_jobs(
    id TEXT PRIMARY KEY, scanner_id TEXT NOT NULL REFERENCES scanners(id),
    vehicle_id TEXT REFERENCES dossiers(id), actor TEXT NOT NULL, type TEXT NOT NULL,
    status TEXT NOT NULL, created TEXT NOT NULL, started TEXT, completed TEXT,
    scan_id TEXT REFERENCES scans(id), detail TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_scan_job ON scan_jobs(scanner_id)
    WHERE status IN ('queued','running','needs_confirmation');
"""

JOB_FIELDS = ('id', 'scannerId', 'vehicleId', 'actor', 'type', 'status', 'createdAt',
              'startedAt', 'completedAt', 'scanId', 'detail')
ASSIGNMENT_FIELDS = ('id', 'scannerId', 'vehicleId', 'startedAt', 'endedAt', 'method', 'verifiedVin', 'actor')
PROFILE_FIELDS = ('name', 'manufacturer', 'model', 'firmware', 'ownership', 'notes', 'controller')
MODE = {'stored': '03', 'pending': '07', 'permanent': '0A'}
SOFTWARE_VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text(encoding='utf-8').strip()
MERCEDES_SERVICE = 'https://developer.mercedes-benz.com/products/workshop_information'
FORD_SERVICE = 'https://www.motorcraftservice.com/Diagnostic/HelmSupport?categoryId=238&channelId=41&country=USA&language=EN-US'
SPECIAL_FUNCTIONS = {
    'mercedes_service_mode': {'label': 'Service mode', 'makes': ('mercedesbenz', 'mercedes', 'mercedesamg'),
                              'tool': 'Mercedes-Benz XENTRY', 'reference': MERCEDES_SERVICE},
    'mercedes_rear_brakes': {'label': 'Rear brake service / retract parking brake',
                              'makes': ('mercedesbenz', 'mercedes', 'mercedesamg'),
                              'tool': 'Mercedes-Benz XENTRY', 'reference': MERCEDES_SERVICE},
    'ford_tire_size': {'label': 'Tire size calibration', 'makes': ('ford',), 'input': 'tireSize',
                       'tool': 'Ford service information and compatible diagnostic software', 'reference': FORD_SERVICE},
}


def compare_codes(before, after):
    def key(code):
        return tuple(str(code.get(field) or '').upper() for field in ('ecu', 'code', 'status'))

    def covered(scan, code):
        coverage = scan.get('codeCoverage')
        mode = MODE.get(str(code.get('status') or '').lower())
        return bool(mode and isinstance(coverage, dict) and isinstance(coverage.get(mode), list)
                    and code.get('ecu') in coverage[mode])

    earlier = {key(c): c for c in before['codes']}
    later = {key(c): c for c in after['codes']}
    return {
        'new': [c for k, c in later.items() if k not in earlier and covered(before, c)],
        'firstObserved': [c for k, c in later.items() if k not in earlier and not covered(before, c)],
        'persisted': [c for k, c in later.items() if k in earlier],
        'noLongerReported': [c for k, c in earlier.items() if k not in later and covered(after, c)],
        'unconfirmed': [c for k, c in earlier.items() if k not in later and not covered(after, c)]
    }


class ScannerRegistry:
    def __init__(self, store, dossiers, fleet):
        self.store, self.dossiers, self.fleet = store, dossiers, fleet
        self.db, self.lock = store.db, store.lock
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    self.db.execute(statement)
            if not self.db.execute('SELECT 1 FROM dossier_migrations WHERE version=3').fetchone():
                self.backfill()
                self.db.execute('INSERT INTO dossier_migrations VALUES(3)')
            self.db.execute("""UPDATE scan_jobs SET status='failed',completed=?,detail=?
                WHERE status='running'""", (now(), json.dumps({'message': 'Interrupted by restart. Request a fresh scan.'})))

    def backfill(self):
        scanners_by_address = {}
        for reader in self.fleet.readers():
            scanner = self.register({'transport': 'ble', 'address': reader['address'],
                                     'name': reader['name'], 'controller': reader['controller']}, commit=False)
            if scanner.get('name'):
                scanners_by_address[scanner['address']] = scanner
            target = self.db.execute('SELECT id FROM dossiers WHERE vin=?', (reader.get('vin'),)).fetchone()
            if target and not self.current(vehicle_id=target[0]):
                self.assign(scanner['id'], target[0], 'legacy_fleet', None, 'migration', reader['createdAt'])
        rows = self.db.execute("""SELECT id,data FROM scans WHERE json_valid(data)
            AND json_extract(data,'$.reader.address') IS NOT NULL ORDER BY created,id""")
        for scan_id, raw in rows:
            scan = json.loads(raw)
            if scan.get('demo') or scan.get('source') == 'Imported scan':
                continue
            address = scan['reader']['address']
            if not isinstance(address, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', address):
                continue
            address = address.upper()
            scanner = scanners_by_address.get(address)
            if scanner is None:
                scanner = self.register({'transport': 'ble', 'address': address,
                                         'name': str(scan['reader'].get('label') or address)[:120]}, commit=False)
                scanners_by_address[address] = scanner
            self.db.execute('INSERT OR IGNORE INTO scanner_scans VALUES(?,?)', (scan_id, scanner['id']))
            scan['scannerId'] = scanner['id']
            dossier = self.db.execute('SELECT vehicle_id FROM dossier_scans WHERE scan_id=?', (scan_id,)).fetchone()
            scan['vehicleId'] = dossier[0] if dossier else None
            scan['scannerIdentifier'] = scanner['identifier']
            self.db.execute('UPDATE scans SET data=? WHERE id=?', (json.dumps(scan), scan_id))

    def register(self, data, commit=True):
        transport = data.get('transport')
        address = clean(data.get('address'), 200, 'Hardware identifier')
        if transport not in ('ble', 'serial') or not address or not address.isprintable():
            raise ValueError('Choose BLE or serial and enter the hardware identifier.')
        address = address.upper() if transport == 'ble' else address
        if transport == 'ble' and not (
                re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address)
                or re.fullmatch(r'[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}', address)):
            raise ValueError('Enter a Bluetooth address or an OS-provided BLE UUID.')
        fields = {key: clean(data[key], 1000 if key == 'notes' else 120, key)
                  for key in PROFILE_FIELDS if key in data and data[key] is not None}
        if fields.get('ownership', 'shop') not in ('shop', 'dedicated'):
            raise ValueError('Choose shop-owned or vehicle-dedicated.')
        with self.lock:
            row = self.db.execute('SELECT id,profile FROM scanners WHERE transport=? AND address=?', (transport, address)).fetchone()
            if row:
                scanner_id, saved = row
                profile = json.loads(saved)
                if not commit:
                    fields = {k: v for k, v in fields.items() if not profile.get(k)}
                profile.update(fields)
                self.db.execute('UPDATE scanners SET profile=? WHERE id=?', (json.dumps(profile), scanner_id))
            else:
                scanner_id = uid()
                profile = {'ownership': 'shop', 'name': address, **fields}
                self.db.execute('INSERT INTO scanners(id,transport,address,profile) VALUES(?,?,?,?)',
                                (scanner_id, transport, address, json.dumps(profile)))
            if commit:
                self.db.commit()
            return self.get(scanner_id)

    def get(self, scanner_id):
        with self.lock:
            row = self.db.execute('SELECT number,id,transport,address,profile FROM scanners WHERE id=?', (scanner_id,)).fetchone()
            if not row:
                raise LookupError('Scanner not found.')
            number, scanner_id, transport, address, profile = row
            assignment = self.current(scanner_id=scanner_id)
            reader = self.fleet.reader(address) if transport == 'ble' else None
            recent = self.db.execute("""SELECT s.id,s.created,d.vehicle_id,json_extract(s.data,'$.scanStatus')
                FROM scanner_scans x JOIN scans s ON s.id=x.scan_id LEFT JOIN dossier_scans d ON d.scan_id=s.id
                WHERE x.scanner_id=? ORDER BY s.created DESC LIMIT 20""", (scanner_id,)).fetchall()
            last_scan = self.db.execute("""SELECT MAX(s.created) FROM scanner_scans x JOIN scans s ON s.id=x.scan_id
                WHERE x.scanner_id=? AND coalesce(json_extract(s.data,'$.scanStatus'),'')!='failed'""",
                (scanner_id,)).fetchone()[0]
        details = json.loads(profile)
        if reader:
            with self.lock:
                row = self.db.execute("""SELECT created FROM fleet_events WHERE address=? AND type='reader_connected'
                    ORDER BY seq DESC LIMIT 1""", (address,)).fetchone()
            if row:
                details['lastConnectedAt'] = max(details.get('lastConnectedAt') or '', row[0])
        return {**details, 'id': scanner_id, 'identifier': f'OBD-{number:03d}', 'transport': transport, 'address': address,
                'assignment': assignment, 'lastDetectedAt': (reader or {}).get('lastSeenAt') or details.get('lastDetectedAt'),
                'lastScanAt': last_scan,
                'recentScans': [dict(zip(('id','createdAt','vehicleId','status'), item)) for item in recent]}

    def list(self):
        with self.lock:
            return [self.get(row[0]) for row in self.db.execute('SELECT id FROM scanners ORDER BY number').fetchall()]

    def find(self, transport, address):
        with self.lock:
            row = self.db.execute('SELECT id FROM scanners WHERE transport=? AND address=?', (transport, address)).fetchone()
        return self.get(row[0]) if row else None

    def current(self, *, scanner_id=None, vehicle_id=None):
        column, value = ('scanner_id', scanner_id) if scanner_id is not None else ('vehicle_id', vehicle_id)
        with self.lock:
            row = self.db.execute(f'SELECT * FROM scanner_assignments WHERE {column}=? AND ended IS NULL', (value,)).fetchone()
        return self.assignment(row) if row else None

    def assignment(self, row):
        return {**dict(zip(ASSIGNMENT_FIELDS, row)), 'vehicle': self.dossiers.get(row[2])}

    def history(self, scanner_id):
        with self.lock:
            return [self.assignment(row) for row in self.db.execute(
                'SELECT * FROM scanner_assignments WHERE scanner_id=? ORDER BY started DESC', (scanner_id,))]

    def assign(self, scanner_id, vehicle_id, method, vin, actor, timestamp=None):
        """Caller holds the database lock and owns the transaction."""
        previous = self.current(scanner_id=scanner_id)
        occupied = self.current(vehicle_id=vehicle_id)
        if previous and previous['vehicleId'] == vehicle_id and previous['verifiedVin'] == vin:
            return previous
        stamp = timestamp or now()
        for item in (previous, occupied):
            if item:
                self.db.execute('UPDATE scanner_assignments SET ended=? WHERE id=? AND ended IS NULL', (stamp, item['id']))
        self.db.execute('INSERT INTO scanner_assignments VALUES(?,?,?,?,NULL,?,?,?)',
                        (uid(), scanner_id, vehicle_id, stamp, method, vin, actor))
        self.dossiers.audit(vehicle_id, actor, 'scanner_assigned', {'scannerId': scanner_id, 'method': method})
        return self.current(scanner_id=scanner_id)

    def sync_fleet(self, scanner, assignment):
        """Update a registered fleet reader in the same transaction as the assignment."""
        if scanner['transport'] != 'ble':
            return
        reader = self.fleet.reader(scanner['address'])
        if not reader:
            return
        vin = assignment['verifiedVin'] if assignment else None
        if reader.get('vin') != vin or not assignment or not vin:
            reader.update(vin=vin, vehicle=assignment['vehicle'] if vin else {},
                          lastScanAt=None, lastScanId=None, lastAttemptAt=None, returnScanDueAt=None,
                          lastError=None, lastScanStatus='waiting')
        if not vin:
            if reader['enabled']:
                reader['pausedForScannerAssignment'] = True
            reader['enabled'] = False
        elif reader.pop('pausedForScannerAssignment', False):
            reader['enabled'] = True
        self.db.execute('UPDATE fleet_readers SET data=? WHERE address=?',
                        (json.dumps(reader), scanner['address']))

    def apply_assignment(self, scanner, vehicle_id, method, vin, actor):
        other = self.current(vehicle_id=vehicle_id)
        if other and other['scannerId'] != scanner['id']:
            self.sync_fleet(self.get(other['scannerId']), None)
        assignment = self.assign(scanner['id'], vehicle_id, method, vin, actor)
        self.sync_fleet(scanner, assignment)
        return assignment

    def release(self, scanner_id, actor):
        with self.lock, self.db:
            self.expire_confirmations()
            if self.db.execute("""SELECT 1 FROM scan_jobs WHERE scanner_id=? AND
                    status IN ('queued','running','needs_confirmation')""", (scanner_id,)).fetchone():
                raise ValueError('Resolve the active scan job before releasing this scanner.')
            scanner = self.get(scanner_id)
            current = scanner['assignment']
            if current:
                self.db.execute('UPDATE scanner_assignments SET ended=? WHERE id=?', (now(), current['id']))
                self.sync_fleet(scanner, None)
                self.dossiers.audit(current['vehicleId'], actor, 'scanner_released', {'scannerId': scanner_id})
        return self.get(scanner_id)

    def seen(self, scanner_id, **updates):
        with self.lock, self.db:
            row = self.db.execute('SELECT profile FROM scanners WHERE id=?', (scanner_id,)).fetchone()
            self.db.execute('UPDATE scanners SET profile=? WHERE id=?',
                            (json.dumps({**json.loads(row[0]), **updates}), scanner_id))

    def capabilities(self, scanner_id, state):
        self.seen(scanner_id, capabilities={'identity': state.get('identity'), 'protocol': state.get('protocol'),
            'supportedPidsByEcu': state.get('supported', {}), 'characteristics': state.get('characteristics', [])})

    def link_scan(self, scan):
        evidence = scan.get('scannerEvidence')
        if scan.get('demo') or scan.get('source') == 'Imported scan' or not evidence:
            return
        scanner = self.register(evidence, commit=False)
        scan['scannerId'] = scanner['id']
        scan['scannerIdentifier'] = scanner['identifier']
        scan['softwareVersion'] = SOFTWARE_VERSION
        scan['trigger'] = scan.get('trigger') or (scan.get('automation') or {}).get('trigger') or 'manual'
        linked = self.db.execute('SELECT vehicle_id FROM dossier_scans WHERE scan_id=?', (scan['id'],)).fetchone()
        scan['vehicleId'] = linked[0] if linked else None
        scan['vinVerification'] = {'obd': 'verified', 'manual': 'unverified'}.get(scan.get('vinSource'), 'unavailable')
        self.db.execute('INSERT INTO scanner_scans VALUES(?,?)', (scan['id'], scanner['id']))
        if scan.get('scanJobId'):
            self.db.execute("""UPDATE scan_jobs SET status=?,completed=?,scan_id=?,detail=?
                WHERE id=? AND scanner_id=? AND status='running'""",
                (scan['scanStatus'], scan['createdAt'], scan['id'],
                 json.dumps({'warnings': scan.get('warnings', []),
                             'message': scan.get('error')}), scan['scanJobId'], scanner['id']))

    def job(self, job_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM scan_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise LookupError('Scan job not found.')
        return {**dict(zip(JOB_FIELDS, row)), 'detail': json.loads(row[-1])}

    def jobs(self, *, vehicle_id=None, scanner_id=None):
        column, value = ('scanner_id', scanner_id) if scanner_id is not None else ('vehicle_id', vehicle_id)
        with self.lock, self.db:
            self.expire_confirmations()
            rows = self.db.execute(f'SELECT id FROM scan_jobs WHERE {column}=? ORDER BY created DESC LIMIT 30', (value,)).fetchall()
        return [self.job(row[0]) for row in rows]

    def expire_confirmations(self):
        deadline = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.db.execute("""UPDATE scan_jobs SET status='failed',completed=?,detail=?
            WHERE status='needs_confirmation' AND completed<?""",
            (now(), json.dumps({'message':'VIN confirmation expired. Verify the scanner again.'}), deadline))

    def enqueue(self, scanner_id, vehicle_id, actor, kind, *, tire_size=None):
        special = SPECIAL_FUNCTIONS.get(kind)
        if kind not in ('verify', 'scan') and not special:
            raise ValueError('Choose VIN verification or a diagnostic scan.')
        with self.lock, self.db:
            self.expire_confirmations()
            self.get(scanner_id)
            if vehicle_id:
                self.dossiers.get(vehicle_id)
            detail = {}
            if special:
                assignment = self.special_target(scanner_id, vehicle_id, kind)
                detail = {'assignmentId': assignment['id'], 'expectedVin': assignment['verifiedVin']}
                if special.get('input') == 'tireSize':
                    size = tire_size.strip().upper() if isinstance(tire_size, str) else ''
                    if not re.fullmatch(r'(?:(?:LT|P)?\d{3}/\d{2}R\d{2}|\d{2}(?:\.\d{1,2})?X\d{1,2}(?:\.\d{1,2})?R\d{2})', size):
                        raise ValueError('Enter a tire size such as LT275/70R18 or 35x12.50R20.')
                    detail['requestedTireSize'] = size
            if kind == 'scan':
                target = self.current(scanner_id=scanner_id)
                if not target or target['vehicleId'] != vehicle_id:
                    raise ValueError('Choose the scanner currently assigned to this dossier.')
            if self.db.execute("SELECT count(*) FROM scan_jobs WHERE status='queued'").fetchone()[0] >= 40:
                raise ValueError('The scan queue is full. Retry after a job finishes.')
            job_id = uid()
            try:
                self.db.execute('INSERT INTO scan_jobs VALUES(?,?,?,?,?,\'queued\',?,NULL,NULL,NULL,?)',
                                (job_id, scanner_id, vehicle_id, actor, kind, now(), json.dumps(detail)))
            except sqlite3.IntegrityError as exc:
                raise ValueError('This scanner already has an active job. Check its status in the scanner profile.') from exc
        return self.job(job_id)

    def special_target(self, scanner_id, vehicle_id, kind):
        assignment = self.current(scanner_id=scanner_id)
        if not assignment or assignment['vehicleId'] != vehicle_id or not assignment['verifiedVin']:
            raise ValueError('Choose a scanner with a verified assignment to this vehicle.')
        make = re.sub(r'[^a-z]', '', (assignment['vehicle'].get('make') or '').lower())
        if make not in SPECIAL_FUNCTIONS[kind]['makes']:
            raise ValueError('This function is not listed for the assigned vehicle make.')
        return assignment

    def next_job(self):
        with self.lock:
            row = self.db.execute("SELECT id FROM scan_jobs WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
        return self.job(row[0]) if row else None

    def transition(self, job_id, status, detail=None):
        with self.lock, self.db:
            stamp = now()
            if status == 'running':
                self.db.execute("UPDATE scan_jobs SET status=?,started=? WHERE id=? AND status='queued'", (status, stamp, job_id))
            else:
                self.db.execute('UPDATE scan_jobs SET status=?,completed=?,detail=? WHERE id=?',
                                (status, stamp, json.dumps(detail or {}), job_id))

    def compare(self, vehicle_id, left, right):
        self.dossiers.get(vehicle_id)
        if left == right:
            raise ValueError('Choose two different scans.')
        with self.lock:
            for scan_id in (left, right):
                if not self.db.execute('SELECT 1 FROM dossier_scans WHERE scan_id=? AND vehicle_id=?',
                                       (scan_id, vehicle_id)).fetchone():
                    raise LookupError('Choose two scans from this vehicle dossier.')
        before, after = self.store.get(left), self.store.get(right)
        if before['createdAt'] > after['createdAt']:
            before, after = after, before
        return {'before': before['createdAt'], 'after': after['createdAt'], **compare_codes(before, after)}

    def confirmation(self, job, scanner, detected, selected):
        current = self.current(scanner_id=scanner['id'])
        target = self.current(vehicle_id=selected['id']) if selected else None
        return {'detectedVin': detected, 'suggestedVehicle': selected,
                'currentVehicle': current['vehicle'] if current else None,
                'currentAssignmentId': current['id'] if current else None,
                'targetAssignmentId': target['id'] if target else None,
                'displacedScannerId': target['scannerId'] if target and target['scannerId'] != scanner['id'] else None,
                'message': 'Review the vehicle and confirm the assignment.' if detected else
                           'The vehicle did not report a VIN. An administrator can record an unverified assignment.'}

    def inspect_vin(self, job, scanner, detected):
        with self.lock, self.db:
            current = self.current(scanner_id=scanner['id'])
            requested = self.dossiers.get(job['vehicleId']) if job['vehicleId'] else None
            row = self.db.execute('SELECT id FROM dossiers WHERE vin=?', (detected,)).fetchone() if detected else None
            target = self.dossiers.get(row[0]) if row else None
            other = self.current(vehicle_id=target['id']) if target else None
            same_vehicle = current and current['vehicle']['vin'] == detected
            if (not detected or (current and not same_vehicle)
                    or (requested and requested['vin'] != detected)
                    or (other and other['scannerId'] != scanner['id'])):
                suggested = (target or (requested if requested and not requested['vin'] else None)) if detected else (
                    requested or (current['vehicle'] if current else None))
                detail = self.confirmation(job, scanner, detected, suggested)
                self.db.execute("UPDATE scan_jobs SET status='needs_confirmation',completed=?,detail=? WHERE id=?",
                                (now(), json.dumps(detail), job['id']))
                return None
            if not target:
                target = self.create_detected(detected, job['actor'])
            self.apply_assignment(scanner, target['id'], 'obd_vin', detected, job['actor'])
            self.db.execute('UPDATE scan_jobs SET vehicle_id=? WHERE id=?', (target['id'], job['id']))
            return target

    def create_detected(self, vin, actor):
        vehicle_id = uid()
        self.db.execute('INSERT INTO dossiers VALUES(?,?,?,?)', (vehicle_id, vin, '{}', now()))
        self.dossiers.audit(vehicle_id, actor, 'vehicle_created_from_scanner', {'vin': vin})
        return self.dossiers.get(vehicle_id)

    def confirm(self, job_id, selected_id, actor, *, cancel=False):
        with self.lock, self.db:
            self.expire_confirmations()
            job = self.job(job_id)
            if job['status'] != 'needs_confirmation':
                raise ValueError('This job is no longer awaiting confirmation.')
            if cancel:
                self.db.execute("UPDATE scan_jobs SET status='cancelled',completed=? WHERE id=?", (now(), job_id))
                return self.job(job_id)
            scanner = self.get(job['scannerId'])
            detail = job['detail']
            vin = detail['detectedVin']
            current = self.current(scanner_id=scanner['id'])
            if (current['id'] if current else None) != detail['currentAssignmentId']:
                raise ValueError('The scanner assignment changed. Verify again.')
            target = self.dossiers.get(selected_id) if selected_id else None
            suggested_id = (detail.get('suggestedVehicle') or {}).get('id')
            if vin and selected_id and selected_id != suggested_id:
                raise ValueError('Choose the dossier matching the detected VIN.')
            if not vin and not target:
                raise ValueError('Select a dossier for the unverified assignment.')
            occupied = self.current(vehicle_id=target['id']) if target else None
            if occupied and occupied['id'] != detail['targetAssignmentId'] and occupied['scannerId'] != scanner['id']:
                raise ValueError('That vehicle already has another scanner. Verify with that dossier selected to review the conflict.')
            if suggested_id and vin and not selected_id:
                raise ValueError('Confirm the suggested dossier rather than creating a duplicate.')
            if vin:
                existing = self.db.execute('SELECT id FROM dossiers WHERE vin=?', (vin,)).fetchone()
                if existing and (not target or target['id'] != existing[0]):
                    raise ValueError('A dossier now exists for this VIN. Verify the scanner again.')
                if target and target['vin'] not in (None, vin):
                    raise ValueError('The selected dossier VIN changed. Verify the scanner again.')
                if target and target['vin'] is None:
                    self.db.execute('UPDATE dossiers SET vin=? WHERE id=?', (vin, target['id']))
                    self.dossiers.audit(target['id'], actor, 'vehicle_vin_verified', {'vin': vin})
                if not target:
                    target = self.create_detected(vin, actor)
            assignment = self.apply_assignment(scanner, target['id'], 'confirmed_obd' if vin else 'manual_unverified', vin, actor)
            # A confirmed observation authorizes the assignment. The queued scan still reads VIN again.
            status = 'queued' if vin else 'complete'
            self.db.execute("""UPDATE scan_jobs SET vehicle_id=?,actor=?,type=?,status=?,started=NULL,
                completed=?,detail=? WHERE id=?""",
                (target['id'], actor, 'scan' if vin else 'verify', status, None if vin else now(),
                 json.dumps({'assignment': assignment}), job_id))
        return self.job(job_id)
