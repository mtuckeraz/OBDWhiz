"""Persistent code rules and an outbound mail queue for completed scan writes."""

from email.message import EmailMessage
import json
import logging
import re
import smtplib
import ssl
import threading
import time
import uuid


SMTP_DEFAULTS = {'host': '', 'port': 587, 'security': 'starttls',
                 'sender': '', 'username': '', 'password': ''}
ADDRESS = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')
RETRY_DELAYS = (30, 120)


def field(data, key, limit, *, required=False, trim=True):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'Invalid {key}.')
    if trim:
        value = value.strip()
    if required and not value:
        raise ValueError(f'Enter {key}.')
    return value


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not ADDRESS.fullmatch(value):
        raise ValueError('Enter a valid email address.')
    return value


def send_mail(config, recipient, subject, body):
    if not config['host']:
        raise ValueError('Configure outbound email in Setup first.')
    message = EmailMessage()
    message['From'] = config['sender']
    message['To'] = recipient
    message['Subject'] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    options = {'host': config['host'], 'port': config['port'], 'timeout': 15}
    client = (smtplib.SMTP_SSL(**options, context=context) if config['security'] == 'tls'
              else smtplib.SMTP(**options))
    try:
        if config['security'] == 'starttls':
            client.starttls(context=context)
        if config['username']:
            client.login(config['username'], config['password'])
        client.send_message(message)
    finally:
        client.close()


