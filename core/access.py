"""Shared-key browser sessions for the Pi's trusted local network."""
from http.cookies import SimpleCookie
import hmac
import os
from pathlib import Path
import secrets
import threading
import time


def load_access_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text(encoding='utf-8').strip()
    else:
        key = secrets.token_urlsafe(24)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(key + '\n')
    if len(key) < 20:
        raise ValueError('The LAN access key must contain at least 20 characters.')
    return key


class BrowserAccess:
    lifetime = 12 * 60 * 60

    def __init__(self, key, authenticate=None):
        self.key = key
        self.authenticate = authenticate
        self.sessions = {}
        self.identities = {}
        self.attempts = {}
        self.lock = threading.Lock()

    def login(self, key, address, username=None):
        with self.lock:
            now = time.monotonic()
            self.sessions = {k: expiry for k, expiry in self.sessions.items() if expiry > now}
            self.identities = {k: self.identities[k] for k in self.sessions}
            self.attempts = {ip: item for ip, item in self.attempts.items() if item[0] > now - 60}
            attempts = self.attempts.get(address, (now, 0))
            if attempts[1] >= 5:
                raise ValueError('Too many attempts. Wait one minute before trying again.')
            identity = None
            if username and self.authenticate:
                identity = self.authenticate(username, key)
            elif isinstance(key, str) and self.key and hmac.compare_digest(key.encode(), self.key.encode()):
                identity = {'id': 'installation', 'name': 'Installation administrator', 'role': 'admin'}
            if not identity:
                self.attempts[address] = (attempts[0], attempts[1] + 1)
                return None
            self.attempts.pop(address, None)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + self.lifetime
            self.identities[token] = identity
            return token

    def token(self, cookie):
        try:
            parsed = SimpleCookie()
            parsed.load(cookie or '')
            return parsed['obdwhiz_session'].value if 'obdwhiz_session' in parsed else ''
        except Exception:
            return ''

    def allowed(self, cookie):
        return self.identity(cookie) is not None

    def identity(self, cookie):
        with self.lock:
            token = self.token(cookie)
            if self.sessions.get(token, 0) > time.monotonic():
                return self.identities.get(token)
            return None

    def logout(self, cookie):
        with self.lock:
            token = self.token(cookie)
            self.sessions.pop(token, None)
            self.identities.pop(token, None)

    def complete_password_change(self, cookie, password, change):
        with self.lock:
            token = self.token(cookie)
            identity = self.identities.get(token)
            if (self.sessions.get(token, 0) <= time.monotonic() or not identity
                    or not identity.get('mustChangePassword')):
                raise ValueError('Sign in with the initial password first.')
            change(identity['id'], password)
            for old, account in list(self.identities.items()):
                if account['id'] == identity['id']:
                    self.identities.pop(old)
                    self.sessions.pop(old, None)
            token = secrets.token_urlsafe(32)
            self.identities[token] = {**identity, 'mustChangePassword': False}
            self.sessions[token] = time.monotonic() + self.lifetime
            return token
