"""Persistent vehicle records and evidence metadata."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from uuid import uuid4

from .store import normalize_vin

FIELDS = ('make', 'model', 'year', 'trim', 'engine', 'color', 'plate', 'jurisdiction')
PROFILE_FIELDS = (('garagingLocation', 'Garaging location', 250), ('profileNotes', 'Notes', 4000))
ROLES = ('admin', 'user')
INITIAL_PASSWORD = '123456'
SCHEMA = """
CREATE TABLE dossier_owners(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE dossiers(id TEXT PRIMARY KEY, vin TEXT UNIQUE, details TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE dossier_ownership(id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES dossiers(id),
    owner_id TEXT NOT NULL REFERENCES dossier_owners(id), started TEXT NOT NULL, ended TEXT);
CREATE UNIQUE INDEX current_dossier_owner ON dossier_ownership(vehicle_id) WHERE ended IS NULL;
CREATE TABLE dossier_scans(scan_id TEXT PRIMARY KEY REFERENCES scans(id),
    vehicle_id TEXT NOT NULL REFERENCES dossiers(id));
CREATE INDEX dossier_scan_vehicle ON dossier_scans(vehicle_id);
CREATE TABLE dossier_visits(id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES dossiers(id),
    label TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(id,vehicle_id));
CREATE TABLE dossier_users(id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL, role TEXT NOT NULL, salt TEXT NOT NULL, password TEXT NOT NULL);
CREATE TABLE dossier_notes(id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES dossiers(id),
    body TEXT NOT NULL, author TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE dossier_media(id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES dossiers(id),
    visit_id TEXT, kind TEXT NOT NULL, original TEXT NOT NULL, metadata TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, uploaded TEXT NOT NULL, uploader TEXT NOT NULL,
    FOREIGN KEY(visit_id,vehicle_id) REFERENCES dossier_visits(id,vehicle_id));
CREATE INDEX dossier_media_vehicle ON dossier_media(vehicle_id, uploaded);
CREATE INDEX dossier_media_hash ON dossier_media(sha256);
CREATE TABLE dossier_audit(id TEXT PRIMARY KEY, vehicle_id TEXT REFERENCES dossiers(id),
    actor TEXT NOT NULL, action TEXT NOT NULL, created TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE dossier_settings(id INTEGER PRIMARY KEY CHECK(id=1), flag_missing_gps INTEGER NOT NULL);
INSERT INTO dossier_settings VALUES(1,1);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid4())


def clean(value, limit, label):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{label} must be text up to {limit} characters.')
    return value.strip()


def normalized(value):
    return ''.join(c for c in value.casefold() if c.isalnum())


class Dossiers:
    def __init__(self, store):
        self.db, self.lock = store.db, store.lock
        self.migrate()
        self.migrate_accounts()

    def migrate(self):
        with self.lock, self.db:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('CREATE TABLE IF NOT EXISTS dossier_migrations(version INTEGER PRIMARY KEY)')
            if self.db.execute('SELECT 1 FROM dossier_migrations WHERE version=1').fetchone():
                return
            self.db.execute('BEGIN IMMEDIATE')
            for statement in SCHEMA.strip().split(';'):
                if statement.strip():
                    self.db.execute(statement)
            rows = self.db.execute("""
                SELECT id,created,data FROM scans WHERE json_extract(data,'$.vin') IS NOT NULL
                AND coalesce(json_extract(data,'$.demo'),0)=0
                AND coalesce(json_extract(data,'$.scanStatus'),'') != 'failed'
                ORDER BY created,id
            """)
            for scan_id, created, data in rows:
                self.link_scan({**json.loads(data), 'id': scan_id, 'createdAt': created})
            self.db.execute('INSERT INTO dossier_migrations VALUES(1)')

    def migrate_accounts(self):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM dossier_migrations WHERE version=2').fetchone():
                return
            self.db.execute('ALTER TABLE dossier_users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0')
            self.db.execute("UPDATE dossier_users SET role='user' WHERE role IN ('manager','technician')")
            for username, name in (('admin', 'Administrator'), ('user', 'User')):
                if not self.db.execute('SELECT 1 FROM dossier_users WHERE username=?', (username,)).fetchone():
                    self.seed_account(username, name, username)
            if not self.db.execute("SELECT 1 FROM dossier_users WHERE role='admin'").fetchone():
                username = 'obdwhiz-admin'
                while self.db.execute('SELECT 1 FROM dossier_users WHERE username=?', (username,)).fetchone():
                    username = 'obdwhiz-' + uid()[:8]
                self.seed_account(username, 'Administrator', 'admin')
            self.db.execute('INSERT INTO dossier_migrations VALUES(2)')

    def seed_account(self, username, name, role):
        salt = secrets.token_hex(16)
        self.db.execute('INSERT INTO dossier_users VALUES(?,?,?,?,?,?,1)',
                        (uid(), username, name, role, salt, self.password_hash(INITIAL_PASSWORD, salt)))

    def link_scan(self, scan):
        if scan.get('demo') or scan.get('scanStatus') == 'failed' or not scan.get('vin'):
            return
        vin = normalize_vin(scan['vin'])
        vehicle = self.db.execute('SELECT id,details FROM dossiers WHERE vin=?', (vin,)).fetchone()
        details = {field: scan['vehicle'][field] for field in FIELDS if scan['vehicle'].get(field)}
        if vehicle:
            vehicle_id, saved = vehicle
            current = json.loads(saved)
            for field, value in details.items():
                if not current.get(field) or str(current[field]).casefold() in ('unknown', 'unspecified', 'connected vehicle'):
                    current[field] = value
            self.db.execute('UPDATE dossiers SET details=? WHERE id=?', (json.dumps(current), vehicle_id))
        else:
            vehicle_id = uid()
            self.db.execute('INSERT INTO dossiers VALUES(?,?,?,?)', (vehicle_id, vin, json.dumps(details), scan['createdAt']))
        self.db.execute('INSERT OR IGNORE INTO dossier_scans VALUES(?,?)', (scan['id'], vehicle_id))

    def audit(self, vehicle_id, actor, action, detail):
        self.db.execute('INSERT INTO dossier_audit VALUES(?,?,?,?,?,?)',
                        (uid(), vehicle_id, actor, action, now(), json.dumps(detail)))

    def actor_names(self):
        with self.lock:
            return {'installation': 'Installation administrator', **dict(self.db.execute(
                'SELECT id,name FROM dossier_users'))}

    def get(self, vehicle_id):
        with self.lock:
            row = self.db.execute("""SELECT v.id,v.vin,v.details,v.created,o.name
                FROM dossiers v LEFT JOIN dossier_ownership h ON h.vehicle_id=v.id AND h.ended IS NULL
                LEFT JOIN dossier_owners o ON o.id=h.owner_id WHERE v.id=?""", (vehicle_id,)).fetchone()
        if not row:
            raise LookupError('Vehicle dossier not found.')
        return self.summary(row)

    @staticmethod
    def summary(row):
        return {'id': row[0], 'vin': row[1], **json.loads(row[2]), 'createdAt': row[3], 'owner': row[4]}

    def search(self, query):
        query = clean(query, 200, 'Search')
        terms = [normalized(word) for word in query.split() if normalized(word)]
        with self.lock:
            rows = self.db.execute("""SELECT v.id,v.vin,v.details,v.created,o.name,
                (SELECT m.id FROM dossier_media m WHERE m.vehicle_id=v.id AND m.kind='photo'
                 ORDER BY m.uploaded DESC LIMIT 1)
                FROM dossiers v LEFT JOIN dossier_ownership h ON h.vehicle_id=v.id AND h.ended IS NULL
                LEFT JOIN dossier_owners o ON o.id=h.owner_id ORDER BY v.created DESC,v.id""")
            results = []
            for row in rows:
                vehicle = self.summary(row)
                searchable = [normalized(str(value or '')) for key, value in vehicle.items()
                              if key not in ('id', 'createdAt')]
                combined = normalized(query)
                if all(any(term in field for field in searchable) for term in terms) or (
                        combined and combined in normalized(vehicle.get('plate') or '')):
                    results.append({**vehicle, 'thumbnail': row[5]})
                    if len(results) == 100:
                        break
        return results

    def save(self, data, actor, vehicle_id=None):
        with self.lock, self.db:
            before = self.get(vehicle_id) if vehicle_id else {}
            details = {key: before[key] for key in FIELDS if key in before}
            for key in FIELDS:
                if key in data and key != 'year':
                    details[key] = clean(data[key], 120, key)
            for key, label, limit in PROFILE_FIELDS:
                if key in data:
                    details[key] = clean(data[key], limit, label)
                elif key in before:
                    details[key] = before[key]
            if 'year' in data:
                year = data['year']
                if year is not None and (type(year) is not int or not 1886 <= year <= 2100):
                    raise ValueError('Enter a model year between 1886 and 2100.')
                details['year'] = year
            vin = normalize_vin(data.get('vin', before.get('vin')))
            if not vin and not any(details.get(k) for k in ('make', 'model', 'plate')):
                raise ValueError('Enter a VIN, make, model or plate to create a dossier.')
            owner = clean(data.get('owner', before.get('owner') or ''), 120, 'Owner')
            if vin:
                conflict = self.db.execute('SELECT id FROM dossiers WHERE vin=?', (vin,)).fetchone()
                if conflict and conflict[0] != vehicle_id:
                    raise ValueError('This VIN already has a dossier. Open it from search.')
            timestamp = now()
            if vehicle_id:
                if before.get('vin') and before['vin'] != vin and self.db.execute(
                        'SELECT 1 FROM dossier_scans WHERE vehicle_id=?', (vehicle_id,)).fetchone():
                    raise ValueError('This VIN has linked scan history. Review that history before correcting the VIN.')
                self.db.execute('UPDATE dossiers SET vin=?,details=? WHERE id=?', (vin, json.dumps(details), vehicle_id))
            else:
                vehicle_id = uid()
                self.db.execute('INSERT INTO dossiers VALUES(?,?,?,?)', (vehicle_id, vin, json.dumps(details), timestamp))
            if owner.casefold() != (before.get('owner') or '').casefold():
                self.db.execute('UPDATE dossier_ownership SET ended=? WHERE vehicle_id=? AND ended IS NULL',
                                (timestamp, vehicle_id))
                if owner:
                    self.db.execute('INSERT OR IGNORE INTO dossier_owners VALUES(?,?)', (uid(), owner))
                    owner_id = self.db.execute('SELECT id FROM dossier_owners WHERE name=?', (owner,)).fetchone()[0]
                    self.db.execute('INSERT INTO dossier_ownership VALUES(?,?,?,?,NULL)',
                                    (uid(), vehicle_id, owner_id, timestamp))
            self.audit(vehicle_id, actor, 'vehicle_edited' if before else 'vehicle_created', {'previous': before})
        return self.get(vehicle_id)

    def detail(self, vehicle_id):
        with self.lock:
            vehicle = self.get(vehicle_id)
            names = self.actor_names()
            vehicle['visits'] = [
                {'id': row[0], 'label': row[1], 'createdAt': row[2]} for row in self.db.execute(
                    'SELECT id,label,created FROM dossier_visits WHERE vehicle_id=? ORDER BY created DESC', (vehicle_id,))]
            vehicle['notes'] = [
                {'id': row[0], 'body': row[1], 'author': row[2], 'authorName': names.get(row[2], row[2]),
                 'createdAt': row[3]} for row in self.db.execute(
                    'SELECT id,body,author,created FROM dossier_notes WHERE vehicle_id=? ORDER BY created DESC', (vehicle_id,))]
            vehicle['owners'] = [
                {'name': row[0], 'startedAt': row[1], 'endedAt': row[2]} for row in self.db.execute(
                    """SELECT o.name,h.started,h.ended FROM dossier_ownership h JOIN dossier_owners o
                    ON o.id=h.owner_id WHERE h.vehicle_id=? ORDER BY h.started DESC""", (vehicle_id,))]
            vehicle['scans'] = [
                {'id': row[0], 'createdAt': row[1], 'status': row[2], 'scannerId': row[3]} for row in self.db.execute(
                    """SELECT s.id,s.created,json_extract(s.data,'$.scanStatus'),json_extract(s.data,'$.scannerId')
                    FROM scans s JOIN dossier_scans l ON s.id=l.scan_id
                    WHERE l.vehicle_id=? ORDER BY s.created DESC LIMIT 50""", (vehicle_id,))]
        return vehicle

    def add_entry(self, vehicle_id, actor, kind, value):
        value = clean(value, 4000 if kind == 'note' else 120, kind)
        if not value:
            raise ValueError(f'Enter a {kind}.')
        key, timestamp = uid(), now()
        with self.lock, self.db:
            self.get(vehicle_id)
            if kind == 'note':
                self.db.execute('INSERT INTO dossier_notes VALUES(?,?,?,?,?)', (key, vehicle_id, value, actor, timestamp))
            else:
                self.db.execute('INSERT INTO dossier_visits VALUES(?,?,?,?)', (key, vehicle_id, value, timestamp))
            self.audit(vehicle_id, actor, kind + '_added', {'id': key})
        return {'id': key}

    def flag_missing_gps(self, value=None, actor=None):
        with self.lock, self.db:
            if value is not None:
                if type(value) is not bool:
                    raise ValueError('Missing GPS policy must be true or false.')
                self.db.execute('UPDATE dossier_settings SET flag_missing_gps=? WHERE id=1', (value,))
                self.audit(None, actor, 'location_policy_changed', {'flagMissingGps': value})
            return bool(self.db.execute('SELECT flag_missing_gps FROM dossier_settings WHERE id=1').fetchone()[0])

    def create_user(self, data, actor):
        username = clean(data.get('username', ''), 40, 'Username')
        name = clean(data.get('name', ''), 120, 'Name')
        role, password = data.get('role'), data.get('password')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{2,40}', username) or not name or role not in ROLES:
            raise ValueError('Enter a name, valid username and role.')
        if not isinstance(password, str) or not 12 <= len(password) <= 200:
            raise ValueError('Use a password of 12 to 200 characters.')
        user_id, salt = uid(), secrets.token_hex(16)
        digest = self.password_hash(password, salt)
        with self.lock, self.db:
            try:
                self.db.execute('INSERT INTO dossier_users VALUES(?,?,?,?,?,?,1)',
                                (user_id, username, name, role, salt, digest))
            except sqlite3.IntegrityError as exc:
                raise ValueError('That username already exists.') from exc
            self.audit(None, actor, 'account_created', {'id': user_id, 'username': username, 'role': role})
        return {'id': user_id, 'username': username, 'name': name, 'role': role}

    def users(self):
        with self.lock:
            return [dict(zip(('id', 'username', 'name', 'role'), row)) for row in self.db.execute(
                'SELECT id,username,name,role FROM dossier_users ORDER BY username')]

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()

    def authenticate(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 40 or len(password) > 200:
            return None
        with self.lock:
            row = self.db.execute('SELECT id,name,role,salt,password,must_change FROM dossier_users WHERE username=?', (username,)).fetchone()
        digest = self.password_hash(password, row[3] if row else '0' * 32)
        if row and hmac.compare_digest(digest, row[4]):
            return {'id': row[0], 'name': row[1], 'role': row[2], 'username': username,
                    'mustChangePassword': bool(row[5])}

    def change_initial_password(self, user_id, password):
        # Preserve spaces in passwords, while requiring enough non-padding characters.
        if not isinstance(password, str) or len(password.strip()) < 12 or len(password) > 200:
            raise ValueError('Use a new password of 12 to 200 characters.')
        salt = secrets.token_hex(16)
        digest = self.password_hash(password, salt)
        with self.lock, self.db:
            previous = self.db.execute('SELECT salt,password FROM dossier_users WHERE id=? AND must_change=1',
                                       (user_id,)).fetchone()
            if not previous:
                raise ValueError('The initial password has already been changed. Sign in again.')
            if hmac.compare_digest(self.password_hash(password, previous[0]), previous[1]):
                raise ValueError('Choose a password different from the initial password.')
            self.db.execute('UPDATE dossier_users SET salt=?,password=?,must_change=0 WHERE id=?',
                            (salt, digest, user_id))
            self.audit(None, user_id, 'initial_password_changed', {})