def mail_error(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'SMTP authentication failed. Check the username and password.'
    if isinstance(exc, ssl.SSLError):
        return 'SMTP TLS connection failed. Check the hostname, port and encryption mode.'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'The mail server refused a recipient.'
    return 'Email delivery failed. Check the SMTP settings and connectivity.'


class Notifications:
    def __init__(self, store, suggest):
        self.store = store
        self.db = store.db
        self.suggest = suggest
        self.stopped = threading.Event()
        with store.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS notification_smtp (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notification_rules (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notification_matches (
                    rule_id TEXT, identity TEXT, PRIMARY KEY(rule_id, identity));
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL, rule_id TEXT NOT NULL, created TEXT NOT NULL,
                    data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL DEFAULT 0, error TEXT, UNIQUE(scan_id, rule_id));
                CREATE INDEX IF NOT EXISTS notification_due ON notification_outbox(status, due, id);
            """)

    def _smtp(self):
        row = self.db.execute('SELECT data FROM notification_smtp WHERE id=1').fetchone()
        return json.loads(row[0]) if row else SMTP_DEFAULTS.copy()

    def smtp_config(self):
        with self.store.lock:
            config = self._smtp()
        config['hasPassword'] = bool(config.pop('password'))
        return config

    def configure_smtp(self, data):
        host = field(data, 'host', 253, required=True)
        if not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
            raise ValueError('Enter an SMTP hostname or IP address.')
        port = data.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Enter an SMTP port from 1 to 65535.')
        security = data.get('security')
        if security not in ('starttls', 'tls'):
            raise ValueError('Choose STARTTLS or implicit TLS.')
        sender = address(field(data, 'sender', 254, required=True))
        username = field(data, 'username', 254)
        password = field(data, 'password', 1024, trim=False)
        with self.store.lock, self.db:
            previous = self._smtp()
            if not password and host == previous['host'] and username == previous['username']:
                password = previous['password']
            if username and not password:
                raise ValueError('Enter an SMTP password, or leave the username blank for a relay without authentication.')
            config = dict(host=host, port=port, security=security, sender=sender,
                          username=username, password=password if username else '')
            self.db.execute('INSERT OR REPLACE INTO notification_smtp VALUES(1, ?)', (json.dumps(config),))
        return self.smtp_config()

    def test_mail(self, recipient):
        recipient = address(recipient)
        with self.store.lock:
            config = self._smtp()
        try:
            send_mail(config, recipient, 'OBDWhiz email test', 'Outbound email is working for OBDWhiz.')
        except (OSError, smtplib.SMTPException) as exc:
            raise ValueError(mail_error(exc)) from exc
        return {'sent': True}

    def _rules(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM notification_rules ORDER BY rowid')]

    def overview(self):
        with self.store.lock:
            rules = self._rules()
            recent = self.db.execute("""SELECT scan_id, created, data, status, attempts, error
                                        FROM notification_outbox ORDER BY id DESC LIMIT 30""").fetchall()
        return {'rules': rules, 'deliveries': [
            {'scanId': scan_id, 'createdAt': created, 'name': json.loads(data)['name'],
             'status': status, 'attempts': attempts, 'error': error}
            for scan_id, created, data, status, attempts, error in recent]}

    def save_rule(self, data):
        if data.get('id') and not isinstance(data['id'], str):
            raise ValueError('Invalid notification rule ID.')
        rule_id = data.get('id') or str(uuid.uuid4())
        name = field(data, 'name', 100, required=True)
        codes = data.get('codes')
        recipients = data.get('recipients')
        if not isinstance(codes, list) or not 1 <= len(codes) <= 20 or not all(
                isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_\- ]{2,32}', code.strip()) for code in codes):
            raise ValueError('Enter 1 to 20 OBD codes, separated by commas or new lines.')
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 10:
            raise ValueError('Enter 1 to 10 recipient addresses.')
        if any(type(data.get(key)) is not bool for key in ('enabled', 'includeAI')):
            raise ValueError('Specify whether the rule and AI suggestions are enabled.')
        rule = dict(id=rule_id, name=name, codes=sorted({code.strip().upper() for code in codes}),
                    recipients=list(dict.fromkeys(address(value) for value in recipients)),
                    enabled=data['enabled'], includeAI=data['includeAI'])
        with self.store.lock, self.db:
            current = self.db.execute('SELECT data FROM notification_rules WHERE id=?', (rule_id,)).fetchone()
            if data.get('id') and not current:
                raise ValueError('Notification rule not found.')
            if current:
                if json.loads(current[0])['codes'] != rule['codes']:
                    self.db.execute('DELETE FROM notification_matches WHERE rule_id=?', (rule_id,))
                self.db.execute('UPDATE notification_rules SET data=? WHERE id=?', (json.dumps(rule), rule_id))
            else:
                if self.db.execute('SELECT COUNT(*) FROM notification_rules').fetchone()[0] >= 100:
                    raise ValueError('You can save up to 100 notification rules.')
                self.db.execute('INSERT INTO notification_rules VALUES(?, ?)', (rule_id, json.dumps(rule)))
        return rule

    def remove_rule(self, rule_id):
        with self.store.lock, self.db:
            self.db.execute('DELETE FROM notification_rules WHERE id=?', (rule_id,))
            self.db.execute('DELETE FROM notification_matches WHERE rule_id=?', (rule_id,))
        return {'removed': True}

    def record_scan(self, scan):
        # Called inside the scan transaction so matching and the queue are committed together.
        status = scan.get('scanStatus')
        if scan.get('demo') or scan.get('source') == 'Imported scan' or status not in ('complete', 'partial'):
            return
        reader = scan.get('reader') if isinstance(scan.get('reader'), dict) else {}
        identity = scan.get('vin') or scan.get('vehicleId') or scan.get('scannerId') or reader.get('address')
        codes = {entry['code'].strip().upper() for entry in scan['codes']}
        for rule in self._rules():
            required = set(rule['codes'])
            if not required <= codes:
                if identity and status == 'complete':
                    self.db.execute('DELETE FROM notification_matches WHERE rule_id=? AND identity=?',
                                    (rule['id'], identity))
                continue
            if not rule['enabled']:
                continue
            if identity and not self.db.execute('INSERT OR IGNORE INTO notification_matches VALUES(?, ?)',
                                                (rule['id'], identity)).rowcount:
                continue
            job = {key: rule[key] for key in ('name', 'codes', 'recipients', 'includeAI')}
            self.db.execute('INSERT OR IGNORE INTO notification_outbox(scan_id,rule_id,created,data) VALUES(?,?,?,?)',
                            (scan['id'], rule['id'], scan['createdAt'], json.dumps(job)))

    def deliver_next(self):
        with self.store.lock:
            config = self._smtp()
            if not config['host']:
                return False
            row = self.db.execute("""SELECT id, scan_id, data, attempts FROM notification_outbox
                                     WHERE status='queued' AND due<=? ORDER BY due,id LIMIT 1""",
                                  (time.time(),)).fetchone()
        if not row:
            return False
        job_id, scan_id, raw, attempts = row
        job = json.loads(raw)
        if 'body' not in job:
            scan = self.store.get(scan_id)
            vehicle = ' '.join(str(scan['vehicle'][key]) for key in ('year', 'make', 'model')
                               if scan['vehicle'].get(key)) or 'Unidentified vehicle'
            if scan.get('vin'):
                vehicle += f" (VIN ending {scan['vin'][-6:]})"
            job['body'] = (f"Rule: {job['name']}\nVehicle: {vehicle}\n"
                           f"Scan time (UTC): {scan['createdAt']}\nScan ID: {scan_id}\n"
                           f"Matched codes: {', '.join(job['codes'])}\n")
            if job['includeAI']:
                try:
                    suggestion = self.suggest(scan, job['codes'])
                except Exception:
                    suggestion = None
                job['body'] += ('\nAI-suggested next steps to verify:\n' + suggestion + '\n'
                                if suggestion else '\nAI suggestions were unavailable for this alert.\n')
            self._save_progress(job_id, job)
        failed = []
        error = None
        for recipient in job['recipients'][:]:
            if self.stopped.is_set():
                return False
            try:
                send_mail(config, recipient, f"OBDWhiz alert: {job['name']}", job['body'])
            except (OSError, smtplib.SMTPException) as exc:
                failed.append(recipient)
                error = mail_error(exc)
            else:
                job['recipients'].remove(recipient)
                self._save_progress(job_id, job)
        status, due = 'sent', 0
        if failed:
            status = 'failed'
            if attempts < len(RETRY_DELAYS):
                status, due = 'queued', time.time() + RETRY_DELAYS[attempts]
        with self.store.lock, self.db:
            self.db.execute('UPDATE notification_outbox SET status=?, attempts=?, due=?, error=? WHERE id=?',
                            (status, attempts + 1, due, error, job_id))
        return True

    def _save_progress(self, job_id, job):
        with self.store.lock, self.db:
            self.db.execute('UPDATE notification_outbox SET data=? WHERE id=?', (json.dumps(job), job_id))

    def run(self):
        while not self.stopped.is_set():
            try:
                if self.deliver_next():
                    continue
            except Exception:
                logging.getLogger(__name__).error('The notification worker could not process its queue.')
            self.stopped.wait(2)
