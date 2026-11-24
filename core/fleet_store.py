"""Persistent ten-reader fleet, presence visits and append-only diagnostic events."""
import datetime as dt
import json
import re
import uuid

from .store import normalize_vin, validate_scan


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


DEFAULTS = {'enabled': True, 'scanIntervalSeconds': 5, 'retrySeconds': 30, 'presenceTimeoutSeconds': 90}


class FleetStore:
    capacity = 10

    def __init__(self, store):
        self.store = store
        self.db, self.lock = store.db, store.lock
        with self.lock, self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS fleet_readers(address TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fleet_settings(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fleet_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    created TEXT NOT NULL,address TEXT,type TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fleet_faults(identity TEXT,code_key TEXT,data TEXT NOT NULL,
                    PRIMARY KEY(identity,code_key));
            ''')

    def settings(self):
        with self.lock:
            row = self.db.execute('SELECT data FROM fleet_settings WHERE id=1').fetchone()
        return {**DEFAULTS, **(json.loads(row[0]) if row else {})}

    def configure(self, changes):
        settings = self.settings()
        if 'enabled' in changes:
            if not isinstance(changes['enabled'], bool):
                raise ValueError('Fleet enabled must be true or false.')
            settings['enabled'] = changes['enabled']
        for key, low, high in (('scanIntervalSeconds', 5, 3600), ('retrySeconds', 15, 600), ('presenceTimeoutSeconds', 30, 600)):
            if key in changes:
                value = changes[key]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise ValueError(f'{key} must be an integer from {low} to {high}.')
                settings[key] = value
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO fleet_settings VALUES(1,?)', (json.dumps(settings),))
        return settings

    def readers(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM fleet_readers ORDER BY address')]

    def reader(self, address):
        with self.lock:
            row = self.db.execute('SELECT data FROM fleet_readers WHERE address=?', (address,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, reader):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO fleet_readers VALUES(?,?)', (reader['address'], json.dumps(reader)))

    def register(self, spec):
        address = str(spec.get('address', '')).upper()
        if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address):
            raise ValueError('Choose a Bluetooth receiver address.')
        controller = str(spec.get('controller', ''))
        if not re.fullmatch(r'/org/bluez/hci\d+', controller):
            raise ValueError('Choose the Pi Bluetooth radio for this reader.')
        for key in ('name', 'label'):
            if key in spec and (not isinstance(spec[key], str) or len(spec[key]) > 120):
                raise ValueError(f'Reader {key} must be text up to 120 characters.')
        vehicle = spec.get('vehicle', {})
        if not isinstance(vehicle, dict):
            raise ValueError('Vehicle context must be an object.')
        checked = validate_scan({'vehicle': dict(vehicle), 'codes': []})
        vin = normalize_vin(spec.get('vin'))
        with self.lock, self.db:
            previous = self.reader(address)
            if not previous and len(self.readers()) >= self.capacity:
                raise ValueError('The fleet supports up to 10 readers. Remove a reader before adding another.')
            reader = previous or {'address': address, 'createdAt': utcnow(), 'enabled': True, 'presence': 'unknown',
                'visitId': None, 'lastSeenAt': None, 'lastScanAt': None, 'lastScanId': None,
                'lastAttemptAt': None, 'lastError': None, 'lastScanStatus': 'waiting'}
            if previous and 'vin' in spec and vin != previous.get('vin'):
                reader.update(lastScanAt=None, lastScanId=None, lastAttemptAt=None, lastError=None, lastScanStatus='waiting')
                self.event(address, 'vehicle_assignment_changed', {'previousVin': previous.get('vin'), 'vin': vin})
            reader.update(controller=controller, name=spec.get('name', previous.get('name') if previous else 'Carista'),
                          label=spec.get('label') or reader.get('label') or spec.get('name') or address)
            if spec.get('controllerAddress'):
                if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', str(spec['controllerAddress']).upper()):
                    raise ValueError('Invalid Pi radio address.')
                reader['controllerAddress'] = spec['controllerAddress'].upper()
            if 'vehicle' in spec or not previous:
                reader['vehicle'] = checked['vehicle']
            if 'vin' in spec or not previous:
                reader['vin'] = vin
            if 'enabled' in spec:
                if not isinstance(spec['enabled'], bool):
                    raise ValueError('Reader enabled must be true or false.')
                reader['enabled'] = spec['enabled']
            self.put(reader)
            if not previous:
                self.event(address, 'reader_registered', {'label': reader['label'], 'controller': controller})
        return reader

    def remove(self, address):
        with self.lock, self.db:
            reader = self.reader(address)
            if not reader:
                raise LookupError('Fleet reader not found.')
            self.event(address, 'reader_removed', {'label': reader['label'], 'vin': reader.get('vin')})
            self.db.execute('DELETE FROM fleet_readers WHERE address=?', (address,))
        return {'removed': True}

    def event(self, address, kind, details=None, timestamp=None):
        created = timestamp or utcnow()
        with self.lock, self.db:
            cursor = self.db.execute('INSERT INTO fleet_events(created,address,type,data) VALUES(?,?,?,?)',
                                     (created, address, kind, json.dumps(details or {})))
        return {'seq': cursor.lastrowid, 'createdAt': created, 'address': address, 'type': kind, **(details or {})}

    def events(self, after=None, limit=100):
        if (after is not None and not 0 <= after <= 2**63 - 1) or not 1 <= limit <= 500:
            raise ValueError('Use a nonnegative event cursor and a limit from 1 to 500.')
        with self.lock:
            if after is None:
                rows = self.db.execute('SELECT seq,created,address,type,data FROM fleet_events ORDER BY seq DESC LIMIT ?', (limit,)).fetchall()[::-1]
            else:
                rows = self.db.execute('SELECT seq,created,address,type,data FROM fleet_events WHERE seq>? ORDER BY seq LIMIT ?', (after, limit)).fetchall()
        events = [{'seq': r[0], 'createdAt': r[1], 'address': r[2], 'type': r[3], **json.loads(r[4])} for r in rows]
        return {'events': events, 'nextCursor': events[-1]['seq'] if events else after or 0}

    def observe(self, address, timestamp=None):
        timestamp = timestamp or utcnow()
        reader = self.reader(address)
        if not reader or not reader['enabled']:
            return None
        if reader['presence'] != 'present':
            previous = reader['presence']
            reader.update(presence='present', visitId=str(uuid.uuid4()), lastAttemptAt=None)
            self.event(address, 'return_observed' if previous == 'absent' else 'presence_observed',
                       {'visitId': reader['visitId'], 'observation': 'Bluetooth advertisement, not a GPS location'}, timestamp)
        reader['lastSeenAt'] = timestamp
        self.put(reader)
        return reader

    def mark_absent(self, reader, timestamp=None):
        timestamp = timestamp or utcnow()
        reader.update(presence='absent')
        self.put(reader)
        return self.event(reader['address'], 'departure_inferred', {
            'visitId': reader['visitId'], 'lastSeenAt': reader['lastSeenAt'],
            'lastScanId': reader.get('lastScanId'), 'lastScanAt': reader.get('lastScanAt'),
            'observation': 'Bluetooth signal absent; departure, sleep, interference and unplugging cannot be distinguished',
            'freshScanAtDeparture': False}, timestamp)

    def scan_started(self, address, trigger, timestamp=None):
        reader = self.reader(address)
        if not reader:
            return
        reader.update(lastAttemptAt=timestamp or utcnow(), lastScanStatus='scanning', lastError=None)
        self.put(reader)
        self.event(address, 'scan_started', {'visitId': reader['visitId'], 'trigger': trigger}, reader['lastAttemptAt'])

    def scan_failed(self, address, error, trigger):
        reader = self.reader(address)
        if not reader:
            return
        reader.update(lastScanStatus='error', lastError=str(error))
        self.put(reader)
        self.event(address, 'scan_failed', {'visitId': reader['visitId'], 'trigger': trigger, 'error': str(error)})

    def scan_saved(self, address, scan, trigger):
        reader = self.reader(address)
        if not reader:
            return
        if not reader.get('vin') and scan.get('vin') and scan.get('vinSource') == 'obd':
            reader['vin'] = scan['vin']
        reader['vehicle'] = dict(scan['vehicle'])
        reader.update(lastScanAt=scan['createdAt'], lastScanId=scan['id'],
                      lastCodeCount=len(scan['codes']), lastCodeCoverage=scan.get('codeCoverage', {}),
                      lastScanStatus='complete' if all(scan.get('codeCoverage', {}).get(m) for m in ('03', '07', '0A')) else 'partial',
                      lastError=scan.get('diagnosticFileError'))
        self.put(reader)
        details = {'visitId': reader['visitId'], 'trigger': trigger, 'scanId': scan['id'], 'vin': scan.get('vin'),
                   'codeCount': len(scan['codes']), 'codeCoverage': scan.get('codeCoverage', {}),
                   'diagnosticFile': scan.get('diagnosticFile'), 'fileError': scan.get('diagnosticFileError')}
        self.event(address, 'scan_saved', details, scan['createdAt'])
        self.track_codes(address, scan)

    def track_codes(self, address, scan):
        identity = scan.get('vin') or 'reader:' + address
        timestamp = scan['createdAt']
        mode_for_status = {'stored': '03', 'pending': '07', 'permanent': '0A'}
        with self.lock, self.db:
            known = {r[0]: json.loads(r[1]) for r in self.db.execute('SELECT code_key,data FROM fleet_faults WHERE identity=?', (identity,))}
            current = {'|'.join(str(code.get(k) or '') for k in ('ecu', 'code', 'status')): code for code in scan['codes']}
            for key, code in current.items():
                observed = scan.get('codeReadAt', {}).get(mode_for_status.get(code.get('status')), timestamp)
                previous = known.get(key)
                if not previous or not previous.get('active'):
                    item = {**code, 'active': True, 'firstObservedAt': observed, 'lastObservedAt': observed, 'scanId': scan['id']}
                    self.event(address, 'code_first_observed', {'vin': scan.get('vin'), **item}, observed)
                else:
                    item = {**previous, 'lastObservedAt': observed, 'scanId': scan['id']}
                self.db.execute('INSERT OR REPLACE INTO fleet_faults VALUES(?,?,?)', (identity, key, json.dumps(item)))
            for key, code in known.items():
                mode = mode_for_status.get(code.get('status'))
                if code.get('active') and key not in current and code.get('ecu') in scan.get('codeCoverage', {}).get(mode, []):
                    code.update(active=False, noLongerObservedAt=timestamp, scanId=scan['id'])
                    self.db.execute('UPDATE fleet_faults SET data=? WHERE identity=? AND code_key=?', (json.dumps(code), identity, key))
                    self.event(address, 'code_no_longer_reported', {'vin': scan.get('vin'), **code}, timestamp)
