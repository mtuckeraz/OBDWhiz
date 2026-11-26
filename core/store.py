import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import tempfile
import uuid

DEFINITION_TEXT_FIELDS = ('make', 'model', 'ecu', 'code', 'description', 'sourceUrl', 'reference')
DEFINITION_COLUMNS = (*DEFINITION_TEXT_FIELDS, 'yearFrom', 'yearTo')
MAX_DEFINITIONS = 5000
DTC_DATA = Path(__file__).resolve().parents[1] / 'data'


def definition_source_batches(data):
    source = data.get('source')
    if not isinstance(source, str) or not source.strip() or len(source) > 1_000_000:
        raise ValueError('Choose a source with up to 1,000,000 characters.')
    source = source.lstrip('\ufeff').strip()
    rows, header = source.splitlines(), ''
    if data.get('format') == 'json':
        parsed = json.loads(source)
        if isinstance(parsed, dict):
            arrays = [name for name, value in parsed.items() if isinstance(value, list)]
            if len(arrays) == 1:
                entries = parsed.pop(arrays[0])
                header = json.dumps(parsed, ensure_ascii=False) if parsed else ''
                parsed = entries
        rows = [json.dumps(item, ensure_ascii=False) for item in (parsed if isinstance(parsed, list) else [parsed])]
    if data.get('format') == 'csv':
        try:
            dialect = csv.Sniffer().sniff(source[:8000], delimiters=',;\t|')
            rows = [json.dumps(row, ensure_ascii=False) for row in csv.reader(io.StringIO(source), dialect, strict=True)]
            if rows:
                # Repeat the first row as context. It remains part of the first
                # batch so headerless exports keep their first definition.
                header = rows[0]
        except csv.Error:
            rows = source.splitlines()
    batches, current, length = [], [], len(header)
    for row in rows:
        if len(row) + len(header) + 2 > 12_000:
            raise ValueError('A source record is too long. Split it into smaller records and retry.')
        if current and (len(current) == 40 or length + len(row) + 1 > 12_000):
            batches.append('\n'.join(([header] if header else []) + current))
            current, length = [], len(header)
        current.append(row)
        length += len(row) + 1
    if current:
        batches.append('\n'.join(([header] if header else []) + current))
    return batches


def read_definition_csv(text):
    rows = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        columns = [column.strip() for column in next(rows, [])]
        missing = set(DEFINITION_COLUMNS) - set(columns)
        if missing:
            raise ValueError('CSV header is missing: ' + ', '.join(sorted(missing)) + '.')
        if len(set(columns)) != len(columns):
            raise ValueError('CSV header contains duplicate column names.')
        definitions = []
        for row in rows:
            if not row:
                continue
            if len(row) != len(columns):
                raise ValueError(f'CSV record ending on line {rows.line_num} has the wrong number of columns.')
            if len(definitions) == MAX_DEFINITIONS:
                raise ValueError(f'A file can contain up to {MAX_DEFINITIONS} definitions.')
            definitions.append(dict(zip(columns, row)))
    except csv.Error as exc:
        raise ValueError(f'Invalid CSV near line {rows.line_num}: {exc}.') from exc
    if not definitions:
        raise ValueError('The CSV file contains no definitions.')
    return {'definitions': definitions}


VEHICLE_FIELDS = ('make', 'model', 'year', 'trim', 'drivetrain', 'engine', 'platform')
UNKNOWN_LABELS = {'', 'unknown', 'unknown make', 'unspecified', 'connected vehicle', 'other'}


def normalize_vin(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValueError('VIN must be a 17-character string.')
    value = value.strip().upper()
    if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', value):
        raise ValueError('VIN must contain 17 letters/digits, excluding I, O and Q.')
    return value


def vehicle_sort_key(vehicle):
    make = str(vehicle.get('make') or '').strip().casefold()
    model = str(vehicle.get('model') or '').strip().casefold()
    return (make in UNKNOWN_LABELS, make, model, str(vehicle.get('year') or ''))


class Store:
    def __init__(self, path):
        self.directory = Path(path).resolve().parent
        self.scan_linker = None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, created TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, scan_id TEXT, time TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS samples_scan ON samples(scan_id, id);
            CREATE TABLE IF NOT EXISTS definitions(id TEXT PRIMARY KEY, data TEXT);
            CREATE INDEX IF NOT EXISTS definitions_code ON definitions(json_extract(data,'$.code'));
            CREATE TABLE IF NOT EXISTS dtc_catalog(code TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(code, data));
            CREATE TABLE IF NOT EXISTS dtc_catalog_version(digest TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS scans_vin_created ON scans(json_extract(data,'$.vin'),created);
            CREATE TABLE IF NOT EXISTS diagnostic_files(
                scan_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                written_at TEXT, error TEXT);
        ''')
        self.db.commit()
        self.repair_diagnostic_files()
        # Jobs cannot outlive the server process; never leave a stale spinner.
        for row in self.db.execute('SELECT id,data FROM scans').fetchall():
            scan = json.loads(row[1])
            if scan.get('research', {}).get('status') == 'running':
                scan['research'] = {'status': 'error', 'error': 'Research was interrupted by a server restart. Run it again when ready.'}
                self.db.execute('UPDATE scans SET data=? WHERE id=?', (json.dumps(scan), row[0]))
        self.db.commit()

    def save_scan(self, scan):
        scan = validate_scan(scan)
        if scan.get('vin') and not scan.get('demo'):
            known = self.vehicle(scan['vin'])
            if known:
                for field in VEHICLE_FIELDS:
                    if str(scan['vehicle'].get(field) or '').strip().casefold() in UNKNOWN_LABELS and known['vehicle'].get(field):
                        scan['vehicle'][field] = known['vehicle'][field]
        scan['id'] = str(uuid.uuid4())
        scan['createdAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        scan.setdefault('research', {'status': 'idle'})
        scan.pop('diagnosticFile', None)
        scan.pop('diagnosticFileError', None)
        if not scan.get('demo'):
            timestamp = dt.datetime.fromisoformat(scan['createdAt'])
            reader = scan.get('reader') if isinstance(scan.get('reader'), dict) else {}
            identity = scan.get('vin') or re.sub(r'[^A-Za-z0-9]', '', str(reader.get('address', 'unidentified')))[:32] or 'unidentified'
            scan['diagnosticFile'] = f'diagnostics/{timestamp:%Y-%m-%d}/{identity}_{timestamp:%Y-%m-%dT%H-%M-%S.%fZ}_{scan["id"]}.json'
        with self.lock, self.db:
            self.db.execute('INSERT INTO scans VALUES(?,?,?)', (scan['id'], scan['createdAt'], json.dumps(scan)))
            if self.scan_linker:
                self.scan_linker(scan)
                self.db.execute('UPDATE scans SET data=? WHERE id=?', (json.dumps(scan), scan['id']))
            if scan.get('diagnosticFile'):
                self.db.execute('INSERT INTO diagnostic_files(scan_id,path,payload) VALUES(?,?,?)',
                                (scan['id'], scan['diagnosticFile'], json.dumps(scan, ensure_ascii=False, indent=2) + '\n'))
        if scan.get('diagnosticFile'):
            self.write_diagnostic_file(scan['id'])
        return self.get(scan['id'])

    def write_diagnostic_file(self, scan_id):
        with self.lock:
            row = self.db.execute('SELECT path,payload FROM diagnostic_files WHERE scan_id=?', (scan_id,)).fetchone()
            if not row:
                return
            temporary = None
            try:
                destination = (self.directory / row[0]).resolve()
                if self.directory not in destination.parents:
                    raise ValueError('Diagnostic file is outside the data directory.')
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = row[1].encode('utf-8')
                if destination.exists():
                    if destination.read_bytes() != payload:
                        raise ValueError('An existing diagnostic file differs from the saved snapshot; it was preserved.')
                else:
                    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.pending-', delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(destination)
                    temporary = None
                with self.db:
                    self.db.execute('UPDATE diagnostic_files SET written_at=?,error=NULL WHERE scan_id=?',
                                    (dt.datetime.now(dt.timezone.utc).isoformat(), scan_id))
            except Exception as exc:
                with self.db:
                    self.db.execute('UPDATE diagnostic_files SET error=? WHERE scan_id=?', (str(exc), scan_id))
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)

    def repair_diagnostic_files(self):
        with self.lock:
            pending = [r[0] for r in self.db.execute('SELECT scan_id FROM diagnostic_files WHERE written_at IS NULL')]
        for scan_id in pending:
            self.write_diagnostic_file(scan_id)

    def diagnostic_file(self, scan_id):
        with self.lock:
            row = self.db.execute('SELECT path,written_at,error FROM diagnostic_files WHERE scan_id=?', (scan_id,)).fetchone()
        if not row or not row[1]:
            raise LookupError('No completed diagnostic file is available for this scan.')
        path = (self.directory / row[0]).resolve()
        if self.directory not in path.parents or not path.is_file():
            raise LookupError('Diagnostic file not found.')
        return path

    def get(self, scan_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM scans WHERE id=?', (scan_id,)).fetchone()
        if not row:
            raise LookupError('Scan not found.')
        scan = json.loads(row[0])
        with self.lock:
            artifact = self.db.execute('SELECT error FROM diagnostic_files WHERE scan_id=?', (scan_id,)).fetchone()
        if artifact and artifact[0]:
            scan['diagnosticFileError'] = artifact[0]
        return scan

    def update(self, scan):
        with self.lock, self.db:
            self.db.execute('UPDATE scans SET data=? WHERE id=?', (json.dumps(scan), scan['id']))

    def _summaries(self, recent_per_vehicle=None):
        with self.lock:
            source = 'scans'
            prefix = ''
            suffix = ''
            parameters = ()
            if recent_per_vehicle:
                prefix = '''WITH recent AS (SELECT *,row_number() OVER (
                    PARTITION BY coalesce(json_extract(data,'$.vin'),id) ORDER BY created DESC,id DESC) AS position,
                    count(*) OVER (PARTITION BY coalesce(json_extract(data,'$.vin'),id)) AS total
                    FROM scans WHERE coalesce(json_extract(data,'$.scanStatus'),'')!='failed'
                    AND (json_extract(data,'$.vin') IS NOT NULL OR json_extract(data,'$.automation') IS NULL)) '''
                source = 'recent'
                suffix = ' WHERE position<=?'
                parameters = (recent_per_vehicle,)
            rows = self.db.execute(prefix + '''SELECT id,created,json_extract(data,'$.vehicle'),
                json_extract(data,'$.vin'),json_extract(data,'$.demo'),json_extract(data,'$.source'),
                json_extract(data,'$.codes'),json_extract(data,'$.scope'),json_extract(data,'$.research'),
                json_extract(data,'$.scanStatus'),json_extract(data,'$.diagnosticFile'),json_extract(data,'$.automation'),
                ''' + ('total' if recent_per_vehicle else '1') + f' FROM {source}{suffix} ORDER BY created DESC,id DESC', parameters).fetchall()
        return [{'id': r[0], 'createdAt': r[1], 'vehicle': json.loads(r[2]), 'vin': r[3], 'demo': bool(r[4]),
                 'source': r[5], 'codes': json.loads(r[6]), 'scope': r[7], 'research': json.loads(r[8]) if r[8] else None,
                 'scanStatus': r[9], 'diagnosticFile': r[10], 'automation': json.loads(r[11]) if r[11] else None,
                 'totalScanCount': r[12]}
                for r in rows]

    def list(self):
        # Stable sort keeps newer sessions first within the same vehicle.
        rows = self._summaries()
        rows.sort(key=lambda s: (*vehicle_sort_key(s['vehicle']), s.get('vin') or ''))
        return rows

    def vehicles(self):
        grouped = {}
        for scan in self._summaries(recent_per_vehicle=50):
            if scan.get('scanStatus') == 'failed':
                continue
            # Never merge unidentified cars just because their model names match.
            key = ('demo:' if scan['demo'] else 'vin:') + scan['vin'] if scan.get('vin') else 'scan:' + scan['id']
            if key not in grouped:
                grouped[key] = {'key': key, 'vin': scan.get('vin'), 'vehicle': dict(scan['vehicle']),
                                'demo': scan['demo'], 'scans': [], 'totalScanCount': scan['totalScanCount'],
                                'latestScanId': scan['id'], 'lastScanAt': scan['createdAt']}
            group = grouped[key]
            for field in VEHICLE_FIELDS:
                if str(group['vehicle'].get(field) or '').strip().casefold() in UNKNOWN_LABELS and scan['vehicle'].get(field):
                    group['vehicle'][field] = scan['vehicle'][field]
            group['scans'].append(scan)
        return sorted(grouped.values(), key=lambda g: (*vehicle_sort_key(g['vehicle']), g.get('vin') or '', g['key']))

    def vehicle(self, vin):
        vin = normalize_vin(vin)
        return next((g for g in self.vehicles() if g['vin'] == vin and not g['demo']), None)

    def update_vehicle(self, vin, vehicle):
        vin = normalize_vin(vin)
        if not vin:
            raise ValueError('A captured or manually entered VIN is required to save vehicle details.')
        checked = validate_scan({'vin': vin, 'vehicle': dict(vehicle), 'codes': []})
        identity = {k: checked['vehicle'][k] for k in VEHICLE_FIELDS if k in checked['vehicle']}
        with self.lock, self.db:
            rows = self.db.execute("SELECT id,data FROM scans WHERE json_extract(data,'$.vin')=? AND coalesce(json_extract(data,'$.demo'),0)=0", (vin,)).fetchall()
            if not rows:
                raise LookupError('No saved scans match this VIN. Run or import a scan first.')
            scans = [json.loads(row[1]) for row in rows]
            if any(s.get('research', {}).get('status') == 'running' for s in scans):
                raise ValueError('Wait for active research to finish before updating this vehicle.')
            for scan in scans:
                changed = any(scan['vehicle'].get(k) != value for k, value in identity.items())
                scan['vehicle'].update(identity)
                if changed and scan.get('research', {}).get('status') == 'complete':
                    scan.setdefault('researchHistory', []).append(scan['research'])
                    scan['research'] = {'status': 'idle', 'reason': 'Vehicle details changed. Research again with the corrected context.'}
                self.db.execute('UPDATE scans SET data=? WHERE id=?', (json.dumps(scan), scan['id']))
        return self.vehicle(vin)

    def samples(self, scan_id, values=None):
        if values is not None:
            if not values:
                return
            with self.lock, self.db:
                self.db.execute('INSERT INTO samples(scan_id,time,data) VALUES(?,?,?)', (scan_id, values[0]['time'], json.dumps(values)))
                # Bound storage to 10000 cycles per scan; raw scan evidence is retained.
                self.db.execute('DELETE FROM samples WHERE scan_id=? AND id NOT IN (SELECT id FROM samples WHERE scan_id=? ORDER BY id DESC LIMIT 10000)', (scan_id, scan_id))
            return
        with self.lock:
            rows = self.db.execute('SELECT data FROM samples WHERE scan_id=? ORDER BY id DESC LIMIT 200', (scan_id,)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def definitions(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM definitions')]

    def load_dtc_catalog(self):
        generic = (DTC_DATA / 'dtc-generic.csv').read_bytes()
        oem = (DTC_DATA / 'dtc-oem.csv').read_bytes()
        digest = hashlib.sha256(generic + b'\0' + oem).hexdigest()
        with self.lock, self.db:
            if self.db.execute('SELECT digest FROM dtc_catalog_version').fetchone() == (digest,):
                return
            generic_rows = csv.DictReader(io.StringIO(generic.decode('utf-8'), newline=''))
            records = [{**row, 'reference': 'OBDex community reference (CC0)',
                        'sourceUrl': 'https://github.com/foerbsnavi/obdex', 'origin': 'Generic OBD-II'}
                       for row in generic_rows]
            for row in csv.DictReader(io.StringIO(oem.decode('utf-8'), newline='')):
                row.update(yearFrom=int(row['yearFrom']), yearTo=int(row['yearTo']),
                           origin='Manufacturer bulletin reference')
                records.append(row)
            self.db.execute('DELETE FROM dtc_catalog')
            self.db.executemany('INSERT INTO dtc_catalog VALUES(?,?)',
                                ((record['code'], json.dumps(record)) for record in records))
            self.db.execute('DELETE FROM dtc_catalog_version')
            self.db.execute('INSERT INTO dtc_catalog_version VALUES(?)', (digest,))

    def definition_page(self, query='', page=0):
        if not 0 <= page <= 100_000 or len(query) > 100:
            raise ValueError('Invalid catalog search or page.')
        size = 50
        # The search treats percent signs and underscores as literal characters.
        clauses = []
        for word in query.split():
            clauses.append((word.casefold(),) * 4)
        where = ' AND '.join(
            "(instr(lower(code),?) OR instr(lower(coalesce(json_extract(data,'$.make'),'')),?) "
            "OR instr(lower(coalesce(json_extract(data,'$.model'),'')),?) "
            "OR instr(lower(json_extract(data,'$.description')),?))" for _ in clauses)
        catalog = ("FROM (SELECT code, data FROM dtc_catalog UNION ALL "
                   "SELECT json_extract(data,'$.code') AS code, data FROM definitions)")
        selection = catalog + (' WHERE ' + where if where else '')
        args = tuple(word for clause in clauses for word in clause)
        with self.lock:
            total = self.db.execute('SELECT count(*) ' + selection, args).fetchone()[0]
            rows = self.db.execute('SELECT data ' + selection + " ORDER BY json_extract(data,'$.make') IS NULL, code, data LIMIT ? OFFSET ?",
                                   (*args, size, page * size)).fetchall()
        return {'records': [json.loads(row[0]) for row in rows], 'total': total, 'pageSize': size}

    @staticmethod
    def validate_definitions(data, *, source_import=False):
        if not isinstance(data, dict) or not isinstance(data.get('definitions'), list) or len(data['definitions']) > MAX_DEFINITIONS:
            raise ValueError('Expected an object with a definitions array (up to 5000 entries).')
        records = []
        for number, item in enumerate(data['definitions'], start=1):
            if not isinstance(item, dict):
                raise ValueError(f'Definition {number} must be an object.')
            required = ('make', 'model', 'code', 'description') if source_import else DEFINITION_TEXT_FIELDS
            record = {}
            for key in DEFINITION_TEXT_FIELDS:
                value = item.get(key, '')
                if not isinstance(value, str) or len(value) > 3000 or (key in required and not value.strip()):
                    raise ValueError(f'Definition {number} needs a valid {key} of up to 3000 characters.')
                record[key] = value.strip()
            if record['sourceUrl'] and not re.match(r'^https?://[^\s]+$', record['sourceUrl']):
                raise ValueError(f'Definition {number} sourceUrl must be an HTTP(S) reference.')
            if source_import and not re.fullmatch(r'[A-Za-z0-9_\- ]{2,32}', record['code']):
                raise ValueError(f'Definition {number} needs a valid code identifier.')
            try:
                years = [str(item.get(field, '')).strip() for field in ('yearFrom', 'yearTo')]
                if not all(re.fullmatch(r'[0-9]{4}', year) for year in years):
                    raise ValueError()
                start, end = map(int, years)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f'Definition {number} needs whole-number years in yearFrom and yearTo.') from exc
            if not 1980 <= start <= end <= 2100:
                raise ValueError(f'Definition {number} needs a valid yearFrom/yearTo range (1980–2100).')
            origin = 'AI-assisted source import · unverified' if source_import else 'user-supplied; not independently verified'
            record.update(yearFrom=start, yearTo=end, origin=origin)
            record['code'] = record['code'].upper()
            records.append(record)
        return records

    def import_definitions(self, data, *, source_import=False):
        records = self.validate_definitions(data, source_import=source_import)
        with self.lock, self.db:
            for record in records:
                key = '|'.join(str(record[k]).casefold() for k in ('make', 'model', 'yearFrom', 'yearTo', 'ecu', 'code'))
                if source_import:
                    key = 'source:' + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
                self.db.execute('INSERT OR REPLACE INTO definitions VALUES(?,?)', (key, json.dumps(record)))
        return {'count': len(records)}

    def matches(self, scan):
        vehicle = scan.get('vehicle', {})
        codes = {c['code'].upper() for c in scan['codes']}
        if not codes:
            return []
        placeholders = ','.join('?' for _ in codes)
        with self.lock:
            imported = self.db.execute(f"SELECT data FROM definitions WHERE json_extract(data,'$.code') IN ({placeholders})",
                                       tuple(codes)).fetchall()
            catalog = self.db.execute(f'SELECT data FROM dtc_catalog WHERE code IN ({placeholders})', tuple(codes)).fetchall()
        result = []
        for row in imported + catalog:
            d = json.loads(row[0])
            if d.get('origin') == 'Generic OBD-II':
                result.append(d)
                continue
            if (str(vehicle.get('make', '')).casefold() == d['make'].casefold()
                    and str(vehicle.get('model', '')).casefold() == d['model'].casefold()
                    and d['yearFrom'] <= int(vehicle.get('year') or 0) <= d['yearTo']):
                if not d.get('ecu') or any(c['code'].upper() == d['code'] and
                                           (c.get('ecu') or '').casefold() == d['ecu'].casefold() for c in scan['codes']):
                    result.append(d)
        return sorted(result, key=lambda d: d.get('origin') == 'Generic OBD-II')


def validate_scan(data):
    if not isinstance(data, dict) or not isinstance(data.get('codes'), list) or len(data['codes']) > 500:
        raise ValueError('Expected a scan object containing a codes array (up to 500 codes).')
    if not isinstance(data.get('vehicle'), dict):
        raise ValueError('Provide vehicle make, model, and year in a vehicle object.')
    vehicle = data['vehicle']
    for key in ('make', 'model', 'trim', 'drivetrain', 'engine', 'platform', 'symptoms'):
        if key in vehicle and (not isinstance(vehicle[key], str) or len(vehicle[key]) > 3000):
            raise ValueError(f'Invalid vehicle {key}.')
    if 'vin' in data:
        data['vin'] = normalize_vin(data['vin'])
    if 'vin' in vehicle:
        nested_vin = normalize_vin(vehicle.pop('vin'))
        if data.get('vin') and nested_vin and data['vin'] != nested_vin:
            raise ValueError('The scan VIN and vehicle VIN do not match.')
        data['vin'] = data.get('vin') or nested_vin
    if vehicle.get('year'):
        year = int(vehicle['year'])
        if not 1980 <= year <= 2100:
            raise ValueError('Vehicle year must be 1980–2100.')
        vehicle['year'] = year
    for field in ('readings', 'warnings', 'trace', 'monitors', 'freezeFrame'):
        value = data.setdefault(field, [])
        if not isinstance(value, list) or len(value) > 5000:
            raise ValueError(f'{field} must be an array with up to 5000 entries.')
    if any(not isinstance(w, str) or len(w) > 5000 for w in data['warnings']):
        raise ValueError('Every scan warning must be text.')
    if any(not isinstance(m, dict) for m in data['monitors']):
        raise ValueError('Monitor entries must be objects.')
    for value in data['readings']:
        if not isinstance(value, dict) or not isinstance(value.get('value'), (int, float)) or not math.isfinite(value['value']):
            raise ValueError('Readings must be objects with finite numeric values.')
        for key in ('label', 'unit', 'ecu', 'pid'):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > 1000):
                raise ValueError(f'Invalid reading {key}.')
    if 'demo' in data and not isinstance(data['demo'], bool):
        raise ValueError('demo must be true or false.')
    for key in ('scope', 'source'):
        if key in data and (not isinstance(data[key], str) or len(data[key]) > 6000):
            raise ValueError(f'Invalid {key}.')
    report = data.get('research')
    if report is not None:
        if not isinstance(report, dict) or not isinstance(report.get('status'), str):
            raise ValueError('research must be an object with a status string.')
        for key in ('text', 'error'):
            if key in report and not isinstance(report[key], str):
                raise ValueError(f'research.{key} must be text.')
        for key in ('citations', 'annotations'):
            if key in report and (not isinstance(report[key], list) or any(not isinstance(a, dict) for a in report[key])):
                raise ValueError(f'research.{key} must be an array of objects.')
    data.pop('definitionMatches', None)
    for code in data['codes']:
        if not isinstance(code, dict) or not isinstance(code.get('code'), str) or not re.fullmatch(r'[A-Za-z0-9_\- ]{2,32}', code['code']):
            raise ValueError('Every code needs a code string; raw OEM/UDS codes are accepted.')
        for key in ('ecu', 'status', 'definition'):
            if code.get(key) is not None and (not isinstance(code[key], str) or len(code[key]) > 1000):
                raise ValueError(f'Invalid code {key}.')
        code['code'] = code['code'].upper()
    return data
